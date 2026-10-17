from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from .models import AnalysisBudgetUsage, AnalysisRun, AnalysisTask


MICRO_USD_PER_USD = 1_000_000


@dataclass(frozen=True, slots=True)
class AnalysisBudgetLimits:
    """Per-customer model budget enforced immediately before each fresh call."""

    maximum_calls: int = 75
    maximum_input_tokens: int = 225_000
    maximum_output_tokens: int = 90_000
    maximum_cost_microusd: int = 1_750_000
    estimated_output_tokens_per_call: int = 1_000
    input_microusd_per_million_tokens: int = 3_000_000
    output_microusd_per_million_tokens: int = 15_000_000
    timezone: str = "Europe/London"

    @classmethod
    def from_environment(cls) -> "AnalysisBudgetLimits":
        return cls(
            maximum_calls=_positive_int("ANALYSIS_DAILY_CALL_LIMIT", 75),
            maximum_input_tokens=_positive_int(
                "ANALYSIS_DAILY_INPUT_TOKEN_LIMIT", 225_000
            ),
            maximum_output_tokens=_positive_int(
                "ANALYSIS_DAILY_OUTPUT_TOKEN_LIMIT", 90_000
            ),
            maximum_cost_microusd=_positive_int(
                "ANALYSIS_DAILY_COST_LIMIT_MICRO_USD", 1_750_000
            ),
            estimated_output_tokens_per_call=_positive_int(
                "ANALYSIS_ESTIMATED_OUTPUT_TOKENS", 1_000
            ),
            input_microusd_per_million_tokens=_positive_int(
                "ANALYSIS_INPUT_MICRO_USD_PER_MILLION_TOKENS", 3_000_000
            ),
            output_microusd_per_million_tokens=_positive_int(
                "ANALYSIS_OUTPUT_MICRO_USD_PER_MILLION_TOKENS", 15_000_000
            ),
            timezone=os.getenv("ANALYSIS_BUDGET_TIMEZONE", "Europe/London"),
        )

    def budget_date(self, observed_at: datetime | None = None) -> str:
        observed_at = observed_at or datetime.now(UTC)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        return observed_at.astimezone(ZoneInfo(self.timezone)).date().isoformat()

    def estimated_cost_microusd(self, input_tokens: int, output_tokens: int) -> int:
        input_cost = max(0, input_tokens) * self.input_microusd_per_million_tokens
        output_cost = max(0, output_tokens) * self.output_microusd_per_million_tokens
        return round((input_cost + output_cost) / MICRO_USD_PER_USD)

    def remaining_calls(self, usage: AnalysisBudgetUsage | None) -> int:
        return max(0, self.maximum_calls - (usage.calls_committed if usage else 0))

    def backlog_call_ceiling(self, usage: AnalysisBudgetUsage | None, discovery_slots: int = 25) -> int:
        # An absent plan is conservative during the rollout or after a missed schedule.
        if usage and usage.discovery_state in {"none", "released"}:
            return self.maximum_calls
        return max(0, self.maximum_calls - discovery_slots)


