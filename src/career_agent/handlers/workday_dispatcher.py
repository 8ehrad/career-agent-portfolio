from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from career_agent.analysis_budget import AnalysisBudgetLimits
from career_agent.models import IngestionRun, IngestionTask
from career_agent.runtime import repository
from career_agent.source_registry import load_source_registry


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    del context
    now = datetime.now(UTC)
    event = event if isinstance(event, dict) else {}
    run_id = event.get("run_id") or f"{now.date().isoformat()}-{uuid.uuid4().hex[:8]}"
    if not isinstance(run_id, str):
        raise ValueError("run_id must be a string")
    if "<aws.scheduler." in run_id:
        raise ValueError(f"EventBridge Scheduler context attribute was not resolved: {run_id}")
    send_email = bool(event.get("send_email", True))

    definitions = [
        definition
        for definition in load_source_registry(os.environ["SOURCE_REGISTRY_PATH"])
        if definition.status == "active" and definition.ats == "workday"
    ]
    deadline_minutes = int(os.getenv("WORKDAY_INGESTION_DEADLINE_MINUTES", "45"))
    run = IngestionRun(
        run_id=run_id,
        started_at=now.isoformat(),
        deadline_at=(now + timedelta(minutes=deadline_minutes)).isoformat(),
        expected_tasks=len(definitions),
        send_email=send_email,
    )
    tasks = [
        IngestionTask(
            run_id=run_id,
            source_key=definition.key,
            employer=definition.employer,
            ats=definition.ats,
            tenant=definition.tenant,
        )
        for definition in definitions
    ]
    repo = repository()
    if event.get("source") == "eventbridge-scheduler":
        scheduled_at = datetime.fromisoformat(run_id.replace("Z", "+00:00"))
        limits = AnalysisBudgetLimits.from_environment()
        customer_id = os.getenv("CAREER_CUSTOMER_ID", "personal")
        maximum_backlog_calls = max(0, limits.maximum_calls - 25)
        for offset_hours, state in ((0, "reserved"), (24, "none"), (48, "reserved")):
            day = limits.budget_date(scheduled_at + timedelta(hours=offset_hours))
            if not repo.set_analysis_discovery_state(
                customer_id, day, state, now.isoformat(),
                maximum_backlog_calls=maximum_backlog_calls,
            ) and offset_hours == 0:
                logger.warning("Unable to establish today's discovery call reservation")
    created = repo.begin_ingestion_run(run, tasks)
    existing = repo.get_ingestion_run(run_id)
    if existing is None:
        raise RuntimeError("Ingestion run was not persisted")
    if existing.status == "completed":
        result = {"run_id": run_id, "status": "duplicate_completed", "tasks": existing.expected_tasks}
        logger.info(json.dumps({"event": "workday_dispatch_skipped", **result}))
        return result

    persisted_tasks = repo.list_ingestion_tasks(run_id)
    sqs = _sqs_client()
    queue_url = os.environ["WORKDAY_QUEUE_URL"]
    messages = [
        {
            "schema_version": 1,
            "kind": "workday_source",
            "run_id": run_id,
            "source_key": task.source_key,
        }
        for task in persisted_tasks
        if not task.terminal
    ]
    _send_batches(sqs, queue_url, messages)
    _send_finalizer_check(sqs, run_id, delay_seconds=min(900, deadline_minutes * 60))
    result = {
        "run_id": run_id,
        "status": "dispatched",
        "created": created,
        "tasks": len(persisted_tasks),
        "messages_sent": len(messages),
        "deadline_at": existing.deadline_at,
        "send_email": existing.send_email,
    }
    if event.get("source") != "eventbridge-scheduler":
        limits = AnalysisBudgetLimits.from_environment()
        usage = repo.get_analysis_budget_usage(
            os.getenv("CAREER_CUSTOMER_ID", "personal"), limits.budget_date(now)
        )
        result["analysis_budget_snapshot"] = {
            "remaining_call_slots": limits.remaining_calls(usage),
            "discovery_state": usage.discovery_state if usage else "unknown",
            "observed_at": now.isoformat(),
            "advisory_only": True,
        }
    logger.info(json.dumps({"event": "workday_dispatch_completed", **result}))
    return result


def _sqs_client():
    import boto3

    return boto3.client("sqs")


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
            raise RuntimeError(f"Unable to enqueue Workday tasks: {failed_ids}")


def _send_finalizer_check(sqs: Any, run_id: str, *, delay_seconds: int = 0) -> None:
    message = {"schema_version": 1, "kind": "finalize_ingestion", "run_id": run_id}
    sqs.send_message(
        QueueUrl=os.environ["PIPELINE_FINALIZER_QUEUE_URL"],
        MessageBody=json.dumps(message, separators=(",", ":")),
        DelaySeconds=max(0, min(delay_seconds, 900)),
    )
