from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Job, JobListing, SalaryConfidence, SalaryEvidence


SALARY_PARSER_VERSION = 2

TRACKING_PARAMETERS = {
    "gh_src",
    "source",
    "src",
    "ref",
    "referrer",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}

SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "Python": ("python",),
    "SQL": ("sql", "sql server", "postgresql", "bigquery"),
    "Machine Learning": ("machine learning", "predictive model", "ml model"),
    "Statistics": ("statistics", "statistical", "experimentation", "a/b test"),
    "NLP": ("nlp", "natural language", "text classification", "language model"),
    "LLM Engineering": ("llm", "large language model", "generative ai", "genai"),
    "RAG": ("rag", "retrieval augmented", "retrieval-augmented"),
    "Agentic AI": ("agentic", "ai agent", "multi-agent", "multi agent"),
    "AWS": ("aws", "amazon web services", "bedrock", "sagemaker", "lambda"),
    "GCP": ("gcp", "google cloud", "bigquery", "vertex ai"),
    "Terraform": ("terraform", "infrastructure as code", "iac"),
    "Data Engineering": ("data engineering", "etl", "elt", "data pipeline", "dbt", "airbyte"),
    "Model Evaluation": ("model evaluation", "evaluation framework", "f1 score", "bertscore"),
    "PyTorch": ("pytorch",),
    "TensorFlow": ("tensorflow", "keras"),
    "Docker": ("docker", "container"),
    "FastAPI": ("fastapi",),
    "MCP": ("model context protocol", "mcp"),
}


