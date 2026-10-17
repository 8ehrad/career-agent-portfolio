"""Production adapter for the versioned requirement/retrieval/assessment pipeline.

Every fresh request uses the existing atomic daily budget. Successful intermediate
responses are durable, so an interrupted/budget-deferred task can resume later.
"""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from .accuracy_assessment import assess_context, VERSION as ASSESSMENT_VERSION, POLICY_VERSION
from .accuracy_v2 import select_context, resolve_provenance, VERSION as CONTEXT_VERSION
from .advert_document import select_existing_version
from .analysis_budget import AnalysisBudgetLimits
from .evidence_selection import fingerprint, serialized
from .hybrid_retrieval import HybridIndex, evidence_documents, VERSION as RETRIEVAL_VERSION
from .requirement_extraction import extract_with_model, VERSION as EXTRACTION_VERSION

VERSION = "rich-job-fit-v1"
EMBEDDING_MODEL = "cohere.embed-english-v3"
REQUEST_READ_TIMEOUT_SECONDS = 65
NEW_REQUEST_DEADLINE_SECONDS = 35


class MatchingDeferred(RuntimeError):
    """Completed stages survive; ordinary backlog recovery can continue this task."""


@lru_cache(maxsize=2)
def artifacts(directory):
    root = Path(directory)
    draft = json.loads((root / "profile.json").read_text())
    record = json.loads((root / "index.json").read_text())
    identity = fingerprint(draft)
    if record["profile_sha256"] != identity or record["model"] != EMBEDDING_MODEL:
        raise ValueError("Rich profile/index identity mismatch")
    docs = evidence_documents(draft)
    if record["documents_sha256"] != fingerprint(docs):
        raise ValueError("Rich evidence index differs from profile")
    if record["vectors_sha256"] != fingerprint(record["vectors"]):
        raise ValueError("Rich embedding checksum mismatch")
    index = HybridIndex(docs, record["vectors"], profile_identity=identity)
    return draft, index


