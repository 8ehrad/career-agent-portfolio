from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from career_agent.analysis_budget import AnalysisBudgetLimits, BacklogDrainPolicy
from career_agent.metrics import MetricsService
from career_agent.operations import OperationsService
from career_agent.coverage import coverage_summary
from career_agent.employer_universe import (
    canonical_employer_name,
    load_employer_source_routes,
    load_employer_universe,
)
from career_agent.models import CoverageSample, JobStatus
from career_agent.runtime import application_service, repository
from career_agent.source_registry import load_source_registry


ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.RECOMMENDED: {JobStatus.SHORTLISTED, JobStatus.DISMISSED},
    JobStatus.SHORTLISTED: {JobStatus.RECOMMENDED, JobStatus.PREPARING, JobStatus.APPLIED, JobStatus.DISMISSED},
    JobStatus.PREPARING: {JobStatus.READY, JobStatus.SHORTLISTED},
    JobStatus.READY: {JobStatus.APPLIED, JobStatus.SHORTLISTED, JobStatus.DISMISSED},
    JobStatus.APPLIED: {JobStatus.RECRUITER_RESPONSE, JobStatus.INTERVIEW, JobStatus.REJECTED, JobStatus.WITHDRAWN},
    JobStatus.RECRUITER_RESPONSE: {JobStatus.INTERVIEW, JobStatus.REJECTED, JobStatus.WITHDRAWN},
    JobStatus.INTERVIEW: {JobStatus.OFFER, JobStatus.REJECTED, JobStatus.WITHDRAWN},
    JobStatus.OFFER: {JobStatus.WITHDRAWN},
    JobStatus.REJECTED: set(),
    JobStatus.WITHDRAWN: set(),
    JobStatus.DISMISSED: {JobStatus.RECOMMENDED, JobStatus.SHORTLISTED},
}


def handler(event, context):
    try:
        method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
        path = event.get("rawPath", "/")
        if method == "OPTIONS":
            return _response(204, {})
        if method == "GET" and path == "/health":
            return _response(200, {"status": "ok"})
        _require_user(event)
        if method == "GET" and path == "/jobs":
            return _list_jobs(event)
        if method == "GET" and path == "/applications":
            return _list_applications()
        if method == "GET" and path == "/metrics":
            return _get_metrics(event)
        if method == "GET" and path == "/operations":
            return _response(200, _operations_service().overview())
        if method == "GET" and path == "/operations/tasks":
            return _response(200, _operations_service().task_page(event.get("queryStringParameters") or {}))
        if method == "GET" and path == "/operations/run":
            return _response(200, _operations_service().run_detail((event.get("queryStringParameters") or {}).get("run_id")))
        if method == "GET" and path == "/pipeline-runs":
            return _list_pipeline_runs(event)
        if method == "GET" and path == "/ingestion-runs":
            return _list_ingestion_runs(event)
        if method == "GET" and path == "/analysis-backlog":
            return _list_analysis_backlog(event)
        if method == "GET" and path == "/analysis-throughput":
            return _get_analysis_throughput(event)
        if method == "GET" and path == "/source-registry":
            return _get_source_registry()
        if method == "GET" and path == "/employer-universe":
            return _get_employer_universe()
        if method == "GET" and path == "/coverage-benchmark":
            return _get_coverage_benchmark(event)
        if method == "POST" and path == "/coverage-benchmark/samples":
            return _create_coverage_sample(_body(event))
        if method == "GET" and path.startswith("/jobs/") and path.endswith("/application-pack"):
            return _get_pack(path.split("/")[2])
        if method == "GET" and path.startswith("/jobs/") and path.endswith("/prepare-status"):
            return _prepare_status(path.split("/")[2])
        if method == "POST" and path.startswith("/jobs/") and path.endswith("/prepare"):
            return _prepare(path.split("/")[2])
        if method == "PATCH" and path.startswith("/jobs/"):
            return _update_job(path.split("/")[2], _body(event))
        if method == "GET" and path.startswith("/jobs/"):
            return _get_job(path.split("/")[2])
        return _response(404, {"error": "not_found"})
    except KeyError as exc:
        return _response(404, {"error": "not_found", "detail": str(exc)})
    except ValueError as exc:
        return _response(400, {"error": "invalid_request", "detail": str(exc)})
    except PermissionError as exc:
        return _response(401, {"error": "unauthorized", "detail": str(exc)})


def _require_user(event: dict[str, Any]) -> str:
    if os.getenv("DEV_AUTH_BYPASS") == "true":
        return "local-user"
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    subject = claims.get("sub")
    email = claims.get("email")
    allowed_email = os.getenv("ALLOWED_EMAIL", "").lower()
    if not subject or not email or str(email).lower() != allowed_email:
        raise PermissionError("A permitted Cognito user is required")
    return str(subject)