def clean_text(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def canonical_url(value: str) -> str:
    if not value:
        return value
    parts = urlsplit(value)
    query = [(key, val) for key, val in parse_qsl(parts.query) if key.lower() not in TRACKING_PARAMETERS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def canonical_company(value: str) -> str:
    company = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    company = re.sub(r"\b(limited|ltd|plc|llp|inc|incorporated|uk)\b", "", company)
    return re.sub(r"\s+", " ", company).strip()


def canonical_title(value: str) -> str:
    title = value.lower().replace("sr.", "senior").replace("sr ", "senior ")
    title = re.sub(r"\([^)]*\)", " ", title)
    title = re.sub(r"[^a-z0-9+]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def job_fingerprint(company: str, title: str, location: str) -> str:
    normalized = "|".join(
        [canonical_company(company), canonical_title(title), re.sub(r"\s+", " ", location.lower()).strip()]
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def parse_salary(text: str, source_url: str | None = None) -> SalaryEvidence:
    normalized = clean_text(text).lower()
    if not any(token in normalized for token in ("£", "gbp", "pound", "salary", "pay", "compensation")):
        return SalaryEvidence(method_version=SALARY_PARSER_VERSION)

    amount_pattern = r"(?:£\s*|gbp\s*)?(?:\d{1,3}(?:,\d{3})+|\d{4,6}|\d{2,3}(?:\.\d+)?\s*k)"
    range_pattern = rf"({amount_pattern})\s*(?:-|–|—|to)\s*({amount_pattern})"
    values: list[int] = []
    for match in re.finditer(range_pattern, normalized):
        context = normalized[max(0, match.start() - 60) : min(len(normalized), match.end() + 30)]
        if not _is_gbp_salary_context(match.group(0), context):
            continue
        parsed = [_annual_salary_amount(value) for value in match.groups()]
        if all(value is not None for value in parsed):
            values = [int(value) for value in parsed if value is not None]
            break

    if not values:
        for match in re.finditer(amount_pattern, normalized):
            context = normalized[max(0, match.start() - 60) : min(len(normalized), match.end() + 30)]
            if not _is_gbp_salary_context(match.group(0), context):
                continue
            annual = _annual_salary_amount(match.group(0))
            if annual is not None:
                values.append(annual)
                break

    if not values:
        return SalaryEvidence(method_version=SALARY_PARSER_VERSION)
    minimum = min(values)
    maximum = max(values)
    return SalaryEvidence(
        minimum_gbp=minimum,
        maximum_gbp=maximum,
        confidence=SalaryConfidence.ADVERTISED,
        source_url=source_url,
        source_label="Job advert",
        inferred=False,
        method_version=SALARY_PARSER_VERSION,
    )


def _annual_salary_amount(raw: str) -> int | None:
    cleaned = re.sub(r"(?:£|gbp|\s|,)", "", raw.lower())
    suffix = cleaned.endswith("k")
    if suffix:
        cleaned = cleaned[:-1]
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if suffix:
        value *= 1000
    annual = round(value)
    return annual if 25_000 <= annual <= 300_000 else None


def _is_gbp_salary_context(raw: str, context: str) -> bool:
    foreign_currency = re.search(r"(?:\$|\busd\b|\beur\b|€|\bcad\b|\baud\b)", context)
    if foreign_currency:
        return False
    if re.search(r"(?:£|\bgbp\b|\bpounds?\b)", raw):
        return True
    return bool(re.search(r"\b(?:salary|pay|compensation|base)\b", context))


def extract_skills(text: str) -> list[str]:
    lower = clean_text(text).lower()
    found = [skill for skill, aliases in SKILL_ALIASES.items() if any(alias in lower for alias in aliases)]
    return sorted(found)


def infer_workplace(text: str, location: str) -> str:
    haystack = f"{location} {clean_text(text)}".lower()
    if (
        "fully remote" in haystack
        or "remote within the uk" in haystack
        or "uk remote" in haystack
        or "remote: true" in haystack
    ):
        return "remote"
    if "hybrid" in haystack:
        return "hybrid"
    if "remote" in haystack and "uk" in haystack:
        return "remote"
    if "on-site" in haystack or "onsite" in haystack or "office based" in haystack:
        return "onsite"
    return "unknown"


def normalize_job(raw: dict[str, Any], source: str, *, preserve_advert: bool = True) -> Job:
    title = clean_text(str(raw.get("title", "")))
    company = clean_text(str(raw.get("company", raw.get("company_name", "Unknown company"))))
    location = clean_text(str(raw.get("location", "Unknown location")))
    description = clean_text(str(raw.get("description", raw.get("content", ""))))
    apply_url = canonical_url(str(raw.get("apply_url", raw.get("url", raw.get("absolute_url", "")))))
    source_url = canonical_url(str(raw.get("source_url", apply_url)))
    source_id = str(raw.get("source_id", raw.get("id", job_fingerprint(company, title, location))))
    observed_at = datetime.now(UTC).isoformat()
    payload_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    salary_text = " ".join(
        f"{key}: {raw.get(key, '')}" for key in ("salary", "salary_text", "compensation", "description")
    )
    advert_document = None
    if preserve_advert:
        from .advert_document import prepare_advert
        advert_document = prepare_advert(str(raw.get("description", raw.get("content", ""))), source=source,
                                         upstream_partial=bool(raw.get("description_partial", False)))
        description = advert_document["text"]
    return Job(
        id=job_fingerprint(company, title, location),
        source=source,
        source_id=source_id,
        title=title,
        company=company,
        location=location,
        description=description,
        advert_document=advert_document,
        apply_url=apply_url,
        source_url=source_url,
        posted_at=raw.get("posted_at") or raw.get("created") or raw.get("updated_at"),
        closes_at=raw.get("closes_at"),
        workplace=infer_workplace(description, location),
        employment_type=str(raw.get("employment_type", "full_time")),
        salary=parse_salary(salary_text, source_url),
        skills=extract_skills(f"{title} {description}"),
        first_seen_at=observed_at,
        last_verified_at=observed_at,
        listings=[
            JobListing(
                source=source,
                source_id=source_id,
                source_url=source_url,
                apply_url=apply_url,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                payload_hash=payload_hash,
            )
        ],
    )
