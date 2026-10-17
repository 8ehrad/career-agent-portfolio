"""Observation-only vacancy freshness. Independent of fit cache and application status.

One CAS-protected row per source/tenant/native ID owns evidence and targeted check work.
No function in this module suppresses analysis, changes JobStatus, or sends a digest.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import os
import re
import threading
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import urlsplit, unquote
from zoneinfo import ZoneInfo

from .models import Job

logger = logging.getLogger(__name__)
NATIVE = {"greenhouse", "ashby", "lever", "workday", "smartrecruiters"}
DAY = 86400


def timestamp(value):
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp())
    except (TypeError, ValueError):
        return 0


def normalized(value):
    return " ".join(re.findall(r"[a-z0-9]+", html.unescape(str(value or "")).lower()))


def reed_expiration_timestamp(value):
    # The real Reed detail API uses British dates without a time. Keep that day inclusive.
    try:
        day = datetime.strptime(str(value), "%d/%m/%Y")
        return int(day.replace(hour=23, minute=59, second=59, tzinfo=ZoneInfo("Europe/London")).timestamp())
    except ValueError:
        return timestamp(value)


def target_for(job, definitions=()):
    """Resolve only existing source identity; never guess a board or follow an aggregator."""
    source = job.source.removesuffix("_discovered")
    definition = next((item for item in definitions if item.key == job.source_key and item.ats == source), None)
    if definition and (definition.status != "active" or definition.permission_status not in {"public", "approved"}):
        return None
    tenant = definition.tenant if definition else ""
    native_id = str(job.source_id)
    parts = urlsplit(job.source_url or job.apply_url)
    path = [unquote(item) for item in parts.path.split("/") if item]
    route = {}
    if source in {"ashby", "lever", "greenhouse", "smartrecruiters"}:
        hosts = {
            "ashby": {"jobs.ashbyhq.com"}, "lever": {"jobs.lever.co"},
            "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"},
            "smartrecruiters": {"jobs.smartrecruiters.com"},
        }
        if not tenant and parts.scheme == "https" and parts.hostname in hosts[source] and path:
            tenant = path[0]
        if source == "ashby":
            identity_url = urlsplit(native_id)
            id_parts = identity_url.path.strip("/").split("/")
            if identity_url.hostname == "jobs.ashbyhq.com" and len(id_parts) >= 2:
                native_id = id_parts[1]
                if tenant and tenant.lower() != id_parts[0].lower():
                    return None
        if not tenant or tenant in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", tenant):
            return None
    elif source == "workday":
        if definition is None:
            return None
        host, site = definition.config.get("host", ""), definition.config.get("site", "")
        if not re.fullmatch(r"[a-z0-9-]+\.wd[0-9]+\.myworkdayjobs\.com", host):
            return None
        if parts.hostname != host or "job" not in path or not re.fullmatch(r"[A-Za-z0-9_-]+", site):
            return None
        external = "/" + "/".join(path[path.index("job"):])
        if any(item in {"..", "."} for item in path):
            return None
        route = {"host": host, "site": site, "external_path": external}
    elif source in {"reed", "adzuna"}:
        tenant = source
        if not native_id.isdigit():
            return None
    else:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", native_id):
        return None
    key = hashlib.sha256(f"{source}:{tenant}:{native_id}".encode()).hexdigest()
    return {"key": key, "source": source, "tenant": tenant, "native_id": native_id,
            "source_key": job.source_key,
            "title": job.title, "company": job.company, "location": job.location,
            "job_id": job.id, "route": route, "native": source in NATIVE}


class FreshnessStore:
    """Memory test store or DynamoDB CAS store, using the existing table and due GSI."""

    def __init__(self, table=None, customer_id="personal"):
        self.table, self.customer_id = table, customer_id
        self.rows = {}
        self.lock = threading.RLock()

    def _key(self, key):
        return {"pk": f"FRESHNESS#{self.customer_id}", "sk": key}

    def get(self, key):
        if self.table is None:
            with self.lock:
                return copy.deepcopy(self.rows.get(key))
        item = self.table.get_item(Key=self._key(key), ConsistentRead=True).get("Item")
        return json.loads(item["payload"]) if item else None

    def mutate(self, key, change):
        for _ in range(8):
            with self.lock:
                old = self.get(key)
                row = change(copy.deepcopy(old))
                if row is None:
                    return old
                row["revision"] = (old or {}).get("revision", 0) + 1
                if self.table is None:
                    self.rows[key] = copy.deepcopy(row)
                    return row
                item = {**self._key(key), "entity_type": "freshness", "revision": row["revision"],
                        "payload": json.dumps(row, separators=(",", ":"))}
                if key.startswith("VACANCY#") and row.get("requested"):
                    item.update(gsi1pk=f"FRESHNESS_DUE#{self.customer_id}",
                                gsi1sk=f"{self.due_at(row):012d}#{key}")
                if row.get("expires_at"):
                    item["expires_at"] = row["expires_at"]
                kwargs = {"Item": item, "ConditionExpression": "attribute_not_exists(pk)"}
                if old:
                    kwargs.update(ConditionExpression="revision = :r",
                                  ExpressionAttributeValues={":r": old["revision"]})
                try:
                    self.table.put_item(**kwargs)
                    return row
                except Exception as exc:
                    if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
        raise RuntimeError("Freshness update contention")

    def list_rows(self, limit=2000):
        if self.table is None:
            rows = [copy.deepcopy(v) for k, v in sorted(self.rows.items()) if k.startswith("VACANCY#")]
            return rows[:limit], len(rows) <= limit
        params = {"KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
                  "ExpressionAttributeValues": {":pk": self._key("")["pk"], ":prefix": "VACANCY#"},
                  "ConsistentRead": True}
        rows = []
        while len(rows) < limit:
            response = self.table.query(**params, Limit=min(100, limit-len(rows)))
            rows.extend(json.loads(item["payload"]) for item in response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return rows, True
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return rows, False

    @staticmethod
    def due_at(row):
        return int(max(row.get("next_check_at", 0), row.get("lease_until", 0),
                       row.get("sent_at", -600) + 600))

    def due(self, now, limit=50):
        if self.table is None:
            rows, _ = self.list_rows(100000)
            return sorted((r for r in rows if r.get("requested") and self.due_at(r) <= now),
                          key=lambda r: (self.due_at(r), r["target"]["key"]))[:limit]
        response = self.table.query(IndexName="gsi1", Limit=limit,
            KeyConditionExpression="gsi1pk = :pk AND gsi1sk <= :until",
            ExpressionAttributeValues={":pk": f"FRESHNESS_DUE#{self.customer_id}", ":until": f"{now:012d}#~"})
        return [json.loads(item["payload"]) for item in response.get("Items", [])]

    def permit_request(self, source, now, daily_limit=100, interval=2):
        """Shared source pace/circuit breaker plus a bounded personal UTC-day allowance."""
        allowed = False
        def pace(old):
            nonlocal allowed
            allowed = False
            row = old or {}
            if max(row.get("blocked_until", 0), row.get("next_request_at", 0)) > now:
                return None
            allowed = True
            row["next_request_at"] = now + interval
            return row
        self.mutate(f"SOURCE#{source}", pace)
        if not allowed:
            return False
        allowed = False
        def reserve(old):
            nonlocal allowed
            allowed = False
            row = old or {"requests": 0, "expires_at": now + 30*DAY}
            if row["requests"] >= daily_limit:
                return None
            allowed = True
            row["requests"] += 1
            return row
        self.mutate(f"BUDGET#{now // DAY}", reserve)
        return allowed

    def block(self, source, until):
        self.mutate(f"SOURCE#{source}", lambda old: {**(old or {}),
                    "blocked_until": max((old or {}).get("blocked_until", 0), until)})


def source_state(row, now, max_age):
    if not row:
        return "verification_due"
    latest, good = row.get("latest", {}), row.get("positive", {})
    if row.get("closed", {}).get("at", 0) >= good.get("at", 0) and row.get("closed") and row["target"]["native"]:
        return "confirmed_closed"
    if latest.get("outcome") in {"not_found", "identity_mismatch", "source_closed"} and latest.get("at", 0) >= good.get("at", 0):
        return "verification_due"
    if good and 0 <= now-good["at"] <= max_age:
        return "verified_current"
    return "verification_due"


def canonical_state(rows, now, max_age):
    native_closed = max((r.get("closed", {}).get("at", 0) for r in rows
                         if r["target"]["native"] and source_state(r, now, max_age) == "confirmed_closed"), default=0)
    live = [r for r in rows if source_state(r, now, max_age) == "verified_current"]
    if native_closed:
        return "verified_current" if any(r["target"]["native"] and r["positive"]["at"] > native_closed for r in live) else "confirmed_closed"
    return "verified_current" if live else "verification_due"


class FreshnessService:
    def __init__(self, store, definitions=(), sqs=None, queue_url=None, clock=None):
        self.store, self.definitions = store, definitions
        self.sqs, self.queue_url = sqs, queue_url
        self.clock = clock or (lambda: int(datetime.now(UTC).timestamp()))

    def observe(self, target, outcome, at, reason="", expected_lease=None):
        at = int(at)
        def update(old):
            row = old or {"target": target, "attempts": 0}
            if expected_lease and row.get("lease_token") != expected_lease:
                return None
            latest = row.get("latest", {})
            if outcome == "seen_in_search" and at <= row.get("last_search_seen_at", 0):
                return None
            stronger_equal = ((latest.get("outcome") == "seen_in_search" and outcome != "seen_in_search")
                              or (outcome == "explicit_closed" and latest.get("outcome") != "explicit_closed"))
            if at < latest.get("at", 0) or (at == latest.get("at", 0) and not stronger_equal):
                if expected_lease:
                    row.pop("lease_token", None)
                    row["lease_until"] = 0
                    # A shared board cache is evidence from its original fetch, not a new check.
                    # Keep any newer contrary evidence and wait beyond the cache lifetime.
                    row["next_check_at"] = max(row.get("next_check_at", 0), self.clock()+3600)
                    if source_state(row, self.clock(), DAY) == "verified_current":
                        row["requested"] = False
                    return row
                return None
            event = {"at": at, "outcome": outcome, "reason": reason}
            if outcome != "seen_in_search" or not row.get("latest"):
                row["latest"] = event
            row["events"] = (row.get("events", []) + [event])[-8:]
            if outcome == "live_detail":
                row["positive"] = event
                row["target"] = target
                row["attempts"] = 0
                row["requested"] = False
                row["next_check_at"] = at + DAY
            elif outcome == "seen_in_search":
                row["last_search_seen_at"] = at
            else:
                if outcome == "explicit_closed":
                    row["closed"] = event
                row["attempts"] = row.get("attempts", 0) + 1
                row["requested"] = outcome not in {"unsupported", "explicit_closed", "source_closed"}
                row["next_check_at"] = at + min(DAY, 900 * 2**min(row["attempts"], 6))
                if outcome in {"unsupported", "explicit_closed", "source_closed"}:
                    row["next_check_at"] = at + 7*DAY
            if expected_lease:
                row.pop("lease_token", None)
                row["lease_until"] = 0
            return row
        return self.store.mutate(f"VACANCY#{target['key']}", update)

    def targets(self, job):
        # Preserve source appearances, but never upgrade them from canonical title alone.
        targets = {}
        primary = target_for(job, self.definitions)
        if primary:
            targets[primary["key"]] = primary
        for listing in job.listings:
            appearance = copy.copy(job)
            for field in ("source", "source_id", "source_url", "apply_url", "source_key"):
                setattr(appearance, field, getattr(listing, field))
            target = target_for(appearance, self.definitions)
            if target:
                targets.setdefault(target["key"], target)
        return list(targets.values())

    def request(self, target, reason, now):
        created = False
        def update(old):
            nonlocal created
            created = False
            row = old or {"target": target, "attempts": 0, "next_check_at": now}
            if row.get("requested"):
                row["interest_until"] = now+7*DAY
                return row
            created = True
            row.update(requested=True, reason=reason, interest_until=now+7*DAY)
            return row
        self.store.mutate(f"VACANCY#{target['key']}", update)
        return created

    def observe_collection(self, jobs):
        # Only previously monitored vacancies: no per-listing DynamoDB lookups over the full corpus.
        rows, complete = self.store.list_rows()
        known = {r["target"]["key"] for r in rows}
        count = 0
        for job in jobs:
            target = target_for(job, self.definitions)
            evidence = job.freshness_evidence or {}
            at = timestamp(evidence.get("observed_at"))
            if target and target["key"] in known and evidence.get("source_id") == job.source_id and 0 < at <= self.clock():
                if evidence.get("outcome") in {"live_detail", "seen_in_search"}:
                    self.observe(target, evidence["outcome"], at, "collection_reuse")
                    count += 1
        return {"observations_reused": count, "complete": complete}

    def assess(self, jobs, stage, max_age=2*DAY):
        """Count hypothetical holds and enqueue only stale candidates; never filter jobs."""
        now, counts = self.clock(), Counter()
        for job in jobs:
            targets = self.targets(job)
            appearances = [job]
            for listing in job.listings:
                appearance = copy.copy(job)
                for field in ("source", "source_id", "source_url", "apply_url", "source_key", "freshness_evidence"):
                    setattr(appearance, field, getattr(listing, field))
                appearances.append(appearance)
            for appearance in appearances:
                evidence = appearance.freshness_evidence or {}
                primary = target_for(appearance, self.definitions)
                if primary and evidence.get("outcome") in {"live_detail", "seen_in_search"}:
                    at = timestamp(evidence.get("observed_at"))
                    if 0 < at <= now and evidence.get("source_id") == appearance.source_id:
                        self.observe(primary, evidence["outcome"], at, "collection")
            rows = []
            for target in targets:
                row = self.store.get(f"VACANCY#{target['key']}")
                if row:
                    if normalized(row["target"]["title"]) != normalized(target["title"]):
                        row["latest"] = {"outcome": "identity_mismatch", "at": now}
                    rows.append(row)
            state = canonical_state(rows, now, max_age)
            counts[state] += 1
            if state == "verification_due":
                # One strongest route is enough. Alternatives can be tried on later observations.
                ranked = sorted(targets, key=lambda t: (not t["native"], t["source"] == "adzuna"))
                target = next((t for t in ranked if (self.store.get(f"VACANCY#{t['key']}") or {}).get("next_check_at", 0) <= now), None)
                if target:
                    counts["check_requested"] += int(self.request(target, stage, now))
                if not targets:
                    counts["unsupported_identity"] += 1
        counts["evaluated"] = len(jobs)
        for key in ("verified_current", "verification_due", "confirmed_closed", "check_requested", "unsupported_identity"):
            counts.setdefault(key, 0)
        counts["would_hold"] = counts["verification_due"] + counts["confirmed_closed"]
        result = {"mode": "observe", "stage": stage, "max_age_seconds": max_age, **dict(counts)}
        try:
            result["messages_sent"] = self.dispatch_due()
        except Exception:
            logger.exception("Freshness dispatch unavailable; durable requests retained")
            result["status"] = "partial"
        return result

    def dispatch_due(self, limit=50):
        if self.sqs is None or not self.queue_url:
            return 0
        now, sent = self.clock(), 0
        for snapshot in self.store.due(now, limit):
            key = snapshot["target"]["key"]
            claimed = False
            def dispatch(old):
                nonlocal claimed
                claimed = False
                if old and old.get("interest_until", now+1) <= now:
                    old["requested"] = False
                    return old
                if not old or not old.get("requested") or old.get("next_check_at", 0) > now or old.get("lease_until", 0) > now or old.get("sent_at", 0)+600 > now:
                    return None
                claimed = True
                old["sent_at"] = now
                return old
            self.store.mutate(f"VACANCY#{key}", dispatch)
            if not claimed:
                continue
            self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps({
                "schema_version": 1, "kind": "freshness_check", "customer_id": self.store.customer_id, "key": key}))
            sent += 1
        return sent

    def refresh_tracked(self, jobs, limit=50):
        priority = {"ready_to_apply": 0, "preparing": 1, "shortlisted": 2}
        rows, _ = self.store.list_rows()
        by_key = {r["target"]["key"]: r for r in rows}
        def last_activity(job):
            return max((max(r.get("latest", {}).get("at", 0), r.get("next_check_at", 0))
                        for t in self.targets(job) if (r := by_key.get(t["key"]))), default=0)
        # Rotate unknown/oldest first; a fixed first page would starve later tracked jobs.
        tracked = [j for j in jobs if str(j.status) in priority]
        routable = [j for j in tracked if self.targets(j)]
        selected = sorted(routable,
                          key=lambda j: (last_activity(j), priority[str(j.status)], j.id))[:limit]
        return {**self.assess(selected, "tracked", DAY), "tracked_without_route": len(tracked)-len(routable)}

    def summary(self, limit=2000):
        now = self.clock()
        rows, complete = self.store.list_rows(limit)
        counts = Counter(source_state(r, now, DAY) for r in rows)
        for state in ("verified_current", "verification_due", "confirmed_closed"):
            counts.setdefault(state, 0)
        sources = {}
        for row in rows:
            sources.setdefault(row["target"]["source"], Counter())[row.get("latest", {}).get("outcome", "unchecked")] += 1
        return {"mode": "observe", "complete": complete, "count_kind": "exact" if complete else "lower_bound",
                "unit": "source vacancies with recorded freshness activity; not all discovered or canonical jobs",
                "observed": len(rows), "state_counts": dict(counts), "by_source": {k: dict(v) for k, v in sources.items()},
                "pending": sum(bool(r.get("requested")) for r in rows),
                "oldest_overdue_seconds": max((max(0, now-r.get("next_check_at", now)) for r in rows if r.get("requested")), default=0),
                "items": [{"key": r["target"]["key"], "source": r["target"]["source"],
                           "title": r["target"]["title"], "job_id": r["target"]["job_id"],
                           "state": source_state(r, now, DAY), "latest": r.get("latest"),
                           "last_positive_at": r.get("positive", {}).get("at"),
                           "next_check_at": r.get("next_check_at"), "requested": bool(r.get("requested"))} for r in rows]}


def service_from_environment(repo, sqs=None):
    if os.getenv("FRESHNESS_MODE", "off") != "observe":
        return None
    try:
        return _configured_service(repo, sqs)
    except Exception:
        logger.exception("Freshness initialization unavailable; existing pipeline unchanged")
        return None


def _configured_service(repo, sqs):
    from .source_registry import load_source_registry
    definitions = load_source_registry(os.environ["SOURCE_REGISTRY_PATH"]) if os.getenv("SOURCE_REGISTRY_PATH") else []
    if not hasattr(repo, "_freshness_store"):
        repo._freshness_store = FreshnessStore(getattr(repo, "table", None), os.getenv("CAREER_CUSTOMER_ID", "personal"))
    if sqs is None and os.getenv("FRESHNESS_QUEUE_URL"):
        import boto3
        sqs = boto3.client("sqs")
    return FreshnessService(repo._freshness_store, definitions, sqs, os.getenv("FRESHNESS_QUEUE_URL"))


def shadow_assess(service, jobs, stage, max_age=2*DAY):
    if service is None:
        return {"mode": "observe", "status": "unavailable"} if os.getenv("FRESHNESS_MODE") == "observe" else {"mode": "off"}
    try:
        return service.assess(jobs, stage, max_age)
    except Exception:
        logger.exception("Freshness observation unavailable")
        return {"mode": "observe", "status": "unavailable"}
