"""S3-compatible object storage (raw filings, charts, PDFs). Falls back to the local output dir if the server is unavailable.

A failed connection is retried after a cool-down, so a store that comes up after the API (or recovers from a restart) is picked up
without restarting the service.
"""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path

from finrobot.config import settings
from finrobot.log import get_logger

log = get_logger("objects")
RETRY_AFTER = 30.0   # seconds before a failed connection is attempted again

_client = None
_failed_at: float | None = None
_lock = threading.Lock()


def _s3():
    global _client, _failed_at
    with _lock:
        if _client is not None:
            return _client
        if _failed_at is not None and time.time() - _failed_at < RETRY_AFTER:
            return None
        try:
            import urllib3
            from minio import Minio  # speaks the S3 protocol; works with any S3-compatible server
            # fail fast: the default client retries a refused connection for ~6 s, which would blow a readiness-probe timeout
            http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=2.0, read=30.0), retries=urllib3.Retry(total=1, backoff_factor=0.2))
            c = Minio(settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key.get_secret_value(), secure=settings.s3_secure,
                      http_client=http)
            if not c.bucket_exists(settings.s3_bucket):
                c.make_bucket(settings.s3_bucket)
            _client, _failed_at = c, None
            log.info("object store connected: %s/%s", settings.s3_endpoint, settings.s3_bucket)
        except Exception as e:
            _failed_at = time.time()
            log.warning("object store unavailable (%s); using local files, will retry in %ss", e, int(RETRY_AFTER))
            return None
    return _client


def reset() -> None:
    """Forget the cached client (tests, config changes)."""
    global _client, _failed_at
    with _lock:
        _client, _failed_at = None, None


def available() -> bool:
    return _s3() is not None


def _local_path(key: str) -> Path:
    root = (settings.output_dir / "objects").resolve()
    path = (root / key).resolve()
    if root not in path.parents:
        raise ValueError(f"illegal object key: {key!r}")
    return path


def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store bytes; returns where they went ('s3://bucket/key' or a local path)."""
    c = _s3()
    if c is not None:
        try:
            c.put_object(settings.s3_bucket, key, io.BytesIO(data), len(data), content_type=content_type)
            return f"s3://{settings.s3_bucket}/{key}"
        except Exception as e:   # the local copy keeps the data; do not fail an ingest or a report over storage
            log.warning("S3 put failed for %s (%s); wrote a local copy", key, e)
    path = _local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def get(key: str) -> bytes:
    c = _s3()
    if c is not None:
        try:
            r = c.get_object(settings.s3_bucket, key)
            try:
                return r.read()
            finally:
                r.close()
                r.release_conn()
        except Exception:
            local = _local_path(key)
            if not local.exists():
                raise
    return _local_path(key).read_bytes()
