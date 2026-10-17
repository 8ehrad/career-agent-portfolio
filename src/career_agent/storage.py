from __future__ import annotations

from abc import ABC, abstractmethod


class ArtifactStore(ABC):
    @abstractmethod
    def put(self, key: str, content: bytes, content_type: str) -> str: ...

    @abstractmethod
    def signed_url(self, key: str, expires_seconds: int = 900) -> str: ...


class MemoryArtifactStore(ArtifactStore):
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, content: bytes, content_type: str) -> str:
        self.objects[key] = (content, content_type)
        return key

    def signed_url(self, key: str, expires_seconds: int = 900) -> str:
        if key not in self.objects:
            raise KeyError(key)
        return f"memory://{key}?expires={expires_seconds}"


class S3ArtifactStore(ArtifactStore):
    def __init__(self, bucket: str, s3_client=None) -> None:
        if s3_client is None:
            import boto3

            s3_client = boto3.client("s3")
        self.bucket = bucket
        self.client = s3_client

    def put(self, key: str, content: bytes, content_type: str) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        return key

    def signed_url(self, key: str, expires_seconds: int = 900) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )
