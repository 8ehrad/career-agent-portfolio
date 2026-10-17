"""Durable source-listing details, separate from recommendations and model results."""
import hashlib
import json
from typing import Protocol
from .normalization import clean_text

VERSION = 1
# Search fields that can signal a material advert change; exclude volatile popularity fields.
SUMMARY_FIELDS = ("jobId", "jobTitle", "employerId", "employerName", "locationName",
                  "jobDescription", "minimumSalary", "maximumSalary", "date", "jobUrl")
DETAIL_FIELDS = ("jobId", "jobTitle", "employerId", "employerName", "locationName",
                 "jobDescription", "minimumSalary", "maximumSalary", "yearlyMinimumSalary",
                 "yearlyMaximumSalary", "currency", "salaryType", "contractType", "fullTime",
                 "partTime", "expirationDate", "datePosted", "jobUrl", "externalUrl")


class ListingDetailsStore(Protocol):
    def get_listing_details(self, source: str, source_ids: list[str]) -> dict[str, dict]: ...
    def put_listing_details(self, record: dict) -> None: ...


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def summary_hash(summary):
    return content_hash({key: summary.get(key) for key in SUMMARY_FIELDS})


def normalized_summary(summary):
    """Return the durable allowlisted Reed search-result snapshot."""
    return {key: summary[key] for key in SUMMARY_FIELDS if key in summary}


def make_record(source_id, summary, detail, now):
    if not isinstance(detail, dict) or not isinstance(detail.get("jobDescription"), str) or not clean_text(detail["jobDescription"]):
        raise ValueError("Missing description in detail response")
    if detail.get("jobId") is not None and str(detail["jobId"]) != source_id:
        raise ValueError("Detail response identity mismatch")
    details = {k: detail[k] for k in DETAIL_FIELDS if k in detail}
    return {"schema_version": VERSION, "source": "reed", "source_id": source_id,
            "fetched_at": now.isoformat(), "summary_hash": summary_hash(summary),
            "summary": normalized_summary(summary),
            "details": details, "content_hash": content_hash(details)}


def reusable(record, source_id, summary):
    """Reuse valid details until relevant search metadata changes; age alone is not a miss."""
    try:
        return (record["schema_version"] == VERSION and record["source"] == "reed"
                and record["source_id"] == source_id
                and record["summary_hash"] == summary_hash(summary)
                and record["content_hash"] == content_hash(record["details"])
                and bool(clean_text(record["details"]["jobDescription"])))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False
