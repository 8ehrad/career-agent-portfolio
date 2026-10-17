from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from career_agent.connectors import ConnectorError, get_json
from career_agent.listing_details import make_record, reusable
from career_agent.reed_hydration import (
    REED_HYDRATION_SCHEMA_VERSION,
    ready_message,
)
from career_agent.runtime import load_integration_environment, repository


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    del context
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "unknown"))
        try:
            result = _process_record(record)
            logger.info(json.dumps({"event": "reed_hydration_task_result", **result}))
        except Exception:
            failures.append({"itemIdentifier": message_id})
            logger.exception("Reed hydration task failed unexpectedly", extra={"message_id": message_id})
    return {"batchItemFailures": failures}


def _process_record(record: dict) -> dict[str, str]:
    message = json.loads(record["body"])
    if (
        message.get("schema_version") != REED_HYDRATION_SCHEMA_VERSION
        or message.get("kind") != "reed_hydration_task"
    ):
        raise ValueError("Unsupported Reed hydration queue message")
    customer_id = str(message["customer_id"])
    source_id = str(message["source_id"])
    if customer_id != os.getenv("CAREER_CUSTOMER_ID", "personal"):
        raise ValueError("Reed hydration customer does not match this deployment")
    repo = repository()
    task = repo.get_reed_hydration_task(customer_id, source_id)
    if task is None:
        raise ValueError(f"Unknown Reed hydration task {customer_id}:{source_id}")
    if task.status == "completed":
        if task.ready_status == "pending":
            _emit_ready(repo, customer_id, source_id)
            return {"status": "ready_replayed", "source_id": source_id}
        return {"status": "duplicate", "source_id": source_id}
    if task.status == "failed" or not repo.start_reed_hydration_attempt(customer_id, source_id):
        return {"status": "not_claimed", "source_id": source_id}

    task = repo.get_reed_hydration_task(customer_id, source_id)
    if task is None:
        raise ValueError(f"Reed hydration task disappeared {customer_id}:{source_id}")
    existing = repo.get_listing_details("reed", [source_id]).get(source_id)
    if existing is not None and reusable(existing, source_id, task.summary):
        completed = repo.complete_reed_hydration_task(
            customer_id, source_id, datetime.now(UTC).isoformat(),
            expected_fingerprint=task.summary_fingerprint,
        )
        if not completed:
            return {"status": "superseded", "source_id": source_id}
        _emit_ready(repo, customer_id, source_id)
        return {"status": "cache_hit", "source_id": source_id}

    now = datetime.now(UTC)
    timezone = ZoneInfo(os.getenv("REED_HYDRATION_BUDGET_TIMEZONE", "Europe/London"))
    budget_date = now.astimezone(timezone).date().isoformat()
    maximum_requests = int(os.getenv("REED_HYDRATION_DAILY_REQUEST_LIMIT", "2000"))
    if not repo.reserve_reed_hydration_request(
        customer_id, budget_date, maximum_requests, now.isoformat()
    ):
        local_now = now.astimezone(timezone)
        next_day = (local_now + timedelta(days=1)).date()
        next_attempt = datetime.combine(next_day, datetime.min.time(), timezone).astimezone(UTC)
        repo.defer_reed_hydration_task(
            customer_id,
            source_id,
            "daily_reed_hydration_budget_exhausted",
            next_attempt.isoformat(),
            expected_fingerprint=task.summary_fingerprint,
        )
        return {"status": "budget_deferred", "source_id": source_id}

    delay = max(0.0, float(os.getenv("REED_HYDRATION_MIN_REQUEST_INTERVAL_SECONDS", "2")))
    if delay:
        time.sleep(delay)
    try:
        load_integration_environment()
        api_key = os.environ["REED_API_KEY"]
        authorization = b64encode(f"{api_key}:".encode()).decode()
        detail = get_json(
            f"https://www.reed.co.uk/api/1.0/jobs/{urllib.parse.quote(source_id, safe='')}",
            headers={"Authorization": f"Basic {authorization}"},
        )
        current = repo.get_reed_hydration_task(customer_id, source_id)
        if current is None or current.summary_fingerprint != task.summary_fingerprint:
            return {"status": "superseded", "source_id": source_id}
        persisted = make_record(source_id, task.summary, detail, datetime.now(UTC))
        repo.put_listing_details(persisted)
        try:
            from career_agent.freshness import service_from_environment, target_for, timestamp, reed_expiration_timestamp
            from career_agent.connectors import _reed_raw_job
            from career_agent.normalization import normalize_job
            freshness = service_from_environment(repo)
            if freshness is not None:
                job = normalize_job(_reed_raw_job(task.summary, detail, "UK"), "reed")
                target = target_for(job)
                at = timestamp(persisted["fetched_at"])
                expiry = reed_expiration_timestamp(detail.get("expirationDate"))
                if target:
                    freshness.observe(target, "source_closed" if expiry and expiry < at else "live_detail",
                                      at, "reed_hydration_http")
        except Exception:
            logger.exception("Reed freshness observation unavailable")
    except (ConnectorError, ValueError, KeyError) as exc:
        attempts = task.attempts
        maximum_attempts = int(os.getenv("REED_HYDRATION_MAX_ATTEMPTS", "5"))
        failed = attempts >= maximum_attempts
        backoff_minutes = min(360, 2 ** max(0, attempts - 1) * 5)
        repo.defer_reed_hydration_task(
            customer_id,
            source_id,
            f"{type(exc).__name__}: {exc}",
            (datetime.now(UTC) + timedelta(minutes=backoff_minutes)).isoformat(),
            expected_fingerprint=task.summary_fingerprint,
            failed=failed,
        )
        logger.warning(
            "Reed hydration request deferred",
            extra={"source_id": source_id, "attempts": attempts, "failed": failed},
        )
        return {"status": "failed" if failed else "deferred", "source_id": source_id}

    completed = repo.complete_reed_hydration_task(
        customer_id, source_id, datetime.now(UTC).isoformat(),
        expected_fingerprint=task.summary_fingerprint,
    )
    if not completed:
        return {"status": "superseded", "source_id": source_id}
    _emit_ready(repo, customer_id, source_id)
    return {"status": "completed", "source_id": source_id}


def _emit_ready(repo, customer_id: str, source_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    _sqs_client().send_message(
        QueueUrl=os.environ["REED_HYDRATION_READY_QUEUE_URL"],
        MessageBody=json.dumps(ready_message(customer_id, source_id), separators=(",", ":")),
    )
    repo.mark_reed_hydration_ready_sent(customer_id, source_id, now)


def _sqs_client():
    import boto3

    return boto3.client("sqs")
