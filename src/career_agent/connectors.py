from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from .models import Job, SalaryConfidence
from .normalization import normalize_job
from .source_registry import SourceDefinition, load_source_registry
from .listing_details import ListingDetailsStore, make_record, reusable
from .reed_hydration import ReedHydrationDispatcher


USER_AGENT = "CareerAgentExample/0.1 (+https://portfolio.example.com)"
logger = logging.getLogger(__name__)
TARGET_QUERIES = (
    "data scientist",
    "applied scientist",
    "ai scientist",
    "ai engineer",
    "generative ai engineer",
    "genai engineer",
    "llm engineer",
    "machine learning engineer",
    "ml engineer",
    "ml scientist",
    "research scientist machine learning",
    "decision scientist",
    "product data scientist",
    "data science consultant",
    "ai consultant",
    "nlp engineer",
    "nlp scientist",
    "quant developer",
    "applied ml engineer",
    "forward deployed engineer",
    "senior forward deployed engineer",
    "quantitative analyst",
    "quantitative researcher",
)
TARGET_TITLE_TERMS = (
    "data scientist",
    "applied scientist",
    "ai scientist",
    "ai engineer",
    "artificial intelligence engineer",
    "generative ai",
    "genai engineer",
    "llm engineer",
    "machine learning engineer",
    "ml engineer",
    "ml scientist",
    "research scientist",
    "decision scientist",
    "product data scientist",
    "data science consultant",
    "ai consultant",
    "nlp engineer",
    "nlp scientist",
    "quant developer",
    "quantitative developer",
    "applied ml engineer",
    "applied machine learning engineer",
    "forward deployed engineer",
    "quantitative analyst",
    "quantitative researcher",
)


def _is_target_title(value: Any) -> bool:
    title = str(value or "").lower()
    return any(term in title for term in TARGET_TITLE_TERMS)


class ConnectorError(RuntimeError):
    pass


@dataclass(slots=True)
class SourceFetchDiagnostic:
    source_key: str
    source: str
    employer: str | None
    ats: str
    tenant: str | None
    started_at: str
    completed_at: str
    duration_ms: int
    status: str
    listings_fetched: int
    error: str | None = None
    detail_metrics: dict[str, int] | None = None
    coverage_scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "source": self.source,
            "employer": self.employer,
            "ats": self.ats,
            "tenant": self.tenant,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "listings_fetched": self.listings_fetched,
            "error": self.error,
            **({"coverage_scope": self.coverage_scope} if self.coverage_scope else {}),
            **({"detail_metrics": self.detail_metrics} if self.detail_metrics is not None else {}),
        }


SENSITIVE_QUERY_PARAMETERS = {"app_id", "app_key", "api_key", "key", "token"}


def _redacted_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [
        (key, "<redacted>" if key.lower() in SENSITIVE_QUERY_PARAMETERS else value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
    )


def get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> Any:
    _validate_public_url(url)
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                raise ConnectorError(f"HTTP {response.status} from {_redacted_url(url)}")
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"Unable to fetch {_redacted_url(url)}: {exc}") from exc


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> Any:
    _validate_public_url(url)
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **(headers or {}),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                raise ConnectorError(f"HTTP {response.status} from {url}")
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"Unable to fetch {url}: {exc}") from exc


def get_text(
    url: str,
    *,
    accepted_types: set[str],
    timeout: int = 30,
    max_bytes: int = 25_000_000,
) -> tuple[str, str]:
    _validate_public_url(url)
    request = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            _validate_public_url(final_url)
            content_type = response.headers.get_content_type()
            if content_type not in accepted_types:
                raise ConnectorError(f"Unsupported content type {content_type} from {url}")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ConnectorError(f"Response from {url} exceeded {max_bytes} bytes")
            charset = response.headers.get_content_charset() or "utf-8"
            return final_url, body.decode(charset, errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, UnicodeError) as exc:
        raise ConnectorError(f"Unable to fetch {url}: {exc}") from exc


def get_public_text(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    max_bytes: int = 1_500_000,
) -> tuple[str, str]:
    """Fetch a bounded public HTTP page and return its final URL and decoded body."""
    _validate_public_url(url)
    request_headers = {
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
        "User-Agent": USER_AGENT,
        **(headers or {}),
    }
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            _validate_public_url(final_url)
            if response.status >= 400:
                raise ConnectorError(f"HTTP {response.status} from {url}")
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise ConnectorError(f"Unsupported content type {content_type} from {url}")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ConnectorError(f"Response from {url} exceeded {max_bytes} bytes")
            charset = response.headers.get_content_charset() or "utf-8"
            return final_url, body.decode(charset, errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, UnicodeError) as exc:
        raise ConnectorError(f"Unable to fetch {url}: {exc}") from exc


def _validate_public_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ConnectorError("Salary evidence URL must be a public HTTP(S) URL")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ConnectorError("Salary evidence URL cannot target a private or reserved address")


class JobConnector(ABC):
    name: str

    @abstractmethod
    def fetch(self) -> list[Job]: ...

    def telemetry_configuration(self) -> dict[str, Any]:
        """Return reproducibility metadata, never credentials."""
        return {"source": self.name}


@dataclass(slots=True)
class FixtureConnector(JobConnector):
    fixtures: list[dict[str, Any]]
    name: str = "fixture"

    def fetch(self) -> list[Job]:
        return [
            Job.from_dict(item) if isinstance(item.get("salary"), dict) else normalize_job(item, self.name)
            for item in self.fixtures
        ]


@dataclass(slots=True)
class AdzunaConnector(JobConnector):
    app_id: str
    app_key: str
    queries: tuple[str, ...]
    country: str = "gb"
    query_parameter: str = "title_only"
    location: str | None = None
    distance_km: int | None = None
    category: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_include_unknown: bool | None = None
    full_time: bool | None = None
    part_time: bool | None = None
    contract: bool | None = None
    permanent: bool | None = None
    company: str | None = None
    what_exclude: str | None = None
    results_per_page: int = 50
    max_results_per_query: int = 100
    max_days_old: int | None = 14
    sort_by: str | None = "date"
    minimum_request_interval_seconds: float = 2.5
    name: str = "adzuna"

    def __post_init__(self) -> None:
        valid_query_parameters = {"what", "what_and", "what_phrase", "what_or", "title_only"}
        if self.query_parameter not in valid_query_parameters:
            raise ValueError(f"Unsupported Adzuna query parameter: {self.query_parameter}")
        if not 1 <= self.results_per_page <= 50:
            raise ValueError("Adzuna results_per_page must be between 1 and 50")
        if self.max_results_per_query < 1:
            raise ValueError("Adzuna max_results_per_query must be positive")

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        seen_source_ids: set[str] = set()
        request_count = 0
        for query in self.queries:
            page = 1
            fetched_for_query = 0
            while fetched_for_query < self.max_results_per_query:
                if request_count and self.minimum_request_interval_seconds:
                    time.sleep(self.minimum_request_interval_seconds)
                params: dict[str, Any] = {
                    "app_id": self.app_id,
                    "app_key": self.app_key,
                    self.query_parameter: query,
                    "results_per_page": self.results_per_page,
                    "content-type": "application/json",
                }
                _set_adzuna_query_option(params, "where", self.location)
                _set_adzuna_query_option(params, "distance", self.distance_km)
                _set_adzuna_query_option(params, "category", self.category)
                _set_adzuna_query_option(params, "salary_min", self.salary_min)
                _set_adzuna_query_option(params, "salary_max", self.salary_max)
                _set_adzuna_query_option(params, "salary_include_unknown", self.salary_include_unknown)
                _set_adzuna_query_option(params, "full_time", self.full_time)
                _set_adzuna_query_option(params, "part_time", self.part_time)
                _set_adzuna_query_option(params, "contract", self.contract)
                _set_adzuna_query_option(params, "permanent", self.permanent)
                _set_adzuna_query_option(params, "company", self.company)
                _set_adzuna_query_option(params, "what_exclude", self.what_exclude)
                _set_adzuna_query_option(params, "max_days_old", self.max_days_old)
                _set_adzuna_query_option(params, "sort_by", self.sort_by)
                encoded = urllib.parse.urlencode(params)
                country = urllib.parse.quote(self.country, safe="")
                payload = get_json(
                    f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?{encoded}"
                )
                request_count += 1
                items = payload.get("results", [])
                remaining = self.max_results_per_query - fetched_for_query
                for item in items[:remaining]:
                    source_id = str(item.get("id") or "")
                    if source_id and source_id in seen_source_ids:
                        continue
                    if source_id:
                        seen_source_ids.add(source_id)
                    raw = {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "company": (item.get("company") or {}).get("display_name", "Unknown company"),
                        "location": (item.get("location") or {}).get(
                            "display_name", self.location or "United Kingdom"
                        ),
                        "description": item.get("description", ""),
                        "url": item.get("redirect_url", ""),
                        "source_url": item.get("redirect_url", ""),
                        "posted_at": item.get("created"),
                        "employment_type": _adzuna_employment_type(item),
                        "salary": _salary_string(item.get("salary_min"), item.get("salary_max")),
                    }
                    job = normalize_job(raw, self.name)
                    if job.salary.midpoint_gbp is not None and item.get("salary_is_predicted"):
                        job.salary.confidence = SalaryConfidence.MEDIUM
                        job.salary.inferred = True
                        job.salary.source_label = "Adzuna estimate"
                    jobs.append(job)
                fetched_for_query += min(len(items), remaining)
                total = int(payload.get("count", len(items)) or 0)
                if (
                    not items
                    or len(items) < self.results_per_page
                    or page * self.results_per_page >= total
                    or fetched_for_query >= self.max_results_per_query
                ):
                    break
                page += 1
        return jobs

    def telemetry_configuration(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "queries": list(self.queries),
            "country": self.country,
            "query_parameter": self.query_parameter,
            "where": self.location,
            "distance_km": self.distance_km,
            "category": self.category,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_include_unknown": self.salary_include_unknown,
            "full_time": self.full_time,
            "part_time": self.part_time,
            "contract": self.contract,
            "permanent": self.permanent,
            "company": self.company,
            "what_exclude": self.what_exclude,
            "results_per_page": self.results_per_page,
            "max_results_per_query": self.max_results_per_query,
            "max_days_old": self.max_days_old,
            "sort_by": self.sort_by,
        }


@dataclass(slots=True)
class GreenhouseConnector(JobConnector):
    board_tokens: tuple[str, ...]
    name: str = "greenhouse"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for token in self.board_tokens:
            payload = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
            for item in payload.get("jobs", []):
                location = (item.get("location") or {}).get("name", "")
                raw = {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "company": token.replace("-", " ").title(),
                    "location": location,
                    "description": item.get("content", ""),
                    "url": item.get("absolute_url", ""),
                    "updated_at": item.get("updated_at"),
                }
                jobs.append(normalize_job(raw, self.name))
        return jobs


@dataclass(slots=True)
class LeverConnector(JobConnector):
    site_names: tuple[str, ...]
    name: str = "lever"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for site in self.site_names:
            payload = get_json(f"https://api.lever.co/v0/postings/{site}?mode=json")
            for item in payload:
                categories = item.get("categories") or {}
                lists = "\n\n".join(
                    f"{section.get('text', '')}\n{section.get('content', '')}" for section in item.get("lists", [])
                )
                raw = {
                    "id": item.get("id"),
                    "title": item.get("text"),
                    "company": site.replace("-", " ").title(),
                    "location": categories.get("location", ""),
                    "description": f"{item.get('descriptionPlain', '')}\n\n{lists}",
                    "url": item.get("hostedUrl", item.get("applyUrl", "")),
                    "apply_url": item.get("applyUrl", item.get("hostedUrl", "")),
                    "employment_type": categories.get("commitment", "full_time"),
                }
                jobs.append(normalize_job(raw, self.name))
        return jobs


@dataclass(slots=True)
class AshbyConnector(JobConnector):
    board_names: tuple[str, ...]
    name: str = "ashby"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for board in self.board_names:
            payload = get_json(
                f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true"
            )
            for item in payload.get("jobs", []):
                compensation = item.get("compensation") or {}
                raw = {
                    "id": item.get("jobUrl", item.get("title")),
                    "title": item.get("title"),
                    "company": board.replace("-", " ").title(),
                    "location": item.get("location", ""),
                    "description": item.get("descriptionPlain", item.get("descriptionHtml", "")),
                    "url": item.get("jobUrl", ""),
                    "apply_url": item.get("applyUrl", item.get("jobUrl", "")),
                    "posted_at": item.get("publishedAt"),
                    "employment_type": item.get("employmentType", "full_time"),
                    "salary": compensation.get("scrapeableCompensationSalarySummary", ""),
                }
                jobs.append(normalize_job(raw, self.name))
        return jobs


