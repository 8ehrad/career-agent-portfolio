"""Durable, owner-triggered application preparation transport."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

from .models import ApplicationTask, JobStatus

SCHEMA_VERSION = 1


def new_task(customer_id: str, job_id: str, identity: str, previous_status: JobStatus) -> ApplicationTask:
    now = datetime.now(UTC).isoformat()
    return ApplicationTask(customer_id=customer_id, job_id=job_id, task_id=uuid4().hex,
        identity=identity, status="queued", created_at=now, updated_at=now,
        previous_status=previous_status.value)


def queue_task(task: ApplicationTask, *, client=None, queue_url: str | None = None) -> None:
    queue_url = queue_url or os.environ["APPLICATION_PREPARE_QUEUE_URL"]
    if client is None:
        import boto3
        client = boto3.client("sqs")
    client.send_message(QueueUrl=queue_url, MessageBody=json.dumps({
        "schema_version": SCHEMA_VERSION, "kind": "application_prepare",
        "customer_id": task.customer_id, "job_id": task.job_id, "task_id": task.task_id,
    }, separators=(",", ":")))


def task_view(task: ApplicationTask) -> dict:
    return {"job_id": task.job_id, "task_id": task.task_id, "status": task.status,
        "created_at": task.created_at, "updated_at": task.updated_at,
        "attempts": task.attempts, "last_error": task.last_error,
        "retryable": task.status == "failed"}
