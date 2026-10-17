from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


VALID_STATUSES = {"active", "candidate", "disabled"}
VALID_PERMISSION_STATUSES = {"public", "approved", "approval_required", "not_applicable"}


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    """Version-controlled identity and configuration for one employer source."""

    key: str
    employer: str
    ats: str
    tenant: str | None
    uk_presence: bool
    status: str = "active"
    permission_status: str = "public"
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceDefinition":
        definition = cls(
            key=str(value["key"]),
            employer=str(value["employer"]),
            ats=str(value["ats"]),
            tenant=str(value["tenant"]) if value.get("tenant") is not None else None,
            uk_presence=bool(value.get("uk_presence", False)),
            status=str(value.get("status", "active")),
            permission_status=str(value.get("permission_status", "public")),
            config=dict(value.get("config") or {}),
        )
        definition.validate()
        return definition

    def validate(self) -> None:
        if not self.key or ":" not in self.key:
            raise ValueError(f"Source registry key must be namespaced: {self.key!r}")
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Unsupported source status {self.status!r} for {self.key}")
        if self.permission_status not in VALID_PERMISSION_STATUSES:
            raise ValueError(
                f"Unsupported permission status {self.permission_status!r} for {self.key}"
            )
        if self.status == "active" and not self.uk_presence:
            raise ValueError(f"Active source must have verified UK presence: {self.key}")
        if self.status == "active" and self.permission_status == "approval_required":
            raise ValueError(f"Approval-gated source cannot be active: {self.key}")
        forbidden_config_keys = {"password", "secret", "api_key", "token", "key"}
        if forbidden_config_keys & {str(key).lower() for key in self.config}:
            raise ValueError(f"Source registry must reference environment keys, not store credentials: {self.key}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_source_registry(path: str | Path) -> list[SourceDefinition]:
    registry_path = Path(path)
    with registry_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    values = payload.get("sources") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("Source registry must contain a sources array")
    definitions = [SourceDefinition.from_dict(item) for item in values]
    keys = [item.key for item in definitions]
    if len(keys) != len(set(keys)):
        raise ValueError("Source registry contains duplicate keys")
    return definitions
