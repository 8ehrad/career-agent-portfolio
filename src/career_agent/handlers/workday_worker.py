from __future__ import annotations

import json
import logging
import os

from career_agent.connectors import (
    RegisteredConnector,
    collect_jobs_with_diagnostics,
    connector_from_definition,
)
from career_agent.handlers.workday_dispatcher import _send_finalizer_check, _sqs_client
from career_agent.ingestion import S3JobStagingStore
from career_agent.runtime import repository
from career_agent.source_registry import load_source_registry


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    del context
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "unknown"))
        try:
            _process_record(record)
        except Exception as exc:
            failures.append({"itemIdentifier": message_id})
            logger.exception(
                "Workday task failed",
                extra={"message_id": message_id, "error_type": type(exc).__name__},
            )
    return {"batchItemFailures": failures}


def _process_record(record: dict) -> None:
    message = json.loads(record["body"])
    if message.get("schema_version") != 1 or message.get("kind") != "workday_source":
        raise ValueError("Unsupported Workday queue message")
    run_id = str(message["run_id"])
    source_key = str(message["source_key"])
    repo = repository()
    task = repo.get_ingestion_task(run_id, source_key)
    if task is None:
        raise ValueError(f"Unknown ingestion task {run_id}:{source_key}")
    if task.terminal:
        _enqueue_finalizer_if_ready(repo, run_id)
        return
    if not repo.start_ingestion_task_attempt(run_id, source_key):
        return

    definitions = {
        definition.key: definition
        for definition in load_source_registry(os.environ["SOURCE_REGISTRY_PATH"])
        if definition.status == "active" and definition.ats == "workday"
    }
    try:
        definition = definitions[source_key]
        connector = connector_from_definition(definition)
        if connector is None:
            raise ValueError(f"No connector available for {source_key}")
        jobs, errors, _, diagnostics = collect_jobs_with_diagnostics(
            [RegisteredConnector(definition, connector)]
        )
        if errors:
            raise RuntimeError(errors[0])
        diagnostic = diagnostics[0].to_dict()
        staging = S3JobStagingStore(os.environ["ARTIFACT_BUCKET"])
        artifact_key = staging.put_jobs(run_id, source_key, jobs)
        repo.complete_ingestion_task(run_id, source_key, artifact_key, diagnostic)
    except Exception as exc:
        repo.record_ingestion_task_error(run_id, source_key, f"{type(exc).__name__}: {exc}")
        raise

    _enqueue_finalizer_if_ready(repo, run_id)
    logger.info(
        json.dumps(
            {
                "event": "workday_task_completed",
                "run_id": run_id,
                "source_key": source_key,
                "listings_fetched": len(jobs),
                "duration_ms": diagnostic["duration_ms"],
            }
        )
    )


def _enqueue_finalizer_if_ready(repo, run_id: str) -> None:
    run = repo.get_ingestion_run(run_id)
    tasks = repo.list_ingestion_tasks(run_id)
    if run and len(tasks) == run.expected_tasks and all(task.terminal for task in tasks):
        _send_finalizer_check(_sqs_client(), run_id)
