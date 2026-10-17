from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime

from career_agent.analysis_budget import AnalysisBudgetLimits
from career_agent.async_analysis import (
    ANALYSIS_QUEUE_SCHEMA_VERSION,
    _send_finalizer_check,
)
from career_agent.ingestion import S3JobStagingStore
from career_agent.runtime import pipeline, repository


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    del context
    failures: list[dict[str, str]] = []
    results: list[dict] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "unknown"))
        try:
            results.append(_process_record(record))
        except Exception as exc:
            failures.append({"itemIdentifier": message_id})
            logger.exception(
                "Deep-analysis finalization failed",
                extra={"message_id": message_id, "error_type": type(exc).__name__},
            )
    return {"batchItemFailures": failures, "results": results}


def _process_record(record: dict) -> dict:
    message = json.loads(record["body"])
    if (
        message.get("schema_version") != ANALYSIS_QUEUE_SCHEMA_VERSION
        or message.get("kind") != "finalize_deep_analysis"
    ):
        raise ValueError("Unsupported deep-analysis finalizer message")
    run_id = str(message["run_id"])
    repo = repository()
    analysis_run = repo.get_analysis_run(run_id)
    if analysis_run is None:
        raise ValueError(f"Unknown analysis run {run_id}")
    if analysis_run.status == "completed":
        return {"run_id": run_id, "status": "duplicate_completed"}

    now = datetime.now(UTC)
    deadline = datetime.fromisoformat(analysis_run.deadline_at)
    tasks_by_id = repo.get_analysis_tasks(
        analysis_run.customer_id, analysis_run.selected_task_ids
    )
    ready = (
        len(tasks_by_id) == analysis_run.expected_tasks
        and all(task.terminal for task in tasks_by_id.values())
    )
    if not ready and now < deadline:
        delay = max(1, min(900, round((deadline - now).total_seconds())))
        _send_finalizer_check(
            _sqs_client(),
            os.environ["ANALYSIS_FINALIZER_QUEUE_URL"],
            run_id,
            delay_seconds=delay,
        )
        result = {
            "run_id": run_id,
            "status": "waiting",
            "terminal_tasks": sum(task.terminal for task in tasks_by_id.values()),
            "expected_tasks": analysis_run.expected_tasks,
            "next_check_seconds": delay,
        }
        logger.info(json.dumps({"event": "deep_analysis_finalizer_deferred", **result}))
        return result
    if not ready:
        repo.time_out_pending_analysis_tasks(run_id, now.isoformat())
        tasks_by_id = repo.get_analysis_tasks(
            analysis_run.customer_id, analysis_run.selected_task_ids
        )

    if analysis_run.run_type == "backlog_drain":
        return _finalize_backlog_drain(repo, analysis_run, tasks_by_id, now)

    staging = S3JobStagingStore(os.environ["ARTIFACT_BUCKET"])
    candidates = staging.get_jobs(analysis_run.candidate_artifact_key)
    tasks = [
        tasks_by_id[task_id]
        for task_id in analysis_run.selected_task_ids
        if task_id in tasks_by_id
    ]
    report, recommendations = pipeline(enable_async_analysis=False).finalize_analysis_run(
        analysis_run, candidates, tasks
    )
    statuses = Counter(task.status for task in tasks)
    result = {
        "run_id": run_id,
        "status": report.status,
        "task_statuses": dict(statuses),
        "recommendation_ids": [job.id for job in recommendations],
    }
    logger.info(json.dumps({"event": "deep_analysis_finalization_completed", **result}))
    return result


def _finalize_backlog_drain(repo, analysis_run, tasks_by_id, now: datetime) -> dict:
    tasks = [
        tasks_by_id[task_id]
        for task_id in analysis_run.selected_task_ids
        if task_id in tasks_by_id
    ]
    statuses = Counter(task.status for task in tasks)
    limits = AnalysisBudgetLimits.from_environment()
    model_calls = sum(task.model_calls for task in tasks)
    input_tokens = sum(task.input_tokens for task in tasks)
    output_tokens = sum(task.output_tokens for task in tasks)
    actual_cost = limits.estimated_cost_microusd(input_tokens, output_tokens)
    pipeline_status = "partial" if statuses.get("timed_out", 0) else "completed"
    repo.complete_analysis_run(
        analysis_run.run_id,
        now.isoformat(),
        pipeline_status,
        metrics={
            "tasks_completed": statuses.get("completed", 0),
            "tasks_timed_out": statuses.get("timed_out", 0),
            "model_calls": model_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "actual_cost_microusd": actual_cost,
        },
    )
    result = {
        "run_id": analysis_run.run_id,
        "status": "completed",
        "pipeline_status": pipeline_status,
        "task_statuses": dict(statuses),
        "model_calls": model_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "actual_cost_microusd": actual_cost,
    }
    logger.info(json.dumps({"event": "analysis_backlog_drain_completed", **result}))
    return result


def _sqs_client():
    import boto3

    return boto3.client("sqs")
