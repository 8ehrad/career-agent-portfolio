from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime

from career_agent.connectors import SourceFetchDiagnostic
from career_agent.handlers.workday_dispatcher import _send_finalizer_check, _sqs_client
from career_agent.ingestion import S3JobStagingStore
from career_agent.pipeline import DiscoverySeed
from career_agent.runtime import pipeline, repository


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handle_records(event: dict) -> dict:
    results = [_process_record(record) for record in event.get("Records", [])]
    return {"batchItemFailures": [], "results": results}


def _process_record(record: dict) -> dict:
    message = json.loads(record["body"])
    if message.get("schema_version") != 1 or message.get("kind") != "finalize_ingestion":
        raise ValueError("Unsupported finalizer queue message")
    run_id = str(message["run_id"])
    repo = repository()
    ingestion_run = repo.get_ingestion_run(run_id)
    if ingestion_run is None:
        raise ValueError(f"Unknown ingestion run {run_id}")
    if ingestion_run.status == "completed":
        return {"run_id": run_id, "status": "duplicate_completed"}

    now = datetime.now(UTC)
    deadline = datetime.fromisoformat(ingestion_run.deadline_at)
    tasks = repo.list_ingestion_tasks(run_id)
    ready = len(tasks) == ingestion_run.expected_tasks and all(task.terminal for task in tasks)
    if not ready and now < deadline:
        delay = max(1, min(900, round((deadline - now).total_seconds())))
        _send_finalizer_check(_sqs_client(), run_id, delay_seconds=delay)
        result = {
            "run_id": run_id,
            "status": "waiting",
            "terminal_tasks": sum(task.terminal for task in tasks),
            "expected_tasks": ingestion_run.expected_tasks,
            "next_check_seconds": delay,
        }
        logger.info(json.dumps({"event": "workday_finalizer_deferred", **result}))
        return result
    if not ready:
        repo.time_out_pending_ingestion_tasks(run_id, now.isoformat())
        tasks = repo.list_ingestion_tasks(run_id)

    staging = S3JobStagingStore(os.environ["ARTIFACT_BUCKET"])
    jobs = []
    errors: list[str] = []
    diagnostics: list[SourceFetchDiagnostic] = []
    statuses = Counter(task.status for task in tasks)
    for task in tasks:
        if task.status == "completed" and task.artifact_key and task.diagnostic:
            try:
                jobs.extend(staging.get_jobs(task.artifact_key))
                diagnostics.append(SourceFetchDiagnostic(**task.diagnostic))
            except Exception as exc:
                errors.append(f"{task.source_key}:staged_artifact:{type(exc).__name__}")
        else:
            completed_at = task.completed_at or now.isoformat()
            diagnostics.append(
                SourceFetchDiagnostic(
                    source_key=task.source_key,
                    source="workday",
                    employer=task.employer,
                    ats=task.ats,
                    tenant=task.tenant,
                    started_at=ingestion_run.started_at,
                    completed_at=completed_at,
                    duration_ms=round(
                        (
                            datetime.fromisoformat(completed_at)
                            - datetime.fromisoformat(ingestion_run.started_at)
                        ).total_seconds()
                        * 1000
                    ),
                    status="error",
                    listings_fetched=0,
                    error=task.last_error or "ingestion_deadline_exceeded",
                )
            )
            errors.append(f"{task.source_key}:{task.last_error or 'ingestion_deadline_exceeded'}")

    fanout_ms = round((now - datetime.fromisoformat(ingestion_run.started_at)).total_seconds() * 1000)
    seed = DiscoverySeed(
        jobs=jobs,
        errors=errors,
        source_fetches=diagnostics,
        configuration={
            "asynchronous_workday_ingestion": True,
            "workday_expected_tasks": ingestion_run.expected_tasks,
            "workday_completed_tasks": statuses.get("completed", 0),
            "workday_timed_out_tasks": statuses.get("timed_out", 0),
        },
        timings_ms={"workday_fanout_wall_clock": fanout_ms},
    )
    report, recommendations = pipeline(excluded_ats={"workday"}).run(
        run_id=run_id,
        send_email=ingestion_run.send_email,
        discovery_seed=seed,
    )
    if report.status != "duplicate":
        repo.complete_ingestion_run(run_id, datetime.now(UTC).isoformat(), report.status)
    else:
        # Recover the coordination record if an earlier finalizer completed
        # scoring but failed before it could update the ingestion metadata.
        existing_report = repo.get_run(run_id)
        if existing_report and existing_report.status == "completed":
            repo.complete_ingestion_run(run_id, datetime.now(UTC).isoformat(), existing_report.status)
            report = existing_report
    result = {
        "run_id": run_id,
        "status": report.status,
        "workday_listings": len(jobs),
        "workday_task_statuses": dict(statuses),
        "recommendation_ids": [job.id for job in recommendations],
    }
    logger.info(json.dumps({"event": "workday_finalization_completed", **result}))
    return result
