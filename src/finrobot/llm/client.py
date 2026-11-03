"""Ollama chat client plus a tool-calling loop.

Ollama here runs a single slot, so model work is serial. `_lane` is held only for the
duration of one HTTP request, never while a tool runs, so nothing can acquire it twice.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from finrobot.config import settings

_lane = threading.Lock()


class LLMError(RuntimeError):
    pass


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""
    seconds: float = 0.0


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]

    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _think_for(model: str) -> Any:
    # gpt-oss only accepts effort levels; gemma4 takes a bool and is faster with thinking off.
    return "low" if model.startswith("gpt-oss") else False


class OllamaLLM:
    def __init__(self, host: str | None = None, timeout: float = 600.0):
        self.host = (host or settings.ollama_host).rstrip("/")
        self.timeout = timeout

    def chat(self, role: str, messages: list[dict], tools: list[Tool] | None = None,
             schema: dict | None = None, temperature: float = 0.2, model: str | None = None) -> Reply:
        model = model or settings.model_for(role)
        body: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False, "keep_alive": "10m",
            "think": _think_for(model),
            "options": {"temperature": temperature, "num_ctx": settings.num_ctx},
        }
        if tools:
            body["tools"] = [t.spec() for t in tools]
        if schema:
            body["format"] = schema
        t0 = time.time()
        try:
            with _lane:
                r = httpx.post(f"{self.host}/api/chat", json=body, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request failed: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama {r.status_code}: {r.text[:300]}")
        msg = r.json().get("message", {})
        return Reply(content=msg.get("content", "") or "", tool_calls=msg.get("tool_calls") or [],
                     model=model, seconds=time.time() - t0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            batch = texts[i:i + 32]
            try:
                with _lane:
                    r = httpx.post(f"{self.host}/api/embed", json={"model": settings.embed_model, "input": batch, "keep_alive": "10m"},
                                   timeout=self.timeout)
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama embed failed: {e}") from e
            if r.status_code != 200:
                raise LLMError(f"Ollama embed {r.status_code}: {r.text[:300]}")
            out.extend(r.json()["embeddings"])
        return out

    def installed(self) -> set[str]:
        r = httpx.get(f"{self.host}/api/tags", timeout=10)
        r.raise_for_status()
        return {m["name"] for m in r.json().get("models", [])}


def run_tools(llm: OllamaLLM, role: str, system: str, user: str, tools: list[Tool],
              max_steps: int = 6, on_event: Callable[[dict], None] | None = None,
              temperature: float = 0.2) -> tuple[str, list[dict]]:
    """Let the model call tools until it answers in plain text. Returns (text, calls made)."""
    by_name = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    calls: list[dict] = []
    for step in range(max_steps):
        # On the last step withhold tools and say so: a model that is still mid-tool-loop otherwise returns nothing.
        last = step == max_steps - 1
        if last:
            messages.append({"role": "user", "content": "No more tool calls. Write your final answer now, using only the tool results you already have."})
        try:
            reply = llm.chat(role, messages, tools=None if last else tools, temperature=temperature)
        except LLMError as e:
            if "error parsing tool call" not in str(e):
                raise
            # the model emitted broken JSON for a tool call; ask it to try again rather than failing the run
            messages.append({"role": "user", "content": "Your last tool call was malformed JSON. Call the tool again with valid arguments."})
            continue
        if not reply.tool_calls:
            return reply.content.strip(), calls
        messages.append({"role": "assistant", "content": reply.content, "tool_calls": reply.tool_calls})
        for tc in reply.tool_calls:
            fn = tc.get("function", {})
            name, args = fn.get("name", ""), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool = by_name.get(name)
            try:
                result = tool.fn(**args) if tool else {"error": f"unknown tool {name}"}
            except Exception as e:  # tool errors go back to the model so it can adjust
                result = {"error": f"{type(e).__name__}: {e}"}
            calls.append({"tool": name, "args": args, "result": result})
            if on_event:
                on_event({"type": "tool", "tool": name, "args": args})
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, default=str)[:6000]})
    return "", calls


def parse_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                return None
    return None