@dataclass(slots=True)
class ReedConnector(JobConnector):
    api_key: str
    queries: tuple[str, ...]
    employer_id: int | None = None
    employer_profile_id: int | None = None
    location: str | None = None
    distance_miles: int | None = None
    permanent: bool | None = None
    contract: bool | None = None
    temp: bool | None = None
    part_time: bool | None = None
    full_time: bool | None = None
    minimum_salary: int | None = None
    maximum_salary: int | None = None
    posted_by_recruitment_agency: bool | None = None
    posted_by_direct_employer: bool | None = None
    graduate: bool | None = None
    results_per_page: int = 100
    max_results_per_query: int = 200
    detail_results_limit: int = 100
    name: str = "reed"
    detail_store: ListingDetailsStore | None = None
    hydration_dispatcher: ReedHydrationDispatcher | None = None
    detail_metrics: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.employer_id is not None and self.employer_profile_id is not None:
            raise ValueError("Reed accepts only one of employer_id and employer_profile_id")
        if not 1 <= self.results_per_page <= 100:
            raise ValueError("Reed results_per_page must be between 1 and 100")
        if self.max_results_per_query < 1 or self.detail_results_limit < 0:
            raise ValueError("Reed result limits cannot be negative")

    def fetch(self) -> list[Job]:
        search_results: dict[str, dict[str, Any]] = {}
        self.detail_metrics = {k: 0 for k in ("cache_hits", "cache_misses", "refresh_due",
            "requests", "fetched", "fetch_errors", "invalid_responses", "persist_errors",
            "cache_read_errors", "deferred", "stored", "hydration_registered",
            "hydration_dispatch_errors")}
        authorization = b64encode(f"{self.api_key}:".encode()).decode()
        headers = {"Authorization": f"Basic {authorization}"}
        for query in self.queries:
            offset = 0
            while offset < self.max_results_per_query:
                remaining = self.max_results_per_query - offset
                search_params: dict[str, Any] = {
                    "keywords": query,
                    "resultsToTake": min(self.results_per_page, remaining),
                    "resultsToSkip": offset,
                }
                _set_query_option(search_params, "employerId", self.employer_id)
                _set_query_option(search_params, "employerProfileId", self.employer_profile_id)
                _set_query_option(search_params, "locationName", self.location)
                _set_query_option(search_params, "distanceFromLocation", self.distance_miles)
                _set_query_option(search_params, "permanent", self.permanent)
                _set_query_option(search_params, "contract", self.contract)
                _set_query_option(search_params, "temp", self.temp)
                _set_query_option(search_params, "partTime", self.part_time)
                _set_query_option(search_params, "fullTime", self.full_time)
                _set_query_option(search_params, "minimumSalary", self.minimum_salary)
                _set_query_option(search_params, "maximumSalary", self.maximum_salary)
                _set_query_option(
                    search_params, "postedByRecruitmentAgency", self.posted_by_recruitment_agency
                )
                _set_query_option(search_params, "postedByDirectEmployer", self.posted_by_direct_employer)
                _set_query_option(search_params, "graduate", self.graduate)
                params = urllib.parse.urlencode(search_params)
                payload = get_json(
                    f"https://www.reed.co.uk/api/1.0/search?{params}",
                    headers=headers,
                )
                items = payload.get("results", [])
                for item in items:
                    source_id = str(item.get("jobId") or "")
                    if source_id:
                        search_results.setdefault(source_id, item)
                offset += len(items)
                total = int(payload.get("totalResults", len(items)))
                if not items or len(items) < search_params["resultsToTake"] or offset >= total:
                    break

        jobs: list[Job] = []
        details_requested = 0
        target_ids = [i for i, item in search_results.items() if _is_target_title(item.get("jobTitle"))]
        cached = {}
        cache_available = True
        if self.detail_store:
            try:
                cached = self.detail_store.get_listing_details("reed", target_ids)
            except Exception:
                # An unavailable database is not evidence of a miss: don't cause a refetch storm.
                cache_available = False
                self.detail_metrics["cache_read_errors"] += 1
                logger.warning("Reed detail cache read failed; deferring detail requests")
        if self.hydration_dispatcher is not None and cache_available:
            misses = [
                item
                for source_id, item in search_results.items()
                if source_id in target_ids
                and not (
                    (record := cached.get(source_id)) is not None
                    and reusable(record, source_id, item)
                )
            ]
            if misses:
                try:
                    self.detail_metrics["hydration_registered"] = (
                        self.hydration_dispatcher.register(misses)
                    )
                except Exception:
                    self.detail_metrics["hydration_dispatch_errors"] += 1
                    logger.exception("Reed hydration dispatch failed; durable reconciler will retry")
        for item in search_results.values():
            detail = item
            source_id = str(item["jobId"])
            target = _is_target_title(item.get("jobTitle"))
            record = cached.get(source_id)
            hit = target and record is not None and reusable(record, source_id, item)
            if hit:
                detail = record["details"]
                self.detail_metrics["cache_hits"] += 1
            elif target:
                self.detail_metrics["refresh_due" if record else "cache_misses"] += 1
            if self.hydration_dispatcher is not None:
                if target and hit:
                    raw = _reed_raw_job(item, detail, self.location or "United Kingdom")
                    jobs.append(normalize_job(raw, self.name))
                elif target:
                    self.detail_metrics["deferred"] += 1
                continue
            if target and not hit and cache_available and details_requested < self.detail_results_limit:
                details_requested += 1
                self.detail_metrics["requests"] += 1
                try:
                    job_id = urllib.parse.quote(str(item["jobId"]), safe="")
                    fetched_detail = get_json(
                        f"https://www.reed.co.uk/api/1.0/jobs/{job_id}",
                        headers=headers,
                    )
                except ConnectorError:
                    self.detail_metrics["fetch_errors"] += 1
                    logger.warning("Reed job detail request failed", extra={"job_id": item.get("jobId")})
                else:
                    try:
                        fresh = make_record(source_id, item, fetched_detail, datetime.now(UTC))
                    except ValueError:
                        self.detail_metrics["invalid_responses"] += 1
                    else:
                        detail = fresh["details"]
                        self.detail_metrics["fetched"] += 1
                        if self.detail_store:
                            try:
                                self.detail_store.put_listing_details(fresh)
                                self.detail_metrics["stored"] += 1
                            except Exception:
                                self.detail_metrics["persist_errors"] += 1
                                logger.warning("Reed detail persistence failed", extra={"job_id": source_id})
            elif target and not hit:
                self.detail_metrics["deferred"] += 1
            raw = _reed_raw_job(item, detail, self.location or "United Kingdom")
            jobs.append(normalize_job(raw, self.name))
        return jobs

    def telemetry_configuration(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "queries": list(self.queries),
            "employer_id": self.employer_id,
            "employer_profile_id": self.employer_profile_id,
            "location": self.location,
            "distance_miles": self.distance_miles,
            "permanent": self.permanent,
            "contract": self.contract,
            "temp": self.temp,
            "part_time": self.part_time,
            "full_time": self.full_time,
            "minimum_salary": self.minimum_salary,
            "maximum_salary": self.maximum_salary,
            "posted_by_recruitment_agency": self.posted_by_recruitment_agency,
            "posted_by_direct_employer": self.posted_by_direct_employer,
            "graduate": self.graduate,
            "results_per_page": self.results_per_page,
            "max_results_per_query": self.max_results_per_query,
            "detail_results_limit": (
                None if self.hydration_dispatcher is not None else self.detail_results_limit
            ),
            "legacy_sync_detail_results_limit": self.detail_results_limit,
            "detail_cache_enabled": self.detail_store is not None,
            "detail_mode": "asynchronous" if self.hydration_dispatcher is not None else "synchronous",
            "detail_refresh_policy": "search_metadata_change_only",
        }


@dataclass(slots=True)
class SmartRecruitersConnector(JobConnector):
    company_ids: tuple[str, ...]
    queries: tuple[str, ...] = TARGET_QUERIES
    page_size: int = 100
    max_results_per_query: int = 100
    name: str = "smartrecruiters"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for company_id in self.company_ids:
            company = urllib.parse.quote(company_id)
            seen_postings: set[str] = set()
            for query in self.queries:
                offset = 0
                while offset < self.max_results_per_query:
                    params = urllib.parse.urlencode({"limit": self.page_size, "offset": offset, "q": query})
                    payload = get_json(
                        f"https://api.smartrecruiters.com/v1/companies/{company}/postings?{params}"
                    )
                    items = payload.get("content", [])
                    for item in items:
                        if not _is_target_title(item.get("name")):
                            continue
                        posting_id = str(item.get("uuid") or item.get("id") or "")
                        if not posting_id or posting_id in seen_postings:
                            continue
                        seen_postings.add(posting_id)
                        detail = item
                        if not item.get("jobAd"):
                            detail = get_json(
                                f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"
                            )
                        job_ad = detail.get("jobAd") or {}
                        sections = job_ad.get("sections") or {}
                        description = "\n\n".join(
                            "\n".join(str(section.get(key) or "") for key in ("title", "text"))
                            for section in sections.values()
                            if isinstance(section, dict)
                        )
                        location_data = detail.get("location") or item.get("location") or {}
                        location = ", ".join(
                            str(location_data.get(key, ""))
                            for key in ("city", "region", "country")
                            if location_data.get(key)
                        )
                        posting_url = detail.get("postingUrl") or detail.get("applyUrl") or item.get("ref", "")
                        raw = {
                            "id": posting_id,
                            "title": detail.get("name", item.get("name")),
                            "company": (detail.get("company") or {}).get("name", company_id),
                            "location": location,
                            "description": description,
                            "url": posting_url,
                            "apply_url": detail.get("applyUrl", posting_url),
                            "source_url": posting_url,
                            "posted_at": detail.get("releasedDate", item.get("releasedDate")),
                            "employment_type": (detail.get("typeOfEmployment") or {}).get("label", "full_time"),
                        }
                        jobs.append(normalize_job(raw, self.name))
                    offset += len(items)
                    total = int(payload.get("totalFound", payload.get("total", len(items))))
                    if not items or offset >= total:
                        break
        return jobs


@dataclass(frozen=True, slots=True)
class WorkdayBoard:
    tenant: str
    host: str
    site: str

    @classmethod
    def parse(cls, value: str) -> "WorkdayBoard":
        parts = tuple(part.strip() for part in value.split("|"))
        if len(parts) != 3 or not all(parts):
            raise ValueError("Workday board must use tenant|host|site")
        host = parts[1]
        if "." not in host:
            host = f"{parts[0]}.{host}.myworkdayjobs.com"
        return cls(parts[0], host, parts[2])


@dataclass(slots=True)
class WorkdayConnector(JobConnector):
    boards: tuple[WorkdayBoard, ...]
    queries: tuple[str, ...] = TARGET_QUERIES
    page_size: int = 20
    max_results_per_query: int = 60
    name: str = "workday"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for board in self.boards:
            base = f"https://{board.host}/wday/cxs/{board.tenant}/{board.site}"
            seen_paths: set[str] = set()
            for query in self.queries:
                offset = 0
                while offset < self.max_results_per_query:
                    payload = post_json(
                        f"{base}/jobs",
                        {"appliedFacets": {}, "limit": self.page_size, "offset": offset, "searchText": query},
                    )
                    items = payload.get("jobPostings", [])
                    for item in items:
                        if not _is_target_title(item.get("title")):
                            continue
                        external_path = item.get("externalPath", "")
                        if not external_path or external_path in seen_paths:
                            continue
                        seen_paths.add(external_path)
                        detail = get_json(f"{base}{external_path}")
                        info = detail.get("jobPostingInfo") or {}
                        description = info.get("jobDescription", "")
                        salary = " ".join(
                            str(info.get(key, "")) for key in ("salary", "additionalLocations")
                        )
                        public_url = f"https://{board.host}/en-US/{board.site}{external_path}"
                        raw = {
                            "id": info.get("jobReqId", external_path),
                            "title": info.get("title", item.get("title")),
                            "company": info.get("company", board.tenant.replace("-", " ").title()),
                            "location": info.get("location", item.get("locationsText", "")),
                            "description": description,
                            "url": info.get("externalUrl", public_url),
                            "source_url": info.get("externalUrl", public_url),
                            "posted_at": info.get("startDate", item.get("postedOn")),
                            "employment_type": info.get("timeType", "full_time"),
                            "salary": salary,
                        }
                        jobs.append(normalize_job(raw, self.name))
                    offset += len(items)
                    if not items or offset >= int(payload.get("total", len(items))):
                        break
        return jobs


