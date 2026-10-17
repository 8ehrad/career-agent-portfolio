"""One native verification worker; queue redelivery is guarded by a durable lease."""
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from career_agent.freshness import service_from_environment
from career_agent.freshness_verifiers import NativeVerifier
from career_agent.runtime import repository, load_integration_environment

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def process(service, message, verifier):
    if message.get("schema_version") != 1 or message.get("kind") != "freshness_check":
        raise ValueError("Unsupported freshness message")
    if message.get("customer_id") != service.store.customer_id:
        raise ValueError("Freshness customer mismatch")
    key = message["key"]
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Invalid freshness key")
    now, token = service.clock(), uuid.uuid4().hex
    claimed = False
    def claim(old):
        nonlocal claimed
        claimed = False
        if not old or not old.get("requested") or old.get("interest_until", now+1) <= now or old.get("next_check_at", 0) > now or old.get("lease_until", 0) > now:
            return None
        claimed = True
        old.update(lease_token=token, lease_until=now+120)
        return old
    row = service.store.mutate(f"VACANCY#{key}", claim)
    if not claimed:
        return {"status": "duplicate_or_not_due"}
    target = row["target"]
    definition = next((d for d in service.definitions if d.key == target.get("source_key")), None)
    if ((definition and (definition.status != "active" or definition.permission_status not in {"public", "approved"}))
            or (target["source"] == "workday" and definition is None)):
        result = {"outcome": "unsupported", "at": now, "reason": "registry_route_no_longer_active"}
    else:
        result = verifier.check(target)
    service.observe(row["target"], expected_lease=token, **result)
    return {"status": result["outcome"], "source": row["target"]["source"]}


def handler(event, context):
    del context
    load_integration_environment()
    repo = repository()
    service = service_from_environment(repo)
    if service is None:
        return {"batchItemFailures": []}
    def reed_budget():
        now = datetime.now(UTC)
        return repo.reserve_reed_hydration_request(service.store.customer_id,
            now.astimezone(ZoneInfo("Europe/London")).date().isoformat(),
            int(os.getenv("REED_HYDRATION_DAILY_REQUEST_LIMIT", "2000")), now.isoformat())
    verifier = NativeVerifier(service.store, reed_budget=reed_budget)
    failures = []
    for record in event.get("Records", []):
        try:
            result = process(service, json.loads(record["body"]), verifier)
            logger.info(json.dumps({"event": "freshness_result", **result}))
        except Exception:
            logger.exception("Freshness task failed")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
