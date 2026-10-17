from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .listing_details import normalized_summary, summary_hash
from .models import AnalysisTask, ReedHydrationTask
from .repository import Repository


REED_HYDRATION_SCHEMA_VERSION = 1


class ReedHydrationDispatcher(Protocol):
    def register(self, summaries: list[dict[str, Any]]) -> int: ...


@dataclass(slots=True)
class SqsReedHydrationDispatcher:
    repository: Repository
    sqs: Any
    queue_url: str
    customer_id: str = "personal"

    def register(self, summaries: list[dict[str, Any]]) -> int:
        now = datetime.now(UTC).isoformat()
        tasks = [
            ReedHydrationTask(
                customer_id=self.customer_id,
                source_id=str(summary["jobId"]),
                summary=normalized_summary(summary),
                summary_fingerprint=summary_hash(summary),
                first_seen_at=now,
                last_seen_at=now,
            )
            for summary in summaries
            if summary.get("jobId") is not None
        ]
        source_ids = self.repository.upsert_reed_hydration_tasks(tasks)
        dispatched = self.repository.mark_reed_hydration_dispatched(
            self.customer_id, source_ids, now
        )
        send_reed_messages(
            self.sqs,
            self.queue_url,
            [hydration_message(self.customer_id, source_id) for source_id in dispatched],
        )
        return len(dispatched)


def hydration_message(customer_id: str, source_id: str) -> dict[str, str | int]:
    return {
        "schema_version": REED_HYDRATION_SCHEMA_VERSION,
        "kind": "reed_hydration_task",
        "customer_id": customer_id,
        "source_id": source_id,
    }


def ready_message(customer_id: str, source_id: str) -> dict[str, str | int]:
    return {
        "schema_version": REED_HYDRATION_SCHEMA_VERSION,
        "kind": "reed_hydration_ready",
        "customer_id": customer_id,
        "source_id": source_id,
    }


def send_reed_messages(sqs: Any, queue_url: str, messages: list[dict[str, Any]]) -> None:
    for start in range(0, len(messages), 10):
        chunk = messages[start : start + 10]
        response = sqs.send_message_batch(
            QueueUrl=queue_url,
            Entries=[
                {"Id": str(index), "MessageBody": json.dumps(message, separators=(",", ":"))}
                for index, message in enumerate(chunk)
            ],
        )
        if failures := response.get("Failed"):
            failed_ids = ",".join(str(item.get("Id", "unknown")) for item in failures)
            raise RuntimeError(f"Unable to enqueue Reed hydration messages: {failed_ids}")


def reed_analysis_task_has_full_advert(repository: Repository, task: AnalysisTask) -> bool:
    return task.task_id in hydrated_reed_analysis_task_ids(repository, [task])


def hydrated_reed_analysis_task_ids(
    repository: Repository, tasks: list[AnalysisTask]
) -> set[str]:
    """Batch-check that Reed task snapshots match durable full descriptions."""
    eligible = {task.task_id for task in tasks if task.source != "reed"}
    reed_tasks = [task for task in tasks if task.source == "reed"]
    if not reed_tasks:
        return eligible
    source_ids = [str(task.job.get("source_id") or "") for task in reed_tasks]
    records = repository.get_listing_details("reed", [value for value in source_ids if value])
    from .connectors import _reed_raw_job
    from .listing_details import reusable
    from .normalization import normalize_job

    for task in reed_tasks:
        source_id = str(task.job.get("source_id") or "")
        record = records.get(source_id)
        if record is None or not record.get("summary"):
            continue
        if not reusable(record, source_id, record["summary"]):
            continue
        hydrated = normalize_job(
            _reed_raw_job(record["summary"], record["details"], "United Kingdom"), "reed"
        )
        if task.job.get("description") == hydrated.description[:18_000]:
            eligible.add(task.task_id)
    return eligible
