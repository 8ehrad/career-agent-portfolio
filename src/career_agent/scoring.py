from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Job, ScoreBreakdown, VerificationStatus
from .normalization import canonical_title
from .profile import CandidateProfile


ROLE_TERMS = (
    "data scientist",
    "applied scientist",
    "ai scientist",
    "ai engineer",
    "artificial intelligence engineer",
    "machine learning engineer",
    "ml engineer",
    "generative ai",
    "genai engineer",
    "llm engineer",
    "ml scientist",
    "decision scientist",
    "product data scientist",
    "data science consultant",
    "ai consultant",
    "artificial intelligence consultant",
    "nlp engineer",
    "nlp scientist",
    "natural language processing engineer",
    "natural language processing scientist",
    "quant developer",
    "quantitative developer",
    "applied ml engineer",
    "applied machine learning engineer",
    "forward deployed engineer",
    "quantitative analyst",
    "quantitative researcher",
)

CONDITIONAL_ROLE_TERMS: dict[str, tuple[str, ...]] = {
    "research scientist": (
        "machine learning",
        "artificial intelligence",
        "deep learning",
        "neural network",
        "large language model",
        " llm",
        "nlp",
        "computer vision",
        "reinforcement learning",
    ),
}

EXCLUDED_TITLE_TERMS = (
    "frontend",
    "front end",
    "mobile engineer",
    "site reliability",
    "engineering manager",
    "software engineering manager",
    "java engineer",
    ".net engineer",
)

TOO_SENIOR_TITLE_TERMS = (
    "principal",
    "staff ",
    "head of",
    "director",
    "vice president",
    "vp ",
)

UK_LOCATION_TERMS = (
    "united kingdom",
    "uk",
    "england",
    "scotland",
    "wales",
    "northern ireland",
    "london",
    "manchester",
    "leeds",
    "liverpool",
    "birmingham",
    "bristol",
    "edinburgh",
    "glasgow",
    "cambridge",
    "oxford",
)


@dataclass(slots=True)
class FilterDecision:
    passed: bool
    reasons: list[str]


def role_family(job: Job) -> str | None:
    title = canonical_title(job.title)
    for term in ROLE_TERMS:
        if term in title:
            return term
    evidence = f" {title} {job.description.lower()} "
    for family, evidence_terms in CONDITIONAL_ROLE_TERMS.items():
        if family in title and any(term in evidence for term in evidence_terms):
            return family
    return None


def _required_years(text: str) -> int | None:
    matches = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)(?:\s+of)?\s+(?:relevant\s+)?experience", text.lower())
    return max((int(value) for value in matches), default=None)


def location_verification(job: Job) -> VerificationStatus:
    location = job.location.lower()
    if any(re.search(rf"\b{re.escape(term)}\b", location) for term in UK_LOCATION_TERMS):
        return VerificationStatus.CONFIRMED_PASS
    body = f" {location} {job.description.lower()} "
    foreign_only_patterns = (
        r"\b(?:united states|usa|u\.s\.|canada|australia|india|singapore)\s+only\b",
        r"\bmust be based in (?:the )?(?:united states|usa|u\.s\.|canada|australia|india|singapore)\b",
        r"\b(?:eu|europe|emea) excluding (?:the )?(?:uk|united kingdom)\b",
        r"\beu only\b",
    )
    if any(re.search(pattern, body) for pattern in foreign_only_patterns):
        return VerificationStatus.CONFIRMED_FAIL
    explicit_foreign_location = any(
        re.search(rf"\b{re.escape(term)}\b", location)
        for term in (
            "united states",
            "new york",
            "san francisco",
            "canada",
            "toronto",
            "australia",
            "sydney",
            "india",
            "singapore",
            "france",
            "paris",
            "germany",
            "berlin",
            "netherlands",
            "amsterdam",
            "spain",
            "madrid",
            "italy",
            "dublin",
            "ireland",
            "austria",
            "belgium",
            "bulgaria",
            "croatia",
            "cyprus",
            "czechia",
            "czech republic",
            "denmark",
            "estonia",
            "finland",
            "greece",
            "hungary",
            "latvia",
            "lithuania",
            "luxembourg",
            "malta",
            "norway",
            "poland",
            "portugal",
            "romania",
            "slovakia",
            "slovenia",
            "sweden",
            "switzerland",
            "iceland",
            "united arab emirates",
            "saudi arabia",
            "japan",
            "china",
            "brazil",
            "mexico",
            "south africa",
            "new zealand",
        )
    ) or bool(
        re.search(
            r"(?:^|[,/| ])(?:us|usa|ca|de|fr|nl|es|it|ie|at|be|dk|fi|no|pl|pt|se|ch|ae|au|nz)(?:$|[,/| ])",
            location,
        )
    )
    if explicit_foreign_location:
        return VerificationStatus.CONFIRMED_FAIL
    confirmed_uk_remote = any(
        phrase in body
        for phrase in (
            "remote within the uk",
            "remote within uk",
            "uk remote",
            "uk-based remote",
            "must be based in the uk",
            "must be based in uk",
            "based anywhere in the uk",
            "based anywhere in uk",
        )
    )
    if confirmed_uk_remote:
        return VerificationStatus.CONFIRMED_PASS
    return VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE


