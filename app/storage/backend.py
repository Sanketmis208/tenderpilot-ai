"""Object storage abstraction: S3 (AWS or MinIO) and a local-filesystem backend for development.

Object keys are tenant-scoped and randomized: org/<org_id>/<scope>/<uuid>/<sanitised-name>.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

from app.core.config import StorageBackend, get_settings
from app.core.errors import NotFound, PermissionDenied

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitise_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = _SAFE.sub("_", name).strip("._") or "file"
    return name[:150]


def make_key(org_id: uuid.UUID, scope: str, filename: str) -> str:
    return f"org/{org_id}/{scope}/{uuid.uuid4().hex}/{sanitise_filename(filename)}"


def assert_key_in_org(key: str, org_id: uuid.UUID) -> None:
    if not key.startswith(f"org/{org_id}/") or ".." in key:
        raise PermissionDenied("Object does not belong to this organization.")


class Storage(Protocol):
    name: str

    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def presigned_get(self, key: str, filename: str, ttl: int | None = None) -> str: ...
    def presigned_put(self, key: str, content_type: str, ttl: int | None = None) -> str: ...


class LocalStorage:
    """Filesystem backend. Downloads are served by the app via short-lived HMAC-signed URLs."""

    name = "local"

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise PermissionDenied("Invalid storage key.")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.exists():
            raise NotFound("Stored object not found.")
        return p.read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    @staticmethod
    def sign(key: str, expires: int) -> str:
        secret = get_settings().secret_key.get_secret_value().encode()
        return hmac.new(secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()

    @classmethod
    def verify(cls, key: str, expires: int, sig: str) -> bool:
        return expires >= int(time.time()) and hmac.compare_digest(cls.sign(key, expires), sig)

    def presigned_get(self, key: str, filename: str, ttl: int | None = None) -> str:
        expires = int(time.time()) + (ttl or get_settings().presign_ttl_seconds)
        q = urlencode({"key": key, "exp": expires, "sig": self.sign(key, expires), "fn": filename})
        return f"/files/download?{q}"

    def presigned_put(self, key: str, content_type: str, ttl: int | None = None) -> str:
        # Local backend uploads go through the API's multipart endpoint instead.
        return ""


class S3Storage:
    name = "s3"

    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        s = get_settings()
        kwargs: dict = {
            "region_name": s.s3_region,
            "config": Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"}),
        }
        if s.s3_endpoint_url:
            kwargs["endpoint_url"] = s.s3_endpoint_url
        if s.s3_access_key_id and s.s3_secret_access_key:
            kwargs["aws_access_key_id"] = s.s3_access_key_id.get_secret_value()
            kwargs["aws_secret_access_key"] = s.s3_secret_access_key.get_secret_value()
        # Otherwise boto3 resolves credentials from the IAM task role / environment.
        self.client = boto3.client("s3", **kwargs)
        self.bucket = s.s3_bucket
        self.kms_key = s.s3_kms_key_id

    def _sse(self) -> dict:
        if self.kms_key:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms_key}
        return {"ServerSideEncryption": "AES256"}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **self._sse())

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            raise NotFound("Stored object not found.")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def presigned_get(self, key: str, filename: str, ttl: int | None = None) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
            ExpiresIn=ttl or get_settings().presign_ttl_seconds,
        )

    def presigned_put(self, key: str, content_type: str, ttl: int | None = None) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type, **self._sse()},
            ExpiresIn=ttl or get_settings().presign_ttl_seconds,
        )


@lru_cache
def get_storage() -> Storage:
    s = get_settings()
    if s.storage_backend == StorageBackend.S3:
        return S3Storage()
    return LocalStorage(s.local_storage_path)
