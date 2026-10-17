from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta

from career_agent.analysis_budget import AnalysisBudgetLimits, BacklogDrainPolicy
from career_agent.analysis_cache import vacancy_is_closed
from career_agent.async_analysis import (
    ANALYSIS_QUEUE_SCHEMA_VERSION,
    _seconds_until,
    _send_batches,
    _send_finalizer_check,
)
from career_agent.models import AnalysisBudgetUsage, AnalysisRun, AnalysisTask, Job
from career_agent.reed_hydration import hydrated_reed_analysis_task_ids
from career_agent.runtime import repository


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    del context
    now = datetime.now(UTC)
    limits = AnalysisBudgetLimits.from_environment()
    policy = BacklogDrainPolicy.from_environment()
    customer_id = os.getenv("CAREER_CUSTOMER_ID", "personal")
    run_id = _run_id(event, now)
    repo = repository()
    sqs = _sqs_client()

    existing = repo.get_analysis_run(run_id)
    if existing is not None:
        messages_sent = _enqueue(repo, sqs, existing)
        result = {
            "run_id": run_id,
            "status": "duplicate_completed" if existing.status == "completed" else "resumed",
            "selected_tasks": existing.expected_tasks,
            "messages_sent": messages_sent,
        }
        logger.info(json.dumps({"event": "analysis_backlog_drain_resumed", **result}))
        return result

    all_tasks = repo.list_analysis_tasks(customer_id)
    open_tasks = [
        task
        for task in all_tasks
        if task.status in {"queued", "timed_out"}
        and not vacancy_is_closed(Job.from_dict(task.job), now)
    ]
    eligible_reed_task_ids = hydrated_reed_analysis_task_ids(repo, open_tasks)
    eligible = [task for task in open_tasks if task.task_id in eligible_reed_task_ids]
    oldest_age_seconds = max(
        (_age_seconds(task.first_queued_at, now) for task in eligible), default=0
    )
    budget_date = limits.budget_date(now)
    usage = repo.get_analysis_budget_usage(customer_id, budget_date)
    calls_committed = usage.calls_committed if usage else 0
    backlog_ceiling = limits.backlog_call_ceiling(usage)
    adaptive_target = min(backlog_ceiling, policy.daily_target(eligible, now))
    remaining_calls = max(0, adaptive_target - calls_committed)
    cooldown = policy.throttling_cooldown_active(all_tasks, now)
    deadline_minutes = int(os.getenv("DEEP_ANALYSIS_DEADLINE_MINUTES", "15"))
    recent_runs = repo.list_analysis_runs(5, run_type="backlog_drain")
    batch_size = policy.adaptive_batch_size(
        recent_runs, deadline_seconds=deadline_minutes * 60
    )
    selection_count = 0 if cooldown else min(batch_size, remaining_calls)
    selected = policy.select(
        eligible,
        count=selection_count,
        seed=run_id,
        now=now,
    )
    selected = _within_remaining_budget(selected, usage, limits, maximum_calls=backlog_ceiling)
    from career_agent.freshness import service_from_environment, shadow_assess
    freshness_result = shadow_assess(service_from_environment(repo, sqs),
                                    [Job.from_dict(task.job) for task in selected], "backlog_analysis")
    dispatched_ids = repo.dispatch_analysis_tasks(
        customer_id,
        run_id,
        [task.task_id for task in selected],
        now.isoformat(),
        selection_reason="backlog_drainer",
    )
    dispatched_set = set(dispatched_ids)
    dispatched = [task for task in selected if task.task_id in dispatched_set]
    estimated_input_tokens = sum(task.estimated_input_tokens for task in dispatched)
    estimated_output_tokens = (
        len(dispatched) * limits.estimated_output_tokens_per_call
    )
    run = AnalysisRun(
        run_id=run_id,
        customer_id=customer_id,
        profile_version=(dispatched[0].profile_version if dispatched else "not_applicable"),
        started_at=now.isoformat(),
        prepared_at=now.isoformat(),
        deadline_at=(now + timedelta(minutes=deadline_minutes)).isoformat(),
        candidate_artifact_key="",
        selected_task_ids=dispatched_ids,
        expected_tasks=len(dispatched_ids),
        send_email=False,
        backlog_size=len(eligible),
        oldest_queued_at=min(
            (task.first_queued_at for task in eligible), default=None
        ),
        estimated_input_tokens=estimated_input_tokens,
        run_type="backlog_drain",
        trigger=str(event.get("source", "manual")) if isinstance(event, dict) else "manual",
        daily_target_calls=adaptive_target,
        daily_call_limit=limits.maximum_calls,
        daily_input_token_limit=limits.maximum_input_tokens,
        daily_output_token_limit=limits.maximum_output_tokens,
        daily_cost_limit_microusd=limits.maximum_cost_microusd,
        estimated_output_tokens=estimated_output_tokens,
        estimated_cost_microusd=limits.estimated_cost_microusd(
            estimated_input_tokens, estimated_output_tokens
        ),
        oldest_queue_age_seconds=oldest_age_seconds,
    )
    created = repo.begin_analysis_run(run)
    active_run = run if created else repo.get_analysis_run(run_id)
    if active_run is None:
        raise RuntimeError(f"Unable to persist analysis drain run {run_id}")
    if active_run.expected_tasks:
        messages_sent = _enqueue(repo, sqs, active_run)
        status = "waiting"
    else:
        messages_sent = 0
        status = "cooldown" if cooldown else "idle"
        repo.complete_analysis_run(run_id, now.isoformat(), status)

    result = {
        "run_id": run_id,
        "status": status,
        "backlog_eligible": len(eligible),
        "oldest_queue_age_hours": round(oldest_age_seconds / 3_600, 2),
        "daily_target_calls": adaptive_target,
        "daily_calls_committed": calls_committed,
        "discovery_state": usage.discovery_state if usage else "unknown",
        "backlog_call_ceiling": backlog_ceiling,
        "selected_tasks": active_run.expected_tasks,
        "adaptive_batch_size": batch_size,
        "messages_sent": messages_sent,
        "throttling_cooldown": cooldown,
        "budget_date": budget_date,
        "freshness": freshness_result,
    }
    logger.info(json.dumps({"event": "analysis_backlog_drain_dispatched", **result}))
    _log_metrics(result)
    return result


