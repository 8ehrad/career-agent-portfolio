from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CandidateProfile:
    data: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.data["version"])

    @property
    def name(self) -> str:
        return str(self.data["identity"]["name"])

    @property
    def all_skills(self) -> set[str]:
        groups = self.data["skills"]
        return {skill.lower() for values in groups.values() for skill in values}

    @property
    def evidence(self) -> list[dict[str, Any]]:
        return list(self.data["evidence"])

    @property
    def preferences(self) -> dict[str, Any]:
        return dict(self.data["preferences"])

    def compact_for_model(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "current_position": self.data["current_position"],
            "preferences": self.data["preferences"],
            "skills": self.data["skills"],
            "evidence": [
                {
                    "id": item["id"],
                    "category": item["category"],
                    "claim": item["claim"],
                    "skills": item["skills"],
                }
                for item in self.evidence
            ],
            "claim_policy": self.data["claim_policy"],
        }


def default_profile_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "candidate_profile.example.json"


def load_profile(path: str | Path | None = None) -> CandidateProfile:
    profile_path = Path(path) if path else default_profile_path()
    with profile_path.open(encoding="utf-8") as handle:
        return CandidateProfile(json.load(handle))
