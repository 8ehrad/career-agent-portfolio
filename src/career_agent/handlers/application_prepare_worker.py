"""SQS consumer for owner-requested rich application packs."""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta

from career_agent.application_async import SCHEMA_VERSION, queue_task
from career_agent.models import JobStatus
from career_agent.rich_application import ApplicationGenerationIncomplete
from career_agent.runtime import application_service, repository

LOG = logging.getLogger(__name__)


def handler(event, context):
    if event.get("kind") == "application_reconcile":
        return _reconcile()
    failures = []
    for record in event.get("Records", []):
        try:
            _process_record(record)
        except Exception:
            LOG.exception("Application preparation worker failed", extra={"message_id": record.get("messageId")})
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def _process_record(record: dict) -> None:
    message = json.loads(record["body"])
    if message.get("schema_version") != SCHEMA_VERSION or message.get("kind") != "application_prepare":
        raise ValueError("Unsupported application task message")
    customer_id, job_id, task_id = (str(message[key]) for key in ("customer_id", "job_id", "task_id"))
    if customer_id != os.getenv("CAREER_CUSTOMER_ID", "personal"):
        raise ValueError("Application task customer does not match this deployment")
    repo = repository()
    existing = repo.get_application_task(customer_id, job_id)
    if existing is None:
        raise ValueError("Unknown application task")
    if existing.task_id != task_id or existing.status in {"completed", "failed"}:
        return
    now = datetime.now(UTC)
    task = repo.claim_application_task(customer_id, job_id, task_id, now.isoformat(),
        (now + timedelta(seconds=450)).isoformat())
    if task is None:
        return
    try:
        service = application_service(async_worker=True)
        job = repo.get_job(job_id)
        if job is None:
            raise ValueError("Job was removed before preparation")
        current_identity = service.generator.identity(job, service.profile)
        pack = repo.get_application_pack(job_id)
        if pack and pack.generation_metadata.get("identity") == task.identity:
            if job.status == JobStatus.PREPARING:
                repo.update_job_status(job_id, JobStatus.READY)
        elif current_identity != task.identity or job.status != JobStatus.PREPARING:
            raise ValueError("Job or candidate profile changed before preparation")
        else:
            service.prepare(job_id, expected_identity=task.identity)
        if not repo.complete_application_task(customer_id, job_id, task_id,
                                              datetime.now(UTC).isoformat()):
            raise RuntimeError("Application task could not be marked complete")
    except (ApplicationGenerationIncomplete, ValueError) as exc:
        reason = exc.reason if isinstance(exc, ApplicationGenerationIncomplete) else str(exc)
        _fail(repo, task, reason, retryable=False)
    except Exception as exc:
        _fail(repo, task, type(exc).__name__, retryable=task.attempts < 3)
        raise


def _fail(repo, task, reason: str, *, retryable: bool) -> None:
    changed = repo.record_application_task_error(task.customer_id, task.job_id, task.task_id,
        datetime.now(UTC).isoformat(), reason, retryable=retryable)
    if not changed or retryable:
        return
    job = repo.get_job(task.job_id)
    if job and job.status == JobStatus.PREPARING:
        repo.update_job_status(task.job_id, JobStatus(task.previous_status))


def _reconcile() -> dict[str, int]:
    repo = repository()
    customer_id = os.getenv("CAREER_CUSTOMER_ID", "personal")
    now = datetime.now(UTC)
    stale_before = (now - timedelta(minutes=2)).isoformat()
    dispatched = failed = 0
    for task in repo.list_application_tasks(customer_id):
        recoverable = (task.status == "queued" and task.updated_at <= stale_before or
            task.status == "processing" and task.lease_expires_at and task.lease_expires_at <= now.isoformat())
        if not recoverable:
            continue
        if task.recovery_dispatches >= 4:
            changed = repo.record_application_task_error(customer_id, task.job_id, task.task_id,
                now.isoformat(), "preparation_delivery_exhausted", retryable=False,
                expected_status=task.status)
            if changed:
                job = repo.get_job(task.job_id)
                if job and job.status == JobStatus.PREPARING:
                    repo.update_job_status(task.job_id, JobStatus(task.previous_status))
                failed += 1
            continue
        if not repo.mark_application_recovery_dispatch(customer_id, task.job_id, task.task_id,
                                                       now.isoformat(), stale_before):
            continue
        try:
            queue_task(task)
            dispatched += 1
        except Exception:
            LOG.exception("Application task recovery dispatch failed", extra={"job_id": task.job_id})
    return {"requeued": dispatched, "failed": failed}
