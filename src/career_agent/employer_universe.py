from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def canonical_employer_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return "".join(re.findall(r"[a-z0-9]+", value.casefold().replace("&", "and")))


@dataclass(frozen=True, slots=True)
class EmployerRecord:
    name: str
    aliases: tuple[str, ...]
    cohorts: tuple[str, ...]
    signals: tuple[str, ...]
    source_urls: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "cohorts": list(self.cohorts),
            "signals": list(self.signals),
            "source_urls": list(self.source_urls),
        }


@dataclass(frozen=True, slots=True)
class EmployerUniverse:
    as_of: str
    target_unique_employers: int
    selection_notes: tuple[str, ...]
    employers: tuple[EmployerRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "target_unique_employers": self.target_unique_employers,
            "selection_notes": list(self.selection_notes),
            "count": len(self.employers),
            "employers": [employer.to_dict() for employer in self.employers],
        }


def load_employer_universe(path: str | Path) -> EmployerUniverse:
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    cohorts = payload.get("cohorts")
    if not isinstance(cohorts, list) or not cohorts:
        raise ValueError("Employer universe must contain non-empty cohorts")

    records: dict[str, dict[str, Any]] = {}
    for cohort in cohorts:
        cohort_key = str(cohort["key"])
        signal = str(cohort["signal"])
        source_url = str(cohort["source_url"])
        for raw_name in cohort.get("employers", []):
            name = str(raw_name).strip()
            if not name:
                raise ValueError(f"Employer universe cohort {cohort_key} contains an empty name")
            key = canonical_employer_name(name)
            record = records.setdefault(
                key,
                {
                    "name": name,
                    "cohorts": [],
                    "signals": [],
                    "source_urls": [],
                },
            )
            if cohort_key not in record["cohorts"]:
                record["cohorts"].append(cohort_key)
            if signal not in record["signals"]:
                record["signals"].append(signal)
            if source_url not in record["source_urls"]:
                record["source_urls"].append(source_url)

    aliases_by_name = payload.get("aliases") or {}
    if not isinstance(aliases_by_name, dict):
        raise ValueError("Employer universe aliases must be an object")
    canonical_to_name = {key: value["name"] for key, value in records.items()}
    aliases: dict[str, list[str]] = {name: [] for name in canonical_to_name.values()}
    claimed_aliases = set(records)
    for employer, values in aliases_by_name.items():
        employer_key = canonical_employer_name(str(employer))
        if employer_key not in records:
            raise ValueError(f"Alias target is not in employer universe: {employer}")
        if not isinstance(values, list):
            raise ValueError(f"Aliases for {employer} must be an array")
        canonical_employer = canonical_to_name[employer_key]
        for value in values:
            alias = str(value).strip()
            alias_key = canonical_employer_name(alias)
            if not alias or alias_key in claimed_aliases:
                raise ValueError(f"Duplicate or empty employer alias: {value!r}")
            claimed_aliases.add(alias_key)
            aliases[canonical_employer].append(alias)

    employers = tuple(
        EmployerRecord(
            name=record["name"],
            aliases=tuple(aliases[record["name"]]),
            cohorts=tuple(record["cohorts"]),
            signals=tuple(record["signals"]),
            source_urls=tuple(record["source_urls"]),
        )
        for record in records.values()
    )
    target = int(payload.get("target_unique_employers", 0))
    if not 250 <= len(employers) <= 300:
        raise ValueError(f"Employer universe must contain 250-300 unique employers, got {len(employers)}")
    if target != len(employers):
        raise ValueError(f"Employer universe target is {target}, but contains {len(employers)}")
    return EmployerUniverse(
        as_of=str(payload["as_of"]),
        target_unique_employers=target,
        selection_notes=tuple(str(value) for value in payload.get("selection_notes", [])),
        employers=employers,
    )


def load_employer_source_routes(path: str | Path) -> dict[str, dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    values = payload.get("employers")
    if not isinstance(values, list):
        raise ValueError("Employer source routes must contain an employers array")
    routes: dict[str, dict[str, Any]] = {}
    for item in values:
        name = str(item["employer"])
        key = canonical_employer_name(name)
        if key in routes:
            raise ValueError(f"Duplicate employer source route: {name}")
        routes[key] = dict(item)
    return routes
