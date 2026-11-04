"""Run bookkeeping: id, event log in Postgres, artifacts on disk (and in the object store)."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from finrobot.config import settings
from finrobot.data import objects, store
from finrobot.log import get_logger, run_id_var

log = get_logger("runs")
RUN_ID_RE = re.compile(r"[A-Za-z0-9\-]{1,64}")
ARTIFACT_RE = re.compile(r"[A-Za-z0-9_.\-]{1,200}")


def new_run(mode: str, request: dict, echo: Callable[[dict], None] | None = None) -> tuple[str, Callable[[dict], None]]:
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:5]
    store.create_run(run_id, mode, request)
    return run_id, make_emitter(run_id, echo)


def make_emitter(run_id: str, echo: Callable[[dict], None] | None = None) -> Callable[[dict], None]:
    """An event sink for a run: appended to the run's event log, optionally echoed (CLI). A logging failure never breaks a run."""
    def emit(event: dict) -> None:
        event = {"t": round(time.time(), 1), **event}
        try:
            store.add_event(run_id, event)
        except Exception as e:
            log.warning("could not record event for run %s: %s", run_id, e)
        if echo:
            echo(event)
    return emit


def run_dir(run_id: str, create: bool = True) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"illegal run id: {run_id!r}")
    d = settings.output_dir / "runs" / run_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def artifact_path(run_id: str, name: str) -> Path | None:
    """Existing artifact file for a run, or None. Reads never create directories (an unknown id used to leave one behind)."""
    if not RUN_ID_RE.fullmatch(run_id) or not ARTIFACT_RE.fullmatch(name) or name in (".", ".."):
        return None
    path = run_dir(run_id, create=False) / name
    return path if path.is_file() else None


def save_artifacts(run_id: str, artifacts: dict[str, bytes]) -> list[str]:
    d = run_dir(run_id)
    for name, data in artifacts.items():
        (d / name).write_bytes(data)
        ctype = "application/pdf" if name.endswith(".pdf") else "image/png" if name.endswith(".png") else "application/octet-stream"
        try:
            objects.put(f"runs/{run_id}/{name}", data, ctype)
        except Exception as e:
            log.warning("object store copy of %s failed: %s", name, e)   # the local copy is authoritative
    return sorted(artifacts)


def finish(run_id: str, status: str, result: dict, usage: dict | None = None, error: str | None = None) -> None:
    (run_dir(run_id) / "result.json").write_text(json.dumps(result, indent=2, default=str))
    store.finish_run(run_id, status, result, usage, error)
    run_id_var.set("")
