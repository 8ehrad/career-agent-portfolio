from __future__ import annotations

import re
from datetime import UTC, datetime

from .models import CoverageSample, Job
from .normalization import canonical_company, canonical_title
from .repository import Repository


TITLE_STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "in",
    "of",
    "the",
    "uk",
    "remote",
}

LOCATION_STOP_WORDS = {
    "area",
    "gb",
    "great",
    "britain",
    "england",
    "hybrid",
    "kingdom",
    "northern",
    "onsite",
    "remote",
    "uk",
    "united",
}

UK_LOCATION_TERMS = {
    "birmingham",
    "bristol",
    "cambridge",
    "cardiff",
    "edinburgh",
    "england",
    "glasgow",
    "great britain",
    "leeds",
    "liverpool",
    "london",
    "manchester",
    "northern ireland",
    "oxford",
    "scotland",
    "sheffield",
    "united kingdom",
    "wales",
}


def evaluate_coverage_samples(
    repository: Repository, jobs: list[Job], observed_at: datetime
) -> dict[str, int | float]:
    samples = repository.list_coverage_samples()
    for sample in samples:
        if sample.status in {"matched_within_24h", "matched_late"}:
            continue
        match = next((job for job in jobs if _same_vacancy(sample, job)), None)
        deadline = datetime.fromisoformat(sample.deadline_at)
        if match is not None:
            sample.first_matched_at = observed_at.isoformat()
            sample.matched_job_id = match.id
            sample.matched_source = match.source
            sample.matched_source_key = match.source_key
            sample.status = "matched_within_24h" if observed_at <= deadline else "matched_late"
            repository.put_coverage_sample(sample)
        elif observed_at > deadline and sample.status != "missed":
            sample.status = "missed"
            repository.put_coverage_sample(sample)
    return coverage_summary(repository.list_coverage_samples())


def coverage_summary(samples: list[CoverageSample]) -> dict[str, int | float]:
    counts = {status: sum(sample.status == status for sample in samples) for status in (
        "pending", "matched_within_24h", "matched_late", "missed"
    )}
    due = counts["matched_within_24h"] + counts["matched_late"] + counts["missed"]
    return {
        "samples": len(samples),
        **counts,
        "resolved_samples": due,
        "recall_within_24h_percent": round(counts["matched_within_24h"] / due * 100, 1) if due else 0.0,
    }


def _same_vacancy(sample: CoverageSample, job: Job) -> bool:
    sample_company = canonical_company(sample.company)
    job_company = canonical_company(job.company)
    if not sample_company or not job_company:
        return False
    company_match = (
        sample_company == job_company
        or sample_company in job_company
        or job_company in sample_company
    )
    if not company_match:
        return False
    sample_title = _title_tokens(sample.title)
    job_title = _title_tokens(job.title)
    if not sample_title or not job_title:
        return False
    overlap = len(sample_title & job_title) / len(sample_title | job_title)
    return overlap >= 0.7 and _locations_compatible(sample.location, job.location)


def _title_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9+]+", canonical_title(value))
        if token not in TITLE_STOP_WORDS
    }


def _locations_compatible(sample_location: str, job_location: str) -> bool:
    """Avoid conflating same-title openings in different cities when location is known."""
    if not sample_location.strip() or not job_location.strip():
        return True
    sample = _normalise_location(sample_location)
    job = _normalise_location(job_location)
    if sample == job or sample in job or job in sample:
        return True

    sample_specific = _location_tokens(sample)
    job_specific = _location_tokens(job)
    if sample_specific and job_specific:
        return bool(sample_specific & job_specific)

    # A country-level UK value is compatible with a UK city, but not an explicit
    # foreign location. Unknown/generic remote values stay plausible.
    if _is_uk_location(sample) and _is_explicit_foreign_location(job):
        return False
    if _is_uk_location(job) and _is_explicit_foreign_location(sample):
        return False
    return True


def _normalise_location(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _location_tokens(value: str) -> set[str]:
    return {
        token
        for token in value.split()
        if token not in LOCATION_STOP_WORDS and len(token) > 1
    }


def _is_uk_location(value: str) -> bool:
    return value == "uk" or any(term in value for term in UK_LOCATION_TERMS)


def _is_explicit_foreign_location(value: str) -> bool:
    return any(
        term in value
        for term in (
            "amsterdam",
            "australia",
            "berlin",
            "canada",
            "dublin",
            "france",
            "germany",
            "india",
            "ireland",
            "madrid",
            "netherlands",
            "new york",
            "paris",
            "san francisco",
            "singapore",
            "spain",
            "sydney",
            "toronto",
            "united states",
            "usa",
        )
    )