def _list_jobs(event: dict[str, Any]):
    status_value = (event.get("queryStringParameters") or {}).get("status")
    status = JobStatus(status_value) if status_value else None
    jobs = repository().list_jobs(status)
    return _response(200, {"items": [job.to_dict() for job in jobs], "count": len(jobs)})


def _list_applications():
    application_statuses = {
        JobStatus.PREPARING,
        JobStatus.READY,
        JobStatus.APPLIED,
        JobStatus.RECRUITER_RESPONSE,
        JobStatus.INTERVIEW,
        JobStatus.OFFER,
        JobStatus.REJECTED,
        JobStatus.WITHDRAWN,
    }
    jobs = [job for job in repository().list_jobs() if job.status in application_statuses]
    return _response(200, {"items": [job.to_dict() for job in jobs], "count": len(jobs)})


@lru_cache(maxsize=1)
def _operations_service() -> OperationsService:
    return OperationsService.from_environment(repository())


@lru_cache(maxsize=1)
def _metrics_service() -> MetricsService:
    return MetricsService.from_environment()


@lru_cache(maxsize=6)
def _metrics_snapshot(range_name: str, minute_bucket: str) -> dict[str, Any]:
    del minute_bucket
    return _metrics_service().snapshot(range_name)


def _get_metrics(event: dict[str, Any]):
    range_name = (event.get("queryStringParameters") or {}).get("range", "24h")
    minute_bucket = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    return _response(200, _metrics_snapshot(range_name, minute_bucket))


def _list_pipeline_runs(event: dict[str, Any]):
    raw_limit = (event.get("queryStringParameters") or {}).get("limit", "30")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    runs = repository().list_runs(limit)
    return _response(
        200,
        {
            "items": [run.to_dict() for run in runs],
            "count": len(runs),
            "stage_definitions": {
                "collected_listings": "Source listings returned before cross-source deduplication.",
                "canonical_unique": "Canonical vacancies after deterministic deduplication.",
                "hard_filter_passed": "Vacancies passing role, geography, seniority and known-salary gates.",
                "fast_analyzed": "Vacancies processed by the deterministic analyzer.",
                "salary_evaluation_pool": "All gate-passing vacancies classified for salary eligibility.",
                "salary_research_selected": (
                    "Highest-ranked missing-salary vacancies selected for bounded external research."
                ),
                "salary_research_attempted": (
                    "Vacancies that required an external salary search after cache lookup."
                ),
                "salary_confirmed_pass": "Vacancies with salary evidence at or above the configured floor.",
                "salary_unverified_but_plausible": (
                    "Vacancies retained because no below-floor salary was confirmed."
                ),
                "salary_confirmed_fail": "Vacancies rejected by explicit below-floor salary evidence.",
                "salary_qualified": "Compatibility total: confirmed-pass plus plausible-unverified salary.",
                "deep_analysis_candidate_universe": (
                    "All salary-confirmed or plausible vacancies considered for deep analysis."
                ),
                "deep_analysis_dispatched": "Durable backlog tasks dispatched to asynchronous workers.",
                "deep_analysis_cache_hits": (
                    "Vacancies evaluated from an exact versioned analysis-cache match."
                ),
                "deep_analysis_cache_misses": (
                    "Vacancies requiring a fresh analysis before they can use the cache."
                ),
                "deep_analysis_deferred": (
                    "Cache misses left for a later run after the fresh-call budget was allocated."
                ),
                "deep_analysis_attempted": "Fresh model analyses attempted in this run.",
                "deep_analysis_evaluated": (
                    "Cached and freshly processed vacancies available to final ranking."
                ),
                "analysis_backlog_total": "Active cache-miss tasks retained in the durable analysis backlog.",
                "analysis_backlog_queued_after_dispatch": (
                    "Queued or previously timed-out tasks not covered by this run's "
                    "fresh-call budget."
                ),
                "analysis_backlog_expired_total": (
                    "Inactive or explicitly closed backlog tasks retained as expired records."
                ),
                "analysis_backlog_queued_after_completion": (
                    "Tasks still awaiting a later processing budget after this run completed."
                ),
                "analysis_tasks_completed": "Selected tasks that completed before the analysis deadline.",
                "analysis_tasks_timed_out": "Selected tasks returned to backlog eligibility for any reason, including immediate budget deferrals.",
                "analysis_budget_deferred": "Selected tasks deferred by a daily guardrail or reserved discovery capacity; no model call was made for that attempt.",
                "analysis_deadline_timed_out": "Selected tasks still running or dispatched when the analysis deadline elapsed.",
                "qualified_shortlist": "Vacancies passing final attainability and remuneration scoring.",
                "new_recommendations": "Qualified vacancies never previously saved as recommendations.",
                "digest_included": "Current qualified vacancies included in this run's digest.",
                "user_shortlisted_total": "All vacancies currently marked shortlisted by the user.",
            },
            "attribution": "Post-deduplication stages use the canonical job's highest-priority source.",
        },
    )