def salary_verification(job: Job, profile: CandidateProfile) -> VerificationStatus:
    minimum = job.salary.minimum_gbp
    maximum = job.salary.maximum_gbp
    if minimum is None and maximum is None:
        return VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE
    if job.salary.inferred:
        return VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE
    floor = profile.preferences["exceptional_move_floor_gbp"]
    ceiling = maximum if maximum is not None else minimum
    floor_of_range = minimum if minimum is not None else maximum
    if ceiling is not None and ceiling < floor:
        return VerificationStatus.CONFIRMED_FAIL
    if floor_of_range is not None and floor_of_range >= floor:
        return VerificationStatus.CONFIRMED_PASS
    return VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE


def _has_critical_fit_conflict(job: Job) -> bool:
    assessment = " ".join(job.red_flags).lower()
    return any(
        phrase in assessment
        for phrase in (
            "fundamental mismatch",
            "significant domain mismatch",
            "core stated requirement",
            "entirely absent",
            "zero evidence",
        )
    )


def hard_filter(job: Job, profile: CandidateProfile) -> FilterDecision:
    reasons: list[str] = []
    title = canonical_title(job.title)
    body = f" {job.location} {job.description} ".lower()

    job.role_family = role_family(job)
    if job.role_family is None:
        reasons.append("Role title is outside the configured data/AI families")
    if any(term in title for term in EXCLUDED_TITLE_TERMS):
        reasons.append("Role is in an explicitly excluded engineering family")
    if any(term in title for term in TOO_SENIOR_TITLE_TERMS):
        reasons.append("Role is more than one realistic seniority step above the candidate")
    job.location_verification = location_verification(job)
    if job.location_verification == VerificationStatus.CONFIRMED_FAIL:
        reasons.append("Role is not clearly UK based")

    required_years = _required_years(job.description)
    experience = float(profile.data.get("years_experience", profile.data.get("facts", {}).get("years_experience", 0)))
    if required_years is not None and required_years >= 7:
        reasons.append(f"Requires {required_years}+ years of experience")
    if any(term in body for term in ("manage a team of", "line management responsibility", "director of")):
        reasons.append("Requires substantial people-management experience")

    job.salary_verification = salary_verification(job, profile)
    salary = job.salary.midpoint_gbp
    if job.salary_verification == VerificationStatus.CONFIRMED_FAIL:
        ceiling = job.salary.maximum_gbp or job.salary.minimum_gbp or salary
        reasons.append(
            f"Advertised salary ceiling £{ceiling:,} is below the "
            f"£{profile.preferences['exceptional_move_floor_gbp']:,} hard floor"
        )

    return FilterDecision(not reasons, reasons)


def _skill_score(job: Job, profile: CandidateProfile) -> tuple[float, list[str]]:
    job_skills = {skill.lower() for skill in job.skills}
    matched = sorted(job_skills & profile.all_skills)
    if not job_skills:
        return 55.0, []
    coverage = len(matched) / len(job_skills)
    return min(100.0, 45 + coverage * 55), matched


def _career_gain(job: Job) -> float:
    title = canonical_title(job.title)
    body = job.description.lower()
    score = 52.0
    if "senior" in title:
        score += 18
    if any(term in title for term in ("applied", "ai engineer", "generative ai", "machine learning")):
        score += 10
    if any(term in body for term in ("ownership", "end-to-end", "end to end", "production", "deploy")):
        score += 8
    if any(term in body for term in ("mentor", "technical leadership", "lead projects")):
        score += 5
    return min(100.0, score)


def _remuneration(job: Job, profile: CandidateProfile) -> float:
    midpoint = job.salary.midpoint_gbp
    if midpoint is None:
        return 45.0
    floor = profile.preferences["exceptional_move_floor_gbp"]
    target = profile.preferences["target_salary_gbp"]
    verification = salary_verification(job, profile)
    if verification == VerificationStatus.UNVERIFIED_BUT_PLAUSIBLE and midpoint < floor:
        return 30.0
    if midpoint < floor:
        return 0.0
    if midpoint < target:
        return 45.0 + (midpoint - floor) / (target - floor) * 15
    return min(100.0, 60 + (midpoint - target) / 500)


