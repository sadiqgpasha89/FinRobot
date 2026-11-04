"""Logging: one setup function, JSON lines for production, and run/request ids attached to every record."""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar

run_id_var: ContextVar[str] = ContextVar("finrobot_run_id", default="")
request_id_var: ContextVar[str] = ContextVar("finrobot_request_id", default="")

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "run_id", "request_id"}


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage()}
        if getattr(record, "run_id", ""):
            out["run_id"] = record.run_id
        if getattr(record, "request_id", ""):
            out["request_id"] = record.request_id
        out.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(JsonFormatter() if json_logs else
                         logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(run_id)s%(message)s".replace("%(run_id)s", "[%(run_id)s] ")))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "weasyprint", "fontTools", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("finrobot") else f"finrobot.{name}")