def _list_analysis_backlog(event: dict[str, Any]):
    parameters = event.get("queryStringParameters") or {}
    status_filter = parameters.get("status")
    allowed_statuses = {
        "queued",
        "dispatched",
        "processing",
        "completed",
        "timed_out",
        "expired",
        "cancelled",
    }
    if status_filter and status_filter not in allowed_statuses:
        raise ValueError("unsupported analysis task status")
    raw_limit = parameters.get("limit", "100")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")

    customer_id = os.getenv("CAREER_CUSTOMER_ID", "personal")
    tasks = repository().list_analysis_tasks(customer_id)
    status_counts: dict[str, int] = {}
    for task in tasks:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1
    filtered = [task for task in tasks if not status_filter or task.status == status_filter][:limit]
    items = []
    for task in filtered:
        job = task.job
        items.append(
            {
                **{key: value for key, value in task.to_dict().items() if key != "job"},
                "job": {
                    "id": job.get("id"),
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "location": job.get("location"),
                    "apply_url": job.get("apply_url"),
                },
            }
        )
    return _response(
        200,
        {
            "items": items,
            "count": len(items),
            "total": len(tasks),
            "status_counts": status_counts,
            "customer_id": customer_id,
        },
    )


def _get_analysis_throughput(event: dict[str, Any]):
    raw_limit = (event.get("queryStringParameters") or {}).get("limit", "30")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    customer_id = os.getenv("CAREER_CUSTOMER_ID", "personal")
    limits = AnalysisBudgetLimits.from_environment()
    policy = BacklogDrainPolicy.from_environment()
    now = datetime.now(UTC)
    repo = repository()
    usage = repo.get_analysis_budget_usage(customer_id, limits.budget_date(now))
    tasks = repo.list_analysis_tasks(customer_id)
    queued = [task for task in tasks if task.status in {"queued", "timed_out"}]
    oldest = min((task.first_queued_at for task in queued), default=None)
    oldest_age_hours = 0.0
    if oldest:
        observed = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        oldest_age_hours = max(0.0, (now - observed).total_seconds() / 3_600)
    return _response(
        200,
        {
            "customer_id": customer_id,
            "generated_at": now.isoformat(),
            "budget_date": limits.budget_date(now),
            "daily_limits": {
                "calls": limits.maximum_calls,
                "input_tokens": limits.maximum_input_tokens,
                "output_tokens": limits.maximum_output_tokens,
                "cost_microusd": limits.maximum_cost_microusd,
            },
            "daily_usage": usage.to_dict() if usage else None,
            "policy": {
                "minimum_daily_calls": policy.minimum_daily_calls,
                "maximum_daily_calls": policy.maximum_daily_calls,
                "maximum_batch_size": policy.maximum_batch_size,
                "oldest_job_slo_hours": policy.oldest_job_slo_hours,
            },
            "backlog": {
                "queued": len(queued),
                "oldest_queued_at": oldest,
                "oldest_age_hours": round(oldest_age_hours, 2),
                "slo_breached": oldest_age_hours > policy.oldest_job_slo_hours,
            },
            "runs": [
                run.to_dict()
                for run in repo.list_analysis_runs(limit, run_type="backlog_drain")
            ],
        },
    )


def _list_ingestion_runs(event: dict[str, Any]):
    raw_limit = (event.get("queryStringParameters") or {}).get("limit", "10")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= 30:
        raise ValueError("limit must be between 1 and 30")
    repo = repository()
    items = []
    for run in repo.list_ingestion_runs(limit):
        tasks = repo.list_ingestion_tasks(run.run_id)
        status_counts: dict[str, int] = {}
        for task in tasks:
            status_counts[task.status] = status_counts.get(task.status, 0) + 1
        items.append(
            {
                **run.to_dict(),
                "task_status_counts": status_counts,
                "tasks": [task.to_dict() for task in tasks],
            }
        )
    return _response(200, {"items": items, "count": len(items)})


