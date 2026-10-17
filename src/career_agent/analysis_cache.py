from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import DeepAnalysisRecord, Job, JobAnalysisHistory
from .normalization import canonical_company, canonical_title
from .profile import CandidateProfile


DEFAULT_ANALYZER_VERSION = "job-fit-v1"
DEEP_SELECTION_POLICY_VERSION = 2
PRIORITY_PERCENT = 70
DIVERSITY_PERCENT = 20
PRIORITY_FIT_WEIGHT = 0.65
PRIORITY_URGENCY_WEIGHT = 0.15
PRIORITY_WAIT_AGE_WEIGHT = 0.10
PRIORITY_NOVELTY_WEIGHT = 0.10


@dataclass(frozen=True, slots=True)
class AnalysisIdentity:
    cache_key: str
    job_fingerprint: str
    profile_fingerprint: str
    analyzer_version: str
    model_id: str


@dataclass(slots=True)
class DeepSelection:
    jobs: list[Job]
    reasons: dict[str, str]
    priorities: dict[str, dict[str, float]]


def analysis_identity(job: Job, profile: CandidateProfile, analyzer: Any) -> AnalysisIdentity:
    rich_inputs = {}
    if getattr(analyzer, "manages_budget", False):
        # Extraction consumes preserved source text and completeness provenance.
        rich_inputs = {"source": job.source, "advert_document": job.advert_document}
    job_fingerprint = _fingerprint(
        {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": job.description,
            **rich_inputs,
        }
    )
    profile_fingerprint = _fingerprint(profile.compact_for_model())
    analyzer_version = str(getattr(analyzer, "analysis_version", DEFAULT_ANALYZER_VERSION))
    model_id = str(getattr(analyzer, "model_id", type(analyzer).__name__))
    cache_key = _fingerprint(
        {
            "job_id": job.id,
            "job_fingerprint": job_fingerprint,
            "profile_fingerprint": profile_fingerprint,
            "analyzer_version": analyzer_version,
            "model_id": model_id,
        }
    )
    return AnalysisIdentity(
        cache_key=cache_key,
        job_fingerprint=job_fingerprint,
        profile_fingerprint=profile_fingerprint,
        analyzer_version=analyzer_version,
        model_id=model_id,
    )