def _location(job: Job, profile: CandidateProfile) -> float:
    location = job.location.lower()
    preferred = str(profile.preferences.get("preferred_city", "")).strip().lower()
    if preferred and preferred in location and job.workplace == "hybrid":
        return 100.0
    if job.workplace == "remote":
        return 90.0
    if preferred and preferred in location:
        return 85.0
    if job.workplace == "hybrid":
        return 65.0
    if job.salary.midpoint_gbp and job.salary.midpoint_gbp >= profile.preferences["relocation_floor_gbp"]:
        return 60.0
    return 40.0


def score_job(job: Job, profile: CandidateProfile) -> Job:
    decision = hard_filter(job, profile)
    if not decision.passed:
        job.red_flags = sorted(set(job.red_flags + decision.reasons))
        job.score = ScoreBreakdown(hard_gate_passed=False)
        return job

    skill_score, matched_skills = _skill_score(job, profile)
    required_years = _required_years(job.description)
    attainability = skill_score
    if required_years is not None:
        if required_years <= 4:
            attainability += 5
        elif required_years == 5:
            attainability -= 8
        elif required_years == 6:
            attainability -= 18
    if "phd required" in job.description.lower() or "ph.d. required" in job.description.lower():
        attainability -= 35
    if _has_critical_fit_conflict(job):
        attainability = min(attainability, 55)
    rich = job.company_context.get("rich_matching")
    if rich and rich.get("version") == "rich-job-fit-v1":
        from .requirements import recommendation_requirement, recommendation_extraction_review
        assessment = rich["assessment"]
        essential = [r for r in assessment["assessments"] if r["priority"] == "essential"
                     and recommendation_requirement(r)]
        support = {"supported": 1.0, "transferable": 0.5, "unknown": 0.0, "confirmed_gap": 0.0}
        coverage = sum(support[r["status"]] for r in essential) / len(essential) if essential else 0.0
        attainability = 45 + 55 * coverage
        if (any(r["status"] == "confirmed_gap" or r.get("id") in assessment["essential_gap_ids"]
                for r in essential) or not essential
                or recommendation_extraction_review(rich["requirement_document"])):
            attainability = min(attainability, 55)
    attainability = max(0.0, min(100.0, attainability))

    career_gain = _career_gain(job)
    if rich and rich.get("version") == "rich-job-fit-v1" and any(
            term in job.description.lower() for term in ("mentor", "technical leadership", "lead projects")):
        career_gain -= 5  # No behavioural/mentoring bonus in rich recommendations.
    remuneration = _remuneration(job, profile)
    location = _location(job, profile)
    overall = attainability * 0.35 + career_gain * 0.30 + remuneration * 0.25 + location * 0.10
    hard_gate_passed = attainability >= 60 and remuneration > 0

    evidence: list[str] = []
    for item in profile.evidence:
        evidence_skills = {skill.lower() for skill in item["skills"]}
        if evidence_skills & set(matched_skills):
            evidence.append(item["id"])
    if not job.matched_evidence_ids:
        job.matched_evidence_ids = evidence[:5]
    if not job.match_reasons:
        job.match_reasons = [
            f"Matches {', '.join(matched_skills[:5])}" if matched_skills else "Role aligns with the configured role family",
            "Offers stronger ownership or technical scope" if career_gain >= 70 else "Provides a credible next-step scope",
        ]
        if job.salary.midpoint_gbp:
            job.match_reasons.append(f"Expected salary midpoint is £{job.salary.midpoint_gbp:,}")
    if required_years and required_years > 4:
        job.gaps.append(f"Advert asks for {required_years}+ years; positioning must emphasize project ownership and impact")
    job.score = ScoreBreakdown(
        attainability=round(attainability, 1),
        career_gain=round(career_gain, 1),
        remuneration=round(remuneration, 1),
        location=round(location, 1),
        overall=round(overall, 1),
        hard_gate_passed=hard_gate_passed,
    )
    return job


def rank_jobs(jobs: list[Job], profile: CandidateProfile, limit: int = 10) -> list[Job]:
    scored = [score_job(job, profile) for job in jobs]
    viable = [job for job in scored if job.score.hard_gate_passed]
    return sorted(viable, key=lambda item: item.score.overall, reverse=True)[:limit]