def _enqueue(repo, sqs, run: AnalysisRun) -> int:
    if run.status == "completed":
        return 0
    tasks = repo.get_analysis_tasks(run.customer_id, run.selected_task_ids)
    messages = [
        {
            "schema_version": ANALYSIS_QUEUE_SCHEMA_VERSION,
            "kind": "deep_analysis_task",
            "run_id": run.run_id,
            "customer_id": run.customer_id,
            "task_id": task_id,
        }
        for task_id in run.selected_task_ids
        if (task := tasks.get(task_id)) is not None
        and task.status == "dispatched"
        and task.selected_run_id == run.run_id
    ]
    _send_batches(sqs, os.environ["DEEP_ANALYSIS_QUEUE_URL"], messages)
    ready = len(tasks) == run.expected_tasks and all(task.terminal for task in tasks.values())
    _send_finalizer_check(
        sqs,
        os.environ["ANALYSIS_FINALIZER_QUEUE_URL"],
        run.run_id,
        delay_seconds=0 if ready else _seconds_until(run.deadline_at),
    )
    return len(messages)


def _run_id(event: object, now: datetime) -> str:
    if isinstance(event, dict):
        raw = event.get("run_id") or event.get("time")
        if isinstance(raw, str) and raw:
            if "<aws.scheduler." in raw:
                raise ValueError(
                    f"EventBridge Scheduler context attribute was not resolved: {raw}"
                )
            return f"backlog-drain-{raw}"
    return f"backlog-drain-{now.strftime('%Y-%m-%dT%H-%M-%S-%fZ')}"


def _age_seconds(value: str, now: datetime) -> int:
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return max(0, round((now - observed).total_seconds()))


def _within_remaining_budget(
    tasks: list[AnalysisTask],
    usage: AnalysisBudgetUsage | None,
    limits: AnalysisBudgetLimits,
    *, maximum_calls: int | None = None,
) -> list[AnalysisTask]:
    maximum_calls = limits.maximum_calls if maximum_calls is None else maximum_calls
    calls = usage.calls_committed if usage else 0
    input_tokens = usage.input_tokens_committed if usage else 0
    output_tokens = usage.output_tokens_committed if usage else 0
    cost = usage.cost_microusd_committed if usage else 0
    selected: list[AnalysisTask] = []
    for task in tasks:
        estimated_input = max(500, task.estimated_input_tokens)
        estimated_output = limits.estimated_output_tokens_per_call
        estimated_cost = limits.estimated_cost_microusd(
            estimated_input, estimated_output
        )
        if (
            calls + 1 > maximum_calls
            or input_tokens + estimated_input > limits.maximum_input_tokens
            or output_tokens + estimated_output > limits.maximum_output_tokens
            or cost + estimated_cost > limits.maximum_cost_microusd
        ):
            continue
        selected.append(task)
        calls += 1
        input_tokens += estimated_input
        output_tokens += estimated_output
        cost += estimated_cost
    return selected


def _log_metrics(result: dict) -> None:
    # EMF must be the whole log message; the standard logger prefixes records.
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": round(datetime.now(UTC).timestamp() * 1_000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "CareerAgent/Analysis",
                            "Dimensions": [["Environment"]],
                            "Metrics": [
                                {"Name": "BacklogEligible", "Unit": "Count"},
                                {"Name": "BacklogOldestAgeHours", "Unit": "Count"},
                                {"Name": "DrainSelected", "Unit": "Count"},
                                {"Name": "DailyCallsCommitted", "Unit": "Count"},
                                {"Name": "BudgetOrCooldownDeferred", "Unit": "Count"},
                            ],
                        }
                    ],
                },
                "Environment": os.getenv("ENVIRONMENT", "prod"),
                "BacklogEligible": result["backlog_eligible"],
                "BacklogOldestAgeHours": result["oldest_queue_age_hours"],
                "DrainSelected": result["selected_tasks"],
                "DailyCallsCommitted": result["daily_calls_committed"],
                "BudgetOrCooldownDeferred": int(
                    result["backlog_eligible"] > 0 and result["selected_tasks"] == 0
                ),
            }
        )
    )


def _sqs_client():
    import boto3

    return boto3.client("sqs")
