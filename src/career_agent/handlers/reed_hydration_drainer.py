from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta

from career_agent.reed_hydration import (
    hydration_message,
    ready_message,
    send_reed_messages,
)
from career_agent.runtime import repository


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    del event, context
    repo = repository()
    sqs = _sqs_client()
    customer_id = os.getenv("CAREER_CUSTOMER_ID", "personal")
    now = datetime.now(UTC)
    stale_dispatch = now - timedelta(minutes=10)
    tasks = repo.list_reed_hydration_tasks(customer_id)
    eligible = [
        task
        for task in tasks
        if (
            task.status == "queued"
            and (not task.next_attempt_at or _parse(task.next_attempt_at) <= now)
        )
        or (
            task.status == "dispatched"
            and (
                not task.last_dispatched_at
                or _parse(task.last_dispatched_at) <= stale_dispatch
            )
        )
        or (
            task.status == "processing"
            and task.lease_expires_at
            and _parse(task.lease_expires_at) <= now
        )
    ]
    batch_size = max(1, int(os.getenv("REED_HYDRATION_DISPATCH_BATCH_SIZE", "500")))
    selected = eligible[:batch_size]
    dispatched = repo.mark_reed_hydration_dispatched(
        customer_id, [task.source_id for task in selected], now.isoformat()
    )
    send_reed_messages(
        sqs,
        os.environ["REED_HYDRATION_QUEUE_URL"],
        [hydration_message(customer_id, source_id) for source_id in dispatched],
    )

    ready = [
        task
        for task in tasks
        if task.status == "completed"
        and task.ready_status == "pending"
        and (
            not task.ready_sent_at or _parse(task.ready_sent_at) <= stale_dispatch
        )
    ][:batch_size]
    send_reed_messages(
        sqs,
        os.environ["REED_HYDRATION_READY_QUEUE_URL"],
        [ready_message(customer_id, task.source_id) for task in ready],
    )
    for task in ready:
        repo.mark_reed_hydration_ready_sent(customer_id, task.source_id, now.isoformat())

    result = {
        "status": "completed",
        "hydration_backlog": sum(
            task.status in {"queued", "dispatched", "processing"} for task in tasks
        ),
        "hydration_eligible_now": len(eligible),
        "hydration_dispatched": len(dispatched),
        "ready_pending": sum(
            task.status == "completed" and task.ready_status == "pending" for task in tasks
        ),
        "ready_dispatched": len(ready),
        "failed_tasks": sum(task.status == "failed" for task in tasks),
        "oldest_pending_age_hours": round(
            max(
                (
                    (now - _parse(task.first_seen_at)).total_seconds() / 3600
                    for task in tasks
                    if task.status in {"queued", "dispatched", "processing"}
                ),
                default=0,
            ),
            2,
        ),
        "total_tasks": len(tasks),
    }
    # Existing hourly source-maintenance tick also recovers pending freshness messages.
    try:
        from career_agent.freshness import service_from_environment
        freshness = service_from_environment(repo, sqs)
        if freshness is not None:
            result["freshness"] = freshness.refresh_tracked(repo.list_jobs())
    except Exception:
        logger.exception("Freshness maintenance unavailable")
        result["freshness"] = {"status": "unavailable"}
    logger.info(json.dumps({"event": "reed_hydration_drain", **result}))
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": round(now.timestamp() * 1000),
                    "CloudWatchMetrics": [{
                        "Namespace": "CareerAgent/ReedHydration",
                        "Dimensions": [["Environment"]],
                        "Metrics": [
                            {"Name": "Pending", "Unit": "Count"},
                            {"Name": "Failed", "Unit": "Count"},
                            {"Name": "OldestPendingAgeHours", "Unit": "Count"},
                        ],
                    }],
                },
                "Environment": os.getenv("ENVIRONMENT", "prod"),
                "Pending": result["hydration_backlog"],
                "Failed": result["failed_tasks"],
                "OldestPendingAgeHours": result["oldest_pending_age_hours"],
            }
        )
    )
    return result


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _sqs_client():
    import boto3

    return boto3.client("sqs")