def _xml_text(node: ET.Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


@dataclass(slots=True)
class WorkableXmlConnector(JobConnector):
    feed_url: str = "https://www.workable.com/boards/workable.xml"
    name: str = "workable"

    def fetch(self) -> list[Job]:
        _, body = get_text(
            self.feed_url,
            accepted_types={"application/xml", "text/xml", "application/rss+xml", "text/plain"},
            max_bytes=75_000_000,
        )
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ConnectorError(f"Invalid Workable XML: {exc}") from exc
        jobs: list[Job] = []
        for item in root.findall(".//job"):
            url = _xml_text(item, "url", "apply_url")
            raw = {
                "id": _xml_text(item, "id", "shortcode", "referencenumber") or url,
                "title": _xml_text(item, "title"),
                "company": _xml_text(item, "company", "company_name") or "Unknown company",
                "location": ", ".join(
                    value for value in (_xml_text(item, "city"), _xml_text(item, "country")) if value
                ) or _xml_text(item, "location"),
                "description": "\n\n".join(
                    value
                    for value in (
                        _xml_text(item, "description", "description_text"),
                        f"Remote: {_xml_text(item, 'remote')}" if _xml_text(item, "remote") else "",
                    )
                    if value
                ),
                "url": url,
                "source_url": url,
                "posted_at": _xml_text(item, "date", "created_at"),
                "employment_type": _xml_text(item, "employment_type", "jobtype") or "full_time",
                "salary": _xml_text(item, "salary"),
            }
            if raw["title"] and url:
                jobs.append(normalize_job(raw, self.name))
        return jobs


@dataclass(slots=True)
class PersonioConnector(JobConnector):
    company_ids: tuple[str, ...]
    language: str = "en"
    name: str = "personio"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for company_id in self.company_ids:
            url = f"https://{company_id}.jobs.personio.de/xml?language={urllib.parse.quote(self.language)}"
            _, body = get_text(url, accepted_types={"application/xml", "text/xml", "text/plain"})
            try:
                root = ET.fromstring(body)
            except ET.ParseError as exc:
                raise ConnectorError(f"Invalid Personio XML for {company_id}: {exc}") from exc
            for position in root.findall(".//position"):
                position_id = _xml_text(position, "id")
                public_url = f"https://{company_id}.jobs.personio.de/job/{position_id}" if position_id else url
                descriptions = "\n\n".join(
                    element.text or "" for element in position.findall(".//jobDescription/*")
                )
                raw = {
                    "id": position_id,
                    "title": _xml_text(position, "name"),
                    "company": company_id.replace("-", " ").title(),
                    "location": _xml_text(position, "office"),
                    "description": descriptions,
                    "url": public_url,
                    "source_url": public_url,
                    "posted_at": _xml_text(position, "createdAt"),
                    "employment_type": _xml_text(position, "employmentType") or "full_time",
                }
                jobs.append(normalize_job(raw, self.name))
        return jobs


@dataclass(slots=True)
class RecruiteeConnector(JobConnector):
    company_ids: tuple[str, ...]
    name: str = "recruitee"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for company_id in self.company_ids:
            payload = get_json(f"https://{company_id}.recruitee.com/api/offers/")
            for item in payload.get("offers", []):
                location = item.get("location") or ""
                if isinstance(location, dict):
                    location = location.get("name") or location.get("city") or ""
                raw = {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "company": item.get("company_name", company_id.replace("-", " ").title()),
                    "location": location,
                    "description": item.get("description", ""),
                    "url": item.get("careers_url", item.get("url", "")),
                    "source_url": item.get("careers_url", item.get("url", "")),
                    "posted_at": item.get("created_at"),
                    "employment_type": item.get("employment_type", "full_time"),
                }
                jobs.append(normalize_job(raw, self.name))
        return jobs


@dataclass(slots=True)
class TeamtailorConnector(JobConnector):
    company_ids: tuple[str, ...]
    name: str = "teamtailor"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        namespace = {"tt": "https://teamtailor.com/locations"}
        for company_id in self.company_ids:
            url = f"https://{company_id}.teamtailor.com/jobs.rss"
            _, body = get_text(
                url,
                accepted_types={
                    "application/rss+xml",
                    "application/xml",
                    "text/xml",
                    "text/plain",
                },
            )
            try:
                root = ET.fromstring(body)
            except ET.ParseError as exc:
                raise ConnectorError(f"Invalid Teamtailor RSS for {company_id}: {exc}") from exc
            channel = root.find("channel")
            company = _xml_text(channel, "title") if channel is not None else ""
            for item in root.findall(".//item"):
                link = _xml_text(item, "link")
                guid = _xml_text(item, "guid")
                location_nodes = item.findall("tt:locations/tt:location", namespace)
                locations: list[str] = []
                for node in location_nodes:
                    location = ", ".join(
                        value
                        for value in (
                            _xml_text(node, f"{{{namespace['tt']}}}city"),
                            _xml_text(node, f"{{{namespace['tt']}}}country"),
                        )
                        if value
                    ) or _xml_text(node, f"{{{namespace['tt']}}}name")
                    if location and location not in locations:
                        locations.append(location)
                remote_status = _xml_text(item, "remoteStatus")
                description = _xml_text(item, "description")
                if remote_status:
                    description = f"{description} Remote status: {remote_status}"
                raw = {
                    "id": guid or link,
                    "title": _xml_text(item, "title"),
                    "company": company or company_id.replace("-", " ").title(),
                    "location": " | ".join(locations),
                    "description": description,
                    "url": link,
                    "source_url": link,
                    "posted_at": _rss_timestamp(_xml_text(item, "pubDate")),
                    "employment_type": _xml_text(
                        item, f"{{{namespace['tt']}}}employmentType"
                    ) or "full_time",
                }
                if raw["title"] and link:
                    jobs.append(normalize_job(raw, self.name))
        return jobs


def _rss_timestamp(value: str) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_json_ld = False
        self.blocks: list[str] = []
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "script" and values.get("type", "").lower() == "application/ld+json":
            self.in_json_ld = True
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self.in_json_ld:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.in_json_ld:
            self.blocks.append("".join(self._buffer))
            self.in_json_ld = False


def _json_ld_nodes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [node for item in value for node in _json_ld_nodes(item)]
    if not isinstance(value, dict):
        return []
    nodes = [value]
    if "@graph" in value:
        nodes.extend(_json_ld_nodes(value["@graph"]))
    return nodes


@dataclass(slots=True)
class JsonLdConnector(JobConnector):
    urls: tuple[str, ...]
    name: str = "jsonld"

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for url in self.urls:
            final_url, body = get_public_text(url)
            parser = _JsonLdParser()
            parser.feed(body)
            for block in parser.blocks:
                try:
                    payload = json.loads(block)
                except json.JSONDecodeError:
                    continue
                for item in _json_ld_nodes(payload):
                    kind = item.get("@type", "")
                    if "JobPosting" not in ([kind] if isinstance(kind, str) else kind):
                        continue
                    organization = item.get("hiringOrganization") or {}
                    location_data = item.get("jobLocation") or {}
                    if isinstance(location_data, list):
                        location_data = location_data[0] if location_data else {}
                    address = location_data.get("address") or {}
                    if isinstance(address, list):
                        address = address[0] if address else {}
                    location = ", ".join(
                        _json_ld_location_value(address.get(key))
                        for key in ("addressLocality", "addressRegion", "addressCountry")
                        if address.get(key)
                    )
                    applicant_locations = item.get("applicantLocationRequirements") or []
                    if isinstance(applicant_locations, dict):
                        applicant_locations = [applicant_locations]
                    remote_regions = ", ".join(
                        str(entry.get("name", ""))
                        for entry in applicant_locations
                        if isinstance(entry, dict) and entry.get("name")
                    )
                    if item.get("jobLocationType") == "TELECOMMUTE":
                        location = f"Remote{f' — {remote_regions}' if remote_regions else ''}"
                    salary = item.get("baseSalary") or {}
                    value = salary.get("value") or {}
                    salary_text = _salary_string(value.get("minValue"), value.get("maxValue"))
                    posting_url = item.get("url", final_url)
                    raw = {
                        "id": item.get("identifier", {}).get("value") if isinstance(item.get("identifier"), dict) else item.get("identifier"),
                        "title": item.get("title"),
                        "company": organization.get("name", "Unknown company"),
                        "location": location or "Unknown location",
                        "description": item.get("description", ""),
                        "url": posting_url,
                        "source_url": posting_url,
                        "posted_at": item.get("datePosted"),
                        "closes_at": item.get("validThrough"),
                        "employment_type": item.get("employmentType", "full_time"),
                        "salary": salary_text,
                    }
                    jobs.append(normalize_job(raw, self.name))
        return jobs


def _json_ld_location_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("@id") or "")
    return str(value or "")


