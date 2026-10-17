"""Read-only native source verification. Missing records are never inferred closed."""
import base64
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from .freshness import DAY, normalized, reed_expiration_timestamp


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def native_json(url, headers=None):
    # All caller URLs are constructed from known API hosts or an active Workday registry.
    request = urllib.request.Request(url, headers={"Accept": "application/json",
        "User-Agent": "CareerAgentExample/0.1", **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
            body = response.read(8_000_001)
            if len(body) > 8_000_000:
                return 0, None
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 0, None


class NativeVerifier:
    def __init__(self, store, transport=native_json, clock=None, reed_budget=None, sleep=time.sleep):
        from time import time
        self.store, self.transport = store, transport
        self.clock = clock or (lambda: int(time()))
        self.reed_budget = reed_budget
        self.sleep = sleep

    def check(self, target):
        now, source = self.clock(), target["source"]
        if source not in {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "reed"}:
            return {"outcome": "unsupported", "at": now, "reason": "no_native_route"}
        if source == "reed" and not os.getenv("REED_API_KEY"):
            return {"outcome": "unsupported", "at": now, "reason": "reed_credentials_unavailable"}
        tenant, native_id = quote(target["tenant"], safe=""), quote(target["native_id"], safe="")
        cache_key = f"BOARD#{source}#{target['tenant']}"
        board_source = source in {"greenhouse", "lever", "ashby"}
        daily_limit = int(os.getenv("FRESHNESS_DAILY_REQUEST_LIMIT", "100"))
        for attempt in range(3):
            now = self.clock()
            cached = self.store.get(cache_key) if board_source else None
            if cached and 0 <= now-cached["at"] < 3600:
                return self._match(target, cached["jobs"], cached["at"], "board_cache")
            if self.store.permit_request(source, now, daily_limit=daily_limit):
                break
            # Read AFTER a failed atomic claim: another invocation may have won since our last read.
            pace = self.store.get(f"SOURCE#{source}") or {}
            budget = self.store.get(f"BUDGET#{now // DAY}") or {}
            if pace.get("blocked_until", 0) > now:
                return {"outcome": "deferred", "at": now, "reason": "source_circuit_open"}
            if budget.get("requests", 0) >= daily_limit:
                return {"outcome": "deferred", "at": now, "reason": "freshness_daily_budget_exhausted"}
            delay = pace.get("next_request_at", 0)-now
            if attempt == 2 or not 0 < delay <= 2:
                return {"outcome": "deferred", "at": now, "reason": "source_pacing_contended"}
            self.sleep(delay)  # At most two short waits; long outages still use durable backoff.
        headers = {}
        if source == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs"
        elif source == "lever":
            url = f"https://api.lever.co/v0/postings/{tenant}?mode=json"
        elif source == "ashby":
            url = f"https://api.ashbyhq.com/posting-api/job-board/{tenant}"
        elif source == "smartrecruiters":
            url = f"https://api.smartrecruiters.com/v1/companies/{tenant}/postings/{native_id}"
        elif source == "workday":
            route = target["route"]
            url = f"https://{route['host']}/wday/cxs/{tenant}/{quote(route['site'], safe='')}{quote(route['external_path'], safe='/_-')}"
        else:
            api_key = os.getenv("REED_API_KEY")
            if self.reed_budget is None or not self.reed_budget():
                return {"outcome": "deferred", "at": now, "reason": "shared_reed_budget_unavailable_or_exhausted"}
            headers["Authorization"] = "Basic " + base64.b64encode(f"{api_key}:".encode()).decode()
            url = f"https://www.reed.co.uk/api/1.0/jobs/{native_id}"
        code, payload = self.transport(url, headers)
        checked_at = self.clock()
        if code in {401, 403, 429} or code >= 500 or code == 0:
            self.store.block(source, checked_at + (6*3600 if code in {401, 403, 429} else 900))
            return {"outcome": "blocked" if code in {401, 403, 429} else "check_failed",
                    "at": checked_at, "reason": f"http_{code}"}
        if code in {404, 410}:
            return {"outcome": "not_found", "at": checked_at, "reason": f"native_http_{code}_not_closure"}
        if code != 200:
            return {"outcome": "check_failed", "at": checked_at, "reason": "unexpected_response"}
        try:
            if source in {"greenhouse", "ashby"} and not isinstance(payload["jobs"], list):
                raise ValueError("Expected published jobs list")
            if source == "greenhouse":
                jobs = [{"id": str(j["id"]), "title": j["title"]} for j in payload["jobs"]]
            elif source == "lever":
                if not isinstance(payload, list):
                    raise ValueError("Expected published postings list")
                jobs = [{"id": str(j["id"]), "title": j["text"]} for j in payload]
            elif source == "ashby":
                # isListed=false means direct-link-only, NOT closed (Ashby public API docs).
                jobs = [{"id": urlsplit(j["jobUrl"]).path.strip("/").split("/")[1],
                         "title": j["title"]} for j in payload["jobs"]]
            elif source == "workday":
                info = payload["jobPostingInfo"]
                jobs = [{"id": str(info["jobReqId"]), "title": info["title"]}]
            elif source == "smartrecruiters":
                jobs = [{"id": str(payload["id"]), "title": payload["name"]}]
            else:
                if str(payload.get("jobId")) != target["native_id"] or normalized(payload.get("jobTitle")) != normalized(target["title"]):
                    return {"outcome": "identity_mismatch", "at": checked_at, "reason": "reed_id_or_title_changed"}
                if normalized(payload.get("employerName")) != normalized(target["company"]):
                    return {"outcome": "identity_mismatch", "at": checked_at, "reason": "reed_employer_changed"}
                expired = reed_expiration_timestamp(payload.get("expirationDate"))
                if expired and expired < checked_at:
                    return {"outcome": "source_closed", "at": checked_at, "reason": "reed_advert_expiration_not_employer_closure"}
                if not payload.get("jobDescription"):
                    raise ValueError("Missing detail")
                return {"outcome": "live_detail", "at": checked_at, "reason": "reed_native_detail"}
            if not all(j["id"] and isinstance(j["title"], str) and j["title"].strip() for j in jobs):
                raise ValueError("Malformed board")
            if board_source and len(json.dumps(jobs).encode()) < 200_000:
                self.store.mutate(cache_key, lambda old: None if old and old["at"] > checked_at else
                                  {"at": checked_at, "jobs": jobs, "expires_at": checked_at+DAY})
            return self._match(target, jobs, checked_at, "native_api")
        except (KeyError, IndexError, TypeError, ValueError):
            return {"outcome": "check_failed", "at": checked_at, "reason": "invalid_native_schema"}

    @staticmethod
    def _match(target, jobs, at, reason):
        matches = [j for j in jobs if j["id"] == target["native_id"]]
        if not matches:
            outcome = "not_found"
            reason += "_absence_not_closure"
        elif len(matches) != 1 or normalized(matches[0]["title"]) != normalized(target["title"]):
            outcome = "identity_mismatch"
        else:
            outcome = "live_detail"
        return {"outcome": outcome, "at": at, "reason": reason}
