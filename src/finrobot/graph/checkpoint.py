"""Durable graph state in Postgres (LangGraph's PostgresSaver on the shared connection pool)."""
from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from finrobot.data import store

_saver: BaseCheckpointSaver | None = None


def get_checkpointer() -> BaseCheckpointSaver:
    """The process-wide Postgres checkpointer; creates its tables on first use."""
    global _saver
    if _saver is None:
        from langgraph.checkpoint.postgres import PostgresSaver
        saver = PostgresSaver(store.get_pool())   # the pool is created with autocommit + dict_row, as the saver requires
        saver.setup()
        _saver = saver
    return _saver


def reset_checkpointer() -> None:
    global _saver
    _saver = None


def memory_checkpointer() -> BaseCheckpointSaver:
    """In-process only: enough for tests and single-process review flows, lost on restart."""
    return InMemorySaver()
