from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from career_agent.analysis_cache import (
    analysis_identity,
    analysis_priority_components,
    estimated_analysis_input_tokens,
    vacancy_is_closed,
)
from career_agent.connectors import _reed_raw_job
from career_agent.enrichment import BedrockAnalyzer, RuleBasedAnalyzer
from career_agent.rich_matching import make_analyzer
from career_agent.listing_details import reusable
from career_agent.models import AnalysisTask
from career_agent.normalization import normalize_job
from career_agent.profile import load_profile
from career_agent.reed_hydration import REED_HYDRATION_SCHEMA_VERSION
from career_agent.runtime import repository
from career_agent.scoring import hard_filter, score_job


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    del context
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "unknown"))
        try:
            result = _process_record(record)
            logger.info(json.dumps({"event": "reed_hydration_ready_result", **result}))
        except Exception:
            failures.append({"itemIdentifier": message_id})
            logger.exception("Reed hydration ready event failed", extra={"message_id": message_id})
    return {"batchItemFailures": failures}


def _process_record(record: dict) -> dict[str, str | None]:
    message = json.loads(record["body"])
    if (
        message.get("schema_version") != REED_HYDRATION_SCHEMA_VERSION
        or message.get("kind") != "reed_hydration_ready"
    ):
        raise ValueError("Unsupported Reed hydration ready message")
    customer_id = str(message["customer_id"])
    source_id = str(message["source_id"])
    if customer_id != os.getenv("CAREER_CUSTOMER_ID", "personal"):
        raise ValueError("Reed ready customer does not match this deployment")
    repo = repository()
    task = repo.get_reed_hydration_task(customer_id, source_id)
    if task is None:
        raise ValueError(f"Unknown Reed hydration task {customer_id}:{source_id}")
    if task.ready_status != "pending":
        return {"status": "duplicate", "source_id": source_id}
    if task.status != "completed":
        raise ValueError(f"Reed hydration task is not complete: {task.status}")
    record_value = repo.get_listing_details("reed", [source_id]).get(source_id)
    if record_value is None or not reusable(record_value, source_id, task.summary):
        raise ValueError("Hydrated Reed listing is missing or stale")

    job = normalize_job(
        _reed_raw_job(task.summary, record_value["details"], "United Kingdom"), "reed"
    )
    job.source_key = "reed"
    now = datetime.now(UTC)
    profile = load_profile(os.environ.get("PROFILE_PATH"))
    decision = hard_filter(job, profile)
    if not decision.passed or vacancy_is_closed(job, now):
        reasons = list(decision.reasons)
        if vacancy_is_closed(job, now):
            reasons.append("Vacancy has closed")
        repo.mark_reed_hydration_ready_processed(
            customer_id,
            source_id,
            now.isoformat(),
            outcome="rejected",
            expected_fingerprint=task.summary_fingerprint,
            gate_reasons=reasons,
        )
        return {"status": "rejected", "source_id": source_id}

    job = score_job(RuleBasedAnalyzer().analyze(job, profile), profile)
    analyzer = (
        (make_analyzer(os.environ["STRONG_MODEL_ID"], bedrock_client=object()) if os.getenv("MATCHING_MODE") == "rich"
         else BedrockAnalyzer(os.environ["STRONG_MODEL_ID"], bedrock_client=object()))
        if os.getenv("STRONG_MODEL_ID")
        else RuleBasedAnalyzer()
    )
    identity = analysis_identity(job, profile, analyzer)
    cached = repo.get_deep_analysis_records([identity.cache_key]).get(identity.cache_key)
    if cached is not None and cached.job_id == job.id:
        repo.mark_reed_hydration_ready_processed(
            customer_id, source_id, now.isoformat(), outcome="analysis_cache_hit",
            expected_fingerprint=task.summary_fingerprint,
        )
        return {"status": "analysis_cache_hit", "source_id": source_id}

    history = repo.get_analysis_histories([job.id]).get(job.id)
    previous = repo.get_analysis_task(customer_id, identity.cache_key)
    first_queued_at = previous.first_queued_at if previous else now.isoformat()
    priority = analysis_priority_components(
        job, history, identity, now, first_queued_at=first_queued_at
    )
    snapshot = job.to_dict()
    snapshot["description"] = job.description[:18_000]
    snapshot["listings"] = []
    analysis_task = AnalysisTask(
        customer_id=customer_id,
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
        priority_score=priority["priority_score"],
        priority_components=priority,
        first_queued_at=first_queued_at,
        last_seen_at=now.isoformat(),
        closes_at=job.closes_at,
        estimated_input_tokens=estimated_analysis_input_tokens(job, profile),
    )
    queued = repo.queue_analysis_task(analysis_task)
    outcome = "analysis_queued" if queued else "analysis_already_active"
    repo.mark_reed_hydration_ready_processed(
        customer_id,
        source_id,
        now.isoformat(),
        outcome=outcome,
        expected_fingerprint=task.summary_fingerprint,
        analysis_task_id=identity.cache_key,
    )
    return {"status": outcome, "source_id": source_id, "analysis_task_id": identity.cache_key}
