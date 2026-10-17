from __future__ import annotations

import json
from pathlib import Path

from career_agent.models import Job
from career_agent.normalization import normalize_job


ROOT = Path(__file__).resolve().parents[1]


def fixture_jobs() -> list[Job]:
    with (ROOT / "tests" / "fixtures" / "jobs.json").open(encoding="utf-8") as handle:
        return [normalize_job(item, "fixture") for item in json.load(handle)]