@dataclass(frozen=True, slots=True)
class BacklogDrainPolicy:
    minimum_daily_calls: int = 50
    maximum_daily_calls: int = 75
    maximum_batch_size: int = 25
    oldest_job_slo_hours: int = 72
    throttling_cooldown_hours: int = 6

    @classmethod
    def from_environment(cls) -> "BacklogDrainPolicy":
        minimum = _positive_int("BACKLOG_DRAIN_MINIMUM_DAILY_CALLS", 50)
        maximum = _positive_int("BACKLOG_DRAIN_MAXIMUM_DAILY_CALLS", 75)
        if minimum > maximum:
            raise ValueError(
                "BACKLOG_DRAIN_MINIMUM_DAILY_CALLS cannot exceed "
                "BACKLOG_DRAIN_MAXIMUM_DAILY_CALLS"
            )
        return cls(
            minimum_daily_calls=minimum,
            maximum_daily_calls=maximum,
            maximum_batch_size=_positive_int("BACKLOG_DRAIN_MAXIMUM_BATCH_SIZE", 25),
            oldest_job_slo_hours=_positive_int("BACKLOG_DRAIN_OLDEST_JOB_SLO_HOURS", 72),
            throttling_cooldown_hours=_positive_int(
                "BACKLOG_DRAIN_THROTTLING_COOLDOWN_HOURS", 6
            ),
        )

    def daily_target(self, eligible_tasks: list[AnalysisTask], now: datetime) -> int:
        if not eligible_tasks:
            return 0
        oldest_hours = max(_age_hours(task.first_queued_at, now) for task in eligible_tasks)
        age_pressure = min(1.0, oldest_hours / self.oldest_job_slo_hours)
        # Four maximum-size batches represent enough queued work to justify the
        # upper bound even before the oldest item breaches its service target.
        backlog_pressure = min(
            1.0,
            max(0, len(eligible_tasks) - self.minimum_daily_calls)
            / max(1, self.maximum_batch_size * 4 - self.minimum_daily_calls),
        )
        pressure = max(age_pressure, backlog_pressure)
        extra = round(
            (self.maximum_daily_calls - self.minimum_daily_calls) * pressure
        )
        return min(len(eligible_tasks), self.minimum_daily_calls + extra)

    def throttling_cooldown_active(
        self, tasks: Iterable[AnalysisTask], now: datetime
    ) -> bool:
        markers = ("throttl", "too many requests", "service unavailable", "model timeout")
        for task in tasks:
            if not task.last_error or not task.last_attempt_at:
                continue
            if not any(marker in task.last_error.lower() for marker in markers):
                continue
            if _age_hours(task.last_attempt_at, now) < self.throttling_cooldown_hours:
                return True
        return False

    def adaptive_batch_size(
        self, recent_runs: list[AnalysisRun], *, deadline_seconds: int
    ) -> int:
        """Reduce a batch when observed throughput approaches the run deadline."""
        completed = next(
            (
                run
                for run in recent_runs
                if run.completed_at and run.expected_tasks > 0
            ),
            None,
        )
        if completed is None:
            return self.maximum_batch_size
        if completed.tasks_timed_out > 0:
            return max(1, self.maximum_batch_size // 2)
        duration = _duration_seconds(completed.started_at, completed.completed_at)
        finished = max(1, completed.tasks_completed)
        if duration <= 0:
            return self.maximum_batch_size
        observed_capacity = int(finished / duration * deadline_seconds * 0.8)
        return max(1, min(self.maximum_batch_size, observed_capacity))

    def select(
        self,
        tasks: list[AnalysisTask],
        *,
        count: int,
        seed: str,
        now: datetime,
    ) -> list[AnalysisTask]:
        """Preserve 70/20/10 priority, diversity, and audit lanes while ageing work."""
        count = min(max(0, count), len(tasks))
        if count == 0:
            return []
        ranked = sorted(
            tasks,
            key=lambda task: (
                -_effective_priority(task, now),
                task.first_queued_at,
                task.task_id,
            ),
        )
        priority_count = max(1, count * 70 // 100)
        diversity_count = count * 20 // 100
        selected = ranked[:priority_count]
        remaining = ranked[priority_count:]

        for _ in range(min(diversity_count, len(remaining))):
            source_keys = {task.source_key for task in selected}
            companies = {task.company.casefold() for task in selected}
            role_families = {task.role_family for task in selected}
            chosen = max(
                remaining,
                key=lambda task: (
                    sum(
                        (
                            task.source_key not in source_keys,
                            task.company.casefold() not in companies,
                            task.role_family not in role_families,
                        )
                    ),
                    _effective_priority(task, now),
                    task.task_id,
                ),
            )
            selected.append(chosen)
            remaining.remove(chosen)

        audit_count = min(count - len(selected), len(remaining))
        audited = sorted(remaining, key=lambda task: _stable_order(f"{seed}:{task.task_id}"))[
            :audit_count
        ]
        selected.extend(audited)
        audited_ids = {task.task_id for task in audited}
        remaining = [task for task in remaining if task.task_id not in audited_ids]
        selected.extend(remaining[: count - len(selected)])
        return selected


def _effective_priority(task: AnalysisTask, now: datetime) -> float:
    components = task.priority_components
    if not components:
        return task.priority_score
    age = min(100.0, _age_hours(task.first_queued_at, now) / (14 * 24) * 100)
    return (
        float(components.get("preliminary_fit", task.priority_score)) * 0.65
        + float(components.get("vacancy_urgency", 40.0)) * 0.15
        + age * 0.10
        + float(components.get("novelty", 90.0)) * 0.10
    )


def _age_hours(value: str, now: datetime) -> float:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return max(0.0, (now - timestamp).total_seconds() / 3_600)


def _duration_seconds(started_at: str, completed_at: str) -> float:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0.0, (end - start).total_seconds())


def _stable_order(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