def _get_source_registry():
    registry_path = os.getenv("SOURCE_REGISTRY_PATH")
    definitions = load_source_registry(registry_path) if registry_path else []
    health_by_key = {item.source_key: item.to_dict() for item in repository().list_source_health()}
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for definition in definitions:
        health = health_by_key.pop(definition.key, None)
        if health and health.get("last_successful_fetch_at"):
            last_success = datetime.fromisoformat(health["last_successful_fetch_at"])
            health["stale"] = (now - last_success).total_seconds() > 7 * 24 * 3600
        elif health:
            health["stale"] = True
        items.append({**definition.to_dict(), "health": health})
    for source_key, health in sorted(health_by_key.items()):
        health["stale"] = not health.get("last_successful_fetch_at") or (
            now - datetime.fromisoformat(health["last_successful_fetch_at"])
        ).total_seconds() > 7 * 24 * 3600
        items.append(
            {
                "key": source_key,
                "employer": health.get("employer"),
                "ats": health.get("ats"),
                "tenant": health.get("tenant"),
                "uk_presence": None,
                "status": "discovered",
                "permission_status": "not_applicable",
                "config": {},
                "health": health,
            }
        )
    return _response(200, {"items": items, "count": len(items), "stale_after_days": 7})


def _get_employer_universe():
    universe_path = os.getenv("EMPLOYER_UNIVERSE_PATH")
    if not universe_path:
        return _response(200, {"items": [], "count": 0})
    universe = load_employer_universe(universe_path)
    routes_path = os.getenv("EMPLOYER_SOURCE_ROUTES_PATH")
    routes = load_employer_source_routes(routes_path) if routes_path else {}
    registry_path = os.getenv("SOURCE_REGISTRY_PATH")
    definitions = load_source_registry(registry_path) if registry_path else []
    active_by_employer: dict[str, list[str]] = {}
    for definition in definitions:
        if definition.status == "active":
            active_by_employer.setdefault(
                canonical_employer_name(definition.employer), []
            ).append(definition.key)

    items: list[dict[str, Any]] = []
    for employer in universe.employers:
        key = canonical_employer_name(employer.name)
        route = routes.get(key, {})
        active_source_keys = sorted(active_by_employer.get(key, []))
        items.append(
            {
                **employer.to_dict(),
                "fetch": {
                    "strategy": (
                        "active_direct_source"
                        if active_source_keys
                        else route.get("strategy", "aggregator_then_ats_discovery")
                    ),
                    "candidates": route.get("candidates", []),
                    "active_source_keys": active_source_keys,
                },
            }
        )
    strategy_counts: dict[str, int] = {}
    for item in items:
        strategy = item["fetch"]["strategy"]
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
    return _response(
        200,
        {
            "items": items,
            "count": len(items),
            "as_of": universe.as_of,
            "selection_notes": list(universe.selection_notes),
            "strategy_counts": strategy_counts,
        },
    )


def _create_coverage_sample(body: dict[str, Any]):
    title = str(body.get("title") or "").strip()
    company = str(body.get("company") or "").strip()
    if not title or not company:
        raise ValueError("title and company are required")
    observed_at = _parse_timestamp(body.get("observed_at")) if body.get("observed_at") else datetime.now(UTC)
    if observed_at > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("observed_at cannot be in the future")
    reference_url = str(body.get("reference_url") or "").strip() or None
    if reference_url:
        parts = urllib.parse.urlsplit(reference_url)
        hostname = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (hostname == "linkedin.com" or hostname.endswith(".linkedin.com")):
            raise ValueError("reference_url must be an HTTPS LinkedIn URL")
    sample = CoverageSample(
        sample_id=uuid.uuid4().hex,
        observed_at=observed_at.isoformat(),
        deadline_at=(observed_at + timedelta(hours=24)).isoformat(),
        title=title[:300],
        company=company[:200],
        location=str(body.get("location") or "")[:300],
        reference_url=reference_url,
    )
    repository().put_coverage_sample(sample)
    return _response(201, sample.to_dict())


def _get_coverage_benchmark(event: dict[str, Any]):
    raw_limit = (event.get("queryStringParameters") or {}).get("limit", "200")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    repo = repository()
    samples = repo.list_coverage_samples(limit)
    return _response(
        200,
        {
            "items": [sample.to_dict() for sample in samples],
            "count": len(samples),
            "metrics": coverage_summary(samples),
            "method": "Human-entered LinkedIn sample; no LinkedIn page is fetched or scraped.",
        },
    )


def _parse_timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(UTC)


def _get_job(job_id: str):
    job = repository().get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    return _response(200, job.to_dict())


