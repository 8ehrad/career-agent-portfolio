from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from career_agent.analysis_budget import AnalysisBudgetLimits
from career_agent.analysis_cache import AnalysisIdentity, analysis_identity, record_from_job
from career_agent.async_analysis import (
    ANALYSIS_QUEUE_SCHEMA_VERSION,
    _send_finalizer_check,
)
from career_agent.enrichment import BedrockAnalyzer, RuleBasedAnalyzer
from career_agent.rich_matching import make_analyzer, MatchingDeferred
from career_agent.models import Job
from career_agent.profile import load_profile
from career_agent.reed_hydration import reed_analysis_task_has_full_advert
from career_agent.runtime import repository


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
                "Deep-analysis task failed",
                extra={"message_id": message_id, "error_type": type(exc).__name__},
            )
    return {"batchItemFailures": failures}


def _process_record(record: dict) -> None:
    message = json.loads(record["body"])
    if (
        message.get("schema_version") != ANALYSIS_QUEUE_SCHEMA_VERSION
        or message.get("kind") != "deep_analysis_task"
    ):
        raise ValueError("Unsupported deep-analysis queue message")
    run_id = str(message["run_id"])
    customer_id = str(message["customer_id"])
    task_id = str(message["task_id"])
    repo = repository()
    task = repo.get_analysis_task(customer_id, task_id)
    if task is None:
        raise ValueError(f"Unknown analysis task {customer_id}:{task_id}")
    run = repo.get_analysis_run(run_id)
    run_type = run.run_type if run and run.customer_id == customer_id else (
        "backlog_drain" if run_id.startswith("backlog-drain-") else "pipeline"
    )
    if task.terminal:
        _enqueue_finalizer_if_ready(repo, run_id)
        return
    if not repo.start_analysis_task_attempt(customer_id, task_id, run_id):
        return

    if not reed_analysis_task_has_full_advert(repo, task):
        repo.defer_analysis_task(
            customer_id,
            task_id,
            run_id,
            datetime.now(UTC).isoformat(),
            "reed_full_description_not_available_or_snapshot_stale",
        )
        _enqueue_finalizer_if_ready(repo, run_id)
        return

    profile = load_profile(os.environ.get("PROFILE_PATH"))
    analyzer = (
        (make_analyzer(os.environ["STRONG_MODEL_ID"]) if os.getenv("MATCHING_MODE") == "rich"
         else BedrockAnalyzer(os.environ["STRONG_MODEL_ID"]))
        if os.getenv("STRONG_MODEL_ID")
        else RuleBasedAnalyzer()
    )
    job = Job.from_dict(task.job)
    current_identity = analysis_identity(job, profile, analyzer)
    if (
        task.task_id != task.cache_key
        or task.job_id != job.id
        or task.profile_version != profile.version
        or task.profile_fingerprint != current_identity.profile_fingerprint
        or task.analyzer_version != current_identity.analyzer_version
        or task.model_id != current_identity.model_id
    ):
        repo.record_analysis_task_error(
            customer_id,
            task_id,
            run_id,
            "ValueError: Analysis task identity no longer matches the runtime configuration",
        )
        raise ValueError("Analysis task identity no longer matches the runtime configuration")
    identity = AnalysisIdentity(
        cache_key=task.cache_key,
        job_fingerprint=task.job_fingerprint,
        profile_fingerprint=task.profile_fingerprint,
        analyzer_version=task.analyzer_version,
        model_id=task.model_id,
    )
    input_before = int(getattr(analyzer, "input_tokens", 0))
    output_before = int(getattr(analyzer, "output_tokens", 0))
    calls_before = int(getattr(analyzer, "model_calls", 0))
    budget: tuple[str, int, int, int] | None = None
    model_calls = input_tokens = output_tokens = 0
    try:
        existing = repo.get_deep_analysis_records([task.cache_key]).get(task.cache_key)
        if existing is not None and existing.job_id == task.job_id:
            repo.complete_analysis_task(
                customer_id,
                task_id,
                run_id,
                datetime.now(UTC).isoformat(),
                result_source="cache",
                model_calls=0,
                input_tokens=0,
                output_tokens=0,
            )
        else:
            if getattr(analyzer, "manages_budget", False):
                analyzer.bind(repo, customer_id, run_type)
            if hasattr(analyzer, "model_calls") and not getattr(analyzer, "manages_budget", False):
                limits = AnalysisBudgetLimits.from_environment()
                budget_date = limits.budget_date()
                reserved_input_tokens = max(500, task.estimated_input_tokens)
                reserved_output_tokens = limits.estimated_output_tokens_per_call
                reserved_cost = limits.estimated_cost_microusd(
                    reserved_input_tokens, reserved_output_tokens
                )
                reserved_at = datetime.now(UTC).isoformat()
                if not repo.reserve_analysis_capacity(
                    customer_id,
                    budget_date,
                    reserved_at,
                    input_tokens=reserved_input_tokens,
                    output_tokens=reserved_output_tokens,
                    cost_microusd=reserved_cost,
                    maximum_calls=limits.maximum_calls,
                    maximum_input_tokens=limits.maximum_input_tokens,
                    maximum_output_tokens=limits.maximum_output_tokens,
                    maximum_cost_microusd=limits.maximum_cost_microusd,
                    run_type=run_type,
                ):
                    current_usage = repo.get_analysis_budget_usage(customer_id, budget_date)
                    discovery_reserved = (
                        run_type in {"backlog_drain", "manual_discovery"}
                        and current_usage is not None
                        and current_usage.discovery_state not in {"none", "released"}
                        and current_usage.calls_committed >= limits.backlog_call_ceiling(current_usage)
                        and current_usage.calls_committed < limits.maximum_calls
                    )
                    reason = (
                        "discovery_capacity_reserved" if discovery_reserved
                        else "daily_analysis_budget_exhausted"
                    )
                    repo.defer_analysis_task(
                        customer_id,
                        task_id,
                        run_id,
                        reserved_at,
                        reason,
                    )
                    _enqueue_finalizer_if_ready(repo, run_id)
                    logger.info(
                        json.dumps(
                            {
                                "event": "deep_analysis_task_budget_deferred",
                                "run_id": run_id,
                                "customer_id": customer_id,
                                "task_id": task_id,
                                "budget_date": budget_date,
                                "reason": reason,
                            }
                        )
                    )
                    return
                budget = (
                    budget_date,
                    reserved_input_tokens,
                    reserved_output_tokens,
                    reserved_cost,
                )
            analyzed = analyzer.analyze(job, profile)
            input_tokens = int(getattr(analyzer, "input_tokens", 0)) - input_before
            output_tokens = int(getattr(analyzer, "output_tokens", 0)) - output_before
            model_calls = int(getattr(analyzer, "model_calls", 0)) - calls_before
            repo.put_deep_analysis_record(
                record_from_job(
                    analyzed,
                    identity,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            )
            repo.complete_analysis_task(
                customer_id,
                task_id,
                run_id,
                datetime.now(UTC).isoformat(),
                result_source="fresh",
                model_calls=model_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
    except MatchingDeferred as exc:
        repo.defer_analysis_task(customer_id, task_id, run_id, datetime.now(UTC).isoformat(), str(exc))
        _enqueue_finalizer_if_ready(repo, run_id)
        logger.info(json.dumps({"event": "rich_matching_deferred", "run_id": run_id,
                               "task_id": task_id, "reason": str(exc),
                               "model_calls": analyzer.model_calls}))
        return
    except Exception as exc:
        input_tokens = max(
            0, int(getattr(analyzer, "input_tokens", 0)) - input_before
        )
        output_tokens = max(
            0, int(getattr(analyzer, "output_tokens", 0)) - output_before
        )
        model_calls = max(
            0, int(getattr(analyzer, "model_calls", 0)) - calls_before
        )
        repo.record_analysis_task_error(
            customer_id,
            task_id,
            run_id,
            f"{type(exc).__name__}: {exc}",
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        _reconcile_budget(
            repo,
            customer_id,
            budget,
            limits if budget else None,
            model_calls,
            input_tokens,
            output_tokens,
        )
        raise

    _reconcile_budget(
        repo,
        customer_id,
        budget,
        limits if budget else None,
        model_calls,
        input_tokens,
        output_tokens,
    )

    _enqueue_finalizer_if_ready(repo, run_id)
    logger.info(
        json.dumps(
            {
                "event": "deep_analysis_task_completed",
                "run_id": run_id,
                "customer_id": customer_id,
                "task_id": task_id,
                "job_id": task.job_id,
            }
        )
    )


def _enqueue_finalizer_if_ready(repo, run_id: str) -> None:
    run = repo.get_analysis_run(run_id)
    if run is None or run.status == "completed":
        return
    tasks = repo.get_analysis_tasks(run.customer_id, run.selected_task_ids)
    ready = len(tasks) == run.expected_tasks and all(task.terminal for task in tasks.values())
    if ready:
        _send_finalizer_check(
            _sqs_client(), os.environ["ANALYSIS_FINALIZER_QUEUE_URL"], run_id
        )


def _reconcile_budget(
    repo,
    customer_id: str,
    budget: tuple[str, int, int, int] | None,
    limits: AnalysisBudgetLimits | None,
    model_calls: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    if budget is None or limits is None or model_calls <= 0:
        return
    budget_date, reserved_input, reserved_output, reserved_cost = budget
    repo.reconcile_analysis_capacity(
        customer_id,
        budget_date,
        datetime.now(UTC).isoformat(),
        reserved_input_tokens=reserved_input,
        reserved_output_tokens=reserved_output,
        reserved_cost_microusd=reserved_cost,
        actual_calls=model_calls,
        actual_input_tokens=input_tokens,
        actual_output_tokens=output_tokens,
        actual_cost_microusd=limits.estimated_cost_microusd(
            input_tokens, output_tokens
        ),
    )


def _sqs_client():
    import boto3

    return boto3.client("sqs")
