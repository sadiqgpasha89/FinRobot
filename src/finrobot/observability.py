"""Tracing, scores and usage for every run: Langfuse and LangSmith side by side, both optional.

Neither backend is required. With no keys configured every function here is a cheap no-op and a run still records its own token usage.

* **LangSmith** traces LangChain/LangGraph automatically once ``LANGSMITH_TRACING=true`` and ``LANGSMITH_API_KEY`` are set. We pin the
  root run id (derived from the FinRobot run id) so verification scores can be attached afterwards as feedback, and we use it for
  offline dataset evaluation (see ``evals/``).
* **Langfuse** is attached as a LangChain callback inside one root observation whose trace id is derived from the FinRobot run id
  (``create_trace_id(seed=run_id)``), so the run row, the Langfuse trace and the LangSmith run can always be joined, and scores can be
  posted at any later time. The run id is also the Langfuse *session*.

Privacy: ``FINROBOT_TRACE_INPUTS=false`` redacts prompts and outputs before they leave the process (scores and timings are still sent).
"""
from __future__ import annotations

import os
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from finrobot.config import settings
from finrobot.llm.usage import UsageTracker
from finrobot.log import get_logger

log = get_logger("observability")
LANGSMITH_NS = uuid.UUID("5c1f3f4e-0a56-4b3a-9a53-7f6a1c3a9d10")
_initialised = False


def _redact(data: Any = None, **_: Any) -> str:
    return "[redacted]"


def init_observability() -> dict:
    """Configure the SDKs once per process. Safe to call repeatedly."""
    global _initialised
    status = {"langfuse": settings.langfuse_enabled, "langsmith": settings.langsmith_enabled}
    if _initialised:
        return status
    _initialised = True
    if not settings.trace_inputs:
        os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
        os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")
    if settings.langfuse_enabled:
        try:
            from langfuse import Langfuse

            from finrobot import __version__
            Langfuse(public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key.get_secret_value(),
                     base_url=os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or None,
                     environment=settings.environment, release=__version__, mask=None if settings.trace_inputs else _redact)
            log.info("Langfuse tracing enabled")
        except Exception as e:   # tracing must never stop the service from starting
            log.warning("Langfuse disabled: %s", e)
            status["langfuse"] = False
    if settings.langsmith_enabled:
        log.info("LangSmith tracing enabled (project %s)", settings.langsmith_project)
    return status


def shutdown_observability() -> None:
    if settings.langfuse_enabled:
        try:
            from langfuse import get_client
            get_client().shutdown()
        except Exception as e:
            log.warning("Langfuse shutdown: %s", e)


@dataclass
class Tracing:
    """What a run needs from observability: a LangChain config to pass to ``graph.invoke`` and a way to attach scores."""
    run_id: str
    mode: str
    usage: UsageTracker = field(default_factory=UsageTracker)
    trace_id: str | None = None             # Langfuse trace id (hex), None when Langfuse is off
    langsmith_run_id: uuid.UUID | None = None
    callbacks: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    _root: Any = None
    _output: Any = None

    @property
    def config(self) -> dict:
        cfg: dict[str, Any] = {"callbacks": [self.usage, *self.callbacks], "run_name": f"finrobot.{self.mode}", "tags": self.tags,
                               "metadata": {"finrobot_run_id": self.run_id, "mode": self.mode, **self.metadata}}
        if settings.langsmith_enabled:
            cfg["run_id"] = self.langsmith_run_id
        return cfg

    def set_output(self, output: Any) -> None:
        self._output = output

    def score(self, name: str, value: float | bool, comment: str | None = None) -> None:
        """Attach a quality score to this run's trace in every enabled backend. Never raises."""
        if self.trace_id:
            try:
                from langfuse import get_client
                is_bool = isinstance(value, bool)
                get_client().create_score(trace_id=self.trace_id, name=name, value=float(value), data_type="BOOLEAN" if is_bool else "NUMERIC",
                                          comment=comment)
            except Exception as e:
                log.warning("langfuse score %s failed: %s", name, e)
        if settings.langsmith_enabled and self.langsmith_run_id:
            try:
                from langsmith import Client
                Client().create_feedback(run_id=self.langsmith_run_id, key=name, score=float(value), comment=comment)
            except Exception as e:
                log.warning("langsmith feedback %s failed: %s", name, e)

    def scores(self, values: dict[str, float | bool]) -> None:
        for k, v in values.items():
            self.score(k, v)


@contextmanager
def trace_run(run_id: str, mode: str, request: dict | None = None, *, tags: list[str] | None = None, metadata: dict | None = None,
              flush: bool = False) -> Iterator[Tracing]:
    """Wrap one run. ``with trace_run(...) as tr: graph.invoke(state, tr.config)``."""
    init_observability()
    tr = Tracing(run_id, mode, tags=[mode, settings.environment, *(tags or [])], metadata=metadata or {},
                 langsmith_run_id=uuid.uuid5(LANGSMITH_NS, run_id))
    with ExitStack() as stack:
        if settings.langfuse_enabled:
            try:
                from langfuse import get_client, propagate_attributes
                from langfuse.langchain import CallbackHandler
                client = get_client()
                tr.trace_id = client.create_trace_id(seed=run_id)
                tr._root = stack.enter_context(client.start_as_current_observation(
                    name=f"finrobot.{mode}.request", as_type="chain", trace_context={"trace_id": tr.trace_id}, input=request))
                stack.enter_context(propagate_attributes(
                    session_id=run_id, trace_name=f"finrobot.{mode}", tags=[t[:200] for t in tr.tags],
                    metadata={k: str(v)[:200] for k, v in {"mode": mode, **tr.metadata}.items()}))
                tr.callbacks.append(CallbackHandler())
            except Exception as e:
                log.warning("Langfuse tracing unavailable for run %s: %s", run_id, e)
        try:
            yield tr
        finally:
            if tr._root is not None and tr._output is not None:
                try:
                    tr._root.update(output=tr._output)
                except Exception as e:
                    log.warning("could not set Langfuse output for run %s: %s", run_id, e)
    if flush and settings.langfuse_enabled:
        try:
            from langfuse import get_client
            get_client().flush()
        except Exception as e:
            log.warning("Langfuse flush: %s", e)


def _count(v: Any) -> int:
    """Removed-line bookkeeping is a count in some places and a list of the removed sentences in others."""
    return len(v) if isinstance(v, (list, tuple, set)) else int(v or 0)


def qa_scores(result: dict) -> dict[str, float | bool]:
    """The numbers worth watching per question: did verification pass, how much was checked, how much had to be thrown away."""
    ev = result.get("eval", {})
    claims = ev.get("claims", 0)
    agents = result.get("agents", {})
    return {"verification_ok": bool(ev.get("ok")), "figures_checked": claims,
            "figures_tied_ratio": round((ev.get("matched_facts", 0) + ev.get("grounded_in_passages", 0)) / claims, 3) if claims else 1.0,
            "lines_removed": sum(_count(a.get("removed")) + _count(a.get("grounding_removed")) for a in agents.values()),
            "agent_retries": sum(bool(a.get("retried")) for a in agents.values())}


def report_scores(result: dict) -> dict[str, float | bool]:
    ev, stats = result.get("eval", {}), result.get("stats", {})
    return {"verification_ok": bool(ev.get("ok")), "format_ok": bool(result.get("format", {}).get("ok")), "figures_checked": ev.get("claims", 0),
            "lines_removed": sum(_count(s.get("removed")) + _count(s.get("grounding_removed")) + _count(s.get("dropped")) for s in stats.values()),
            "pages": result.get("pages", 0)}
