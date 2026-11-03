"""Run bookkeeping: id, event log in Postgres, artifacts on disk (and in the object store)."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable

from finrobot.config import settings
from finrobot.data import objects, store


def new_run(mode: str, request: dict, echo: Callable[[dict], None] | None = None) -> tuple[str, Callable[[dict], None]]:
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:5]
    store.create_run(run_id, mode, request)

    def emit(event: dict) -> None:
        event = {"t": round(time.time(), 1), **event}
        try:
            store.add_event(run_id, event)
        except Exception:
            pass  # tracing must never break a run
        if echo:
            echo(event)

    return run_id, emit


def run_dir(run_id: str) -> Path:
    d = settings.output_dir / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_artifacts(run_id: str, artifacts: dict[str, bytes]) -> list[str]:
    d = run_dir(run_id)
    for name, data in artifacts.items():
        (d / name).write_bytes(data)
        ctype = "application/pdf" if name.endswith(".pdf") else "image/png" if name.endswith(".png") else "application/octet-stream"
        try:
            objects.put(f"runs/{run_id}/{name}", data, ctype)
        except Exception:
            pass  # local copy is authoritative
    return sorted(artifacts)


def finish(run_id: str, status: str, result: dict) -> None:
    (run_dir(run_id) / "result.json").write_text(json.dumps(result, indent=2, default=str))
    store.finish_run(run_id, status, result)
