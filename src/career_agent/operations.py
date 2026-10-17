"""Read-only, owner-scoped pipeline observability. Never invokes a pipeline or model."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from .analysis_budget import AnalysisBudgetLimits

logger = logging.getLogger(__name__)
ACTIVE_ANALYSIS = {"queued", "timed_out", "dispatched", "processing"}
PENDING_HYDRATION = {"queued", "dispatched", "processing"}
TASK_FIELDS = (
    "status", "attempts", "last_error", "completed_at", "last_attempt_at",
    "lease_expires_at", "next_attempt_at", "analysis_task_id", "gate_reasons",
    "ready_status", "ready_processed_at", "selected_run_id", "selection_reason",
    "priority_score", "priority_components", "result_source", "model_calls",
    "profile_version", "analyzer_version", "model_id",
)


def resource_configuration(environment):
    """Use the deployed queue URLs and Terraform naming contract, not caller input."""
    prefix = environment.get("OPERATIONS_RESOURCE_PREFIX", "")
    queues = []
    for key, label, unit in (
        ("WORKDAY_QUEUE_URL", "Workday collection", "employer-board messages"),
        ("PIPELINE_FINALIZER_QUEUE_URL", "Collection finalizer", "run/check messages"),
        ("REED_HYDRATION_QUEUE_URL", "Reed description retrieval", "listing messages"),
        ("REED_HYDRATION_READY_QUEUE_URL", "Description-ready admission", "listing messages"),
        ("DEEP_ANALYSIS_QUEUE_URL", "Deep analysis", "analysis-task messages"),
        ("ANALYSIS_FINALIZER_QUEUE_URL", "Analysis finalizer", "run/check messages"),
        ("FRESHNESS_QUEUE_URL", "Vacancy freshness", "source-vacancy check messages"),
        ("APPLICATION_PREPARE_QUEUE_URL", "Application preparation", "CV/letter request messages"),
    ):
        url = environment.get(key)
        if url:
            queues.append({"id": key, "label": label, "unit": unit, "url": url})
            queues.append({"id": key+"_DLQ", "label": label+" failures", "unit": "messages",
                           "url": url+"-dlq", "dlq": True})
    if queues and prefix:
        base = queues[0]["url"].rsplit("/", 1)[0]
        queues.append({"id": "scheduler_dlq", "label": "Schedule delivery failures",
                       "unit": "messages", "url": f"{base}/{prefix}-scheduler-dlq", "dlq": True})
    schedules = [{"label": label, "name": f"{prefix}-{suffix}",
                  "function": f"{prefix}-{function}", "cadence_seconds": seconds}
                 for label, suffix, function, seconds in (
                     ("Discovery", "every-two-days", "workday-dispatcher", 172800),
                     ("Analysis backlog", "backlog-drainer", "backlog-drainer", 21600),
                     ("Coverage probe", "coverage-probe", "coverage-probe", 43200),
                     ("Reed recovery", "reed-hydration-drainer", "reed-hydration-drainer", 3600),
                     ("Application recovery", "application-prepare-reconciler", "application-prepare-worker", 900),
                 )] if prefix else []
    return queues, schedules, f"{prefix}-" if prefix else ""


def timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def compact_task(kind: str, task: Any) -> dict[str, Any]:
    data = task.to_dict()
    job = data.get("job") or data.get("summary") or {}
    return {
        "kind": kind,
        "id": data.get("task_id") or data.get("source_id"),
        "job_id": data.get("job_id"),
        "source_id": data.get("source_id") or job.get("source_id"),
        "title": job.get("title") or job.get("jobTitle") or "Untitled listing",
        "company": data.get("company") or job.get("company") or job.get("employerName"),
        "location": job.get("location") or job.get("locationName"),
        "source": data.get("source") or "reed",
        "first_seen_at": data.get("first_queued_at") or data.get("first_seen_at"),
        "last_seen_at": data.get("last_seen_at"),
        **{key: data[key] for key in TASK_FIELDS if key in data},
    }


def summarize_tasks(items: list[dict], kind: str, complete: bool, now: datetime) -> dict:
    statuses = Counter(item["status"] for item in items)
    active_states = ACTIVE_ANALYSIS if kind == "analysis" else PENDING_HYDRATION
    active = [item for item in items if item["status"] in active_states]
    ages = [timestamp(item["first_seen_at"]) for item in active]
    valid_ages = [age for age in ages if age is not None]
    oldest = min(valid_ages, default=None)
    summary = {
        "status": "ok" if complete else "partial", "observed_at": now.isoformat(),
        "complete": complete, "unit": "versioned analysis tasks" if kind == "analysis" else "Reed listing IDs",
        "count_basis": "retained records; paginated consistent reads, not an atomic cross-record snapshot",
        "retained_total": len(items), "status_counts": dict(statuses), "active": len(active),
        "active_unique_jobs": len({item.get("job_id") or item["id"] for item in active}),
        "oldest_active_at": oldest.isoformat() if oldest else None,
        "oldest_active_hours": round(max(0, (now - oldest).total_seconds() / 3600), 2) if oldest else None,
        "unknown_age_count": sum(age is None for age in ages),
    }
    if kind == "analysis":
        summary["budget_deferred"] = sum(
            item["status"] == "timed_out" and item.get("last_error") in {
                "daily_analysis_budget_exhausted", "discovery_capacity_reserved"}
            for item in items
        )
        summary["other_timed_out"] = statuses["timed_out"] - summary["budget_deferred"]
        summary["waiting_dispatch"] = statuses["queued"] + statuses["timed_out"]
    else:
        ready = [item for item in items if item["status"] == "completed"]
        summary["ready_outcomes"] = dict(Counter(item.get("ready_status", "pending") for item in ready))
        summary["description_ready_waiting_admission"] = sum(
            item.get("ready_status") == "pending" for item in ready
        )
        summary["description_ready_total"] = len(ready)
    return summary


class OperationsService:
    """Bounded indexed reads, cached 60s per warm API instance; no table scans.

    Detail pages and summary share a snapshot ID. A stale cursor must restart rather
    than silently skip/duplicate records. No full adverts or candidate evidence leave
    this service. Cold instances may independently read; this is not a shared cache.
    """

    def __init__(self, repository, *, sqs=None, cloudwatch=None, scheduler=None,
                 queues=None, schedules=None, alarm_prefix="", customer_id="personal",
                 clock=None, max_pages=50):
        self.repo = repository
        self.sqs, self.cloudwatch, self.scheduler = sqs, cloudwatch, scheduler
        self.queues, self.schedules = queues or [], schedules or []
        self.alarm_prefix, self.customer_id = alarm_prefix, customer_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_pages = max_pages
        self._cached = None
        self._expires = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, repository):
        import boto3
        from botocore.config import Config
        from .repository import DynamoRepository
        config = Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1})
        if isinstance(repository, DynamoRepository):
            repository = DynamoRepository(repository.table_name, boto3.resource("dynamodb", config=config))
        queues, schedules, alarm_prefix = resource_configuration(os.environ)
        return cls(repository, sqs=boto3.client("sqs", config=config),
                   cloudwatch=boto3.client("cloudwatch", config=config),
                   scheduler=boto3.client("scheduler", config=config),
                   queues=queues, schedules=schedules, alarm_prefix=alarm_prefix,
                   customer_id=os.getenv("CAREER_CUSTOMER_ID", "personal"))

    def _tasks(self, kind):
        if not hasattr(self.repo, "table"):
            tasks = (self.repo.list_analysis_tasks(self.customer_id) if kind == "analysis"
                     else self.repo.list_reed_hydration_tasks(self.customer_id))
            return [compact_task(kind, task) for task in tasks], True
        prefix = "ANALYSIS_BACKLOG" if kind == "analysis" else "REED_HYDRATION"
        decoder = (self.repo._analysis_task_from_item if kind == "analysis"
                   else self.repo._reed_hydration_task_from_item)
        parameters = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :task)",
            "ExpressionAttributeValues": {":pk": f"{prefix}#{self.customer_id}", ":task": "TASK#"},
            "ConsistentRead": True, "Limit": 250,
        }
        # Identity deduplication is defensive; status is decoded from mutable fields,
        # not the original JSON payload (which deliberately retains earlier values).
        items = {}
        for _ in range(self.max_pages):
            response = self.repo.table.query(**parameters)
            for item in response.get("Items", []):
                task = compact_task(kind, decoder(item))
                items[task["id"]] = task
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return list(items.values()), True
            parameters["ExclusiveStartKey"] = cursor
        return list(items.values()), False

    def _task_section(self, kind, now):
        items, complete = self._tasks(kind)
        items.sort(key=lambda item: (item.get("first_seen_at") or "", item["id"]))
        return {**summarize_tasks(items, kind, complete, now), "items": items}

    def _runs(self):
        keys = ("run_id", "started_at", "completed_at", "status", "last_completed_stage",
                "stage_counts", "new_stage_counts", "source_metrics", "rejection_counts",
                "model_usage", "timings_ms", "errors", "telemetry_schema_version", "newness_scope",
                "emailed", "configuration", "source_fetches", "employer_metrics", "role_family_metrics")
        return {"items": [{key: run.to_dict()[key] for key in keys}
                          for run in self.repo.list_runs(30)],
                "scope": "latest 30 discovery run records; index is eventually consistent"}

    def _ingestion(self):
        items = []
        for run in self.repo.list_ingestion_runs(5):
            tasks = self.repo.list_ingestion_tasks(run.run_id)
            items.append({**run.to_dict(), "status_counts": dict(Counter(task.status for task in tasks)),
                          "observed_tasks": len(tasks), "tasks": [{
                              key: value for key, value in task.to_dict().items()
                              if key not in {"artifact_key", "diagnostic"}
                          } for task in tasks]})
        return {"items": items, "scope": "latest 5 collection runs; counts are employer boards per run"}

    def _budget(self, now):
        limits = AnalysisBudgetLimits.from_environment()
        usage = self.repo.get_analysis_budget_usage(self.customer_id, limits.budget_date(now))
        fields = {"calls": limits.maximum_calls, "input_tokens": limits.maximum_input_tokens,
                  "output_tokens": limits.maximum_output_tokens, "cost_microusd": limits.maximum_cost_microusd}
        data = usage.to_dict() if usage else {}
        return {"budget_date": limits.budget_date(now), "timezone": limits.timezone,
                "limits": fields, "usage": data,
                "remaining": {key: max(0, value - data.get(f"{key}_committed", 0))
                              for key, value in fields.items()},
                "ledger_present": usage is not None,
                "scope": "Deep-analysis guardrail only; priced token estimates, not the total AWS bill"}

    def _queue(self, config, now):
        result = {"id": config["id"], "label": config["label"], "unit": config["unit"],
                  "dlq": bool(config.get("dlq")), "approximate": True,
                  "observed_at": now.isoformat()}
        try:
            attributes = self.sqs.get_queue_attributes(QueueUrl=config["url"], AttributeNames=[
                "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ])["Attributes"]
            # Missing attributes are errors, never a fabricated empty queue.
            result.update(status="ok", waiting=int(attributes["ApproximateNumberOfMessages"]),
                          in_flight=int(attributes["ApproximateNumberOfMessagesNotVisible"]),
                          delayed=int(attributes["ApproximateNumberOfMessagesDelayed"]))
        except Exception:
            logger.exception("Operations queue read failed: %s", config["id"])
            result.update(status="unavailable", waiting=None, in_flight=None, delayed=None)
        return result

    def _transport(self, now):
        if not self.queues:
            return {"status": "unavailable", "items": [], "reason": "Queue monitoring is not configured"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            items = list(pool.map(lambda config: self._queue(config, now), self.queues))
        queries = []
        for index, queue in enumerate(self.queues):
            name = queue["url"].rstrip("/").rsplit("/", 1)[-1]
            for suffix, metric, stat in (("age", "ApproximateAgeOfOldestMessage", "Maximum"),
                                         ("received", "NumberOfMessagesReceived", "Sum")):
                queries.append({"Id": f"q{index}{suffix}", "MetricStat": {"Metric": {
                    "Namespace": "AWS/SQS", "MetricName": metric,
                    "Dimensions": [{"Name": "QueueName", "Value": name}]},
                    "Period": 300, "Stat": stat}, "ReturnData": True})
        try:
            response = self.cloudwatch.get_metric_data(MetricDataQueries=queries,
                StartTime=now - timedelta(hours=1), EndTime=now, ScanBy="TimestampDescending")
            metrics = {row["Id"]: row for row in response.get("MetricDataResults", [])}
            for index, item in enumerate(items):
                for suffix in ("age", "received"):
                    row = metrics.get(f"q{index}{suffix}", {})
                    usable = row.get("StatusCode") == "Complete" and not response.get("NextToken")
                    values = row.get("Values", [])
                    item[f"{suffix}_metric_status"] = "ok" if usable and values else "unavailable"
                    item["oldest_message_seconds" if suffix == "age" else "deliveries_last_hour"] = (
                        (values[0] if suffix == "age" else sum(values)) if usable and values else None)
                    if suffix == "age":
                        times = row.get("Timestamps", [])
                        item["age_observed_at"] = times[0].isoformat() if times else None
        except Exception:
            logger.exception("Operations queue CloudWatch metrics failed")
        return {"items": items, "status": "ok" if all(row["status"] == "ok" for row in items) else "partial"}

    def _schedule(self, config, now):
        result = {"label": config["label"], "name": config["name"], "observed_at": now.isoformat()}
        try:
            schedule = self.scheduler.get_schedule(Name=config["name"], GroupName=config.get("group", "default"))
            result.update(status="ok", state=schedule["State"], expression=schedule["ScheduleExpression"],
                          timezone=schedule.get("ScheduleExpressionTimezone", "UTC"))
        except Exception:
            logger.exception("Operations schedule read failed: %s", config["name"])
            result.update(status="unavailable", state=None)
        # Last invocation is telemetry, not proof of successful application work.
        try:
            response = self.cloudwatch.get_metric_data(MetricDataQueries=[{
                "Id": "invocations", "MetricStat": {"Metric": {"Namespace": "AWS/Lambda",
                    "MetricName": "Invocations", "Dimensions": [{"Name": "FunctionName", "Value": config["function"]}]},
                    "Period": 300, "Stat": "Sum"}, "ReturnData": True}],
                StartTime=now-timedelta(days=3), EndTime=now, ScanBy="TimestampDescending")
            row = response.get("MetricDataResults", [{}])[0]
            pairs = [(stamp, value) for stamp, value in zip(row.get("Timestamps", []), row.get("Values", [])) if value > 0]
            if row.get("StatusCode") == "Complete" and not response.get("NextToken"):
                result["last_invocation_bucket"] = max((stamp for stamp, _ in pairs), default=None)
                last = result["last_invocation_bucket"]
                result["last_invocation_bucket"] = last.isoformat() if last else None
                result["cadence_overdue"] = ((now-last).total_seconds() > config["cadence_seconds"] + 2700) if last else None
        except Exception:
            logger.exception("Operations schedule invocation read failed")
        return result

    def _schedules(self, now):
        if not self.schedules:
            return {"status": "unavailable", "items": []}
        with ThreadPoolExecutor(max_workers=4) as pool:
            items = list(pool.map(lambda config: self._schedule(config, now), self.schedules))
        return {"items": items, "status": "ok" if all(item["status"] == "ok" for item in items) else "partial"}

    def _alarms(self):
        if not self.alarm_prefix:
            return {"status": "unavailable", "items": []}
        parameters = {"AlarmNamePrefix": self.alarm_prefix, "MaxRecords": 100}
        items = []
        for _ in range(5):
            response = self.cloudwatch.describe_alarms(**parameters)
            for alarm in response.get("MetricAlarms", []):
                items.append({"name": alarm["AlarmName"], "state": alarm["StateValue"],
                              "reason": alarm.get("StateReason"),
                              "updated_at": alarm["StateUpdatedTimestamp"].isoformat()})
            if not response.get("NextToken"):
                return {"items": items, "complete": True}
            parameters["NextToken"] = response["NextToken"]
        return {"status": "partial", "items": items, "complete": False}

    def _section(self, callback):
        started = self.clock().isoformat()
        try:
            return {"status": "ok", "observed_at": started, **callback()}
        except Exception:
            logger.exception("Operations section unavailable")
            return {"status": "unavailable", "observed_at": started,
                    "reason": "Telemetry could not be read; do not interpret this as zero."}

    def _freshness(self):
        from .freshness import FreshnessService, FreshnessStore
        if os.getenv("FRESHNESS_MODE", "off") != "observe":
            return {"mode": "off", "status": "disabled"}
        store = getattr(self.repo, "_freshness_store", None)
        if store is None:
            store = FreshnessStore(getattr(self.repo, "table", None), self.customer_id)
        return FreshnessService(store, clock=lambda: int(self.clock().timestamp())).summary()

    def snapshot(self):
        with self._lock:
            if self._cached is not None and time.monotonic() < self._expires:
                return self._cached
            now = self.clock()
            callbacks = {
                "analysis": lambda: self._task_section("analysis", now),
                "hydration": lambda: self._task_section("hydration", now),
                "runs": self._runs, "ingestion": self._ingestion, "budget": lambda: self._budget(now),
                "queues": lambda: self._transport(now), "schedules": lambda: self._schedules(now),
                "alarms": self._alarms,
                "freshness": self._freshness,
            }
            # boto3 clients may be shared across threads; resource/Table objects may
            # not. Keep DynamoDB reads on this thread and overlap only AWS clients.
            cloud_sections = {"queues", "schedules", "alarms"}
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {key: pool.submit(self._section, callback) for key, callback in callbacks.items()
                           if key in cloud_sections}
                sections = {key: self._section(callback) for key, callback in callbacks.items()
                            if key not in cloud_sections}
                sections.update({key: future.result() for key, future in futures.items()})
            result = {"schema_version": 1, "generated_at": now.isoformat(), "cache_seconds": 60,
                      "customer_id": self.customer_id, **sections}
            # Content identity survives a refresh or a different warm Lambda when
            # listing state is unchanged. Unrelated SQS/clock changes must not break
            # pagination. A changed record still forces a safe first-page restart.
            identity = json.dumps({"customer_id": self.customer_id, **{
                kind: {"items": sections[kind].get("items"), "complete": sections[kind].get("complete")}
                for kind in ("analysis", "hydration")
            }}, sort_keys=True).encode()
            result["snapshot_id"] = hashlib.sha256(identity).hexdigest()[:20]
            self._cached, self._expires = result, time.monotonic() + 60
            return result

    def overview(self):
        value = self.snapshot()
        # Source diagnostics/configuration are often large. Fetch only the selected
        # run through run_detail, rather than sending 30 full reports every minute.
        run_keys = {"run_id", "started_at", "completed_at", "status", "last_completed_stage", "emailed"}
        runs = value["runs"]
        return {**value,
                "runs": {**runs, "items": [{key: item for key, item in run.items() if key in run_keys}
                                             for run in runs.get("items", [])]},
                **{kind: {key: item for key, item in value[kind].items() if key != "items"}
                   for kind in ("analysis", "hydration")}}

    def run_detail(self, run_id):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 200:
            raise ValueError("run_id is required and must be at most 200 characters")
        snapshot = self.snapshot()
        section = snapshot["runs"]
        if section["status"] == "unavailable":
            return {**section, "item": None}
        for run in section.get("items", []):
            if run["run_id"] == run_id:
                return {"status": "ok", "observed_at": section["observed_at"],
                        "snapshot_id": snapshot["snapshot_id"], "item": run}
        raise KeyError("Run is outside the current latest-30 snapshot; refresh the run list")

    def task_page(self, parameters):
        kind = parameters.get("kind", "analysis")
        if kind not in {"analysis", "hydration"}:
            raise ValueError("kind must be analysis or hydration")
        try:
            offset, limit = int(parameters.get("offset", "0")), int(parameters.get("limit", "50"))
        except (TypeError, ValueError) as exc:
            raise ValueError("offset and limit must be integers") from exc
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative and limit between 1 and 100")
        snapshot = self.snapshot()
        if (offset and not parameters.get("snapshot_id")) or (
            parameters.get("snapshot_id") and parameters["snapshot_id"] != snapshot["snapshot_id"]
        ):
            raise ValueError("Listing snapshot changed or is missing; refresh and restart from the first page")
        section = snapshot[kind]
        if section["status"] == "unavailable":
            return {**section, "items": [], "total": None, "snapshot_id": snapshot["snapshot_id"]}
        status, query = parameters.get("status", ""), parameters.get("q", "").strip().casefold()
        allowed = (ACTIVE_ANALYSIS | {"completed", "expired", "cancelled"} if kind == "analysis"
                   else PENDING_HYDRATION | {"completed", "failed"})
        if status and status not in allowed:
            raise ValueError("Unsupported task status for this queue")
        if len(query) > 200:
            raise ValueError("Search must be at most 200 characters")
        items = [item for item in section["items"] if (not status or item["status"] == status) and
                 (not query or query in " ".join(str(item.get(key) or "") for key in
                     ("id", "job_id", "source_id", "title", "company", "source")).casefold())]
        return {"status": section["status"], "complete": section["complete"],
                "snapshot_id": snapshot["snapshot_id"], "observed_at": section["observed_at"],
                "items": items[offset:offset+limit], "total": len(items),
                "next_offset": offset+limit if offset+limit < len(items) else None}