def _update_job(job_id: str, body: dict[str, Any]):
    repo = repository()
    job = repo.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    next_status = JobStatus(body["status"])
    if next_status != job.status and next_status not in ALLOWED_TRANSITIONS[job.status]:
        raise ValueError(f"Cannot move {job.status.value} to {next_status.value}")
    job.status = next_status
    if next_status == JobStatus.DISMISSED:
        job.dismissal_reason = str(body.get("reason", ""))[:500] or None
    tracking = job.application_tracking
    if notes := body.get("notes"):
        tracking["notes"] = str(notes)[:5000]
    if next_status == JobStatus.APPLIED and "applied_at" not in tracking:
        tracking["applied_at"] = body.get("applied_at") or datetime.now(UTC).isoformat()
    if next_status in {JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED, JobStatus.WITHDRAWN}:
        tracking[f"{next_status.value}_at"] = datetime.now(UTC).isoformat()
    repo.put_job(job)
    return _response(200, job.to_dict())


def _prepare(job_id: str):
    if os.getenv("APPLICATION_PROFILE_MODE", "legacy") == "rich":
        return _prepare_async(job_id)
    service = application_service()
    from ..rich_application import ApplicationGenerationIncomplete
    try:
        pack = service.prepare(job_id)
    except ApplicationGenerationIncomplete as exc:
        return _response(503, {"error":"application_generation_incomplete", "detail":str(exc),
                               "retryable":True, "generation_state":"incomplete"})
    return _response(201, {**pack.to_dict(), "download_urls": service.download_urls(job_id)})


def _prepare_async(job_id: str):
    from ..application_async import new_task, queue_task, task_view
    queue_url = os.getenv("APPLICATION_PREPARE_QUEUE_URL")
    if not queue_url:
        return _response(503, {"error": "application_queue_unavailable",
                               "detail": "Application preparation is temporarily unavailable."})
    service = application_service()
    repo = service.repository
    job = repo.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.status not in {JobStatus.SHORTLISTED, JobStatus.PREPARING, JobStatus.READY}:
        raise ValueError("Shortlist the job before preparing an application")
    identity = service.generator.identity(job, service.profile)
    pack = repo.get_application_pack(job_id)
    if pack and pack.generation_metadata.get("generation_mode") == "rich_model" and pack.generation_metadata.get("identity") == identity:
        if job.status == JobStatus.PREPARING:
            repo.update_job_status(job_id, JobStatus.READY)
        return _response(200, {**pack.to_dict(), "download_urls": service.download_urls(job_id)})
    customer_id = os.getenv("CAREER_CUSTOMER_ID", "personal")
    task, created = repo.begin_application_task(new_task(customer_id, job_id, identity,
        JobStatus.SHORTLISTED if job.status == JobStatus.PREPARING else job.status))
    if created and job.status != JobStatus.PREPARING:
        repo.update_job_status(job_id, JobStatus.PREPARING)
    if created:
        try:
            queue_task(task, queue_url=queue_url)
        except Exception:
            repo.record_application_task_error(customer_id, job_id, task.task_id,
                datetime.now(UTC).isoformat(), "queue_dispatch_failed", retryable=False,
                expected_status="queued")
            current = repo.get_job(job_id)
            if current and current.status == JobStatus.PREPARING:
                repo.update_job_status(job_id, JobStatus(task.previous_status))
            return _response(503, {"error": "application_queue_unavailable",
                "detail": "Application preparation could not be queued. Please retry."})
    return _response(202, task_view(task))


def _prepare_status(job_id: str):
    from ..application_async import task_view
    service = application_service()
    repo = service.repository
    job = repo.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    identity = service.generator.identity(job, service.profile) if hasattr(service.generator, "identity") else None
    pack = repo.get_application_pack(job_id)
    if pack and identity and pack.generation_metadata.get("identity") == identity:
        return _response(200, {"status": "completed", "job_id": job_id,
            "pack": {**pack.to_dict(), "download_urls": service.download_urls(job_id)}})
    task = repo.get_application_task(os.getenv("CAREER_CUSTOMER_ID", "personal"), job_id)
    if task and task.status == "completed":
        return _response(200, {"status": "not_started", "job_id": job_id,
            "detail": "The saved application is missing or outdated; prepare it again."})
    return _response(200, task_view(task) if task else {"status": "not_started", "job_id": job_id})


def _get_pack(job_id: str):
    service = application_service()
    pack = repository().get_application_pack(job_id)
    if pack is None:
        raise KeyError(job_id)
    return _response(200, {**pack.to_dict(), "download_urls": service.download_urls(job_id),
                           "freshness": service.pack_freshness(job_id, pack)})


def _body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode()
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Body must be a JSON object")
    return payload


def _response(status: int, body: dict[str, Any]):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }
