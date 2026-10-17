from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    RECOMMENDED = "recommended"
    SHORTLISTED = "shortlisted"
    PREPARING = "preparing"
    READY = "ready_to_apply"
    APPLIED = "applied"
    RECRUITER_RESPONSE = "recruiter_response"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    DISMISSED = "dismissed"


@dataclass(slots=True)
class ApplicationTask:
    """One durable preparation request for a particular job and pack identity."""

    customer_id: str
    job_id: str
    task_id: str
    identity: str
    status: str
    created_at: str
    updated_at: str
    previous_status: str
    attempts: int = 0
    recovery_dispatches: int = 0
    lease_expires_at: str | None = None
    completed_at: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ApplicationTask":
        return cls(**value)


class SalaryConfidence(StrEnum):
    ADVERTISED = "advertised"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    """Evidence state for a gate where missing information is not a rejection."""

    CONFIRMED_PASS = "confirmed_pass"
    UNVERIFIED_BUT_PLAUSIBLE = "unverified_but_plausible"
    CONFIRMED_FAIL = "confirmed_fail"


@dataclass(slots=True)
class SalaryEvidence:
    minimum_gbp: int | None = None
    maximum_gbp: int | None = None
    currency: str = "GBP"
    period: str = "year"
    confidence: SalaryConfidence = SalaryConfidence.UNKNOWN
    source_url: str | None = None
    source_label: str | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    inferred: bool = False
    method_version: int = 1

    @property
    def midpoint_gbp(self) -> int | None:
        if self.minimum_gbp is not None and self.maximum_gbp is not None:
            return round((self.minimum_gbp + self.maximum_gbp) / 2)
        return self.minimum_gbp or self.maximum_gbp


@dataclass(slots=True)
class ScoreBreakdown:
    attainability: float = 0
    career_gain: float = 0
    remuneration: float = 0
    location: float = 0
    overall: float = 0
    hard_gate_passed: bool = False


@dataclass(slots=True)
class JobListing:
    """A source-specific appearance of a canonical vacancy."""

    source: str
    source_id: str
    source_url: str
    apply_url: str
    first_seen_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_seen_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload_hash: str | None = None
    source_key: str | None = None
    freshness_evidence: dict[str, Any] | None = None


@dataclass(slots=True)
class JobAnalysisHistory:
    """Durable novelty and stage history for one canonical vacancy."""

    job_id: str
    first_seen_at: str
    last_seen_at: str
    seen_runs: int = 0
    last_seen_run_id: str | None = None
    sources: list[str] = field(default_factory=list)
    first_fast_analyzed_at: str | None = None
    last_fast_analyzed_at: str | None = None
    fast_analysis_runs: int = 0
    last_fast_analysis_run_id: str | None = None
    first_deep_analyzed_at: str | None = None
    last_deep_analyzed_at: str | None = None
    deep_analysis_runs: int = 0
    last_deep_analysis_run_id: str | None = None
    last_deep_job_fingerprint: str | None = None
    last_deep_profile_fingerprint: str | None = None
    last_deep_analyzer_version: str | None = None
    last_deep_model_id: str | None = None
    first_qualified_at: str | None = None
    last_qualified_at: str | None = None
    qualified_runs: int = 0
    last_qualified_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JobAnalysisHistory":
        return cls(**value)


@dataclass(slots=True)
class DeepAnalysisRecord:
    """A versioned, profile-specific deep-analysis result safe to reuse."""

    cache_key: str
    job_id: str
    job_fingerprint: str
    profile_fingerprint: str
    analyzer_version: str
    model_id: str
    analyzed_at: str
    requirements: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    matched_evidence_ids: list[str] = field(default_factory=list)
    match_reasons: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    company_context: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeepAnalysisRecord":
        return cls(**value)


@dataclass(slots=True)
class AnalysisTask:
    """Durable, customer-scoped work item for one exact analysis identity."""

    customer_id: str
    task_id: str
    cache_key: str
    job_id: str
    profile_version: str
    job_fingerprint: str
    profile_fingerprint: str
    analyzer_version: str
    model_id: str
    job: dict[str, Any]
    source: str
    source_key: str
    company: str
    role_family: str
    priority_score: float
    priority_components: dict[str, float]
    first_queued_at: str
    last_seen_at: str
    closes_at: str | None = None
    estimated_input_tokens: int = 0
    status: str = "queued"
    selection_reason: str | None = None
    selected_run_id: str | None = None
    dispatched_at: str | None = None
    attempts: int = 0
    last_attempt_at: str | None = None
    lease_expires_at: str | None = None
    completed_at: str | None = None
    last_error: str | None = None
    result_source: str | None = None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "timed_out", "expired", "cancelled"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnalysisTask":
        return cls(**value)


@dataclass(slots=True)
class ReedHydrationTask:
    """Durable work item for resolving one Reed search result into a full advert."""

    customer_id: str
    source_id: str
    summary: dict[str, Any]
    summary_fingerprint: str
    first_seen_at: str
    last_seen_at: str
    status: str = "queued"
    attempts: int = 0
    last_dispatched_at: str | None = None
    last_attempt_at: str | None = None
    next_attempt_at: str | None = None
    lease_expires_at: str | None = None
    completed_at: str | None = None
    last_error: str | None = None
    ready_status: str = "pending"
    ready_sent_at: str | None = None
    ready_processed_at: str | None = None
    analysis_task_id: str | None = None
    gate_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReedHydrationTask":
        return cls(**value)


@dataclass(slots=True)
class AnalysisBudgetUsage:
    """A customer-local daily guardrail and exact post-call usage ledger."""

    customer_id: str
    budget_date: str
    calls_committed: int = 0
    input_tokens_committed: int = 0
    output_tokens_committed: int = 0
    cost_microusd_committed: int = 0
    calls_actual: int = 0
    input_tokens_actual: int = 0
    output_tokens_actual: int = 0
    cost_microusd_actual: int = 0
    updated_at: str | None = None
    discovery_state: str | None = None  # reserved, released, or none

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnalysisBudgetUsage":
        return cls(**value)


@dataclass(slots=True)
class AnalysisRun:
    """Coordination record for an asynchronous deep-analysis batch."""

    run_id: str
    customer_id: str
    profile_version: str
    started_at: str
    prepared_at: str
    deadline_at: str
    candidate_artifact_key: str
    selected_task_ids: list[str]
    expected_tasks: int
    send_email: bool
    backlog_size: int
    oldest_queued_at: str | None = None
    estimated_input_tokens: int = 0
    run_type: str = "pipeline"
    trigger: str = "pipeline"
    daily_target_calls: int = 0
    daily_call_limit: int = 0
    daily_input_token_limit: int = 0
    daily_output_token_limit: int = 0
    daily_cost_limit_microusd: int = 0
    estimated_output_tokens: int = 0
    estimated_cost_microusd: int = 0
    oldest_queue_age_seconds: int = 0
    tasks_completed: int = 0
    tasks_timed_out: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    actual_cost_microusd: int = 0
    status: str = "waiting"
    completed_at: str | None = None
    pipeline_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnalysisRun":
        return cls(**value)


@dataclass(slots=True)
class Job:
    id: str
    source: str
    source_id: str
    title: str
    company: str
    location: str
    description: str
    apply_url: str
    source_url: str
    posted_at: str | None = None
    closes_at: str | None = None
    workplace: str = "unknown"
    employment_type: str = "full_time"
    salary: SalaryEvidence = field(default_factory=SalaryEvidence)
    salary_verification: VerificationStatus = VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE
    location_verification: VerificationStatus = VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE
    role_family: str | None = None
    source_key: str | None = None
    skills: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    matched_evidence_ids: list[str] = field(default_factory=list)
    match_reasons: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    company_context: dict[str, Any] = field(default_factory=dict)
    score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    status: JobStatus = JobStatus.RECOMMENDED
    first_seen_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_verified_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    recommendation_date: str | None = None
    dismissal_reason: str | None = None
    application_tracking: dict[str, Any] = field(default_factory=dict)
    listings: list[JobListing] = field(default_factory=list)
    # Original source description plus structured readable view; absent from legacy records.
    advert_document: dict[str, Any] | None = None
    freshness_evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["salary"]["confidence"] = self.salary.confidence.value
        value["salary_verification"] = self.salary_verification.value
        value["location_verification"] = self.location_verification.value
        if value["advert_document"] is None:
            value.pop("advert_document")
        if value["freshness_evidence"] is None:
            value.pop("freshness_evidence")
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Job":
        payload = dict(value)
        payload["salary"] = SalaryEvidence(
            **{**payload.get("salary", {}), "confidence": SalaryConfidence(payload.get("salary", {}).get("confidence", "unknown"))}
        )
        payload["score"] = ScoreBreakdown(**payload.get("score", {}))
        payload["status"] = JobStatus(payload.get("status", "recommended"))
        payload["salary_verification"] = VerificationStatus(
            payload.get("salary_verification", "unverified_but_plausible")
        )
        payload["location_verification"] = VerificationStatus(
            payload.get("location_verification", "unverified_but_plausible")
        )
        payload["listings"] = [JobListing(**item) for item in payload.get("listings", [])]
        return cls(**payload)


@dataclass(slots=True)
class ApplicationPack:
    job_id: str
    created_at: str
    profile_version: str
    cv_headline: str
    cv_summary: str
    selected_evidence_ids: list[str]
    tailored_bullets: list[str]
    cover_letter: str
    recruiter_message: str
    interview_topics: list[str]
    gaps_to_address: list[str]
    artifact_keys: dict[str, str] = field(default_factory=dict)
    generation_metadata: dict[str, Any] = field(default_factory=dict)
    rich_content: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunReport:
    run_id: str
    started_at: str
    completed_at: str | None = None
    discovered: int = 0
    unique: int = 0
    hard_filter_passed: int = 0
    deeply_analyzed: int = 0
    salary_researched: int = 0
    recommended: int = 0
    digest_count: int = 0
    emailed: bool = False
    source_counts: dict[str, int] = field(default_factory=dict)
    stage_counts: dict[str, int] = field(default_factory=dict)
    new_stage_counts: dict[str, int] = field(default_factory=dict)
    source_metrics: dict[str, dict[str, int]] = field(default_factory=dict)
    registry_source_metrics: dict[str, dict[str, int]] = field(default_factory=dict)
    employer_metrics: dict[str, dict[str, int]] = field(default_factory=dict)
    role_family_metrics: dict[str, dict[str, int]] = field(default_factory=dict)
    source_fetches: list[dict[str, Any]] = field(default_factory=list)
    coverage_metrics: dict[str, int | float] = field(default_factory=dict)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    timings_ms: dict[str, int] = field(default_factory=dict)
    model_usage: dict[str, int] = field(default_factory=dict)
    telemetry_schema_version: int = 4
    newness_scope: str = "exact"
    configuration: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    last_completed_stage: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SourceHealth:
    source_key: str
    source: str
    ats: str
    employer: str | None
    status: str
    last_attempt_at: str
    tenant: str | None = None
    last_successful_fetch_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    listings_fetched: int = 0
    relevant_jobs_produced: int = 0
    qualified_jobs: int = 0
    conversion_basis: str = "latest_run"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceHealth":
        return cls(**value)


@dataclass(slots=True)
class IngestionRun:
    """Durable coordination record for one asynchronous source fan-out."""

    run_id: str
    started_at: str
    deadline_at: str
    expected_tasks: int
    send_email: bool
    status: str = "waiting"
    completed_at: str | None = None
    pipeline_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IngestionRun":
        return cls(**value)


@dataclass(slots=True)
class IngestionTask:
    """One employer fetch within an asynchronous ingestion run."""

    run_id: str
    source_key: str
    employer: str
    ats: str
    tenant: str | None
    status: str = "queued"
    attempts: int = 0
    artifact_key: str | None = None
    diagnostic: dict[str, Any] | None = None
    last_error: str | None = None
    completed_at: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "timed_out"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IngestionTask":
        return cls(**value)


@dataclass(slots=True)
class CoverageSample:
    """A vacancy entered by a human for recall measurement; never scraped from LinkedIn."""

    sample_id: str
    observed_at: str
    deadline_at: str
    title: str
    company: str
    location: str = ""
    reference_url: str | None = None
    status: str = "pending"
    first_matched_at: str | None = None
    matched_job_id: str | None = None
    matched_source: str | None = None
    matched_source_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CoverageSample":
        return cls(**value)