@dataclass(slots=True)
class SitemapJsonLdConnector(JobConnector):
    """Discover employer-published vacancy pages, then read their JobPosting JSON-LD."""

    sitemap_urls: tuple[str, ...]
    max_sitemaps: int = 20
    max_job_pages: int = 250
    path_terms: tuple[str, ...] = ("job", "jobs", "career", "careers", "vacancy", "position")
    name: str = "sitemap_jsonld"

    def fetch(self) -> list[Job]:
        pending = list(self.sitemap_urls)
        visited_sitemaps: set[str] = set()
        job_urls: list[str] = []
        while pending and len(visited_sitemaps) < self.max_sitemaps:
            sitemap_url = pending.pop(0)
            if sitemap_url in visited_sitemaps:
                continue
            visited_sitemaps.add(sitemap_url)
            _, body = get_text(
                sitemap_url,
                accepted_types={"application/xml", "text/xml", "application/rss+xml", "text/plain"},
                max_bytes=10_000_000,
            )
            try:
                root = ET.fromstring(body)
            except ET.ParseError as exc:
                raise ConnectorError(f"Invalid sitemap XML from {sitemap_url}: {exc}") from exc
            locations = [
                str(node.text).strip()
                for node in root.findall(".//{*}loc")
                if node.text and str(node.text).strip()
            ]
            if root.tag.lower().endswith("sitemapindex"):
                pending.extend(locations)
                continue
            for url in locations:
                path = urllib.parse.urlsplit(url).path.lower()
                if any(term in path for term in self.path_terms):
                    job_urls.append(url)
                if len(job_urls) >= self.max_job_pages:
                    break
        return JsonLdConnector(tuple(dict.fromkeys(job_urls[: self.max_job_pages])), name=self.name).fetch()

    def telemetry_configuration(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "sitemap_count": len(self.sitemap_urls),
            "max_sitemaps": self.max_sitemaps,
            "max_job_pages": self.max_job_pages,
        }


def _basic_auth_headers(username: str, password: str) -> dict[str, str]:
    token = b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@dataclass(slots=True)
class CVLibraryConnector(JobConnector):
    """CV-Library partner API; construction requires recorded partner approval."""

    api_key: str
    queries: tuple[str, ...]
    partner_approved: bool
    geo: str = "United Kingdom"
    distance_miles: int = 750
    posted_days: int = 14
    per_page: int = 100
    max_results_per_query: int = 200
    name: str = "cv_library"

    def __post_init__(self) -> None:
        if not self.partner_approved:
            raise ValueError("CV-Library connector requires explicit partner approval")
        if self.per_page not in {25, 50, 75, 100}:
            raise ValueError("CV-Library per_page must be 25, 50, 75 or 100")

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        for query in self.queries:
            offset = 0
            while offset < self.max_results_per_query:
                params = urllib.parse.urlencode(
                    {
                        "key": self.api_key,
                        "q": query,
                        "geo": self.geo,
                        "distance": self.distance_miles,
                        "posted": self.posted_days,
                        "salarytype": "annum",
                        "salary_detailed": 1,
                        "description_full": 1,
                        "nohl": 1,
                        "applyurl": 1,
                        "id_as_string": 1,
                        "order": "date",
                        "perpage": self.per_page,
                        "offset": offset,
                    }
                )
                payload = get_json(f"https://www.cv-library.co.uk/search-jobs-json?{params}")
                items = payload.get("jobs", [])
                for item in items:
                    source_id = str(item.get("id") or "")
                    if not source_id or source_id in seen:
                        continue
                    seen.add(source_id)
                    salary = item.get("salary") or ""
                    if isinstance(salary, dict):
                        salary = _salary_string(salary.get("min"), salary.get("max"))
                    url = urllib.parse.urljoin("https://www.cv-library.co.uk", str(item.get("url") or ""))
                    agency = item.get("agency") or {}
                    jobs.append(
                        normalize_job(
                            {
                                "id": source_id,
                                "title": item.get("title"),
                                "company": agency.get("title", "Unknown company"),
                                "location": item.get("location", self.geo),
                                "description": item.get("description", ""),
                                "url": url,
                                "source_url": url,
                                "posted_at": item.get("posted"),
                                "closes_at": item.get("expiry_date"),
                                "employment_type": " ".join(item.get("type") or []),
                                "salary": salary,
                            },
                            self.name,
                        )
                    )
                offset += len(items)
                total = int(payload.get("total_entries", len(items)) or 0)
                if not items or offset >= total:
                    break
        return jobs


@dataclass(slots=True)
class OracleHcmConnector(JobConnector):
    """Authenticated Oracle HCM Recruiting requisition export."""

    base_url: str
    username: str
    password: str
    employer: str
    apply_url_template: str
    page_size: int = 100
    name: str = "oracle_hcm"

    def fetch(self) -> list[Job]:
        endpoint = f"{self.base_url.rstrip('/')}/hcmRestApi/resources/11.13.18.05/recruitingJobRequisitions"
        headers = _basic_auth_headers(self.username, self.password)
        jobs: list[Job] = []
        offset = 0
        while True:
            params = urllib.parse.urlencode(
                {
                    "limit": self.page_size,
                    "offset": offset,
                    "onlyData": "true",
                    "expand": "publishedJobs",
                }
            )
            payload = get_json(f"{endpoint}?{params}", headers=headers)
            items = payload.get("items", [])
            for item in items:
                published = item.get("publishedJobs") or []
                if isinstance(published, dict):
                    published = published.get("items") or published.get("results") or []
                external_postings = [
                    posting
                    for posting in published
                    if posting.get("Visibility") == "ORA_EXTERNAL"
                    and posting.get("PostingStatus") == "ORA_POSTED"
                ]
                if not external_postings:
                    continue
                posting = external_postings[0]
                requisition_id = item.get("RequisitionNumber") or item.get("RequisitionId")
                if requisition_id is None:
                    continue
                apply_url = self.apply_url_template.format(
                    requisition_id=requisition_id,
                    requisition_number=item.get("RequisitionNumber", requisition_id),
                )
                jobs.append(
                    normalize_job(
                        {
                            "id": requisition_id,
                            "title": item.get("Title"),
                            "company": self.employer,
                            "location": item.get("PrimaryLocation") or item.get("Location") or "Unknown location",
                            "description": "\n\n".join(
                                str(item.get(key) or "")
                                for key in ("ExternalDescriptionStr", "ExternalRespStr", "ExternalQualStr")
                            ),
                            "url": apply_url,
                            "source_url": apply_url,
                            "posted_at": posting.get("StartDate") or item.get("ExternalPublishedJobStartDate"),
                            "closes_at": posting.get("EndDate") or item.get("ExternalPublishedJobEndDate"),
                            "employment_type": item.get("RegularOrTemporary", "unknown"),
                            "salary": _salary_string(item.get("MinimumSalary"), item.get("MaximumSalary")),
                        },
                        self.name,
                    )
                )
            if not payload.get("hasMore") or not items:
                break
            offset += len(items)
        return jobs


