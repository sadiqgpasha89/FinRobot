"""Token, call and latency accounting for one run, as a LangChain callback (provider-agnostic, thread-safe)."""
from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


class UsageTracker(BaseCallbackHandler):
    """Add it to a graph's callbacks; read `snapshot()` when the run ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started: dict[UUID, tuple[float, str, str]] = {}
        self.by_role: dict[str, dict[str, float]] = {}

    def on_chat_model_start(self, serialized: dict, messages: list, *, run_id: UUID, metadata: dict | None = None, **kw: Any) -> None:
        meta = metadata or {}
        with self._lock:
            self._started[run_id] = (time.time(), str(meta.get("role", "unknown")), str(meta.get("model", "")))

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kw: Any) -> None:
        with self._lock:
            t0, role, model = self._started.pop(run_id, (time.time(), "unknown", ""))
            row = self.by_role.setdefault(role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0})
            row["calls"] += 1
            row["seconds"] += time.time() - t0
            row["model"] = model  # type: ignore[assignment]
            for gens in response.generations:
                for g in gens:
                    um = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                    row["input_tokens"] += int(um.get("input_tokens", 0))
                    row["output_tokens"] += int(um.get("output_tokens", 0))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kw: Any) -> None:
        with self._lock:
            _, role, _m = self._started.pop(run_id, (time.time(), "unknown", ""))
            self.by_role.setdefault(role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}).setdefault("errors", 0)
            self.by_role[role]["errors"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            roles = {k: {**v, "seconds": round(v["seconds"], 1)} for k, v in self.by_role.items()}
        return {"calls": int(sum(r["calls"] for r in roles.values())), "input_tokens": int(sum(r["input_tokens"] for r in roles.values())),
                "output_tokens": int(sum(r["output_tokens"] for r in roles.values())),
                "llm_seconds": round(sum(r["seconds"] for r in roles.values()), 1), "by_role": roles}
