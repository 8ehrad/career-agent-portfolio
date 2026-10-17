from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import (
    AnalysisBudgetUsage,
    AnalysisRun,
    AnalysisTask,
    ApplicationPack,
    ApplicationTask,
    CoverageSample,
    DeepAnalysisRecord,
    Job,
    JobAnalysisHistory,
    JobStatus,
    ReedHydrationTask,
    IngestionRun,
    IngestionTask,
    RunReport,
    SalaryConfidence,
    SalaryEvidence,
    SourceHealth,
)


def _conditional_check_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code == "ConditionalCheckFailedException" or "ConditionalCheckFailed" in type(exc).__name__


class Repository(ABC):
    @abstractmethod
    def get_listing_details(self, source: str, source_ids: list[str]) -> dict[str, dict]: ...

    @abstractmethod
    def put_listing_details(self, record: dict) -> None: ...

    @abstractmethod
    def put_job(self, job: Job) -> None: ...

    @abstractmethod
    def get_job(self, job_id: str) -> Job | None: ...

    @abstractmethod
    def list_jobs(self, status: JobStatus | None = None) -> list[Job]: ...

    @abstractmethod
    def update_job_status(self, job_id: str, status: JobStatus, reason: str | None = None) -> Job: ...

    @abstractmethod
    def put_salary_evidence(self, key: str, evidence: SalaryEvidence) -> None: ...

    @abstractmethod
    def get_salary_evidence(self, key: str) -> SalaryEvidence | None: ...

    @abstractmethod
    def put_application_pack(self, pack: ApplicationPack) -> None: ...

    @abstractmethod
    def get_application_pack(self, job_id: str) -> ApplicationPack | None: ...

    @abstractmethod
    def get_application_task(self, customer_id: str, job_id: str) -> ApplicationTask | None: ...

    @abstractmethod
    def list_application_tasks(self, customer_id: str) -> list[ApplicationTask]: ...

    @abstractmethod
    def begin_application_task(self, task: ApplicationTask) -> tuple[ApplicationTask, bool]: ...

    @abstractmethod
    def claim_application_task(self, customer_id: str, job_id: str, task_id: str,
                               now: str, lease_until: str) -> ApplicationTask | None: ...

    @abstractmethod
    def mark_application_recovery_dispatch(self, customer_id: str, job_id: str,
                                           task_id: str, now: str, stale_before: str) -> bool: ...

    @abstractmethod
    def complete_application_task(self, customer_id: str, job_id: str, task_id: str,
                                  now: str) -> bool: ...

    @abstractmethod
    def record_application_task_error(self, customer_id: str, job_id: str, task_id: str,
                                      now: str, reason: str, *, retryable: bool,
                                      expected_status: str = "processing") -> bool: ...

    @abstractmethod
    def put_run(self, report: RunReport) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> RunReport | None: ...

    @abstractmethod
    def list_runs(self, limit: int = 30) -> list[RunReport]: ...

    @abstractmethod
    def get_source_health(self, source_key: str) -> SourceHealth | None: ...

    @abstractmethod
    def put_source_health(self, health: SourceHealth) -> None: ...

    @abstractmethod
    def list_source_health(self) -> list[SourceHealth]: ...

    @abstractmethod
    def put_coverage_sample(self, sample: CoverageSample) -> None: ...

    @abstractmethod
    def list_coverage_samples(self, limit: int = 500) -> list[CoverageSample]: ...

    @abstractmethod
    def get_analysis_histories(self, job_ids: list[str]) -> dict[str, JobAnalysisHistory]: ...

    @abstractmethod
    def put_analysis_histories(self, histories: list[JobAnalysisHistory]) -> None: ...

    @abstractmethod
    def get_deep_analysis_records(self, cache_keys: list[str]) -> dict[str, DeepAnalysisRecord]: ...

    @abstractmethod
    def put_deep_analysis_record(self, record: DeepAnalysisRecord) -> None: ...

    @abstractmethod
    def put_analysis_tasks(self, tasks: list[AnalysisTask]) -> None: ...

    @abstractmethod
    def queue_analysis_task(self, task: AnalysisTask) -> bool: ...

    @abstractmethod
    def upsert_reed_hydration_tasks(self, tasks: list[ReedHydrationTask]) -> list[str]: ...

    @abstractmethod
    def get_reed_hydration_task(
        self, customer_id: str, source_id: str
    ) -> ReedHydrationTask | None: ...

    @abstractmethod
    def list_reed_hydration_tasks(self, customer_id: str) -> list[ReedHydrationTask]: ...

    @abstractmethod
    def mark_reed_hydration_dispatched(
        self, customer_id: str, source_ids: list[str], dispatched_at: str
    ) -> list[str]: ...

    @abstractmethod
    def start_reed_hydration_attempt(self, customer_id: str, source_id: str) -> bool: ...

    @abstractmethod
    def defer_reed_hydration_task(
        self,
        customer_id: str,
        source_id: str,
        error: str,
        next_attempt_at: str,
        *,
        expected_fingerprint: str,
        failed: bool = False,
    ) -> bool: ...

    @abstractmethod
    def complete_reed_hydration_task(
        self, customer_id: str, source_id: str, completed_at: str,
        *, expected_fingerprint: str,
    ) -> bool: ...

    @abstractmethod
    def mark_reed_hydration_ready_sent(
        self, customer_id: str, source_id: str, sent_at: str
    ) -> None: ...

    @abstractmethod
    def mark_reed_hydration_ready_processed(
        self,
        customer_id: str,
        source_id: str,
        processed_at: str,
        *,
        outcome: str,
        expected_fingerprint: str,
        analysis_task_id: str | None = None,
        gate_reasons: list[str] | None = None,
    ) -> None: ...

    @abstractmethod
    def reserve_reed_hydration_request(
        self, customer_id: str, budget_date: str, maximum_requests: int, reserved_at: str
    ) -> bool: ...

    @abstractmethod
    def get_analysis_task(self, customer_id: str, task_id: str) -> AnalysisTask | None: ...

    @abstractmethod
    def get_analysis_tasks(
        self, customer_id: str, task_ids: list[str]
    ) -> dict[str, AnalysisTask]: ...

    @abstractmethod
    def list_analysis_tasks(self, customer_id: str) -> list[AnalysisTask]: ...

    @abstractmethod
    def dispatch_analysis_tasks(
        self,
        customer_id: str,
        run_id: str,
        task_ids: list[str],
        dispatched_at: str,
        *,
        selection_reason: str,
    ) -> list[str]: ...

    @abstractmethod
    def defer_analysis_task(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        deferred_at: str,
        reason: str,
    ) -> None: ...

    @abstractmethod
    def expire_inactive_analysis_tasks(
        self,
        customer_id: str,
        active_task_ids: set[str],
        stale_before: str,
        completed_at: str,
    ) -> int: ...

    @abstractmethod
    def begin_analysis_run(self, run: AnalysisRun) -> bool: ...

    @abstractmethod
    def get_analysis_run(self, run_id: str) -> AnalysisRun | None: ...

    @abstractmethod
    def list_analysis_runs(
        self, limit: int = 30, *, run_type: str | None = None
    ) -> list[AnalysisRun]: ...

    @abstractmethod
    def get_analysis_budget_usage(
        self, customer_id: str, budget_date: str
    ) -> AnalysisBudgetUsage | None: ...

    @abstractmethod
    def set_analysis_discovery_state(
        self, customer_id: str, budget_date: str, state: str, observed_at: str,
        *, maximum_backlog_calls: int,
    ) -> bool: ...

    @abstractmethod
    def reserve_analysis_capacity(
        self,
        customer_id: str,
        budget_date: str,
        reserved_at: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int,
        maximum_calls: int,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        maximum_cost_microusd: int,
        run_type: str = "pipeline",
    ) -> bool: ...

    @abstractmethod
    def reconcile_analysis_capacity(
        self,
        customer_id: str,
        budget_date: str,
        reconciled_at: str,
        *,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_microusd: int,
        actual_calls: int,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_cost_microusd: int,
    ) -> None: ...

    @abstractmethod
    def start_analysis_task_attempt(
        self, customer_id: str, task_id: str, run_id: str
    ) -> bool: ...

    @abstractmethod
    def record_analysis_task_error(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        error: str,
        *,
        model_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None: ...

    @abstractmethod
    def complete_analysis_task(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        completed_at: str,
        *,
        result_source: str,
        model_calls: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None: ...

    @abstractmethod
    def time_out_pending_analysis_tasks(self, run_id: str, completed_at: str) -> int: ...

    @abstractmethod
    def complete_analysis_run(
        self,
        run_id: str,
        completed_at: str,
        pipeline_status: str,
        *,
        metrics: dict[str, int] | None = None,
    ) -> None: ...

    @abstractmethod
    def analysis_history_initialized(self) -> bool: ...

    @abstractmethod
    def mark_analysis_history_initialized(self, initialized_at: str) -> None: ...

    @abstractmethod
    def claim_once(self, key: str) -> bool: ...

    @abstractmethod
    def release_claim(self, key: str) -> None: ...

    @abstractmethod
    def begin_ingestion_run(self, run: IngestionRun, tasks: list[IngestionTask]) -> bool: ...

    @abstractmethod
    def get_ingestion_run(self, run_id: str) -> IngestionRun | None: ...

    @abstractmethod
    def list_ingestion_runs(self, limit: int = 10) -> list[IngestionRun]: ...

    @abstractmethod
    def get_ingestion_task(self, run_id: str, source_key: str) -> IngestionTask | None: ...

    @abstractmethod
    def list_ingestion_tasks(self, run_id: str) -> list[IngestionTask]: ...

    @abstractmethod
    def start_ingestion_task_attempt(self, run_id: str, source_key: str) -> bool: ...

    @abstractmethod
    def record_ingestion_task_error(self, run_id: str, source_key: str, error: str) -> None: ...

    @abstractmethod
    def complete_ingestion_task(
        self, run_id: str, source_key: str, artifact_key: str, diagnostic: dict[str, Any]
    ) -> None: ...

    @abstractmethod
    def time_out_pending_ingestion_tasks(self, run_id: str, completed_at: str) -> int: ...

    @abstractmethod
    def complete_ingestion_run(
        self, run_id: str, completed_at: str, pipeline_status: str
    ) -> None: ...


class MemoryRepository(Repository):
    def __init__(self) -> None:
        self.listing_details: dict[tuple[str, str], dict] = {}
        self.jobs: dict[str, Job] = {}
        self.salaries: dict[str, SalaryEvidence] = {}
        self.packs: dict[str, ApplicationPack] = {}
        self.application_tasks: dict[tuple[str, str], ApplicationTask] = {}
        self.runs: dict[str, RunReport] = {}
        self.source_health: dict[str, SourceHealth] = {}
        self.coverage_samples: dict[str, CoverageSample] = {}
        self.analysis_histories: dict[str, JobAnalysisHistory] = {}
        self.deep_analysis_records: dict[str, DeepAnalysisRecord] = {}
        self.analysis_tasks: dict[tuple[str, str], AnalysisTask] = {}
        self.reed_hydration_tasks: dict[tuple[str, str], ReedHydrationTask] = {}
        self.reed_hydration_budgets: dict[tuple[str, str], int] = {}
        self.analysis_runs: dict[str, AnalysisRun] = {}
        self.analysis_budgets: dict[tuple[str, str], AnalysisBudgetUsage] = {}
        self.history_initialized = False
        self.claims: set[str] = set()
        self.ingestion_runs: dict[str, IngestionRun] = {}
        self.ingestion_tasks: dict[tuple[str, str], IngestionTask] = {}

    def get_listing_details(self, source: str, source_ids: list[str]) -> dict[str, dict]:
        return {i: json.loads(json.dumps(self.listing_details[(source, i)]))
                for i in dict.fromkeys(source_ids) if (source, i) in self.listing_details}

    def put_listing_details(self, record: dict) -> None:
        self.listing_details[(record["source"], record["source_id"])] = json.loads(json.dumps(record))

    def put_job(self, job: Job) -> None:
        self.jobs[job.id] = Job.from_dict(job.to_dict())

    def get_job(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        return Job.from_dict(job.to_dict()) if job else None

    def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        values = [job for job in self.jobs.values() if status is None or job.status == status]
        return [Job.from_dict(job.to_dict()) for job in sorted(values, key=lambda item: item.score.overall, reverse=True)]

    def update_job_status(self, job_id: str, status: JobStatus, reason: str | None = None) -> Job:
        job = self.jobs[job_id]
        job.status = status
        if status == JobStatus.DISMISSED:
            job.dismissal_reason = reason
        self.put_job(job)
        return Job.from_dict(job.to_dict())

    def put_salary_evidence(self, key: str, evidence: SalaryEvidence) -> None:
        self.salaries[key] = SalaryEvidence(**evidence.__dict__) if hasattr(evidence, "__dict__") else SalaryEvidence(
            minimum_gbp=evidence.minimum_gbp,
            maximum_gbp=evidence.maximum_gbp,
            currency=evidence.currency,
            period=evidence.period,
            confidence=evidence.confidence,
            source_url=evidence.source_url,
            source_label=evidence.source_label,
            observed_at=evidence.observed_at,
            inferred=evidence.inferred,
        )

    def get_salary_evidence(self, key: str) -> SalaryEvidence | None:
        return self.salaries.get(key)

    def put_application_pack(self, pack: ApplicationPack) -> None:
        self.packs[pack.job_id] = pack

    def get_application_pack(self, job_id: str) -> ApplicationPack | None:
        return self.packs.get(job_id)

    def get_application_task(self, customer_id: str, job_id: str) -> ApplicationTask | None:
        value = self.application_tasks.get((customer_id, job_id))
        return ApplicationTask.from_dict(value.to_dict()) if value else None

    def list_application_tasks(self, customer_id: str) -> list[ApplicationTask]:
        return [ApplicationTask.from_dict(task.to_dict()) for (customer, _), task in
                self.application_tasks.items() if customer == customer_id]

    def begin_application_task(self, task: ApplicationTask) -> tuple[ApplicationTask, bool]:
        key = (task.customer_id, task.job_id)
        current = self.application_tasks.get(key)
        if current and current.status in {"queued", "processing"}:
            if current.identity != task.identity:
                raise ValueError("Another application preparation is in progress for this job")
            return ApplicationTask.from_dict(current.to_dict()), False
        self.application_tasks[key] = ApplicationTask.from_dict(task.to_dict())
        return ApplicationTask.from_dict(task.to_dict()), True

    def claim_application_task(self, customer_id: str, job_id: str, task_id: str,
                               now: str, lease_until: str) -> ApplicationTask | None:
        task = self.application_tasks.get((customer_id, job_id))
        if not task or task.task_id != task_id or not (task.status == "queued" or
                task.status == "processing" and task.lease_expires_at and task.lease_expires_at <= now):
            return None
        task.status = "processing"
        task.attempts += 1
        task.updated_at = now
        task.lease_expires_at = lease_until
        return ApplicationTask.from_dict(task.to_dict())

    def mark_application_recovery_dispatch(self, customer_id: str, job_id: str,
                                           task_id: str, now: str, stale_before: str) -> bool:
        task = self.application_tasks.get((customer_id, job_id))
        if not task or task.task_id != task_id or task.status not in {"queued", "processing"} or \
                task.updated_at > stale_before or task.recovery_dispatches >= 4:
            return False
        task.recovery_dispatches += 1
        task.updated_at = now
        return True

    def complete_application_task(self, customer_id: str, job_id: str, task_id: str, now: str) -> bool:
        task = self.application_tasks.get((customer_id, job_id))
        if not task or task.task_id != task_id or task.status != "processing":
            return False
        task.status = "completed"
        task.updated_at = task.completed_at = now
        task.lease_expires_at = None
        return True

    def record_application_task_error(self, customer_id: str, job_id: str, task_id: str,
                                      now: str, reason: str, *, retryable: bool,
                                      expected_status: str = "processing") -> bool:
        task = self.application_tasks.get((customer_id, job_id))
        if not task or task.task_id != task_id or task.status != expected_status:
            return False
        task.status = "queued" if retryable else "failed"
        task.updated_at = now
        task.last_error = reason[:500]
        task.lease_expires_at = None
        task.completed_at = None if retryable else now
        return True

    def put_run(self, report: RunReport) -> None:
        self.runs[report.run_id] = report

    def get_run(self, run_id: str) -> RunReport | None:
        return self.runs.get(run_id)

    def list_runs(self, limit: int = 30) -> list[RunReport]:
        values = sorted(self.runs.values(), key=lambda item: item.started_at, reverse=True)
        return values[:limit]

    def get_source_health(self, source_key: str) -> SourceHealth | None:
        value = self.source_health.get(source_key)
        return SourceHealth.from_dict(value.to_dict()) if value else None

    def put_source_health(self, health: SourceHealth) -> None:
        self.source_health[health.source_key] = SourceHealth.from_dict(health.to_dict())

    def list_source_health(self) -> list[SourceHealth]:
        return [
            SourceHealth.from_dict(item.to_dict())
            for item in sorted(self.source_health.values(), key=lambda value: value.source_key)
        ]

    def put_coverage_sample(self, sample: CoverageSample) -> None:
        self.coverage_samples[sample.sample_id] = CoverageSample.from_dict(sample.to_dict())

    def list_coverage_samples(self, limit: int = 500) -> list[CoverageSample]:
        values = sorted(
            self.coverage_samples.values(), key=lambda item: item.observed_at, reverse=True
        )
        return [CoverageSample.from_dict(item.to_dict()) for item in values[:limit]]

    def get_analysis_histories(self, job_ids: list[str]) -> dict[str, JobAnalysisHistory]:
        return {
            job_id: JobAnalysisHistory.from_dict(self.analysis_histories[job_id].to_dict())
            for job_id in job_ids
            if job_id in self.analysis_histories
        }

    def put_analysis_histories(self, histories: list[JobAnalysisHistory]) -> None:
        for history in histories:
            self.analysis_histories[history.job_id] = JobAnalysisHistory.from_dict(history.to_dict())

    def get_deep_analysis_records(self, cache_keys: list[str]) -> dict[str, DeepAnalysisRecord]:
        return {
            key: DeepAnalysisRecord.from_dict(self.deep_analysis_records[key].to_dict())
            for key in cache_keys
            if key in self.deep_analysis_records
        }

    def put_deep_analysis_record(self, record: DeepAnalysisRecord) -> None:
        self.deep_analysis_records[record.cache_key] = DeepAnalysisRecord.from_dict(record.to_dict())

    def put_analysis_tasks(self, tasks: list[AnalysisTask]) -> None:
        for task in tasks:
            self.analysis_tasks[(task.customer_id, task.task_id)] = AnalysisTask.from_dict(
                task.to_dict()
            )

    def queue_analysis_task(self, task: AnalysisTask) -> bool:
        key = (task.customer_id, task.task_id)
        existing = self.analysis_tasks.get(key)
        if existing and existing.status in {"dispatched", "processing"}:
            return False
        if existing:
            task.first_queued_at = existing.first_queued_at
            task.attempts = existing.attempts
        task.status = "queued"
        task.selected_run_id = None
        task.dispatched_at = None
        task.completed_at = None
        self.analysis_tasks[key] = AnalysisTask.from_dict(task.to_dict())
        return True

    def upsert_reed_hydration_tasks(self, tasks: list[ReedHydrationTask]) -> list[str]:
        queued: list[str] = []
        for task in tasks:
            key = (task.customer_id, task.source_id)
            existing = self.reed_hydration_tasks.get(key)
            if existing and existing.summary_fingerprint == task.summary_fingerprint:
                existing.last_seen_at = task.last_seen_at
                existing.summary = dict(task.summary)
                if existing.status in {"queued", "dispatched"}:
                    queued.append(task.source_id)
                    continue
                if existing.status != "completed":
                    continue
            if existing:
                task.first_seen_at = existing.first_seen_at
            self.reed_hydration_tasks[key] = ReedHydrationTask.from_dict(task.to_dict())
            queued.append(task.source_id)
        return queued

    def get_reed_hydration_task(
        self, customer_id: str, source_id: str
    ) -> ReedHydrationTask | None:
        task = self.reed_hydration_tasks.get((customer_id, source_id))
        return ReedHydrationTask.from_dict(task.to_dict()) if task else None

    def list_reed_hydration_tasks(self, customer_id: str) -> list[ReedHydrationTask]:
        tasks = [
            task for (task_customer, _), task in self.reed_hydration_tasks.items()
            if task_customer == customer_id
        ]
        return [
            ReedHydrationTask.from_dict(task.to_dict())
            for task in sorted(tasks, key=lambda item: item.first_seen_at)
        ]

    def mark_reed_hydration_dispatched(
        self, customer_id: str, source_ids: list[str], dispatched_at: str
    ) -> list[str]:
        dispatched: list[str] = []
        for source_id in source_ids:
            task = self.reed_hydration_tasks.get((customer_id, source_id))
            if task is None or task.status not in {"queued", "dispatched", "processing"}:
                continue
            if (
                task.status == "processing"
                and (
                    not task.lease_expires_at
                    or datetime.fromisoformat(task.lease_expires_at) >= datetime.now(UTC)
                )
            ):
                continue
            task.status = "dispatched"
            task.last_dispatched_at = dispatched_at
            dispatched.append(source_id)
        return dispatched

    def start_reed_hydration_attempt(self, customer_id: str, source_id: str) -> bool:
        task = self.reed_hydration_tasks[(customer_id, source_id)]
        now = datetime.now(UTC)
        lease_expired = bool(
            task.status == "processing"
            and task.lease_expires_at
            and datetime.fromisoformat(task.lease_expires_at) < now
        )
        if task.status != "dispatched" and not lease_expired:
            return False
        if task.next_attempt_at and datetime.fromisoformat(task.next_attempt_at) > now:
            return False
        task.status = "processing"
        task.attempts += 1
        task.last_attempt_at = now.isoformat()
        task.lease_expires_at = (now + timedelta(minutes=2)).isoformat()
        return True

    def defer_reed_hydration_task(
        self,
        customer_id: str,
        source_id: str,
        error: str,
        next_attempt_at: str,
        *,
        expected_fingerprint: str,
        failed: bool = False,
    ) -> bool:
        task = self.reed_hydration_tasks[(customer_id, source_id)]
        if task.status != "processing" or task.summary_fingerprint != expected_fingerprint:
            return False
        task.status = "failed" if failed else "queued"
        task.last_error = error[:1000]
        task.next_attempt_at = next_attempt_at
        task.lease_expires_at = None
        return True

    def complete_reed_hydration_task(
        self, customer_id: str, source_id: str, completed_at: str,
        *, expected_fingerprint: str,
    ) -> bool:
        task = self.reed_hydration_tasks[(customer_id, source_id)]
        if task.status != "processing" or task.summary_fingerprint != expected_fingerprint:
            return False
        task.status = "completed"
        task.completed_at = completed_at
        task.last_error = None
        task.next_attempt_at = None
        task.lease_expires_at = None
        task.ready_status = "pending"
        return True

    def mark_reed_hydration_ready_sent(
        self, customer_id: str, source_id: str, sent_at: str
    ) -> None:
        task = self.reed_hydration_tasks[(customer_id, source_id)]
        if task.status == "completed" and task.ready_status == "pending":
            task.ready_sent_at = sent_at

    def mark_reed_hydration_ready_processed(
        self,
        customer_id: str,
        source_id: str,
        processed_at: str,
        *,
        outcome: str,
        expected_fingerprint: str,
        analysis_task_id: str | None = None,
        gate_reasons: list[str] | None = None,
    ) -> None:
        task = self.reed_hydration_tasks[(customer_id, source_id)]
        if (
            task.status != "completed"
            or task.ready_status != "pending"
            or task.summary_fingerprint != expected_fingerprint
        ):
            return
        task.ready_status = outcome
        task.ready_processed_at = processed_at
        task.analysis_task_id = analysis_task_id
        task.gate_reasons = list(gate_reasons or [])

    def reserve_reed_hydration_request(
        self, customer_id: str, budget_date: str, maximum_requests: int, reserved_at: str
    ) -> bool:
        del reserved_at
        key = (customer_id, budget_date)
        used = self.reed_hydration_budgets.get(key, 0)
        if used >= maximum_requests:
            return False
        self.reed_hydration_budgets[key] = used + 1
        return True

    def get_analysis_task(self, customer_id: str, task_id: str) -> AnalysisTask | None:
        value = self.analysis_tasks.get((customer_id, task_id))
        return AnalysisTask.from_dict(value.to_dict()) if value else None

    def get_analysis_tasks(
        self, customer_id: str, task_ids: list[str]
    ) -> dict[str, AnalysisTask]:
        return {
            task_id: AnalysisTask.from_dict(self.analysis_tasks[(customer_id, task_id)].to_dict())
            for task_id in dict.fromkeys(task_ids)
            if (customer_id, task_id) in self.analysis_tasks
        }

    def list_analysis_tasks(self, customer_id: str) -> list[AnalysisTask]:
        values = [
            task for (task_customer_id, _), task in self.analysis_tasks.items()
            if task_customer_id == customer_id
        ]
        return [
            AnalysisTask.from_dict(task.to_dict())
            for task in sorted(values, key=lambda item: (-item.priority_score, item.first_queued_at))
        ]

    def dispatch_analysis_tasks(
        self,
        customer_id: str,
        run_id: str,
        task_ids: list[str],
        dispatched_at: str,
        *,
        selection_reason: str,
    ) -> list[str]:
        dispatched: list[str] = []
        for task_id in task_ids:
            task = self.analysis_tasks.get((customer_id, task_id))
            if task is None:
                continue
            if task.selected_run_id == run_id and task.status in {
                "dispatched",
                "processing",
                "completed",
                "timed_out",
            }:
                dispatched.append(task_id)
                continue
            if task.status not in {"queued", "timed_out"}:
                continue
            task.status = "dispatched"
            task.selected_run_id = run_id
            task.selection_reason = selection_reason
            task.dispatched_at = dispatched_at
            task.completed_at = None
            task.lease_expires_at = None
            dispatched.append(task_id)
        return dispatched

    def defer_analysis_task(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        deferred_at: str,
        reason: str,
    ) -> None:
        task = self.analysis_tasks[(customer_id, task_id)]
        if task.status == "processing" and task.selected_run_id == run_id:
            task.status = "timed_out"
            task.completed_at = deferred_at
            task.last_error = reason[:1000]
            task.lease_expires_at = None

    def expire_inactive_analysis_tasks(
        self,
        customer_id: str,
        active_task_ids: set[str],
        stale_before: str,
        completed_at: str,
    ) -> int:
        changed = 0
        for (task_customer_id, task_id), task in self.analysis_tasks.items():
            if (
                task_customer_id == customer_id
                and task_id not in active_task_ids
                and task.status in {"queued", "timed_out"}
                and (
                    task.last_seen_at <= stale_before
                    or bool(task.closes_at and task.closes_at <= completed_at)
                )
            ):
                task.status = "expired"
                task.completed_at = completed_at
                changed += 1
        return changed

    def begin_analysis_run(self, run: AnalysisRun) -> bool:
        if run.run_id in self.analysis_runs:
            return False
        self.analysis_runs[run.run_id] = AnalysisRun.from_dict(run.to_dict())
        return True

    def get_analysis_run(self, run_id: str) -> AnalysisRun | None:
        value = self.analysis_runs.get(run_id)
        return AnalysisRun.from_dict(value.to_dict()) if value else None

    def list_analysis_runs(
        self, limit: int = 30, *, run_type: str | None = None
    ) -> list[AnalysisRun]:
        values = sorted(
            self.analysis_runs.values(), key=lambda item: item.started_at, reverse=True
        )
        if run_type:
            values = [run for run in values if run.run_type == run_type]
        return [AnalysisRun.from_dict(run.to_dict()) for run in values[: max(1, limit)]]

    def get_analysis_budget_usage(
        self, customer_id: str, budget_date: str
    ) -> AnalysisBudgetUsage | None:
        usage = self.analysis_budgets.get((customer_id, budget_date))
        return AnalysisBudgetUsage.from_dict(usage.to_dict()) if usage else None

    def set_analysis_discovery_state(
        self, customer_id: str, budget_date: str, state: str, observed_at: str,
        *, maximum_backlog_calls: int,
    ) -> bool:
        if state not in {"reserved", "none", "released"}:
            raise ValueError("Invalid discovery state")
        key = (customer_id, budget_date)
        usage = self.analysis_budgets.setdefault(
            key, AnalysisBudgetUsage(customer_id=customer_id, budget_date=budget_date)
        )
        if state == "reserved" and usage.calls_committed > maximum_backlog_calls:
            return False
        if state == "released" and usage.discovery_state != "reserved":
            return False
        if state == "none" and usage.discovery_state not in {None, "none"}:
            return False
        if state == "reserved" and usage.discovery_state not in {None, "none", "reserved"}:
            return False
        usage.discovery_state = state
        usage.updated_at = observed_at
        return True

    def reserve_analysis_capacity(
        self,
        customer_id: str,
        budget_date: str,
        reserved_at: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int,
        maximum_calls: int,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        maximum_cost_microusd: int,
        run_type: str = "pipeline",
    ) -> bool:
        key = (customer_id, budget_date)
        usage = self.analysis_budgets.setdefault(
            key, AnalysisBudgetUsage(customer_id=customer_id, budget_date=budget_date)
        )
        if (
            usage.calls_committed + 1 > (
                min(maximum_calls, max(0, maximum_calls - 25))
                if run_type in {"backlog_drain", "manual_discovery"} and usage.discovery_state not in {"none", "released"}
                else maximum_calls
            )
            or usage.input_tokens_committed + input_tokens > maximum_input_tokens
            or usage.output_tokens_committed + output_tokens > maximum_output_tokens
            or usage.cost_microusd_committed + cost_microusd > maximum_cost_microusd
        ):
            return False
        usage.calls_committed += 1
        usage.input_tokens_committed += max(0, input_tokens)
        usage.output_tokens_committed += max(0, output_tokens)
        usage.cost_microusd_committed += max(0, cost_microusd)
        usage.updated_at = reserved_at
        return True

    def reconcile_analysis_capacity(
        self,
        customer_id: str,
        budget_date: str,
        reconciled_at: str,
        *,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_microusd: int,
        actual_calls: int,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_cost_microusd: int,
    ) -> None:
        usage = self.analysis_budgets[(customer_id, budget_date)]
        usage.input_tokens_committed += actual_input_tokens - reserved_input_tokens
        usage.output_tokens_committed += actual_output_tokens - reserved_output_tokens
        usage.cost_microusd_committed += actual_cost_microusd - reserved_cost_microusd
        usage.calls_actual += max(0, actual_calls)
        usage.input_tokens_actual += max(0, actual_input_tokens)
        usage.output_tokens_actual += max(0, actual_output_tokens)
        usage.cost_microusd_actual += max(0, actual_cost_microusd)
        usage.updated_at = reconciled_at

    def start_analysis_task_attempt(
        self, customer_id: str, task_id: str, run_id: str
    ) -> bool:
        task = self.analysis_tasks[(customer_id, task_id)]
        now = datetime.now(UTC)
        lease_expired = bool(
            task.status == "processing"
            and task.lease_expires_at
            and datetime.fromisoformat(task.lease_expires_at) < now
        )
        if (
            task.status != "dispatched" and not lease_expired
        ) or task.selected_run_id != run_id:
            return False
        task.status = "processing"
        task.attempts += 1
        task.last_attempt_at = now.isoformat()
        task.lease_expires_at = (now + timedelta(minutes=4)).isoformat()
        return True

    def record_analysis_task_error(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        error: str,
        *,
        model_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        task = self.analysis_tasks[(customer_id, task_id)]
        if task.status == "processing" and task.selected_run_id == run_id:
            task.status = "dispatched"
            task.last_error = error[:1000]
            task.lease_expires_at = None
            task.model_calls += max(0, model_calls)
            task.input_tokens += max(0, input_tokens)
            task.output_tokens += max(0, output_tokens)

    def complete_analysis_task(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        completed_at: str,
        *,
        result_source: str,
        model_calls: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        task = self.analysis_tasks[(customer_id, task_id)]
        if task.status != "processing" or task.selected_run_id != run_id:
            return
        task.status = "completed"
        task.completed_at = completed_at
        task.last_error = None
        task.result_source = result_source
        task.model_calls += max(0, model_calls)
        task.input_tokens += max(0, input_tokens)
        task.output_tokens += max(0, output_tokens)
        task.lease_expires_at = None

    def time_out_pending_analysis_tasks(self, run_id: str, completed_at: str) -> int:
        changed = 0
        for task in self.analysis_tasks.values():
            if task.selected_run_id == run_id and task.status in {"dispatched", "processing"}:
                task.status = "timed_out"
                task.completed_at = completed_at
                task.last_error = task.last_error or "analysis_deadline_exceeded"
                task.lease_expires_at = None
                changed += 1
        return changed

    def complete_analysis_run(
        self,
        run_id: str,
        completed_at: str,
        pipeline_status: str,
        *,
        metrics: dict[str, int] | None = None,
    ) -> None:
        run = self.analysis_runs[run_id]
        run.status = "completed"
        run.completed_at = completed_at
        run.pipeline_status = pipeline_status
        for key, value in (metrics or {}).items():
            if hasattr(run, key):
                setattr(run, key, int(value))

    def analysis_history_initialized(self) -> bool:
        return self.history_initialized

    def mark_analysis_history_initialized(self, initialized_at: str) -> None:
        del initialized_at
        self.history_initialized = True

    def claim_once(self, key: str) -> bool:
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    def release_claim(self, key: str) -> None:
        self.claims.discard(key)

    def begin_ingestion_run(self, run: IngestionRun, tasks: list[IngestionTask]) -> bool:
        created = run.run_id not in self.ingestion_runs
        if created:
            self.ingestion_runs[run.run_id] = IngestionRun.from_dict(run.to_dict())
        for task in tasks:
            key = (task.run_id, task.source_key)
            self.ingestion_tasks.setdefault(key, IngestionTask.from_dict(task.to_dict()))
        return created

    def get_ingestion_run(self, run_id: str) -> IngestionRun | None:
        value = self.ingestion_runs.get(run_id)
        return IngestionRun.from_dict(value.to_dict()) if value else None

    def list_ingestion_runs(self, limit: int = 10) -> list[IngestionRun]:
        values = sorted(self.ingestion_runs.values(), key=lambda item: item.started_at, reverse=True)
        return [IngestionRun.from_dict(item.to_dict()) for item in values[:limit]]

    def get_ingestion_task(self, run_id: str, source_key: str) -> IngestionTask | None:
        value = self.ingestion_tasks.get((run_id, source_key))
        return IngestionTask.from_dict(value.to_dict()) if value else None

    def list_ingestion_tasks(self, run_id: str) -> list[IngestionTask]:
        return [
            IngestionTask.from_dict(task.to_dict())
            for (task_run_id, _), task in sorted(self.ingestion_tasks.items())
            if task_run_id == run_id
        ]

    def start_ingestion_task_attempt(self, run_id: str, source_key: str) -> bool:
        task = self.ingestion_tasks[(run_id, source_key)]
        if task.terminal:
            return False
        task.attempts += 1
        return True

    def record_ingestion_task_error(self, run_id: str, source_key: str, error: str) -> None:
        task = self.ingestion_tasks[(run_id, source_key)]
        if not task.terminal:
            task.last_error = error

    def complete_ingestion_task(
        self, run_id: str, source_key: str, artifact_key: str, diagnostic: dict[str, Any]
    ) -> None:
        task = self.ingestion_tasks[(run_id, source_key)]
        if task.terminal:
            return
        task.status = "completed"
        task.artifact_key = artifact_key
        task.diagnostic = dict(diagnostic)
        task.completed_at = str(diagnostic["completed_at"])
        task.last_error = None

    def time_out_pending_ingestion_tasks(self, run_id: str, completed_at: str) -> int:
        changed = 0
        for (task_run_id, _), task in self.ingestion_tasks.items():
            if task_run_id == run_id and not task.terminal:
                task.status = "timed_out"
                task.completed_at = completed_at
                changed += 1
        return changed

    def complete_ingestion_run(
        self, run_id: str, completed_at: str, pipeline_status: str
    ) -> None:
        run = self.ingestion_runs[run_id]
        run.status = "completed"
        run.completed_at = completed_at
        run.pipeline_status = pipeline_status


class DynamoRepository(Repository):
    def __init__(self, table_name: str, dynamodb_resource: Any | None = None) -> None:
        if dynamodb_resource is None:
            import boto3

            dynamodb_resource = boto3.resource("dynamodb")
        self.dynamodb = dynamodb_resource
        self.table = dynamodb_resource.Table(table_name)
        self.table_name = table_name

    @staticmethod
    def _encode(value: dict[str, Any]) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    def get_listing_details(self, source: str, source_ids: list[str]) -> dict[str, dict]:
        records = {}
        ids = list(dict.fromkeys(source_ids))
        for start in range(0, len(ids), 100):
            pending = {self.table_name: {"Keys": [
                {"pk": f"LISTING#{source}#{i}", "sk": "DETAILS"} for i in ids[start:start + 100]],
                "ConsistentRead": True}}
            for attempt in range(4):
                response = self.dynamodb.batch_get_item(RequestItems=pending)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    record = json.loads(item["payload"])
                    records[record["source_id"]] = record
                pending = response.get("UnprocessedKeys", {})
                if not pending:
                    break
                if attempt < 3:
                    time.sleep(0.05 * 2 ** attempt)
            if pending:
                raise RuntimeError("Listing detail reads remain unprocessed")
        return records

    def put_listing_details(self, record: dict) -> None:
        payload = self._encode(record)
        # Leave substantial headroom under DynamoDB's 400KB item limit. Never truncate originals.
        if len(payload.encode("utf-8")) > 300_000:
            raise ValueError("Listing detail record exceeds safe item size")
        self.table.put_item(Item={"pk": f"LISTING#{record['source']}#{record['source_id']}",
            "sk": "DETAILS", "entity_type": "listing_details", "payload": payload,
            "updated_at": record["fetched_at"]})

    def put_job(self, job: Job) -> None:
        self.table.put_item(
            Item={
                "pk": f"JOB#{job.id}",
                "sk": "META",
                "entity_type": "job",
                "gsi1pk": f"STATUS#{job.status.value}",
                "gsi1sk": f"{job.recommendation_date or job.first_seen_at}#{job.id}",
                "payload": self._encode(job.to_dict()),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

    def get_job(self, job_id: str) -> Job | None:
        response = self.table.get_item(Key={"pk": f"JOB#{job_id}", "sk": "META"}, ConsistentRead=True)
        item = response.get("Item")
        return Job.from_dict(json.loads(item["payload"])) if item else None

    def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        items: list[dict[str, Any]] = []
        if status is not None:
            operation = self.table.query
            parameters: dict[str, Any] = {
                "IndexName": "gsi1",
                "KeyConditionExpression": "gsi1pk = :status",
                "ExpressionAttributeValues": {":status": f"STATUS#{status.value}"},
                "ScanIndexForward": False,
            }
        else:
            operation = self.table.scan
            parameters = {
                "FilterExpression": "entity_type = :entity",
                "ExpressionAttributeValues": {":entity": "job"},
            }
        while True:
            response = operation(**parameters)
            items.extend(response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
            parameters["ExclusiveStartKey"] = cursor
        jobs = [Job.from_dict(json.loads(item["payload"])) for item in items]
        return sorted(jobs, key=lambda item: item.score.overall, reverse=True)

    def update_job_status(self, job_id: str, status: JobStatus, reason: str | None = None) -> Job:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        job.status = status
        if status == JobStatus.DISMISSED:
            job.dismissal_reason = reason
        self.put_job(job)
        return job

    def put_salary_evidence(self, key: str, evidence: SalaryEvidence) -> None:
        payload = {
            "minimum_gbp": evidence.minimum_gbp,
            "maximum_gbp": evidence.maximum_gbp,
            "currency": evidence.currency,
            "period": evidence.period,
            "confidence": evidence.confidence.value,
            "source_url": evidence.source_url,
            "source_label": evidence.source_label,
            "observed_at": evidence.observed_at,
            "inferred": evidence.inferred,
            "method_version": evidence.method_version,
        }
        self.table.put_item(
            Item={
                "pk": f"SALARY#{key}",
                "sk": "LATEST",
                "entity_type": "salary",
                "payload": self._encode(payload),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

    def get_salary_evidence(self, key: str) -> SalaryEvidence | None:
        response = self.table.get_item(Key={"pk": f"SALARY#{key}", "sk": "LATEST"})
        item = response.get("Item")
        if not item:
            return None
        payload = json.loads(item["payload"])
        payload["confidence"] = SalaryConfidence(payload.get("confidence", "unknown"))
        return SalaryEvidence(**payload)

    def put_application_pack(self, pack: ApplicationPack) -> None:
        self.table.put_item(
            Item={
                "pk": f"JOB#{pack.job_id}",
                "sk": "APPLICATION_PACK",
                "entity_type": "application_pack",
                "payload": self._encode(pack.to_dict()),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

    def get_application_pack(self, job_id: str) -> ApplicationPack | None:
        response = self.table.get_item(Key={"pk": f"JOB#{job_id}", "sk": "APPLICATION_PACK"},
                                       ConsistentRead=True)
        item = response.get("Item")
        return ApplicationPack(**json.loads(item["payload"])) if item else None

    @staticmethod
    def _application_task_key(customer_id: str, job_id: str) -> dict[str, str]:
        return {"pk": f"APPLICATION_TASK#{customer_id}", "sk": f"JOB#{job_id}"}

    @staticmethod
    def _application_task_from_item(item: dict[str, Any]) -> ApplicationTask:
        fields = {key: item.get(key) for key in ApplicationTask.__dataclass_fields__}
        fields["attempts"] = int(fields["attempts"] or 0)
        fields["recovery_dispatches"] = int(fields["recovery_dispatches"] or 0)
        return ApplicationTask.from_dict(fields)

    def get_application_task(self, customer_id: str, job_id: str) -> ApplicationTask | None:
        item = self.table.get_item(Key=self._application_task_key(customer_id, job_id),
                                   ConsistentRead=True).get("Item")
        return self._application_task_from_item(item) if item else None

    def list_application_tasks(self, customer_id: str) -> list[ApplicationTask]:
        items = []
        args: dict[str, Any] = {"KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": f"APPLICATION_TASK#{customer_id}"},
            "ConsistentRead": True}
        while True:
            response = self.table.query(**args)
            items.extend(response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                break
            args["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return [self._application_task_from_item(item) for item in items]

    def begin_application_task(self, task: ApplicationTask) -> tuple[ApplicationTask, bool]:
        current = self.get_application_task(task.customer_id, task.job_id)
        if current and current.status in {"queued", "processing"}:
            if current.identity != task.identity:
                raise ValueError("Another application preparation is in progress for this job")
            return current, False
        kwargs: dict[str, Any] = {"ConditionExpression": "attribute_not_exists(pk)"}
        if current:
            kwargs = {"ConditionExpression": "task_id = :old AND #status IN (:completed, :failed)",
                      "ExpressionAttributeNames": {"#status": "status"},
                      "ExpressionAttributeValues": {":old": current.task_id,
                          ":completed": "completed", ":failed": "failed"}}
        try:
            self.table.put_item(Item={**self._application_task_key(task.customer_id, task.job_id),
                "entity_type": "application_task", **task.to_dict()}, **kwargs)
        except Exception as exc:
            if not _conditional_check_failed(exc):
                raise
            latest = self.get_application_task(task.customer_id, task.job_id)
            if latest and latest.identity == task.identity and latest.status in {"queued", "processing"}:
                return latest, False
            raise ValueError("Application preparation changed concurrently; please retry") from exc
        return task, True

    def claim_application_task(self, customer_id: str, job_id: str, task_id: str,
                               now: str, lease_until: str) -> ApplicationTask | None:
        try:
            result = self.table.update_item(Key=self._application_task_key(customer_id, job_id),
                UpdateExpression="SET #status = :processing, updated_at = :now, lease_expires_at = :lease "
                                 "ADD attempts :one",
                ConditionExpression="task_id = :id AND (#status = :queued OR "
                                    "(#status = :processing AND lease_expires_at <= :now))",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":id": task_id, ":queued": "queued",
                    ":processing": "processing", ":now": now, ":lease": lease_until, ":one": 1},
                ReturnValues="ALL_NEW")
            return self._application_task_from_item(result["Attributes"])
        except Exception as exc:
            if _conditional_check_failed(exc):
                return None
            raise

    def mark_application_recovery_dispatch(self, customer_id: str, job_id: str,
                                           task_id: str, now: str, stale_before: str) -> bool:
        try:
            self.table.update_item(Key=self._application_task_key(customer_id, job_id),
                UpdateExpression="SET updated_at = :now ADD recovery_dispatches :one",
                ConditionExpression="task_id = :id AND #status IN (:queued, :processing) "
                                    "AND updated_at <= :stale AND recovery_dispatches < :max",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":id": task_id, ":queued": "queued",
                    ":processing": "processing", ":now": now, ":stale": stale_before,
                    ":one": 1, ":max": 4})
            return True
        except Exception as exc:
            if _conditional_check_failed(exc):
                return False
            raise

    def complete_application_task(self, customer_id: str, job_id: str, task_id: str, now: str) -> bool:
        try:
            self.table.update_item(Key=self._application_task_key(customer_id, job_id),
                UpdateExpression="SET #status = :completed, updated_at = :now, completed_at = :now "
                                 "REMOVE lease_expires_at",
                ConditionExpression="task_id = :id AND #status = :processing",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":id": task_id, ":processing": "processing",
                    ":completed": "completed", ":now": now})
            return True
        except Exception as exc:
            if _conditional_check_failed(exc):
                return False
            raise

    def record_application_task_error(self, customer_id: str, job_id: str, task_id: str,
                                      now: str, reason: str, *, retryable: bool,
                                      expected_status: str = "processing") -> bool:
        status = "queued" if retryable else "failed"
        expression = ("SET #status = :status, updated_at = :now, last_error = :error "
                      "REMOVE lease_expires_at" if retryable else
                      "SET #status = :status, updated_at = :now, last_error = :error, "
                      "completed_at = :now REMOVE lease_expires_at")
        try:
            self.table.update_item(Key=self._application_task_key(customer_id, job_id),
                UpdateExpression=expression,
                ConditionExpression="task_id = :id AND #status = :expected",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":id": task_id, ":expected": expected_status,
                    ":status": status, ":now": now, ":error": reason[:500]})
            return True
        except Exception as exc:
            if _conditional_check_failed(exc):
                return False
            raise

    def put_run(self, report: RunReport) -> None:
        self.table.put_item(
            Item={
                "pk": f"RUN#{report.run_id}",
                "sk": "META",
                "entity_type": "run",
                "gsi1pk": "ENTITY#run",
                "gsi1sk": f"{report.started_at}#{report.run_id}",
                "payload": self._encode(report.to_dict()),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

    def get_run(self, run_id: str) -> RunReport | None:
        response = self.table.get_item(Key={"pk": f"RUN#{run_id}", "sk": "META"}, ConsistentRead=True)
        item = response.get("Item")
        return RunReport(**json.loads(item["payload"])) if item else None

    def list_runs(self, limit: int = 30) -> list[RunReport]:
        limit = max(1, min(limit, 100))
        items: list[dict[str, Any]] = []
        parameters: dict[str, Any] = {
            "IndexName": "gsi1",
            "KeyConditionExpression": "gsi1pk = :entity",
            "ExpressionAttributeValues": {":entity": "ENTITY#run"},
            "ScanIndexForward": False,
        }
        while len(items) < limit:
            parameters["Limit"] = limit - len(items)
            response = self.table.query(**parameters)
            items.extend(response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
            parameters["ExclusiveStartKey"] = cursor
        return [RunReport(**json.loads(item["payload"])) for item in items[:limit]]

    def get_source_health(self, source_key: str) -> SourceHealth | None:
        response = self.table.get_item(
            Key={"pk": f"SOURCE#{source_key}", "sk": "HEALTH"}, ConsistentRead=True
        )
        item = response.get("Item")
        return SourceHealth.from_dict(json.loads(item["payload"])) if item else None

    def put_source_health(self, health: SourceHealth) -> None:
        self.table.put_item(
            Item={
                "pk": f"SOURCE#{health.source_key}",
                "sk": "HEALTH",
                "entity_type": "source_health",
                "gsi1pk": "ENTITY#source_health",
                "gsi1sk": health.source_key,
                "payload": self._encode(health.to_dict()),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

    def list_source_health(self) -> list[SourceHealth]:
        items: list[dict[str, Any]] = []
        parameters: dict[str, Any] = {
            "IndexName": "gsi1",
            "KeyConditionExpression": "gsi1pk = :entity",
            "ExpressionAttributeValues": {":entity": "ENTITY#source_health"},
        }
        while True:
            response = self.table.query(**parameters)
            items.extend(response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
            parameters["ExclusiveStartKey"] = cursor
        return [SourceHealth.from_dict(json.loads(item["payload"])) for item in items]

    def put_coverage_sample(self, sample: CoverageSample) -> None:
        self.table.put_item(
            Item={
                "pk": f"COVERAGE#{sample.sample_id}",
                "sk": "SAMPLE",
                "entity_type": "coverage_sample",
                "gsi1pk": "ENTITY#coverage_sample",
                "gsi1sk": f"{sample.observed_at}#{sample.sample_id}",
                "payload": self._encode(sample.to_dict()),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )

    def list_coverage_samples(self, limit: int = 500) -> list[CoverageSample]:
        limit = max(1, min(limit, 1000))
        items: list[dict[str, Any]] = []
        parameters: dict[str, Any] = {
            "IndexName": "gsi1",
            "KeyConditionExpression": "gsi1pk = :entity",
            "ExpressionAttributeValues": {":entity": "ENTITY#coverage_sample"},
            "ScanIndexForward": False,
        }
        while len(items) < limit:
            parameters["Limit"] = limit - len(items)
            response = self.table.query(**parameters)
            items.extend(response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
            parameters["ExclusiveStartKey"] = cursor
        return [CoverageSample.from_dict(json.loads(item["payload"])) for item in items[:limit]]

    def get_analysis_histories(self, job_ids: list[str]) -> dict[str, JobAnalysisHistory]:
        histories: dict[str, JobAnalysisHistory] = {}
        unique_ids = list(dict.fromkeys(job_ids))
        for start in range(0, len(unique_ids), 100):
            request_items = {
                self.table_name: {
                    "Keys": [
                        {"pk": f"ANALYSIS#{job_id}", "sk": "HISTORY"}
                        for job_id in unique_ids[start : start + 100]
                    ],
                    "ConsistentRead": True,
                }
            }
            while request_items:
                response = self.dynamodb.batch_get_item(RequestItems=request_items)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    history = JobAnalysisHistory.from_dict(json.loads(item["payload"]))
                    histories[history.job_id] = history
                request_items = response.get("UnprocessedKeys", {})
        return histories

    def put_analysis_histories(self, histories: list[JobAnalysisHistory]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for history in histories:
                batch.put_item(
                    Item={
                        "pk": f"ANALYSIS#{history.job_id}",
                        "sk": "HISTORY",
                        "entity_type": "analysis_history",
                        "payload": self._encode(history.to_dict()),
                        "updated_at": now,
                    }
                )

    def queue_analysis_task(self, task: AnalysisTask) -> bool:
        existing = self.get_analysis_task(task.customer_id, task.task_id)
        if existing and existing.status in {"dispatched", "processing"}:
            return False
        if existing:
            task.first_queued_at = existing.first_queued_at
            task.attempts = existing.attempts
        task.status = "queued"
        task.selected_run_id = None
        task.dispatched_at = None
        task.completed_at = None
        now = datetime.now(UTC).isoformat()
        try:
            self.table.put_item(
                Item={
                    "pk": f"ANALYSIS_BACKLOG#{task.customer_id}",
                    "sk": f"TASK#{task.task_id}",
                    "entity_type": "analysis_task",
                    "gsi1pk": f"ANALYSIS_TASK#{task.customer_id}#queued",
                    "gsi1sk": (
                        f"{100 - task.priority_score:06.2f}#"
                        f"{task.first_queued_at}#{task.task_id}"
                    ),
                    "status": "queued",
                    "selected_run_id": "",
                    "attempts": task.attempts,
                    "last_attempt_at": task.last_attempt_at or "",
                    "lease_expires_at": "",
                    "completed_at": "",
                    "last_error": task.last_error or "",
                    "result_source": "",
                    "model_calls": task.model_calls,
                    "input_tokens": task.input_tokens,
                    "output_tokens": task.output_tokens,
                    "payload": self._encode(task.to_dict()),
                    "updated_at": now,
                },
                ConditionExpression=(
                    "attribute_not_exists(pk) OR #status IN "
                    "(:queued, :timed_out, :expired, :cancelled, :completed)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":queued": "queued",
                    ":timed_out": "timed_out",
                    ":expired": "expired",
                    ":cancelled": "cancelled",
                    ":completed": "completed",
                },
            )
        except Exception as exc:
            if _conditional_check_failed(exc):
                return False
            raise
        return True

    def upsert_reed_hydration_tasks(self, tasks: list[ReedHydrationTask]) -> list[str]:
        queued: list[str] = []
        for task in tasks:
            existing = self.get_reed_hydration_task(task.customer_id, task.source_id)
            if existing and existing.summary_fingerprint == task.summary_fingerprint:
                if existing.status == "completed":
                    task.first_seen_at = existing.first_seen_at
                    self._put_reed_hydration_task(task)
                    queued.append(task.source_id)
                    continue
                try:
                    self.table.update_item(
                        Key={
                            "pk": f"REED_HYDRATION#{task.customer_id}",
                            "sk": f"TASK#{task.source_id}",
                        },
                        UpdateExpression="SET last_seen_at = :seen, updated_at = :seen",
                        ConditionExpression="summary_fingerprint = :fingerprint",
                        ExpressionAttributeValues={
                            ":seen": task.last_seen_at,
                            ":fingerprint": task.summary_fingerprint,
                        },
                    )
                except Exception as exc:
                    if _conditional_check_failed(exc):
                        continue
                    raise
                if existing.status in {"queued", "dispatched"}:
                    queued.append(task.source_id)
                continue
            if existing:
                task.first_seen_at = existing.first_seen_at
            self._put_reed_hydration_task(task)
            queued.append(task.source_id)
        return queued

    def get_reed_hydration_task(
        self, customer_id: str, source_id: str
    ) -> ReedHydrationTask | None:
        response = self.table.get_item(
            Key={"pk": f"REED_HYDRATION#{customer_id}", "sk": f"TASK#{source_id}"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._reed_hydration_task_from_item(item) if item else None

    def list_reed_hydration_tasks(self, customer_id: str) -> list[ReedHydrationTask]:
        items: list[dict[str, Any]] = []
        parameters: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :task)",
            "ExpressionAttributeValues": {
                ":pk": f"REED_HYDRATION#{customer_id}",
                ":task": "TASK#",
            },
            "ConsistentRead": True,
        }
        while True:
            response = self.table.query(**parameters)
            items.extend(response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
            parameters["ExclusiveStartKey"] = cursor
        return sorted(
            [self._reed_hydration_task_from_item(item) for item in items],
            key=lambda item: item.first_seen_at,
        )

    def mark_reed_hydration_dispatched(
        self, customer_id: str, source_ids: list[str], dispatched_at: str
    ) -> list[str]:
        dispatched: list[str] = []
        for source_id in source_ids:
            try:
                self.table.update_item(
                    Key={
                        "pk": f"REED_HYDRATION#{customer_id}",
                        "sk": f"TASK#{source_id}",
                    },
                    UpdateExpression=(
                        "SET #status = :dispatched, last_dispatched_at = :now, "
                        "updated_at = :now, gsi1pk = :gsi1pk"
                    ),
                    ConditionExpression=(
                        "#status IN (:queued, :dispatched) OR "
                        "(#status = :processing AND lease_expires_at < :now)"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":queued": "queued",
                        ":dispatched": "dispatched",
                        ":processing": "processing",
                        ":now": dispatched_at,
                        ":gsi1pk": f"REED_HYDRATION#{customer_id}#dispatched",
                    },
                )
            except Exception as exc:
                if _conditional_check_failed(exc):
                    continue
                raise
            dispatched.append(source_id)
        return dispatched

    def start_reed_hydration_attempt(self, customer_id: str, source_id: str) -> bool:
        now = datetime.now(UTC)
        try:
            self.table.update_item(
                Key={"pk": f"REED_HYDRATION#{customer_id}", "sk": f"TASK#{source_id}"},
                UpdateExpression=(
                    "SET #status = :processing, attempts = if_not_exists(attempts, :zero) + :one, "
                    "last_attempt_at = :now, lease_expires_at = :lease, updated_at = :now, "
                    "gsi1pk = :gsi1pk"
                ),
                ConditionExpression=(
                    "(#status = :dispatched OR (#status = :processing AND lease_expires_at < :now)) "
                    "AND (attribute_not_exists(next_attempt_at) OR next_attempt_at <= :now)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":dispatched": "dispatched",
                    ":processing": "processing",
                    ":zero": 0,
                    ":one": 1,
                    ":now": now.isoformat(),
                    ":lease": (now + timedelta(minutes=2)).isoformat(),
                    ":gsi1pk": f"REED_HYDRATION#{customer_id}#processing",
                },
            )
        except Exception as exc:
            if _conditional_check_failed(exc):
                return False
            raise
        return True

    def defer_reed_hydration_task(
        self,
        customer_id: str,
        source_id: str,
        error: str,
        next_attempt_at: str,
        *,
        expected_fingerprint: str,
        failed: bool = False,
    ) -> bool:
        status = "failed" if failed else "queued"
        try:
            self.table.update_item(
                Key={"pk": f"REED_HYDRATION#{customer_id}", "sk": f"TASK#{source_id}"},
                UpdateExpression=(
                    "SET #status = :status, last_error = :error, next_attempt_at = :next, "
                    "updated_at = :now, gsi1pk = :gsi1pk REMOVE lease_expires_at"
                ),
                ConditionExpression="#status = :processing AND summary_fingerprint = :fingerprint",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":fingerprint": expected_fingerprint,
                    ":status": status,
                    ":error": error[:1000],
                    ":next": next_attempt_at,
                    ":now": datetime.now(UTC).isoformat(),
                    ":gsi1pk": f"REED_HYDRATION#{customer_id}#{status}",
                },
            )
        except Exception as exc:
            if _conditional_check_failed(exc):
                return False
            raise
        return True

    def complete_reed_hydration_task(
        self, customer_id: str, source_id: str, completed_at: str,
        *, expected_fingerprint: str,
    ) -> bool:
        try:
            self.table.update_item(
                Key={"pk": f"REED_HYDRATION#{customer_id}", "sk": f"TASK#{source_id}"},
                UpdateExpression=(
                    "SET #status = :completed, completed_at = :now, ready_status = :pending, "
                    "updated_at = :now, gsi1pk = :gsi1pk REMOVE lease_expires_at, "
                    "next_attempt_at, last_error"
                ),
                ConditionExpression="#status = :processing AND summary_fingerprint = :fingerprint",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":fingerprint": expected_fingerprint,
                    ":completed": "completed",
                    ":pending": "pending",
                    ":now": completed_at,
                    ":gsi1pk": f"REED_HYDRATION#{customer_id}#completed",
                },
            )
        except Exception as exc:
            if _conditional_check_failed(exc):
                return False
            raise
        return True

    def mark_reed_hydration_ready_sent(
        self, customer_id: str, source_id: str, sent_at: str
    ) -> None:
        try:
            self.table.update_item(
                Key={"pk": f"REED_HYDRATION#{customer_id}", "sk": f"TASK#{source_id}"},
                UpdateExpression="SET ready_sent_at = :now, updated_at = :now",
                ConditionExpression="#status = :completed AND ready_status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":completed": "completed", ":pending": "pending", ":now": sent_at
                },
            )
        except Exception as exc:
            if not _conditional_check_failed(exc):
                raise

    def mark_reed_hydration_ready_processed(
        self,
        customer_id: str,
        source_id: str,
        processed_at: str,
        *,
        outcome: str,
        expected_fingerprint: str,
        analysis_task_id: str | None = None,
        gate_reasons: list[str] | None = None,
    ) -> None:
        try:
            self.table.update_item(
                Key={"pk": f"REED_HYDRATION#{customer_id}", "sk": f"TASK#{source_id}"},
                UpdateExpression=(
                    "SET ready_status = :outcome, ready_processed_at = :now, "
                    "analysis_task_id = :task_id, gate_reasons = :reasons, updated_at = :now"
                ),
                ConditionExpression=(
                    "#status = :completed AND ready_status = :pending "
                    "AND summary_fingerprint = :fingerprint"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":completed": "completed",
                    ":pending": "pending",
                    ":fingerprint": expected_fingerprint,
                    ":outcome": outcome,
                    ":now": processed_at,
                    ":task_id": analysis_task_id or "",
                    ":reasons": list(gate_reasons or []),
                },
            )
        except Exception as exc:
            if not _conditional_check_failed(exc):
                raise

    def reserve_reed_hydration_request(
        self, customer_id: str, budget_date: str, maximum_requests: int, reserved_at: str
    ) -> bool:
        if maximum_requests <= 0:
            return False
        try:
            self.table.update_item(
                Key={
                    "pk": f"REED_HYDRATION_BUDGET#{customer_id}",
                    "sk": f"DAY#{budget_date}",
                },
                UpdateExpression=(
                    "SET entity_type = :entity, updated_at = :now "
                    "ADD requests_committed :one"
                ),
                ConditionExpression=(
                    "attribute_not_exists(requests_committed) OR requests_committed < :maximum"
                ),
                ExpressionAttributeValues={
                    ":entity": "reed_hydration_budget",
                    ":now": reserved_at,
                    ":one": 1,
                    ":maximum": maximum_requests,
                },
            )
        except Exception as exc:
            if _conditional_check_failed(exc):
                return False
            raise
        return True

    def _put_reed_hydration_task(self, task: ReedHydrationTask) -> None:
        now = datetime.now(UTC).isoformat()
        self.table.put_item(
            Item={
                "pk": f"REED_HYDRATION#{task.customer_id}",
                "sk": f"TASK#{task.source_id}",
                "entity_type": "reed_hydration_task",
                "gsi1pk": f"REED_HYDRATION#{task.customer_id}#{task.status}",
                "gsi1sk": f"{task.first_seen_at}#{task.source_id}",
                "status": task.status,
                "summary_fingerprint": task.summary_fingerprint,
                "last_seen_at": task.last_seen_at,
                "attempts": task.attempts,
                "last_dispatched_at": task.last_dispatched_at or "",
                "last_attempt_at": task.last_attempt_at or "",
                "next_attempt_at": task.next_attempt_at or "",
                "lease_expires_at": task.lease_expires_at or "",
                "completed_at": task.completed_at or "",
                "last_error": task.last_error or "",
                "ready_status": task.ready_status,
                "ready_sent_at": task.ready_sent_at or "",
                "ready_processed_at": task.ready_processed_at or "",
                "analysis_task_id": task.analysis_task_id or "",
                "gate_reasons": list(task.gate_reasons),
                "payload": self._encode(task.to_dict()),
                "updated_at": now,
            }
        )

    def get_deep_analysis_records(self, cache_keys: list[str]) -> dict[str, DeepAnalysisRecord]:
        records: dict[str, DeepAnalysisRecord] = {}
        unique_keys = list(dict.fromkeys(cache_keys))
        for start in range(0, len(unique_keys), 100):
            request_items = {
                self.table_name: {
                    "Keys": [
                        {"pk": f"DEEP_ANALYSIS#{cache_key}", "sk": "RESULT"}
                        for cache_key in unique_keys[start : start + 100]
                    ],
                    "ConsistentRead": True,
                }
            }
            while request_items:
                response = self.dynamodb.batch_get_item(RequestItems=request_items)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    record = DeepAnalysisRecord.from_dict(json.loads(item["payload"]))
                    records[record.cache_key] = record
                request_items = response.get("UnprocessedKeys", {})
        return records

    def put_deep_analysis_record(self, record: DeepAnalysisRecord) -> None:
        now = datetime.now(UTC)
        self.table.put_item(
            Item={
                "pk": f"DEEP_ANALYSIS#{record.cache_key}",
                "sk": "RESULT",
                "entity_type": "deep_analysis_cache",
                "job_id": record.job_id,
                "analyzed_at": record.analyzed_at,
                "expires_at": int((now + timedelta(days=180)).timestamp()),
                "payload": self._encode(record.to_dict()),
                "updated_at": now.isoformat(),
            }
        )

    def put_analysis_tasks(self, tasks: list[AnalysisTask]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for task in tasks:
                batch.put_item(
                    Item={
                        "pk": f"ANALYSIS_BACKLOG#{task.customer_id}",
                        "sk": f"TASK#{task.task_id}",
                        "entity_type": "analysis_task",
                        "gsi1pk": f"ANALYSIS_TASK#{task.customer_id}#{task.status}",
                        "gsi1sk": (
                            f"{100 - task.priority_score:06.2f}#"
                            f"{task.first_queued_at}#{task.task_id}"
                        ),
                        "status": task.status,
                        "selected_run_id": task.selected_run_id or "",
                        "attempts": task.attempts,
                        "last_attempt_at": task.last_attempt_at or "",
                        "lease_expires_at": task.lease_expires_at or "",
                        "completed_at": task.completed_at or "",
                        "last_error": task.last_error or "",
                        "result_source": task.result_source or "",
                        "model_calls": task.model_calls,
                        "input_tokens": task.input_tokens,
                        "output_tokens": task.output_tokens,
                        "payload": self._encode(task.to_dict()),
                        "updated_at": now,
                    }
                )

    def get_analysis_task(self, customer_id: str, task_id: str) -> AnalysisTask | None:
        response = self.table.get_item(
            Key={"pk": f"ANALYSIS_BACKLOG#{customer_id}", "sk": f"TASK#{task_id}"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._analysis_task_from_item(item) if item else None

    def get_analysis_tasks(
        self, customer_id: str, task_ids: list[str]
    ) -> dict[str, AnalysisTask]:
        tasks: dict[str, AnalysisTask] = {}
        unique_ids = list(dict.fromkeys(task_ids))
        for start in range(0, len(unique_ids), 100):
            request_items = {
                self.table_name: {
                    "Keys": [
                        {
                            "pk": f"ANALYSIS_BACKLOG#{customer_id}",
                            "sk": f"TASK#{task_id}",
                        }
                        for task_id in unique_ids[start : start + 100]
                    ],
                    "ConsistentRead": True,
                }
            }
            while request_items:
                response = self.dynamodb.batch_get_item(RequestItems=request_items)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    task = self._analysis_task_from_item(item)
                    tasks[task.task_id] = task
                request_items = response.get("UnprocessedKeys", {})
        return tasks

    def list_analysis_tasks(self, customer_id: str) -> list[AnalysisTask]:
        items: list[dict[str, Any]] = []
        parameters: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :task)",
            "ExpressionAttributeValues": {
                ":pk": f"ANALYSIS_BACKLOG#{customer_id}",
                ":task": "TASK#",
            },
            "ConsistentRead": True,
        }
        while True:
            response = self.table.query(**parameters)
            items.extend(response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
            parameters["ExclusiveStartKey"] = cursor
        tasks = [self._analysis_task_from_item(item) for item in items]
        return sorted(tasks, key=lambda item: (-item.priority_score, item.first_queued_at))

    def dispatch_analysis_tasks(
        self,
        customer_id: str,
        run_id: str,
        task_ids: list[str],
        dispatched_at: str,
        *,
        selection_reason: str,
    ) -> list[str]:
        dispatched: list[str] = []
        for task_id in task_ids:
            key = {
                "pk": f"ANALYSIS_BACKLOG#{customer_id}",
                "sk": f"TASK#{task_id}",
            }
            try:
                self.table.update_item(
                    Key=key,
                    UpdateExpression=(
                        "SET #status = :dispatched, selected_run_id = :run_id, "
                        "selection_reason = :reason, dispatched_at = :now, updated_at = :now, "
                        "gsi1pk = :gsi1pk REMOVE completed_at, lease_expires_at"
                    ),
                    ConditionExpression="#status IN (:queued, :timed_out)",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":queued": "queued",
                        ":timed_out": "timed_out",
                        ":dispatched": "dispatched",
                        ":run_id": run_id,
                        ":reason": selection_reason,
                        ":now": dispatched_at,
                        ":gsi1pk": f"ANALYSIS_TASK#{customer_id}#dispatched",
                    },
                )
                dispatched.append(task_id)
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                existing = self.get_analysis_task(customer_id, task_id)
                if existing and existing.selected_run_id == run_id:
                    dispatched.append(task_id)
        return dispatched

    def defer_analysis_task(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        deferred_at: str,
        reason: str,
    ) -> None:
        try:
            self.table.update_item(
                Key={
                    "pk": f"ANALYSIS_BACKLOG#{customer_id}",
                    "sk": f"TASK#{task_id}",
                },
                UpdateExpression=(
                    "SET #status = :timed_out, completed_at = :now, updated_at = :now, "
                    "last_error = :reason, gsi1pk = :gsi1pk REMOVE lease_expires_at"
                ),
                ConditionExpression="#status = :processing AND selected_run_id = :run_id",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":timed_out": "timed_out",
                    ":run_id": run_id,
                    ":now": deferred_at,
                    ":reason": reason[:1000],
                    ":gsi1pk": f"ANALYSIS_TASK#{customer_id}#timed_out",
                },
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return

    def expire_inactive_analysis_tasks(
        self,
        customer_id: str,
        active_task_ids: set[str],
        stale_before: str,
        completed_at: str,
    ) -> int:
        changed = 0
        for task in self.list_analysis_tasks(customer_id):
            if (
                task.task_id in active_task_ids
                or task.status not in {"queued", "timed_out"}
                or (
                    task.last_seen_at > stale_before
                    and not (task.closes_at and task.closes_at <= completed_at)
                )
            ):
                continue
            try:
                self.table.update_item(
                    Key={
                        "pk": f"ANALYSIS_BACKLOG#{customer_id}",
                        "sk": f"TASK#{task.task_id}",
                    },
                    UpdateExpression=(
                        "SET #status = :expired, completed_at = :now, updated_at = :now, "
                        "gsi1pk = :gsi1pk, expires_at = :expires_at"
                    ),
                    ConditionExpression="#status IN (:queued, :timed_out)",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":queued": "queued",
                        ":timed_out": "timed_out",
                        ":expired": "expired",
                        ":now": completed_at,
                        ":gsi1pk": f"ANALYSIS_TASK#{customer_id}#expired",
                        ":expires_at": int(
                            (datetime.now(UTC) + timedelta(days=30)).timestamp()
                        ),
                    },
                )
                changed += 1
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                continue
        return changed

    def begin_analysis_run(self, run: AnalysisRun) -> bool:
        try:
            self.table.put_item(
                Item={
                    "pk": f"ANALYSIS_RUN#{run.run_id}",
                    "sk": "META",
                    "entity_type": "analysis_run",
                    "gsi1pk": "ENTITY#analysis_run",
                    "gsi1sk": f"{run.started_at}#{run.run_id}",
                    **run.to_dict(),
                    "selected_task_ids": self._encode({"ids": run.selected_task_ids}),
                    "updated_at": run.prepared_at,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def get_analysis_run(self, run_id: str) -> AnalysisRun | None:
        response = self.table.get_item(
            Key={"pk": f"ANALYSIS_RUN#{run_id}", "sk": "META"}, ConsistentRead=True
        )
        item = response.get("Item")
        return self._analysis_run_from_item(item) if item else None

    def list_analysis_runs(
        self, limit: int = 30, *, run_type: str | None = None
    ) -> list[AnalysisRun]:
        limit = max(1, min(limit, 100))
        runs: list[AnalysisRun] = []
        parameters: dict[str, Any] = {
            "IndexName": "gsi1",
            "KeyConditionExpression": "gsi1pk = :entity",
            "ExpressionAttributeValues": {":entity": "ENTITY#analysis_run"},
            "ScanIndexForward": False,
        }
        while len(runs) < limit:
            parameters["Limit"] = limit
            response = self.table.query(**parameters)
            for item in response.get("Items", []):
                run = self._analysis_run_from_item(item)
                if run_type is None or run.run_type == run_type:
                    runs.append(run)
                    if len(runs) == limit:
                        break
            cursor = response.get("LastEvaluatedKey")
            if not cursor or len(runs) == limit:
                break
            parameters["ExclusiveStartKey"] = cursor
        return runs

    def get_analysis_budget_usage(
        self, customer_id: str, budget_date: str
    ) -> AnalysisBudgetUsage | None:
        response = self.table.get_item(
            Key={
                "pk": f"ANALYSIS_BUDGET#{customer_id}",
                "sk": f"DAY#{budget_date}",
            },
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return AnalysisBudgetUsage(
            customer_id=customer_id,
            budget_date=budget_date,
            calls_committed=int(item.get("calls_committed", 0)),
            input_tokens_committed=int(item.get("input_tokens_committed", 0)),
            output_tokens_committed=int(item.get("output_tokens_committed", 0)),
            cost_microusd_committed=int(item.get("cost_microusd_committed", 0)),
            calls_actual=int(item.get("calls_actual", 0)),
            input_tokens_actual=int(item.get("input_tokens_actual", 0)),
            output_tokens_actual=int(item.get("output_tokens_actual", 0)),
            cost_microusd_actual=int(item.get("cost_microusd_actual", 0)),
            updated_at=str(item["updated_at"]) if item.get("updated_at") else None,
            discovery_state=str(item["discovery_state"]) if item.get("discovery_state") else None,
        )

    def set_analysis_discovery_state(
        self, customer_id: str, budget_date: str, state: str, observed_at: str,
        *, maximum_backlog_calls: int,
    ) -> bool:
        if state not in {"reserved", "none", "released"}:
            raise ValueError("Invalid discovery state")
        condition = {
            "reserved": "(attribute_not_exists(discovery_state) OR discovery_state IN (:none, :reserved)) AND (attribute_not_exists(calls_committed) OR calls_committed <= :backlog_max)",
            "none": "attribute_not_exists(discovery_state) OR discovery_state = :none",
            "released": "discovery_state = :reserved",
        }[state]
        values = {
            ":state": state, ":now": observed_at,
            ":expires_at": int((datetime.now(UTC) + timedelta(days=400)).timestamp()),
        }
        if state == "reserved":
            values.update({":none": "none", ":reserved": "reserved", ":backlog_max": maximum_backlog_calls})
        elif state == "none":
            values[":none"] = "none"
        else:
            values[":reserved"] = "reserved"
        try:
            self.table.update_item(
                Key={"pk": f"ANALYSIS_BUDGET#{customer_id}", "sk": f"DAY#{budget_date}"},
                UpdateExpression="SET discovery_state = :state, updated_at = :now, expires_at = :expires_at",
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def reserve_analysis_capacity(
        self,
        customer_id: str,
        budget_date: str,
        reserved_at: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int,
        maximum_calls: int,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        maximum_cost_microusd: int,
        run_type: str = "pipeline",
    ) -> bool:
        input_tokens = max(0, input_tokens)
        output_tokens = max(0, output_tokens)
        cost_microusd = max(0, cost_microusd)
        if (
            input_tokens > maximum_input_tokens
            or output_tokens > maximum_output_tokens
            or cost_microusd > maximum_cost_microusd
        ):
            return False
        lower_priority = run_type in {"backlog_drain", "manual_discovery"}
        usage = self.get_analysis_budget_usage(customer_id, budget_date) if lower_priority else None
        state = usage.discovery_state if usage else None
        call_ceiling = (
            max(0, maximum_calls - 25)
            if lower_priority and state not in {"none", "released"}
            else maximum_calls
        )
        if call_ceiling <= 0:
            return False
        state_condition = (
            " AND attribute_not_exists(discovery_state)" if state is None else
            " AND discovery_state = :expected_discovery_state"
        ) if lower_priority else ""
        values = {
            ":entity_type": "analysis_budget", ":customer_id": customer_id,
            ":budget_date": budget_date, ":now": reserved_at,
            ":expires_at": int((datetime.now(UTC) + timedelta(days=400)).timestamp()),
            ":one": 1, ":input_tokens": input_tokens, ":output_tokens": output_tokens,
            ":cost": cost_microusd, ":calls_before": call_ceiling - 1,
            ":input_before": maximum_input_tokens - input_tokens,
            ":output_before": maximum_output_tokens - output_tokens,
            ":cost_before": maximum_cost_microusd - cost_microusd,
        }
        if state is not None and lower_priority:
            values[":expected_discovery_state"] = state
        try:
            self.table.update_item(
                Key={
                    "pk": f"ANALYSIS_BUDGET#{customer_id}",
                    "sk": f"DAY#{budget_date}",
                },
                UpdateExpression=(
                    "SET entity_type = :entity_type, customer_id = :customer_id, "
                    "budget_date = :budget_date, updated_at = :now, expires_at = :expires_at "
                    "ADD calls_committed :one, input_tokens_committed :input_tokens, "
                    "output_tokens_committed :output_tokens, "
                    "cost_microusd_committed :cost"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(calls_committed) OR calls_committed <= :calls_before) "
                    "AND (attribute_not_exists(input_tokens_committed) OR "
                    "input_tokens_committed <= :input_before) "
                    "AND (attribute_not_exists(output_tokens_committed) OR "
                    "output_tokens_committed <= :output_before) "
                    "AND (attribute_not_exists(cost_microusd_committed) OR "
                    "cost_microusd_committed <= :cost_before)" + state_condition
                ),
                ExpressionAttributeValues=values,
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def reconcile_analysis_capacity(
        self,
        customer_id: str,
        budget_date: str,
        reconciled_at: str,
        *,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_microusd: int,
        actual_calls: int,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_cost_microusd: int,
    ) -> None:
        self.table.update_item(
            Key={
                "pk": f"ANALYSIS_BUDGET#{customer_id}",
                "sk": f"DAY#{budget_date}",
            },
            UpdateExpression=(
                "SET updated_at = :now "
                "ADD input_tokens_committed :input_delta, "
                "output_tokens_committed :output_delta, cost_microusd_committed :cost_delta, "
                "calls_actual :actual_calls, input_tokens_actual :actual_input, "
                "output_tokens_actual :actual_output, cost_microusd_actual :actual_cost"
            ),
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={
                ":now": reconciled_at,
                ":input_delta": max(0, actual_input_tokens)
                - max(0, reserved_input_tokens),
                ":output_delta": max(0, actual_output_tokens)
                - max(0, reserved_output_tokens),
                ":cost_delta": max(0, actual_cost_microusd)
                - max(0, reserved_cost_microusd),
                ":actual_calls": max(0, actual_calls),
                ":actual_input": max(0, actual_input_tokens),
                ":actual_output": max(0, actual_output_tokens),
                ":actual_cost": max(0, actual_cost_microusd),
            },
        )

    def start_analysis_task_attempt(
        self, customer_id: str, task_id: str, run_id: str
    ) -> bool:
        now = datetime.now(UTC)
        try:
            self.table.update_item(
                Key={"pk": f"ANALYSIS_BACKLOG#{customer_id}", "sk": f"TASK#{task_id}"},
                UpdateExpression=(
                    "SET #status = :processing, last_attempt_at = :now, updated_at = :now, "
                    "lease_expires_at = :lease_expires_at, gsi1pk = :gsi1pk ADD attempts :one"
                ),
                ConditionExpression=(
                    "(#status = :dispatched OR "
                    "(#status = :processing AND lease_expires_at < :now)) "
                    "AND selected_run_id = :run_id"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":dispatched": "dispatched",
                    ":processing": "processing",
                    ":run_id": run_id,
                    ":now": now.isoformat(),
                    ":lease_expires_at": (now + timedelta(minutes=4)).isoformat(),
                    ":gsi1pk": f"ANALYSIS_TASK#{customer_id}#processing",
                    ":one": 1,
                },
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def record_analysis_task_error(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        error: str,
        *,
        model_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        try:
            self.table.update_item(
                Key={"pk": f"ANALYSIS_BACKLOG#{customer_id}", "sk": f"TASK#{task_id}"},
                UpdateExpression=(
                    "SET #status = :dispatched, last_error = :error, updated_at = :now, "
                    "gsi1pk = :gsi1pk REMOVE lease_expires_at "
                    "ADD model_calls :model_calls, input_tokens :input_tokens, "
                    "output_tokens :output_tokens"
                ),
                ConditionExpression="#status = :processing AND selected_run_id = :run_id",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":dispatched": "dispatched",
                    ":processing": "processing",
                    ":run_id": run_id,
                    ":error": error[:1000],
                    ":now": datetime.now(UTC).isoformat(),
                    ":gsi1pk": f"ANALYSIS_TASK#{customer_id}#dispatched",
                    ":model_calls": max(0, model_calls),
                    ":input_tokens": max(0, input_tokens),
                    ":output_tokens": max(0, output_tokens),
                },
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return

    def complete_analysis_task(
        self,
        customer_id: str,
        task_id: str,
        run_id: str,
        completed_at: str,
        *,
        result_source: str,
        model_calls: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        try:
            self.table.update_item(
                Key={"pk": f"ANALYSIS_BACKLOG#{customer_id}", "sk": f"TASK#{task_id}"},
                UpdateExpression=(
                    "SET #status = :completed, completed_at = :now, updated_at = :now, "
                    "result_source = :result_source, gsi1pk = :gsi1pk, "
                    "expires_at = :expires_at REMOVE last_error, lease_expires_at "
                    "ADD model_calls :model_calls, input_tokens :input_tokens, "
                    "output_tokens :output_tokens"
                ),
                ConditionExpression="#status = :processing AND selected_run_id = :run_id",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":completed": "completed",
                    ":run_id": run_id,
                    ":now": completed_at,
                    ":result_source": result_source,
                    ":model_calls": max(0, model_calls),
                    ":input_tokens": max(0, input_tokens),
                    ":output_tokens": max(0, output_tokens),
                    ":gsi1pk": f"ANALYSIS_TASK#{customer_id}#completed",
                    ":expires_at": int(
                        (datetime.now(UTC) + timedelta(days=180)).timestamp()
                    ),
                },
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return

    def time_out_pending_analysis_tasks(self, run_id: str, completed_at: str) -> int:
        run = self.get_analysis_run(run_id)
        if run is None:
            return 0
        changed = 0
        for task in self.get_analysis_tasks(run.customer_id, run.selected_task_ids).values():
            if task.status not in {"dispatched", "processing"} or task.selected_run_id != run_id:
                continue
            try:
                self.table.update_item(
                    Key={
                        "pk": f"ANALYSIS_BACKLOG#{run.customer_id}",
                        "sk": f"TASK#{task.task_id}",
                    },
                    UpdateExpression=(
                        "SET #status = :timed_out, completed_at = :now, updated_at = :now, "
                        "last_error = :error, gsi1pk = :gsi1pk REMOVE lease_expires_at"
                    ),
                    ConditionExpression=(
                        "#status IN (:dispatched, :processing) AND selected_run_id = :run_id"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":dispatched": "dispatched",
                        ":processing": "processing",
                        ":timed_out": "timed_out",
                        ":run_id": run_id,
                        ":now": completed_at,
                        ":error": "analysis_deadline_exceeded",
                        ":gsi1pk": f"ANALYSIS_TASK#{run.customer_id}#timed_out",
                    },
                )
                changed += 1
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                continue
        return changed

    def complete_analysis_run(
        self,
        run_id: str,
        completed_at: str,
        pipeline_status: str,
        *,
        metrics: dict[str, int] | None = None,
    ) -> None:
        metrics = metrics or {}
        self.table.update_item(
            Key={"pk": f"ANALYSIS_RUN#{run_id}", "sk": "META"},
            UpdateExpression=(
                "SET #status = :completed, completed_at = :now, "
                "pipeline_status = :pipeline_status, updated_at = :now, expires_at = :expires_at, "
                "tasks_completed = :tasks_completed, tasks_timed_out = :tasks_timed_out, "
                "model_calls = :model_calls, input_tokens = :input_tokens, "
                "output_tokens = :output_tokens, actual_cost_microusd = :actual_cost"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":completed": "completed",
                ":now": completed_at,
                ":pipeline_status": pipeline_status,
                ":expires_at": int((datetime.now(UTC) + timedelta(days=365)).timestamp()),
                ":tasks_completed": int(metrics.get("tasks_completed", 0)),
                ":tasks_timed_out": int(metrics.get("tasks_timed_out", 0)),
                ":model_calls": int(metrics.get("model_calls", 0)),
                ":input_tokens": int(metrics.get("input_tokens", 0)),
                ":output_tokens": int(metrics.get("output_tokens", 0)),
                ":actual_cost": int(metrics.get("actual_cost_microusd", 0)),
            },
        )

    def analysis_history_initialized(self) -> bool:
        response = self.table.get_item(
            Key={"pk": "SYSTEM#TELEMETRY", "sk": "ANALYSIS_HISTORY"},
            ConsistentRead=True,
        )
        return "Item" in response

    def mark_analysis_history_initialized(self, initialized_at: str) -> None:
        self.table.put_item(
            Item={
                "pk": "SYSTEM#TELEMETRY",
                "sk": "ANALYSIS_HISTORY",
                "entity_type": "telemetry_state",
                "initialized_at": initialized_at,
            }
        )

    def claim_once(self, key: str) -> bool:
        try:
            self.table.put_item(
                Item={
                    "pk": f"CLAIM#{key}",
                    "sk": "LOCK",
                    "entity_type": "claim",
                    "created_at": datetime.now(UTC).isoformat(),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def release_claim(self, key: str) -> None:
        self.table.delete_item(Key={"pk": f"CLAIM#{key}", "sk": "LOCK"})

    def begin_ingestion_run(self, run: IngestionRun, tasks: list[IngestionTask]) -> bool:
        created = True
        now = datetime.now(UTC).isoformat()
        try:
            self.table.put_item(
                Item={
                    "pk": f"INGESTION#{run.run_id}",
                    "sk": "META",
                    "entity_type": "ingestion_run",
                    "gsi1pk": "ENTITY#ingestion_run",
                    "gsi1sk": f"{run.started_at}#{run.run_id}",
                    **run.to_dict(),
                    "updated_at": now,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            created = False
        for task in tasks:
            try:
                self.table.put_item(
                    Item={
                        "pk": f"INGESTION#{task.run_id}",
                        "sk": f"TASK#{task.source_key}",
                        "entity_type": "ingestion_task",
                        **task.to_dict(),
                        "updated_at": now,
                    },
                    ConditionExpression="attribute_not_exists(pk)",
                )
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                continue
        return created

    def get_ingestion_run(self, run_id: str) -> IngestionRun | None:
        response = self.table.get_item(
            Key={"pk": f"INGESTION#{run_id}", "sk": "META"}, ConsistentRead=True
        )
        item = response.get("Item")
        return self._ingestion_run_from_item(item) if item else None

    def list_ingestion_runs(self, limit: int = 10) -> list[IngestionRun]:
        limit = max(1, min(limit, 30))
        parameters = {"IndexName": "gsi1", "KeyConditionExpression": "gsi1pk = :entity",
                      "ExpressionAttributeValues": {":entity": "ENTITY#ingestion_run"},
                      "ScanIndexForward": False}
        items = []
        while len(items) < limit:
            response = self.table.query(**parameters, Limit=limit-len(items))
            items.extend(response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                break
            parameters["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return [self._ingestion_run_from_item(item) for item in items]

    def get_ingestion_task(self, run_id: str, source_key: str) -> IngestionTask | None:
        response = self.table.get_item(
            Key={"pk": f"INGESTION#{run_id}", "sk": f"TASK#{source_key}"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._ingestion_task_from_item(item) if item else None

    def list_ingestion_tasks(self, run_id: str) -> list[IngestionTask]:
        parameters = {"KeyConditionExpression": "pk = :pk AND begins_with(sk, :task)",
                      "ExpressionAttributeValues": {":pk": f"INGESTION#{run_id}", ":task": "TASK#"},
                      "ConsistentRead": True}
        items = []
        while True:
            response = self.table.query(**parameters)
            items.extend(response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                break
            parameters["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return [self._ingestion_task_from_item(item) for item in items]

    def start_ingestion_task_attempt(self, run_id: str, source_key: str) -> bool:
        try:
            self.table.update_item(
                Key={"pk": f"INGESTION#{run_id}", "sk": f"TASK#{source_key}"},
                UpdateExpression="SET updated_at = :now ADD attempts :one",
                ConditionExpression="#status = :queued",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":queued": "queued",
                    ":now": datetime.now(UTC).isoformat(),
                    ":one": 1,
                },
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def record_ingestion_task_error(self, run_id: str, source_key: str, error: str) -> None:
        try:
            self.table.update_item(
                Key={"pk": f"INGESTION#{run_id}", "sk": f"TASK#{source_key}"},
                UpdateExpression="SET last_error = :error, updated_at = :now",
                ConditionExpression="#status = :queued",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":queued": "queued",
                    ":error": error[:1000],
                    ":now": datetime.now(UTC).isoformat(),
                },
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return

    def complete_ingestion_task(
        self, run_id: str, source_key: str, artifact_key: str, diagnostic: dict[str, Any]
    ) -> None:
        try:
            self.table.update_item(
                Key={"pk": f"INGESTION#{run_id}", "sk": f"TASK#{source_key}"},
                UpdateExpression=(
                    "SET #status = :completed, artifact_key = :artifact_key, "
                    "diagnostic = :diagnostic, completed_at = :completed_at, "
                    "updated_at = :completed_at REMOVE last_error"
                ),
                ConditionExpression="#status = :queued",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":queued": "queued",
                    ":completed": "completed",
                    ":artifact_key": artifact_key,
                    ":diagnostic": self._encode(diagnostic),
                    ":completed_at": str(diagnostic["completed_at"]),
                },
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return

    def time_out_pending_ingestion_tasks(self, run_id: str, completed_at: str) -> int:
        changed = 0
        for task in self.list_ingestion_tasks(run_id):
            if task.terminal:
                continue
            try:
                self.table.update_item(
                    Key={"pk": f"INGESTION#{run_id}", "sk": f"TASK#{task.source_key}"},
                    UpdateExpression="SET #status = :timed_out, completed_at = :now, updated_at = :now",
                    ConditionExpression="#status = :queued",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":queued": "queued",
                        ":timed_out": "timed_out",
                        ":now": completed_at,
                    },
                )
                changed += 1
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                continue
        return changed

    def complete_ingestion_run(
        self, run_id: str, completed_at: str, pipeline_status: str
    ) -> None:
        self.table.update_item(
            Key={"pk": f"INGESTION#{run_id}", "sk": "META"},
            UpdateExpression=(
                "SET #status = :completed, completed_at = :now, "
                "pipeline_status = :pipeline_status, updated_at = :now"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":completed": "completed",
                ":now": completed_at,
                ":pipeline_status": pipeline_status,
            },
        )

    @staticmethod
    def _analysis_task_from_item(item: dict[str, Any]) -> AnalysisTask:
        payload = json.loads(item["payload"])
        payload.update(
            {
                "status": str(item.get("status", payload.get("status", "queued"))),
                "last_seen_at": str(item.get("last_seen_at", payload["last_seen_at"])),
                "selected_run_id": str(item["selected_run_id"])
                if item.get("selected_run_id")
                else None,
                "selection_reason": str(item["selection_reason"])
                if item.get("selection_reason")
                else payload.get("selection_reason"),
                "dispatched_at": str(item["dispatched_at"])
                if item.get("dispatched_at")
                else payload.get("dispatched_at"),
                "attempts": int(item.get("attempts", payload.get("attempts", 0))),
                "last_attempt_at": str(item["last_attempt_at"])
                if item.get("last_attempt_at")
                else None,
                "lease_expires_at": str(item["lease_expires_at"])
                if item.get("lease_expires_at")
                else None,
                "completed_at": str(item["completed_at"])
                if item.get("completed_at")
                else None,
                "last_error": str(item["last_error"]) if item.get("last_error") else None,
                "result_source": str(item["result_source"])
                if item.get("result_source")
                else None,
                "model_calls": int(item.get("model_calls", payload.get("model_calls", 0))),
                "input_tokens": int(item.get("input_tokens", payload.get("input_tokens", 0))),
                "output_tokens": int(item.get("output_tokens", payload.get("output_tokens", 0))),
            }
        )
        return AnalysisTask.from_dict(payload)

    @staticmethod
    def _reed_hydration_task_from_item(item: dict[str, Any]) -> ReedHydrationTask:
        payload = json.loads(item["payload"])
        payload.update(
            {
                "status": str(item.get("status", payload.get("status", "queued"))),
                "attempts": int(item.get("attempts", payload.get("attempts", 0))),
                "last_dispatched_at": item.get("last_dispatched_at") or None,
                "last_attempt_at": item.get("last_attempt_at") or None,
                "next_attempt_at": item.get("next_attempt_at") or None,
                "lease_expires_at": item.get("lease_expires_at") or None,
                "completed_at": item.get("completed_at") or None,
                "last_error": item.get("last_error") or None,
                "ready_status": str(
                    item.get("ready_status", payload.get("ready_status", "pending"))
                ),
                "ready_sent_at": item.get("ready_sent_at") or None,
                "ready_processed_at": item.get("ready_processed_at") or None,
                "analysis_task_id": item.get("analysis_task_id") or None,
                "gate_reasons": list(item.get("gate_reasons", payload.get("gate_reasons", []))),
            }
        )
        return ReedHydrationTask.from_dict(payload)

    @staticmethod
    def _analysis_run_from_item(item: dict[str, Any]) -> AnalysisRun:
        selected = item.get("selected_task_ids", '{"ids":[]}')
        selected_task_ids = json.loads(selected).get("ids", []) if isinstance(selected, str) else []
        return AnalysisRun(
            run_id=str(item["run_id"]),
            customer_id=str(item["customer_id"]),
            profile_version=str(item["profile_version"]),
            started_at=str(item["started_at"]),
            prepared_at=str(item["prepared_at"]),
            deadline_at=str(item["deadline_at"]),
            candidate_artifact_key=str(item["candidate_artifact_key"]),
            selected_task_ids=[str(task_id) for task_id in selected_task_ids],
            expected_tasks=int(item["expected_tasks"]),
            send_email=bool(item["send_email"]),
            backlog_size=int(item["backlog_size"]),
            oldest_queued_at=str(item["oldest_queued_at"])
            if item.get("oldest_queued_at")
            else None,
            estimated_input_tokens=int(item.get("estimated_input_tokens", 0)),
            run_type=str(item.get("run_type", "pipeline")),
            trigger=str(item.get("trigger", "pipeline")),
            daily_target_calls=int(item.get("daily_target_calls", 0)),
            daily_call_limit=int(item.get("daily_call_limit", 0)),
            daily_input_token_limit=int(item.get("daily_input_token_limit", 0)),
            daily_output_token_limit=int(item.get("daily_output_token_limit", 0)),
            daily_cost_limit_microusd=int(
                item.get("daily_cost_limit_microusd", 0)
            ),
            estimated_output_tokens=int(item.get("estimated_output_tokens", 0)),
            estimated_cost_microusd=int(item.get("estimated_cost_microusd", 0)),
            oldest_queue_age_seconds=int(item.get("oldest_queue_age_seconds", 0)),
            tasks_completed=int(item.get("tasks_completed", 0)),
            tasks_timed_out=int(item.get("tasks_timed_out", 0)),
            model_calls=int(item.get("model_calls", 0)),
            input_tokens=int(item.get("input_tokens", 0)),
            output_tokens=int(item.get("output_tokens", 0)),
            actual_cost_microusd=int(item.get("actual_cost_microusd", 0)),
            status=str(item.get("status", "waiting")),
            completed_at=str(item["completed_at"]) if item.get("completed_at") else None,
            pipeline_status=str(item["pipeline_status"])
            if item.get("pipeline_status")
            else None,
        )

    @staticmethod
    def _ingestion_run_from_item(item: dict[str, Any]) -> IngestionRun:
        return IngestionRun(
            run_id=str(item["run_id"]),
            started_at=str(item["started_at"]),
            deadline_at=str(item["deadline_at"]),
            expected_tasks=int(item["expected_tasks"]),
            send_email=bool(item["send_email"]),
            status=str(item.get("status", "waiting")),
            completed_at=str(item["completed_at"]) if item.get("completed_at") else None,
            pipeline_status=str(item["pipeline_status"]) if item.get("pipeline_status") else None,
        )

    @staticmethod
    def _ingestion_task_from_item(item: dict[str, Any]) -> IngestionTask:
        diagnostic = json.loads(item["diagnostic"]) if item.get("diagnostic") else None
        return IngestionTask(
            run_id=str(item["run_id"]),
            source_key=str(item["source_key"]),
            employer=str(item["employer"]),
            ats=str(item["ats"]),
            tenant=str(item["tenant"]) if item.get("tenant") else None,
            status=str(item.get("status", "queued")),
            attempts=int(item.get("attempts", 0)),
            artifact_key=str(item["artifact_key"]) if item.get("artifact_key") else None,
            diagnostic=diagnostic,
            last_error=str(item["last_error"]) if item.get("last_error") else None,
            completed_at=str(item["completed_at"]) if item.get("completed_at") else None,
        )
