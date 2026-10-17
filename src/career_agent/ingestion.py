from __future__ import annotations

import gzip
import json
import re
from typing import Any, Protocol

from .models import Job


class JobStagingStore(Protocol):
    def put_jobs(self, run_id: str, source_key: str, jobs: list[Job]) -> str: ...

    def get_jobs(self, artifact_key: str) -> list[Job]: ...


class MemoryJobStagingStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_jobs(self, run_id: str, source_key: str, jobs: list[Job]) -> str:
        key, body = _encoded_artifact(run_id, source_key, jobs)
        self.objects[key] = body
        return key

    def get_jobs(self, artifact_key: str) -> list[Job]:
        return _decode_jobs(self.objects[artifact_key])


class S3JobStagingStore:
    def __init__(self, bucket: str, s3_client: Any | None = None) -> None:
        if s3_client is None:
            import boto3

            s3_client = boto3.client("s3")
        self.bucket = bucket
        self.s3 = s3_client

    def put_jobs(self, run_id: str, source_key: str, jobs: list[Job]) -> str:
        key, body = _encoded_artifact(run_id, source_key, jobs)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            ContentEncoding="gzip",
            ServerSideEncryption="AES256",
        )
        return key

    def get_jobs(self, artifact_key: str) -> list[Job]:
        response = self.s3.get_object(Bucket=self.bucket, Key=artifact_key)
        return _decode_jobs(response["Body"].read())


def _encoded_artifact(run_id: str, source_key: str, jobs: list[Job]) -> tuple[str, bytes]:
    safe_run_id = _safe_component(run_id)
    safe_source_key = _safe_component(source_key)
    key = f"raw/jobs/{safe_run_id}/{safe_source_key}.json.gz"
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "source_key": source_key,
        "jobs": [job.to_dict() for job in jobs],
    }
    return key, gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _decode_jobs(body: bytes) -> list[Job]:
    payload = json.loads(gzip.decompress(body).decode("utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("jobs"), list):
        raise ValueError("Unsupported staged job artifact")
    return [Job.from_dict(value) for value in payload["jobs"]]


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.=-]+", "_", value).strip("_")[:160] or "unknown"
