"""Test doubles: a scripted chat model (supports tool calls and structured output) and deterministic fake embeddings."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from finrobot.config import Settings
from finrobot.llm.gateway import LLMGateway

DIM = 768   # matches the vector(768) column the migrations create


class ScriptedChatModel(BaseChatModel):
    """Replies come from `script`, in order. An item is an AIMessage, a string, an Exception (raised), or a function of the messages."""
    script: list[Any] = Field(default_factory=list)
    seen: list[dict] = Field(default_factory=list)
    bound: list[str] = Field(default_factory=list)
    default: Any = None   # used (instead of failing) once the script is exhausted; may be a function of the messages

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.seen.append({"messages": list(messages), "tools": list(self.bound)})
        if not self.script and self.default is None:
            raise AssertionError("scripted model ran out of replies")
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(messages)
        if isinstance(item, str):
            item = AIMessage(item)
        return ChatResult(generations=[ChatGeneration(message=item)])

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"bound": [getattr(t, "name", str(t)) for t in tools]})   # shares script and seen

    def with_structured_output(self, schema, *, include_raw: bool = False, **kwargs):
        def run(messages):
            raw = self.invoke(messages)
            try:
                parsed = schema.model_validate_json(raw.content)
                err = None
            except Exception as e:
                parsed, err = None, e
            return {"raw": raw, "parsed": parsed, "parsing_error": err} if include_raw else parsed
        return RunnableLambda(run)


class FakeEmbeddings(Embeddings):
    """Deterministic 768-d vectors derived from a hash of the text; identical text gives identical vectors."""
    dim = DIM

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _vec(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode()).digest()
        return [(h[i % len(h)] - 128) / 128 for i in range(self.dim)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def tool_call(tool_name: str, call_id: str = "c1", /, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": call_id, "type": "tool_call"}])


def last_tool_result(messages: list[BaseMessage]) -> dict:
    """The JSON the most recent tool returned (the scripted model 'reads' it to cite the right fact id)."""
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            return json.loads(m.content)
    raise AssertionError("no tool result in the conversation")


def make_gateway(figures: list | None = None, narrative: list | None = None, **cfg: Any) -> tuple[LLMGateway, dict[str, ScriptedChatModel]]:
    models = {"figures": ScriptedChatModel(script=list(figures or [])), "narrative": ScriptedChatModel(script=list(narrative or []))}
    settings = Settings(_env_file=None, embed_dim=DIM, llm_max_retries=2, llm_concurrency=2, **cfg)
    gw = LLMGateway(settings, chat_factory=lambda role, temperature: models[role], embeddings=FakeEmbeddings())
    return gw, models
