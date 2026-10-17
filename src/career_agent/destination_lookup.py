"""Classify a browser-observed application destination without guessing an ATS.

The browser supplies the outbound link from an Adzuna listing's Apply flow.
This module never opens a URL; callers must enforce their own navigation and
request limits. A destination is evidence of a route, not proof of vacancy
identity or permission to analyse a different posting.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from urllib.parse import parse_qs, urlsplit, urlunsplit


ATS_HOSTS = {
    "workday": ("myworkdayjobs.com",),
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "workable": ("workable.com",),
    "icims": ("icims.com",),
    "oracle_hcm": ("oraclecloud.com",),
    "sap_successfactors": ("successfactors.com",),
}
INTERMEDIARY_HOSTS = {
    "reed": ("reed.co.uk",),
    "totaljobs": ("totaljobs.com",),
    "nijobs": ("nijobs.com",),
    "appcast": ("appcast.io",),
    "hackajob": ("hackajob.com",),
    "free_work": ("free-work.com",),
}


@dataclass(frozen=True, slots=True)
class Destination:
    url: str | None
    platform: str | None
    kind: str  # ats, intermediary, other, malformed
    reason: str | None = None


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def classify_destination(raw_url: str) -> Destination:
    """Unwrap the known Reed tracker and classify the final visible link.

    Strip tracking/query/fragment from stored findings. This is for route
    classification only; fetching should use the trusted original link after
    validation, because some ATS sites require query parameters.
    """
    url = raw_url
    for _ in range(2):
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            if (
                parsed.scheme != "https"
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or host in {"https", "http", "localhost"}
            ):
                return Destination(None, None, "malformed", "invalid_https_url")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                return Destination(None, None, "malformed", "ip_literal")
            if host == "reedq.reed.co.uk" and parsed.path == "/clicktracker":
                values = parse_qs(parsed.query).get("destination", [])
                if len(values) != 1:
                    return Destination(None, None, "malformed", "reed_tracker_without_destination")
                url = values[0]
                continue
            clean = urlunsplit(("https", host, parsed.path or "/", "", ""))
            for name, suffixes in ATS_HOSTS.items():
                if any(_host_matches(host, suffix) for suffix in suffixes):
                    return Destination(clean, name, "ats")
            for name, suffixes in INTERMEDIARY_HOSTS.items():
                if any(_host_matches(host, suffix) for suffix in suffixes):
                    return Destination(clean, name, "intermediary")
            return Destination(clean, None, "other")
        except ValueError:
            return Destination(None, None, "malformed", "invalid_url")
    return Destination(None, None, "malformed", "redirect_nesting_limit")