def record_from_job(
    job: Job,
    identity: AnalysisIdentity,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> DeepAnalysisRecord:
    return DeepAnalysisRecord(
        cache_key=identity.cache_key,
        job_id=job.id,
        job_fingerprint=identity.job_fingerprint,
        profile_fingerprint=identity.profile_fingerprint,
        analyzer_version=identity.analyzer_version,
        model_id=identity.model_id,
        analyzed_at=datetime.now(UTC).isoformat(),
        requirements=list(job.requirements),
        skills=list(job.skills),
        matched_evidence_ids=list(job.matched_evidence_ids),
        match_reasons=list(job.match_reasons),
        gaps=list(job.gaps),
        red_flags=list(job.red_flags),
        company_context=dict(job.company_context),
        input_tokens=max(0, input_tokens),
        output_tokens=max(0, output_tokens),
    )


def apply_record(job: Job, record: DeepAnalysisRecord) -> Job:
    job.requirements = list(record.requirements)
    job.skills = sorted(set(job.skills) | set(record.skills))
    job.matched_evidence_ids = list(record.matched_evidence_ids)
    job.match_reasons = list(record.match_reasons)
    job.gaps = list(record.gaps)
    job.red_flags = list(record.red_flags)
    job.company_context.update(record.company_context)
    return job


def analysis_state(identity: AnalysisIdentity, history: JobAnalysisHistory | None) -> str:
    if history is None or history.deep_analysis_runs == 0:
        return "never_analyzed"
    fingerprints = (
        history.last_deep_job_fingerprint,
        history.last_deep_profile_fingerprint,
        history.last_deep_analyzer_version,
        history.last_deep_model_id,
    )
    if all(value is not None for value in fingerprints) and fingerprints != (
        identity.job_fingerprint,
        identity.profile_fingerprint,
        identity.analyzer_version,
        identity.model_id,
    ):
        return "changed"
    return "legacy_uncached"


def select_fresh_candidates(
    candidates: list[Job],
    histories: dict[str, JobAnalysisHistory],
    identities: dict[str, AnalysisIdentity],
    limit: int,
    *,
    seed: str,
    now: datetime | None = None,
    first_queued_at_by_job_id: dict[str, str] | None = None,
) -> DeepSelection:
    """Allocate fresh calls across priority, diversity, and a deterministic audit sample."""
    budget = min(max(0, limit), len(candidates))
    if budget == 0:
        return DeepSelection([], {}, {})

    observed_at = now or datetime.now(UTC)
    first_queued_at_by_job_id = first_queued_at_by_job_id or {}
    state_order = {"changed": 0, "never_analyzed": 1, "legacy_uncached": 2}
    priorities = {
        job.id: analysis_priority_components(
            job,
            histories.get(job.id),
            identities[job.id],
            observed_at,
            first_queued_at=first_queued_at_by_job_id.get(job.id),
        )
        for job in candidates
    }
    ranked = sorted(
        candidates,
        key=lambda job: (
            -priorities[job.id]["priority_score"],
            state_order[analysis_state(identities[job.id], histories.get(job.id))],
            job.id,
        ),
    )
    exploit_count = max(1, budget * PRIORITY_PERCENT // 100)
    diversity_count = budget * DIVERSITY_PERCENT // 100
    selected = ranked[:exploit_count]
    reasons = {job.id: "priority" for job in selected}
    remaining = ranked[exploit_count:]

    for _ in range(min(diversity_count, len(remaining))):
        selected_source_types = {job.source for job in selected}
        selected_source_keys = {job.source_key or job.source for job in selected}
        selected_companies = {canonical_company(job.company) for job in selected}
        selected_roles = {job.role_family or canonical_title(job.title) for job in selected}

        def diversity_rank(job: Job) -> tuple[int, int, int, float, str]:
            dimensions = sum(
                (
                    (job.source_key or job.source) not in selected_source_keys,
                    canonical_company(job.company) not in selected_companies,
                    (job.role_family or canonical_title(job.title)) not in selected_roles,
                )
            )
            state = analysis_state(identities[job.id], histories.get(job.id))
            return (
                int(job.source not in selected_source_types),
                dimensions,
                -state_order[state],
                job.score.overall,
                job.id,
            )

        chosen = max(remaining, key=diversity_rank)
        remaining.remove(chosen)
        selected.append(chosen)
        reasons[chosen.id] = "diversity"

    audit_count = min(budget - len(selected), len(remaining))
    audited = sorted(remaining, key=lambda job: _fingerprint(f"{seed}:{job.id}"))[:audit_count]
    for job in audited:
        selected.append(job)
        reasons[job.id] = "audit"
    audited_ids = {job.id for job in audited}
    remaining = [job for job in remaining if job.id not in audited_ids]

    for job in remaining[: budget - len(selected)]:
        selected.append(job)
        reasons[job.id] = "priority"
    return DeepSelection(selected, reasons, priorities)


def analysis_priority_components(
    job: Job,
    history: JobAnalysisHistory | None,
    identity: AnalysisIdentity,
    now: datetime,
    *,
    first_queued_at: str | None = None,
) -> dict[str, float]:
    """Return an explainable 0-100 backlog priority and its component scores."""
    fit = _bounded(job.score.overall)
    urgency = _vacancy_urgency(job, now)
    wait_age = _wait_age(
        first_queued_at or (history.first_seen_at if history else job.first_seen_at), now
    )
    novelty = {
        "changed": 100.0,
        "never_analyzed": 90.0,
        "legacy_uncached": 40.0,
    }[analysis_state(identity, history)]
    priority = (
        fit * PRIORITY_FIT_WEIGHT
        + urgency * PRIORITY_URGENCY_WEIGHT
        + wait_age * PRIORITY_WAIT_AGE_WEIGHT
        + novelty * PRIORITY_NOVELTY_WEIGHT
    )
    return {
        "preliminary_fit": round(fit, 1),
        "vacancy_urgency": round(urgency, 1),
        "backlog_wait_age": round(wait_age, 1),
        "novelty": round(novelty, 1),
        "priority_score": round(priority, 2),
    }


def estimated_analysis_input_tokens(job: Job, profile: CandidateProfile) -> int:
    """Conservative pre-dispatch estimate used for backlog and budget telemetry."""
    payload_characters = len(job.description[:18_000]) + len(
        json.dumps(profile.compact_for_model(), separators=(",", ":"), ensure_ascii=False)
    )
    return max(500, round(payload_characters / 4) + 500)


def vacancy_is_closed(job: Job, now: datetime) -> bool:
    closes_at = _parse_datetime(job.closes_at)
    return closes_at is not None and closes_at < now


def _vacancy_urgency(job: Job, now: datetime) -> float:
    closes_at = _parse_datetime(job.closes_at)
    if closes_at is not None:
        days = (closes_at - now).total_seconds() / 86_400
        if days <= 2:
            return 100.0
        if days <= 7:
            return 85.0
        if days <= 14:
            return 65.0
        return 45.0
    posted_at = _parse_datetime(job.posted_at)
    if posted_at is None:
        return 40.0
    days = max(0.0, (now - posted_at).total_seconds() / 86_400)
    if days <= 2:
        return 90.0
    if days <= 7:
        return 75.0
    if days <= 14:
        return 60.0
    if days <= 30:
        return 45.0
    return 30.0


def _wait_age(first_seen_at: str, now: datetime) -> float:
    first_seen = _parse_datetime(first_seen_at)
    if first_seen is None:
        return 0.0
    days = max(0.0, (now - first_seen).total_seconds() / 86_400)
    return min(100.0, days / 30 * 100)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