class ProductionProvider:
    def __init__(self, analyzer, repo, customer_id, run_type):
        self.analyzer, self.repo = analyzer, repo
        self.customer_id, self.run_type = customer_id, run_type
        self.limits = AnalysisBudgetLimits.from_environment()
        self.started = time.monotonic()

    def _request(self, operation, request, output_cap):
        key = fingerprint({"customer": self.customer_id, "operation": operation, "request": request})
        table = self.repo.table
        cache_key = {"pk": f"MATCHING_STAGE#{self.customer_id}", "sk": key}
        old = table.get_item(Key=cache_key, ConsistentRead=True).get("Item")
        if old and int(old["expires_at"]) > int(time.time()):
            response = json.loads(old["payload"])
            if fingerprint(response) != old["response_sha256"]:
                raise ValueError("Matching stage integrity failure")
            return response
        # Leave ample time for one bounded request and the worker's checkpoint/finalizer.
        if time.monotonic() - self.started > NEW_REQUEST_DEADLINE_SECONDS:
            raise MatchingDeferred("rich_matching_continuation_due")
        input_reservation = max(500, len(serialized(request).encode()) // 3)
        limits = self.limits
        day, now = limits.budget_date(), datetime.now(UTC).isoformat()
        cost = limits.estimated_cost_microusd(input_reservation, output_cap)
        if not self.repo.reserve_analysis_capacity(self.customer_id, day, now,
                input_tokens=input_reservation, output_tokens=output_cap, cost_microusd=cost,
                maximum_calls=limits.maximum_calls, maximum_input_tokens=limits.maximum_input_tokens,
                maximum_output_tokens=limits.maximum_output_tokens,
                maximum_cost_microusd=limits.maximum_cost_microusd, run_type=self.run_type):
            usage = self.repo.get_analysis_budget_usage(self.customer_id, day)
            reserved = (self.run_type in {"backlog_drain", "manual_discovery"} and usage
                        and usage.discovery_state not in {"none", "released"}
                        and usage.calls_committed >= limits.backlog_call_ceiling(usage)
                        and usage.calls_committed < limits.maximum_calls)
            raise MatchingDeferred("discovery_capacity_reserved" if reserved else "daily_analysis_budget_exhausted")
        self.analyzer.model_calls += 1
        response = getattr(self.analyzer.client, operation)(**request)
        if operation == "invoke_model":
            response = json.loads(response["body"].read())
            # Cohere does not return Converse usage. Retain conservative input accounting.
            usage = {"inputTokens": input_reservation, "outputTokens": 0}
        else:
            usage = response.get("usage")
            if not isinstance(usage, dict) or not all(type(usage.get(k)) is int for k in ("inputTokens", "outputTokens")):
                raise ValueError("Missing model usage; reservation retained")
        self.analyzer.input_tokens += usage["inputTokens"]
        self.analyzer.output_tokens += usage["outputTokens"]
        payload = serialized(response)
        if len(payload.encode()) > 280000:
            raise ValueError("Matching stage exceeds durable storage bound")
        table.put_item(Item={**cache_key, "payload": payload, "response_sha256": fingerprint(response),
                            "expires_at": int(time.time()) + 30 * 86400})
        self.repo.reconcile_analysis_capacity(self.customer_id, day, datetime.now(UTC).isoformat(),
            reserved_input_tokens=input_reservation, reserved_output_tokens=output_cap,
            reserved_cost_microusd=cost, actual_calls=1, actual_input_tokens=usage["inputTokens"],
            actual_output_tokens=usage["outputTokens"],
            actual_cost_microusd=limits.estimated_cost_microusd(usage["inputTokens"], usage["outputTokens"]))
        return response

    def structured(self, prompt, data, schema, *, version, max_tokens=6500):
        response = self._request("converse", {
            "modelId": self.analyzer.model_id, "system": [{"text": version + "\n" + prompt}],
            "messages": [{"role": "user", "content": [{"text": serialized(data)}]}],
            "inferenceConfig": {"temperature": 0, "maxTokens": max_tokens},
            "outputConfig": {"textFormat": {"type": "json_schema", "structure": {"jsonSchema": {
                "name": "career_accuracy", "schema": serialized(schema)}}}}}, max_tokens)
        if response.get("stopReason") != "end_turn":
            raise ValueError("Incomplete/refused matching response")
        return json.loads("".join(b.get("text", "") for b in response["output"]["message"]["content"]))

    def embed(self, texts, *, input_type):
        if not texts:
            return []
        if input_type != "search_query" or len(texts) > 64 or any(not t or len(t) > 2048 for t in texts):
            raise ValueError("Invalid production embedding query batch")
        response = self._request("invoke_model", {"modelId": EMBEDDING_MODEL,
            "contentType": "application/json", "accept": "application/json",
            "body": serialized({"texts": texts, "input_type": input_type, "truncate": "NONE"})}, 0)
        vectors = response["embeddings"]
        if len(vectors) != len(texts):
            raise ValueError("Embedding count mismatch")
        return vectors


class RichAnalyzer:
    manages_budget = True

    def __init__(self, model_id, *, client=None, directory=None):
        self.model_id = model_id
        self.directory = directory or os.getenv("RICH_MATCHING_PATH", "/var/task/data/rich_matching")
        self.draft, self.index = artifacts(self.directory)
        self.profile_identity = fingerprint(self.draft)
        self.analysis_version = ":".join((VERSION, EXTRACTION_VERSION, RETRIEVAL_VERSION,
            CONTEXT_VERSION, ASSESSMENT_VERSION, POLICY_VERSION, EMBEDDING_MODEL, self.profile_identity))
        self.client = client
        self.provider = None
        self.input_tokens = self.output_tokens = self.model_calls = 0

    def bind(self, repo, customer_id, run_type):
        if self.client is None:
            import boto3
            from botocore.config import Config
            session = boto3.Session()
            loader = session._session.get_component("data_loader")
            loader.search_paths.insert(0, str(Path(self.directory) / "aws_models"))
            self.client = session.client("bedrock-runtime", config=Config(
                connect_timeout=5, read_timeout=REQUEST_READ_TIMEOUT_SECONDS, retries={"total_max_attempts": 1}))
        self.provider = ProductionProvider(self, repo, customer_id, run_type)

    def analyze(self, job, profile):
        if self.provider is None:
            raise RuntimeError("Rich analyzer requires a budgeted worker binding")
        advert = select_existing_version(job.to_dict())
        if len(advert["text"]) > 18000:
            advert = {**advert, "text": advert["text"][:18000], "possibly_incomplete": True,
                      "quality_flags": [*advert["quality_flags"], "model_input_clipped"]}
        document = extract_with_model(advert, self.provider)
        result = select_context(document, self.draft, profile, self.index, self.provider)
        assessment = assess_context(result, self.provider)
        resolve_provenance(result["context"], self.draft, profile)
        rows = assessment["assessments"]
        job.requirements = [r["text"] for r in rows]
        job.matched_evidence_ids = sorted({i for r in rows for i in r["evidence_ids"]})
        job.match_reasons = [r["text"] + ": " + r["explanation"] for r in rows if r["status"] == "supported"][:10]
        job.gaps = [r["text"] + ": " + r["explanation"] for r in rows if r["status"] != "supported"][:20]
        job.red_flags = list(document["concerns"])
        if assessment["state"] == "needs_review":
            job.red_flags.append("Review needed: some essential requirements have unverified or transferable evidence.")
        job.company_context["rich_matching"] = {"version": VERSION, "profile_sha256": self.profile_identity,
            "profile_version": self.draft["version"], "assessment": assessment,
            "requirement_document": {k:v for k,v in document.items() if k != "normalized_text"},
            "candidate_context": result["context"], "telemetry": result["telemetry"]}
        if len(serialized(job.company_context["rich_matching"]).encode()) > 180000:
            raise ValueError("Rich result exceeds storage budget")
        return job


def make_analyzer(model_id, *, bedrock_client=None):
    if os.getenv("MATCHING_MODE", "legacy") == "rich":
        return RichAnalyzer(model_id, client=bedrock_client)
    from .enrichment import BedrockAnalyzer
    return BedrockAnalyzer(model_id, bedrock_client=bedrock_client)
