"""S3-compatible object storage (raw filings, charts, PDFs). Falls back to the local output dir if the server is down."""
from __future__ import annotations

from pathlib import Path

from finrobot.config import settings

_client = None
_ready: bool | None = None


def _s3():
    global _client, _ready
    if _ready is False:
        return None
    if _client is None:
        try:
            from minio import Minio  # speaks the S3 protocol; works with any S3-compatible server
            c = Minio(settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key, secure=False)
            if not c.bucket_exists(settings.s3_bucket):
                c.make_bucket(settings.s3_bucket)
            _client, _ready = c, True
        except Exception:
            _ready = False
            return None
    return _client


def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store bytes; returns where they went ('s3://bucket/key' or a local path)."""
    import io
    c = _s3()
    if c is not None:
        c.put_object(settings.s3_bucket, key, io.BytesIO(data), len(data), content_type=content_type)
        return f"s3://{settings.s3_bucket}/{key}"
    path = settings.output_dir / "objects" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def get(key: str) -> bytes:
    c = _s3()
    if c is not None:
        r = c.get_object(settings.s3_bucket, key)
        try:
            return r.read()
        finally:
            r.close()
            r.release_conn()
    return (Path(settings.output_dir) / "objects" / key).read_bytes()
