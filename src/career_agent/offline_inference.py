"""Explicit opt-in Bedrock experiment client with durable cache and bounded spend proxies.

Not a production budget ledger. One process per cache (enforced with flock), no automatic retries.
Cached-only is the default. Failed/uncertain calls consume the reservation and are not retried silently.
"""
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .evidence_selection import fingerprint, serialized


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class OfflineBedrock:
    def __init__(self, cache, *, live=False, region="eu-west-2", model="eu.anthropic.claude-sonnet-4-6",
                 embedding_model="cohere.embed-english-v3", max_calls=100, max_input_bytes=3000000,
                 max_output_tokens=250000, retry_keys=()):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.live, self.region, self.model, self.embedding_model = live, region, model, embedding_model
        self.max_calls, self.max_input_bytes, self.max_output_tokens = max_calls, max_input_bytes, max_output_tokens
        self.retry_keys = set(retry_keys)
        self.hits = 0
        self.lock = (self.cache / ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another experiment owns this cache")

    def close(self):
        self.lock.close()

    def _call(self, service, operation, request, *, reserved_output=0):
        identity = {"service": service, "operation": operation, "region": self.region, "request": request}
        key = fingerprint(identity)
        path = self.cache / "responses" / (key + ".json")
        if path.exists():
            record = json.loads(path.read_text())
            if record["identity"] != identity or fingerprint(record["response"]) != record["response_sha256"]:
                raise ValueError("Inference cache integrity failure")
            self.hits += 1
            return record["response"]
        if not self.live:
            raise RuntimeError(f"Cache miss {key}; live inference requires --live")
        ledger_path = self.cache / "ledger.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"calls": [], "input_bytes": 0,
                                                                                "reserved_output_tokens": 0}
        # Reconcile completed reservations to returned usage; failed/uncertain calls retain their full cap.
        ledger["reserved_output_tokens"] = sum(
            c.get("usage", {}).get("outputTokens", c["reserved_output_tokens"])
            if isinstance(c.get("usage"), dict) else c["reserved_output_tokens"] for c in ledger["calls"])
        previous = [c for c in ledger["calls"] if c["key"] == key]
        if any(c["state"] != "completed" for c in previous):
            if key not in self.retry_keys or len(previous) != 1:
                raise RuntimeError("Prior failed/uncertain request requires an explicitly reviewed --retry-key; not retrying")
        input_bytes = len(serialized(request).encode())
        if (len(ledger["calls"]) >= self.max_calls or ledger["input_bytes"] + input_bytes > self.max_input_bytes
                or ledger["reserved_output_tokens"] + reserved_output > self.max_output_tokens):
            raise RuntimeError("Offline inference budget exhausted")
        call = {"key": key, "model": request.get("modelId"), "operation": operation, "state": "reserved",
                "at": datetime.now(UTC).isoformat(), "input_bytes": input_bytes,
                "reserved_output_tokens": reserved_output, "explicit_retry": bool(previous)}
        ledger["calls"].append(call)
        ledger["input_bytes"] += input_bytes
        ledger["reserved_output_tokens"] += reserved_output
        write_json(ledger_path, ledger)
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="career-inference-") as directory:
                request_path = Path(directory) / "request.json"
                write_json(request_path, request)
                command = ["aws", service, operation, "--region", self.region, "--cli-input-json",
                           "file://" + str(request_path), "--cli-binary-format", "raw-in-base64-out", "--output", "json"]
                response_path = Path(directory) / "response.json"
                if operation == "invoke-model":
                    body_path = Path(directory) / "body.json"
                    write_json(body_path, json.loads(request["body"]))
                    # invoke-model's custom CLI handler requires explicit model/body flags.
                    command = ["aws", service, operation, "--region", self.region,
                               "--model-id", request["modelId"], "--body", "fileb://" + str(body_path),
                               "--content-type", "application/json", "--accept", "application/json",
                               "--output", "json", str(response_path)]
                command.extend(["--cli-read-timeout", "150", "--cli-connect-timeout", "10"])
                result = subprocess.run(command, capture_output=True, text=True, timeout=180,
                                        env={**os.environ, "AWS_MAX_ATTEMPTS": "1", "AWS_PAGER": ""})
                if result.returncode:
                    # Do not include requests/candidate text in errors.
                    raise RuntimeError(result.stderr[-2000:])
                response = json.loads(response_path.read_text() if operation == "invoke-model" else result.stdout)
            record = {"identity": identity, "response": response, "response_sha256": fingerprint(response),
                      "elapsed_seconds": round(time.monotonic() - started, 3)}
            write_json(path, record)
            call.update(state="completed", elapsed_seconds=record["elapsed_seconds"], usage=response.get("usage"))
            if isinstance(call["usage"], dict) and isinstance(call["usage"].get("outputTokens"), int):
                ledger["reserved_output_tokens"] += call["usage"]["outputTokens"] - reserved_output
            write_json(ledger_path, ledger)
            return response
        except Exception as exc:
            call.update(state="failed_or_uncertain", error_type=type(exc).__name__,
                        elapsed_seconds=round(time.monotonic() - started, 3))
            write_json(ledger_path, ledger)
            raise

    def structured(self, prompt, data, schema, *, version, max_tokens=5000):
        request = {"modelId": self.model, "system": [{"text": version + "\n" + prompt}],
                   "messages": [{"role": "user", "content": [{"text": serialized(data)}]}],
                   "inferenceConfig": {"temperature": 0, "maxTokens": max_tokens},
                   "outputConfig": {"textFormat": {"type": "json_schema", "structure": {"jsonSchema": {
                       "name": "career_accuracy", "schema": serialized(schema)}}}}}
        response = self._call("bedrock-runtime", "converse", request, reserved_output=max_tokens)
        if response.get("stopReason") != "end_turn":
            raise ValueError("Incomplete or refused structured response")
        text = "".join(block.get("text", "") for block in response["output"]["message"]["content"])
        return json.loads(text)

    def embed(self, texts, *, input_type):
        if input_type not in {"search_document", "search_query"}:
            raise ValueError("Invalid embedding input type")
        vectors = []
        for start in range(0, len(texts), 64):
            batch = texts[start:start + 64]
            if any(not text or len(text) > 2048 for text in batch):
                raise ValueError("Embedding text exceeds supported length; no silent truncation")
            request = {"modelId": self.embedding_model, "contentType": "application/json", "accept": "application/json",
                       "body": serialized({"texts": batch, "input_type": input_type, "truncate": "NONE"})}
            response = self._call("bedrock-runtime", "invoke-model", request)
            embeddings = response["embeddings"]
            if len(embeddings) != len(batch):
                raise ValueError("Embedding response count mismatch")
            vectors.extend(embeddings)
        return vectors