@dataclass(slots=True)
class IcimsConnector(JobConnector):
    """Authenticated iCIMS Job Portal API adapter."""

    customer_id: str
    portal: str
    username: str
    password: str
    employer: str
    name: str = "icims"

    def fetch(self) -> list[Job]:
        headers = _basic_auth_headers(self.username, self.password)
        customer = urllib.parse.quote(self.customer_id, safe="")
        portal = urllib.parse.quote(self.portal, safe="")
        payload = get_json(
            f"https://api.icims.com/customers/{customer}/search/portals/{portal}", headers=headers
        )
        jobs: list[Job] = []
        for summary in payload.get("searchResults", []):
            detail_url = summary.get("self")
            detail = get_json(detail_url, headers=headers) if detail_url else summary
            fields = detail.get("fields") or detail
            portal_url = summary.get("portalUrl") or fields.get("portalUrl") or ""
            location = fields.get("location") or fields.get("jobLocation") or "Unknown location"
            if isinstance(location, dict):
                location = location.get("value") or location.get("name") or "Unknown location"
            jobs.append(
                normalize_job(
                    {
                        "id": summary.get("id") or fields.get("id"),
                        "title": _icims_value(fields.get("jobtitle") or fields.get("title")),
                        "company": self.employer,
                        "location": _icims_value(location),
                        "description": _icims_value(
                            fields.get("jobdescription") or fields.get("description")
                        ),
                        "url": portal_url,
                        "source_url": portal_url,
                        "posted_at": summary.get("updatedDate"),
                    },
                    self.name,
                )
            )
        return jobs


def _icims_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or "")
    return str(value or "")


@dataclass(slots=True)
class SapSuccessFactorsConnector(JobConnector):
    """Authenticated SAP SuccessFactors OData JobRequisition adapter."""

    odata_base_url: str
    username: str
    password: str
    employer: str
    apply_url_template: str
    filter_expression: str
    page_size: int = 100
    name: str = "sap_successfactors"

    def fetch(self) -> list[Job]:
        headers = _basic_auth_headers(self.username, self.password)
        skip = 0
        jobs: list[Job] = []
        while True:
            params = urllib.parse.urlencode(
                {
                    "$format": "json",
                    "$top": self.page_size,
                    "$skip": skip,
                    "$expand": "jobReqLocale",
                    "$filter": self.filter_expression,
                }
            )
            payload = get_json(
                f"{self.odata_base_url.rstrip('/')}/odata/v2/JobRequisition?{params}", headers=headers
            )
            items = (payload.get("d") or {}).get("results", [])
            for item in items:
                requisition_id = item.get("jobReqId")
                if requisition_id is None:
                    continue
                locale = item.get("jobReqLocale") or {}
                if isinstance(locale, dict) and "results" in locale:
                    locale = next(iter(locale.get("results") or []), {})
                apply_url = self.apply_url_template.format(requisition_id=requisition_id)
                jobs.append(
                    normalize_job(
                        {
                            "id": requisition_id,
                            "title": locale.get("jobTitle") or item.get("jobTitle"),
                            "company": self.employer,
                            "location": item.get("location") or "Unknown location",
                            "description": "\n\n".join(
                                str(locale.get(key) or item.get(key) or "")
                                for key in ("externalJobDescription", "jobDescription", "externalDescription")
                            ),
                            "url": apply_url,
                            "source_url": apply_url,
                            "posted_at": item.get("start"),
                            "closes_at": item.get("end"),
                        },
                        self.name,
                    )
                )
            if len(items) < self.page_size:
                break
            skip += len(items)
        return jobs


class RegisteredConnector(JobConnector):
    """Attach stable registry identity to an existing connector and its listings."""

    def __init__(self, definition: SourceDefinition, connector: JobConnector) -> None:
        self.definition = definition
        self.connector = connector
        self.name = connector.name

    def fetch(self) -> list[Job]:
        jobs = self.connector.fetch()
        for job in jobs:
            job.source_key = self.definition.key
            for listing in job.listings:
                listing.source_key = self.definition.key
        return jobs

    def telemetry_configuration(self) -> dict[str, Any]:
        return {
            **self.connector.telemetry_configuration(),
            "source_key": self.definition.key,
            "employer": self.definition.employer,
            "ats": self.definition.ats,
            "tenant": self.definition.tenant,
            "uk_presence": self.definition.uk_presence,
            "permission_status": self.definition.permission_status,
        }


def connectors_from_registry(
    path: str, *, excluded_ats: set[str] | frozenset[str] = frozenset()
) -> list[JobConnector]:
    connectors: list[JobConnector] = []
    for definition in load_source_registry(path):
        if definition.status != "active" or definition.ats in excluded_ats:
            continue
        connector = connector_from_definition(definition)
        if connector is not None:
            connectors.append(RegisteredConnector(definition, connector))
    return connectors


def connector_from_definition(definition: SourceDefinition) -> JobConnector | None:
    """Build a connector from trusted registry configuration."""
    tenant = definition.tenant
    config = definition.config
    if definition.ats == "greenhouse" and tenant:
        return GreenhouseConnector((tenant,))
    if definition.ats == "lever" and tenant:
        return LeverConnector((tenant,))
    if definition.ats == "ashby" and tenant:
        return AshbyConnector((tenant,))
    if definition.ats == "smartrecruiters" and tenant:
        return SmartRecruitersConnector(
            (tenant,),
            queries=("",),
            max_results_per_query=int(config.get("max_results", 1000)),
        )
    if definition.ats == "workday" and tenant:
        queries = tuple(str(value) for value in config.get("queries", TARGET_QUERIES))
        return WorkdayConnector(
            (WorkdayBoard(tenant, str(config["host"]), str(config["site"])),),
            queries=queries or TARGET_QUERIES,
            max_results_per_query=int(config.get("max_results_per_query", 60)),
        )
    if definition.ats == "personio" and tenant:
        return PersonioConnector((tenant,))
    if definition.ats == "recruitee" and tenant:
        return RecruiteeConnector((tenant,))
    if definition.ats == "teamtailor" and tenant:
        return TeamtailorConnector((tenant,))
    if definition.ats == "jsonld":
        urls = tuple(str(value) for value in config.get("urls", []) if value)
        return JsonLdConnector(urls) if urls else None
    if definition.ats == "sitemap_jsonld":
        urls = tuple(str(value) for value in config.get("sitemap_urls", []) if value)
        return SitemapJsonLdConnector(
            urls,
            max_sitemaps=int(config.get("max_sitemaps", 20)),
            max_job_pages=int(config.get("max_job_pages", 250)),
        ) if urls else None
    if definition.ats == "workable_xml" and definition.permission_status == "approved":
        return WorkableXmlConnector(
            str(config.get("feed_url", "https://www.workable.com/boards/workable.xml"))
        )
    if definition.ats == "cv_library" and definition.permission_status == "approved":
        api_key = os.getenv(str(config.get("api_key_env", "CVLIBRARY_API_KEY")))
        if api_key:
            queries = tuple(str(value) for value in config.get("queries", TARGET_QUERIES) if value)
            return CVLibraryConnector(
                api_key,
                queries or TARGET_QUERIES,
                partner_approved=True,
                geo=str(config.get("geo", "United Kingdom")),
                distance_miles=int(config.get("distance_miles", 750)),
                posted_days=int(config.get("posted_days", 14)),
                per_page=int(config.get("per_page", 100)),
                max_results_per_query=int(config.get("max_results_per_query", 200)),
            )
    if definition.ats == "oracle_hcm":
        credentials = _registry_basic_credentials(config)
        if credentials and config.get("base_url") and config.get("apply_url_template"):
            return OracleHcmConnector(
                str(config["base_url"]),
                *credentials,
                definition.employer,
                str(config["apply_url_template"]),
            )
    if definition.ats == "icims" and tenant:
        credentials = _registry_basic_credentials(config)
        if credentials and config.get("portal"):
            return IcimsConnector(
                tenant,
                str(config["portal"]),
                *credentials,
                definition.employer,
            )
    if definition.ats == "sap_successfactors":
        credentials = _registry_basic_credentials(config)
        if (
            credentials
            and config.get("odata_base_url")
            and config.get("apply_url_template")
            and config.get("filter_expression")
        ):
            return SapSuccessFactorsConnector(
                str(config["odata_base_url"]),
                *credentials,
                definition.employer,
                str(config["apply_url_template"]),
                str(config["filter_expression"]),
            )
    return None


