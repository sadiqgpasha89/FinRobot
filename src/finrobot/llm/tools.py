"""FinRobot's tool definition and its LangChain form.

A `Tool` is a plain description (name, JSON-schema parameters, Python callable). Agents bind its LangChain form to the chat model and
execute it through `StructuredTool.invoke`, so every call is a tool span in Langfuse and LangSmith.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.tools import StructuredTool

MAX_TOOL_CHARS = 6000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]

    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def to_langchain(self) -> StructuredTool:
        # a JSON-schema dict as args_schema: arguments reach `fn` as keyword arguments, unvalidated; `fn` reports its own errors
        return StructuredTool(name=self.name, description=self.description, args_schema=self.parameters, func=self.fn)


def render_result(result: Any, limit: int = MAX_TOOL_CHARS) -> str:
    """Tool output as the model sees it: JSON, cut with a visible marker rather than mid-token."""
    text = json.dumps(result, default=str)
    return text if len(text) <= limit else text[:limit] + " …[truncated]"
