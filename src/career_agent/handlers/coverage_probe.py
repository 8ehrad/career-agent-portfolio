from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from career_agent.connectors import (
    collect_jobs_with_diagnostics,
    connectors_from_environment,
    deduplicate_jobs,
    discover_ats_connectors,
)
from career_agent.coverage import evaluate_coverage_samples
from career_agent.models import SourceHealth
from career_agent.reed_hydration import SqsReedHydrationDispatcher
from career_agent.runtime import load_integration_environment, repository


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    del event, context
    repo = repository()
    now = datetime.now(UTC)
    pending = [sample for sample in repo.list_coverage_samples() if sample.status == "pending"]
    if not pending:
        result = {"status": "idle", "pending_samples": 0}
        logger.info(json.dumps({"event": "coverage_probe_idle", **result}))
        return result

    load_integration_environment()
    connector_options = {"excluded_ats": {"workday"}, "detail_store": repo}
    if os.getenv("REED_HYDRATION_QUEUE_URL"):
        import boto3

        connector_options["hydration_dispatcher"] = SqsReedHydrationDispatcher(
            repository=repo,
            sqs=boto3.client("sqs"),
            queue_url=os.environ["REED_HYDRATION_QUEUE_URL"],
            customer_id=os.getenv("CAREER_CUSTOMER_ID", "personal"),
        )
    connectors = connectors_from_environment(**connector_options)
    if not connectors:
        raise RuntimeError("Coverage probe has no configured sources")
    jobs, errors, source_counts, source_fetches = collect_jobs_with_diagnostics(connectors)
    discovered_connectors = discover_ats_connectors(jobs, connectors, excluded_ats={"workday"})
    if discovered_connectors:
        discovered_jobs, discovered_errors, discovered_counts, discovered_fetches = (
            collect_jobs_with_diagnostics(discovered_connectors)
        )
        jobs.extend(discovered_jobs)
        errors.extend(discovered_errors)
        source_fetches.extend(discovered_fetches)
        for source, count in discovered_counts.items():
            source_counts[source] = source_counts.get(source, 0) + count

    for fetch in source_fetches:
        previous = repo.get_source_health(fetch.source_key)
        succeeded = fetch.status != "error"
        repo.put_source_health(
            SourceHealth(
                source_key=fetch.source_key,
                source=fetch.source,
                ats=fetch.ats,
                employer=fetch.employer or (previous.employer if previous else None),
                tenant=fetch.tenant,
                status=fetch.status,
                last_attempt_at=fetch.completed_at,
                last_successful_fetch_at=(
                    fetch.completed_at
                    if succeeded
                    else previous.last_successful_fetch_at if previous else None
                ),
                last_error=fetch.error,
                consecutive_failures=(
                    0 if succeeded else (previous.consecutive_failures if previous else 0) + 1
                ),
                listings_fetched=fetch.listings_fetched,
                relevant_jobs_produced=previous.relevant_jobs_produced if previous else 0,
                qualified_jobs=previous.qualified_jobs if previous else 0,
                conversion_basis=previous.conversion_basis if previous else "no_pipeline_run_yet",
            )
        )

    if not jobs and errors:
        raise RuntimeError(
            "Coverage probe could not complete a usable source sweep; "
            f"{len(errors)} source fetches failed"
        )

    metrics = evaluate_coverage_samples(repo, deduplicate_jobs(jobs), datetime.now(UTC))
    result = {
        "status": "completed",
        "pending_samples_before_probe": len(pending),
        "collected_listings": len(jobs),
        "source_counts": source_counts,
        "source_fetches": [item.to_dict() for item in source_fetches],
        "coverage_metrics": metrics,
        "errors": errors,
    }
    logger.info(json.dumps({"event": "coverage_probe_completed", **result}))
    return result