def _registry_basic_credentials(config: dict[str, Any]) -> tuple[str, str] | None:
    username = os.getenv(str(config.get("username_env", ""))) if config.get("username_env") else None
    password = os.getenv(str(config.get("password_env", ""))) if config.get("password_env") else None
    return (username, password) if username and password else None


def _set_query_option(params: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and value != "":
        params[key] = str(value).lower() if isinstance(value, bool) else value


def _set_adzuna_query_option(params: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and value != "":
        params[key] = int(value) if isinstance(value, bool) else value


def _adzuna_employment_type(item: dict[str, Any]) -> str:
    values = [item.get("contract_type"), item.get("contract_time")]
    return "_".join(str(value).lower() for value in values if value) or "unknown"


def _reed_raw_job(summary: dict[str, Any], detail: dict[str, Any], default_location: str) -> dict[str, Any]:
    def value(detail_key: str, summary_key: str | None = None, default: Any = None) -> Any:
        result = detail.get(detail_key)
        if result is not None and result != "":
            return result
        return summary.get(summary_key or detail_key, default)

    contract_type = value("contractType", default="")
    hours = "full_time" if detail.get("fullTime") else "part_time" if detail.get("partTime") else ""
    employment_type = "_".join(part for part in (str(contract_type).lower(), hours) if part) or "unknown"
    currency = str(value("currency", default="GBP") or "GBP").upper()
    minimum = value("yearlyMinimumSalary", "minimumSalary")
    maximum = value("yearlyMaximumSalary", "maximumSalary")
    salary = _salary_string(minimum, maximum) if currency == "GBP" else ""
    source_url = str(value("jobUrl", default="") or "")
    return {
        "id": value("jobId"),
        "title": value("jobTitle", default=""),
        "company": value("employerName", default="Unknown company"),
        "location": value("locationName", default=default_location),
        "description": value("jobDescription", default=""),
        "url": value("externalUrl", default=source_url) or source_url,
        "source_url": source_url,
        "posted_at": value("datePosted", "date"),
        "closes_at": value("expirationDate"),
        "employment_type": employment_type,
        "salary": salary,
    }


def _salary_string(minimum: Any, maximum: Any) -> str:
    if minimum is None and maximum is None:
        return ""
    if minimum is not None and maximum is not None:
        return f"Salary £{minimum} - £{maximum} per year"
    return f"Salary £{minimum or maximum} per year"


def connectors_from_environment(
    *, excluded_ats: set[str] | frozenset[str] = frozenset(),
    detail_store: ListingDetailsStore | None = None,
    hydration_dispatcher: ReedHydrationDispatcher | None = None,
) -> list[JobConnector]:
    connectors: list[JobConnector] = []
    if registry_path := os.getenv("SOURCE_REGISTRY_PATH"):
        connectors.extend(connectors_from_registry(registry_path, excluded_ats=excluded_ats))
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if app_id and app_key:
        connectors.append(
            AdzunaConnector(
                app_id,
                app_key,
                queries=_csv_env("ADZUNA_QUERIES") or TARGET_QUERIES,
                query_parameter=os.getenv("ADZUNA_QUERY_PARAMETER", "title_only"),
                location=os.getenv("ADZUNA_LOCATION") or None,
                results_per_page=_int_env("ADZUNA_RESULTS_PER_PAGE", 50),
                max_results_per_query=_int_env("ADZUNA_MAX_RESULTS_PER_QUERY", 100),
                max_days_old=_optional_int_env("ADZUNA_MAX_DAYS_OLD", 14),
            )
        )
    if boards := _csv_env("GREENHOUSE_BOARDS"):
        connectors.extend(GreenhouseConnector((board,)) for board in boards)
    if sites := _csv_env("LEVER_SITES"):
        connectors.extend(LeverConnector((site,)) for site in sites)
    if boards := _csv_env("ASHBY_BOARDS"):
        connectors.extend(AshbyConnector((board,)) for board in boards)
    reed_key = os.getenv("REED_API_KEY")
    if reed_key:
        connectors.append(
            ReedConnector(
                reed_key,
                queries=_csv_env("REED_QUERIES") or TARGET_QUERIES,
                location=os.getenv("REED_LOCATION") or None,
                results_per_page=_int_env("REED_RESULTS_PER_PAGE", 100),
                max_results_per_query=_int_env("REED_MAX_RESULTS_PER_QUERY", 200),
                detail_results_limit=_int_env("REED_DETAIL_RESULTS_LIMIT", 100),
                detail_store=detail_store,
                hydration_dispatcher=hydration_dispatcher,
            )
        )
    cv_library_key = os.getenv("CVLIBRARY_API_KEY")
    if cv_library_key and _bool_env("CVLIBRARY_PARTNER_APPROVED"):
        connectors.append(
            CVLibraryConnector(
                cv_library_key,
                queries=_csv_env("CVLIBRARY_QUERIES") or TARGET_QUERIES,
                partner_approved=True,
                geo=os.getenv("CVLIBRARY_GEO", "United Kingdom"),
                distance_miles=_int_env("CVLIBRARY_DISTANCE_MILES", 750),
                posted_days=_int_env("CVLIBRARY_POSTED_DAYS", 14),
                per_page=_int_env("CVLIBRARY_RESULTS_PER_PAGE", 100),
                max_results_per_query=_int_env("CVLIBRARY_MAX_RESULTS_PER_QUERY", 200),
            )
        )
    if companies := _csv_env("SMARTRECRUITERS_COMPANIES"):
        connectors.extend(SmartRecruitersConnector((company,)) for company in companies)
    if "workday" not in excluded_ats and (values := _csv_env("WORKDAY_BOARDS")):
        connectors.extend(WorkdayConnector((WorkdayBoard.parse(value),)) for value in values)
    if _bool_env("WORKABLE_GLOBAL_FEED_ENABLED") and _bool_env("WORKABLE_PARTNER_APPROVED"):
        connectors.append(
            WorkableXmlConnector(os.getenv("WORKABLE_FEED_URL", "https://www.workable.com/boards/workable.xml"))
        )
    if companies := _csv_env("PERSONIO_COMPANIES"):
        connectors.extend(PersonioConnector((company,)) for company in companies)
    if companies := _csv_env("RECRUITEE_COMPANIES"):
        connectors.extend(RecruiteeConnector((company,)) for company in companies)
    if companies := _csv_env("TEAMTAILOR_COMPANIES"):
        connectors.extend(TeamtailorConnector((company,)) for company in companies)
    if urls := _csv_env("JSONLD_CAREER_URLS"):
        connectors.extend(JsonLdConnector((url,)) for url in urls)
    if urls := _csv_env("SITEMAP_URLS"):
        connectors.extend(SitemapJsonLdConnector((url,)) for url in urls)
    return connectors


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


def _int_env(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _optional_int_env(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value) if value.strip() else None


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def collect_jobs(connectors: list[JobConnector], polite_delay_seconds: float = 0.0) -> tuple[list[Job], list[str]]:
    jobs, errors, _ = collect_jobs_with_counts(connectors, polite_delay_seconds)
    return jobs, errors


def collect_jobs_with_counts(
    connectors: list[JobConnector], polite_delay_seconds: float = 0.0
) -> tuple[list[Job], list[str], dict[str, int]]:
    jobs, errors, source_counts, _ = collect_jobs_with_diagnostics(connectors, polite_delay_seconds)
    return jobs, errors, source_counts


def collect_jobs_with_diagnostics(
    connectors: list[JobConnector], polite_delay_seconds: float = 0.0
) -> tuple[list[Job], list[str], dict[str, int], list[SourceFetchDiagnostic]]:
    jobs: list[Job] = []
    errors: list[str] = []
    source_counts: dict[str, int] = {}
    diagnostics: list[SourceFetchDiagnostic] = []
    for connector in connectors:
        label = _connector_label(connector)
        started = datetime.now(UTC)
        error: str | None = None
        fetched: list[Job] = []
        try:
            fetched = connector.fetch()
            for job in fetched:
                job.source_key = job.source_key or label
                if os.getenv("FRESHNESS_MODE", "off") == "observe":
                    native = connector.name.removesuffix("_discovered") in {
                        "greenhouse", "lever", "ashby", "workday", "smartrecruiters"
                    }
                    job.freshness_evidence = {
                        "outcome": "live_detail" if native else "seen_in_search",
                        # Conservative request-start time; never the later rank/cache-use time.
                        "observed_at": started.isoformat(), "source_id": job.source_id,
                    }
                for listing in job.listings:
                    listing.source_key = listing.source_key or job.source_key
                    listing.freshness_evidence = job.freshness_evidence
            jobs.extend(fetched)
            source_counts[connector.name] = source_counts.get(connector.name, 0) + len(fetched)
        except ConnectorError as exc:
            error = str(exc)
            errors.append(f"{label}: {error}")
            source_counts.setdefault(connector.name, 0)
        except Exception as exc:  # one malformed tenant must not suppress all other sources
            error = type(exc).__name__
            errors.append(f"{label}:{error}")
            source_counts.setdefault(connector.name, 0)
            logger.exception("Unexpected connector failure", extra={"connector": label})
        completed = datetime.now(UTC)
        definition = connector.definition if isinstance(connector, RegisteredConnector) else None
        discovered_employers = {job.company for job in fetched if job.company}
        diagnostics.append(
            SourceFetchDiagnostic(
                source_key=label,
                source=connector.name,
                employer=(
                    definition.employer
                    if definition
                    else next(iter(discovered_employers)) if len(discovered_employers) == 1 else None
                ),
                ats=definition.ats if definition else connector.name.removesuffix("_discovered"),
                tenant=(
                    definition.tenant
                    if definition
                    else label.split(":", 1)[1] if ":" in label else None
                ),
                started_at=started.isoformat(),
                completed_at=completed.isoformat(),
                duration_ms=round((completed - started).total_seconds() * 1000),
                status="error" if error else ("empty" if not fetched else "healthy"),
                listings_fetched=len(fetched),
                error=error,
                detail_metrics=getattr(connector, "detail_metrics", None),
                coverage_scope=("complete_board" if not error and isinstance(
                    _inner_connector(connector), (GreenhouseConnector, LeverConnector, AshbyConnector)
                ) else "filtered_or_unknown"),
            )
        )
        if polite_delay_seconds:
            time.sleep(polite_delay_seconds)
    return jobs, errors, source_counts, diagnostics


def _connector_label(connector: JobConnector) -> str:
    if isinstance(connector, RegisteredConnector):
        return connector.definition.key
    for attribute in ("board_tokens", "site_names", "board_names", "company_ids", "urls"):
        values = getattr(connector, attribute, ())
        if values:
            return f"{connector.name.removesuffix('_discovered')}:{values[0]}"
    boards = getattr(connector, "boards", ())
    if boards:
        return f"{connector.name.removesuffix('_discovered')}:{boards[0].tenant}"
    return connector.name


def discover_ats_connectors(
    jobs: list[Job],
    configured: list[JobConnector],
    max_connectors: int = 50,
    *,
    excluded_ats: set[str] | frozenset[str] = frozenset(),
) -> list[JobConnector]:
    """Discover known public ATS tenants from direct URLs without crawling arbitrary hosts."""
    greenhouse: set[str] = set()
    lever: set[str] = set()
    ashby: set[str] = set()
    personio: set[str] = set()
    recruitee: set[str] = set()
    smartrecruiters: set[str] = set()
    workday: set[WorkdayBoard] = set()
    for job in jobs:
        for value in (job.apply_url, job.source_url):
            parts = urllib.parse.urlsplit(value)
            host = (parts.hostname or "").lower()
            segments = [urllib.parse.unquote(item) for item in parts.path.split("/") if item]
            if host in {"boards.greenhouse.io", "jobs.greenhouse.io"} and segments:
                greenhouse.add(segments[0])
            elif host == "jobs.lever.co" and segments:
                lever.add(segments[0])
            elif host == "jobs.ashbyhq.com" and segments:
                ashby.add(segments[0])
            elif host.endswith(".jobs.personio.de"):
                personio.add(host.removesuffix(".jobs.personio.de"))
            elif host.endswith(".recruitee.com"):
                recruitee.add(host.removesuffix(".recruitee.com"))
            elif host == "jobs.smartrecruiters.com" and segments:
                smartrecruiters.add(segments[0])
            elif host.endswith(".myworkdayjobs.com") and len(segments) >= 2:
                tenant = host.split(".", 1)[0]
                site_index = 1 if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segments[0]) else 0
                site = segments[site_index]
                if site.lower() not in {"job", "jobs", "search"}:
                    workday.add(WorkdayBoard(tenant, host, site))

    concrete = [_inner_connector(source) for source in configured]
    existing_greenhouse = {
        item for source in concrete if isinstance(source, GreenhouseConnector) for item in source.board_tokens
    }
    existing_lever = {
        item for source in concrete if isinstance(source, LeverConnector) for item in source.site_names
    }
    existing_ashby = {
        item for source in concrete if isinstance(source, AshbyConnector) for item in source.board_names
    }
    existing_personio = {
        item for source in concrete if isinstance(source, PersonioConnector) for item in source.company_ids
    }
    existing_recruitee = {
        item for source in concrete if isinstance(source, RecruiteeConnector) for item in source.company_ids
    }
    existing_smartrecruiters = {
        item
        for source in concrete
        if isinstance(source, SmartRecruitersConnector)
        for item in source.company_ids
    }
    existing_workday = {
        (board.host, board.site)
        for source in concrete
        if isinstance(source, WorkdayConnector)
        for board in source.boards
    }

    discovered: list[JobConnector] = []
    if "greenhouse" not in excluded_ats:
        discovered.extend(
            GreenhouseConnector((value,), name="greenhouse_discovered")
            for value in sorted(greenhouse - existing_greenhouse)
        )
    if "lever" not in excluded_ats:
        discovered.extend(
            LeverConnector((value,), name="lever_discovered")
            for value in sorted(lever - existing_lever)
        )
    if "ashby" not in excluded_ats:
        discovered.extend(
            AshbyConnector((value,), name="ashby_discovered")
            for value in sorted(ashby - existing_ashby)
        )
    if "personio" not in excluded_ats:
        discovered.extend(
            PersonioConnector((value,), name="personio_discovered")
            for value in sorted(personio - existing_personio)
        )
    if "recruitee" not in excluded_ats:
        discovered.extend(
            RecruiteeConnector((value,), name="recruitee_discovered")
            for value in sorted(recruitee - existing_recruitee)
        )
    if "smartrecruiters" not in excluded_ats:
        existing_smartrecruiters_lower = {value.lower() for value in existing_smartrecruiters}
        discovered.extend(
            SmartRecruitersConnector(
                (value,), queries=("",), max_results_per_query=100, name="smartrecruiters_discovered"
            )
            for value in sorted(
                value for value in smartrecruiters if value.lower() not in existing_smartrecruiters_lower
            )
        )
    if "workday" not in excluded_ats:
        discovered.extend(
            WorkdayConnector(
                (board,), queries=("",), max_results_per_query=60, name="workday_discovered"
            )
            for board in sorted(
                (board for board in workday if (board.host, board.site) not in existing_workday),
                key=lambda item: (item.host, item.site),
            )
        )
    return discovered[:max_connectors]


def _inner_connector(connector: JobConnector) -> JobConnector:
    return connector.connector if isinstance(connector, RegisteredConnector) else connector


def deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    by_id: dict[str, Job] = {}
    source_priority = {
        "workday": 5,
        "smartrecruiters": 5,
        "workable": 5,
        "personio": 5,
        "recruitee": 5,
        "ashby": 5,
        "greenhouse": 5,
        "lever": 5,
        "jsonld": 4,
        "reed": 3,
        "adzuna": 2,
        "fixture": 1,
    }
    for job in jobs:
        existing = by_id.get(job.id)
        if existing is None:
            by_id[job.id] = job
            continue
        listings = {(item.source, item.source_id): item for item in existing.listings}
        for listing in job.listings:
            key = (listing.source, listing.source_id)
            previous = listings.get(key)
            if previous:
                listing.first_seen_at = previous.first_seen_at
            listings[key] = listing
        if source_priority.get(job.source.removesuffix("_discovered"), 0) > source_priority.get(
            existing.source.removesuffix("_discovered"), 0
        ):
            job.listings = list(listings.values())
            by_id[job.id] = job
        else:
            existing.listings = list(listings.values())
    return list(by_id.values())
