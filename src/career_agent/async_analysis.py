from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .analysis_cache import AnalysisIdentity, estimated_analysis_input_tokens
from .ingestion import JobStagingStore
from .models import AnalysisRun, AnalysisTask, Job
from .profile import CandidateProfile
from .repository import Repository


ANALYSIS_QUEUE_SCHEMA_VERSION = 1


class AnalysisDispatcher(Protocol):
    customer_id: str
    deadline_minutes: int
    maximum_concurrency: int

    def prepare(
        self,
        *,
        run_id: str,
        started_at: str,
        send_email: bool,
        profile: CandidateProfile,
        candidates: list[Job],
        cache_misses: list[Job],
        selected_jobs: list[Job],
        identities: dict[str, AnalysisIdentity],
        selection_reasons: dict[str, str],
        priorities: dict[str, dict[str, float]],
    ) -> AnalysisRun: ...

    def enqueue(self, run: AnalysisRun) -> int: ...


@dataclass(slots=True)
class SqsAnalysisDispatcher:
    repository: Repository
    staging: JobStagingStore
    sqs: Any
    analysis_queue_url: str
    finalizer_queue_url: str
    customer_id: str = "personal"
    deadline_minutes: int = 15
    maximum_concurrency: int = 4
    stale_after_days: int = 14

    def prepare(
        self,
        *,
        run_id: str,
        started_at: str,
        send_email: bool,
        profile: CandidateProfile,
        candidates: list[Job],
        cache_misses: list[Job],
        selected_jobs: list[Job],
        identities: dict[str, AnalysisIdentity],
        selection_reasons: dict[str, str],
        priorities: dict[str, dict[str, float]],
    ) -> AnalysisRun:
        if existing_run := self.repository.get_analysis_run(run_id):
            return existing_run
        now = datetime.now(UTC)
        prepared_at = now.isoformat()
        active_task_ids = {identities[job.id].cache_key for job in cache_misses}
        selected_job_ids = {job.id for job in selected_jobs}
        existing = self.repository.get_analysis_tasks(
            self.customer_id, list(active_task_ids)
        )
        tasks: list[AnalysisTask] = []
        for job in cache_misses:
            identity = identities[job.id]
            previous = existing.get(identity.cache_key)
            is_selected = job.id in selected_job_ids
            snapshot = job.to_dict()
            snapshot["description"] = job.description[:18_000]
            snapshot["listings"] = []
            task = AnalysisTask(
                customer_id=self.customer_id,
                task_id=identity.cache_key,
                cache_key=identity.cache_key,
                job_id=job.id,
                profile_version=profile.version,
                job_fingerprint=identity.job_fingerprint,
                profile_fingerprint=identity.profile_fingerprint,
                analyzer_version=identity.analyzer_version,
                model_id=identity.model_id,
                job=snapshot,
                source=job.source,
                source_key=job.source_key or job.source,
                company=job.company,
                role_family=job.role_family or "unmatched",
                priority_score=priorities[job.id]["priority_score"],
                priority_components=dict(priorities[job.id]),
                first_queued_at=(
                    previous.first_queued_at
                    if previous
                    else prepared_at
                ),
                last_seen_at=prepared_at,
                closes_at=job.closes_at,
                estimated_input_tokens=estimated_analysis_input_tokens(job, profile),
                status="dispatched" if is_selected else "queued",
                selection_reason=selection_reasons.get(job.id),
                selected_run_id=run_id if is_selected else None,
                dispatched_at=prepared_at if is_selected else None,
                attempts=0 if is_selected else (previous.attempts if previous else 0),
            )
            tasks.append(task)

        self.repository.expire_inactive_analysis_tasks(
            self.customer_id,
            active_task_ids,
            (now - timedelta(days=self.stale_after_days)).isoformat(),
            prepared_at,
        )
        self.repository.put_analysis_tasks(tasks)
        backlog = [
            task
            for task in self.repository.list_analysis_tasks(self.customer_id)
            if task.status in {"queued", "dispatched", "timed_out"}
        ]
        artifact_key = self.staging.put_jobs(run_id, "deep-analysis-candidates", candidates)
        selected_task_ids = [identities[job.id].cache_key for job in selected_jobs]
        oldest_queued_at = min((task.first_queued_at for task in backlog), default=None)
        selected_task_id_set = set(selected_task_ids)
        run = AnalysisRun(
            run_id=run_id,
            customer_id=self.customer_id,
            profile_version=profile.version,
            started_at=started_at,
            prepared_at=prepared_at,
            deadline_at=(now + timedelta(minutes=self.deadline_minutes)).isoformat(),
            candidate_artifact_key=artifact_key,
            selected_task_ids=selected_task_ids,
            expected_tasks=len(selected_task_ids),
            send_email=send_email,
            backlog_size=len(backlog),
            oldest_queued_at=oldest_queued_at,
            estimated_input_tokens=sum(
                task.estimated_input_tokens
                for task in tasks
                if task.task_id in selected_task_id_set
            ),
            run_type=("pipeline" if _scheduled_run_id(run_id) else "manual_discovery"),
        )
        created = self.repository.begin_analysis_run(run)
        return run if created else self.repository.get_analysis_run(run_id) or run
    def enqueue(self, run: AnalysisRun) -> int:
        tasks = self.repository.get_analysis_tasks(run.customer_id, run.selected_task_ids)
        messages = [
            {
                "schema_version": ANALYSIS_QUEUE_SCHEMA_VERSION,
                "kind": "deep_analysis_task",
                "run_id": run.run_id,
                "customer_id": run.customer_id,
                "task_id": task_id,
            }
            for task_id in run.selected_task_ids
            if (task := tasks.get(task_id)) is not None
            and task.status == "dispatched"
            and task.selected_run_id == run.run_id
        ]
        _send_batches(self.sqs, self.analysis_queue_url, messages)
        ready = len(tasks) == run.expected_tasks and all(task.terminal for task in tasks.values())
        _send_finalizer_check(
            self.sqs,
            self.finalizer_queue_url,
            run.run_id,
            delay_seconds=0 if ready else _seconds_until(run.deadline_at),
        )
        return len(messages)


def _scheduled_run_id(run_id: str) -> bool:
    try:
        datetime.fromisoformat(run_id.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _send_batches(sqs: Any, queue_url: str, messages: list[dict[str, Any]]) -> None:
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
            raise RuntimeError(f"Unable to enqueue deep-analysis tasks: {failed_ids}")


def _send_finalizer_check(
    sqs: Any,
    queue_url: str,
    run_id: str,
    *,
    delay_seconds: int = 0,
) -> None:
    message = {
        "schema_version": ANALYSIS_QUEUE_SCHEMA_VERSION,
        "kind": "finalize_deep_analysis",
        "run_id": run_id,
    }
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(message, separators=(",", ":")),
        DelaySeconds=max(0, min(delay_seconds, 900)),
    )


def _seconds_until(deadline_at: str) -> int:
    deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(0, min(900, round((deadline - datetime.now(UTC)).total_seconds())))
