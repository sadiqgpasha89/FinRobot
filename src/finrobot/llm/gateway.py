"""LLM gateway: the one place FinRobot talks to chat models and embedding models, built on LangChain.

* Models are chosen per role (``figures`` = numbers, routing, fact-checks; ``narrative`` = prose) by a ``[provider:]model`` string, so a
  role can move between a local Ollama model and a hosted one with an environment variable.
* Every call goes through one concurrency lane (a single local GPU slot by default), transient network errors are retried with
  jittered backoff, and each call carries ``role`` / ``model`` metadata that Langfuse and LangSmith show on the span.
* Agents never import a provider SDK; tests inject fake models through ``chat_factory``.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Sequence

import httpx
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from finrobot.config import Settings
from finrobot.config import settings as default_settings
from finrobot.llm.parsing import parse_json
from finrobot.llm.tools import Tool
from finrobot.log import get_logger

log = get_logger("llm")

KNOWN_PROVIDERS = {"ollama", "anthropic", "openai", "google_genai", "google_vertexai", "groq", "mistralai", "bedrock_converse", "azure_openai"}
TRANSIENT = (httpx.TransportError, ConnectionError, TimeoutError)
EMBED_BATCH = 32


class LLMError(RuntimeError):
    pass


def split_spec(spec: str) -> tuple[str, str]:
    """'gpt-oss:20b' -> ('ollama', 'gpt-oss:20b'); 'anthropic:claude-sonnet-5-5' -> ('anthropic', 'claude-sonnet-5-5')."""
    head, sep, rest = spec.partition(":")
    if sep and head in KNOWN_PROVIDERS:
        return head, rest
    return "ollama", spec


def text_of(msg: BaseMessage) -> str:
    """The plain text of a message whether the provider returns a string or a list of content blocks."""
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(b if isinstance(b, str) else b.get("text", "") for b in content if isinstance(b, (str, dict)) and (isinstance(b, str) or b.get("type") == "text"))


class LLMGateway:
    def __init__(self, cfg: Settings | None = None, *, chat_factory: Callable[[str, float], BaseChatModel] | None = None,
                 embeddings: Embeddings | None = None) -> None:
        self.cfg = cfg or default_settings
        self._chat_factory = chat_factory
        self._embeddings = embeddings
        self._lane = threading.BoundedSemaphore(self.cfg.llm_concurrency)

    # --- models --------------------------------------------------------------------------------
    def chat_model(self, role: str, temperature: float = 0.2) -> BaseChatModel:
        if self._chat_factory is not None:
            return self._chat_factory(role, temperature)
        provider, name = split_spec(self.cfg.model_for(role))
        if provider == "ollama":
            from langchain_ollama import ChatOllama
            # gpt-oss only accepts effort levels; other models take a bool and are faster with thinking off
            return ChatOllama(model=name, base_url=self.cfg.ollama_host.rstrip("/"), temperature=temperature, num_ctx=self.cfg.num_ctx,
                              keep_alive="10m", reasoning="low" if name.startswith("gpt-oss") else False,
                              client_kwargs={"timeout": self.cfg.llm_timeout})
        from langchain.chat_models import init_chat_model
        try:
            return init_chat_model(name, model_provider=provider, temperature=temperature, timeout=self.cfg.llm_timeout,
                                   max_retries=self.cfg.llm_max_retries)
        except ImportError as e:   # the provider's langchain-<provider> package is an optional extra
            raise LLMError(f"model provider '{provider}' needs its package installed: uv sync --extra {provider} ({e})") from e

    def embeddings(self) -> Embeddings:
        if self._embeddings is None:
            provider, name = split_spec(self.cfg.embed_model)
            if provider == "ollama":
                from langchain_ollama import OllamaEmbeddings
                self._embeddings = OllamaEmbeddings(model=name, base_url=self.cfg.ollama_host.rstrip("/"), keep_alive=600,   # seconds
                                                    client_kwargs={"timeout": self.cfg.llm_timeout})
            else:
                from langchain.embeddings import init_embeddings
                self._embeddings = init_embeddings(name, provider=provider)
        return self._embeddings

    def _meta(self, role: str, name: str) -> dict:
        return {"run_name": f"llm.{role}", "tags": [f"role:{role}"], "metadata": {"role": role, "model": name}}

    def _retrying(self, runnable):
        return runnable.with_retry(retry_if_exception_type=TRANSIENT, stop_after_attempt=self.cfg.llm_max_retries, wait_exponential_jitter=True)

    # --- chat ----------------------------------------------------------------------------------
    def invoke(self, role: str, messages: Sequence[BaseMessage | dict], *, tools: list[Tool] | None = None, temperature: float = 0.2) -> AIMessage:
        """One chat completion, optionally with tools bound. Raises the provider's error for non-transient failures."""
        model = self.chat_model(role, temperature)
        if tools:
            model = model.bind_tools([t.to_langchain() for t in tools])
        with self._lane:
            return self._retrying(model).invoke(list(messages), config=self._meta(role, self.cfg.model_for(role)))

    def structured(self, role: str, schema: type[BaseModel], messages: Sequence[BaseMessage | dict], temperature: float = 0.0) -> BaseModel | None:
        """Structured output validated against a pydantic model; None when the model cannot produce one (callers have a fallback).

        Uses the provider's schema-constrained decoding where it exists (Ollama `format`), tool calling elsewhere, and falls back
        to parsing JSON out of the raw text.
        """
        model = self.chat_model(role, temperature)
        provider, _ = split_spec(self.cfg.model_for(role))
        kw: dict[str, Any] = {"method": "json_schema"} if provider == "ollama" else {}
        runnable = self._retrying(model.with_structured_output(schema, include_raw=True, **kw))
        with self._lane:
            out = runnable.invoke(list(messages), config=self._meta(role, self.cfg.model_for(role)))
        parsed = out.get("parsed") if isinstance(out, dict) else out
        if isinstance(parsed, schema):
            return parsed
        raw = out.get("raw") if isinstance(out, dict) else None
        data = parse_json(text_of(raw)) if raw is not None else None
        if data is not None:
            try:
                return schema.model_validate(data)
            except ValueError as e:   # pydantic.ValidationError: the JSON parsed but does not fit the schema
                log.warning("structured output from %s role failed validation: %s", role, str(e).splitlines()[0])
        log.warning("no structured output from %s role (%s)", role, out.get("parsing_error") if isinstance(out, dict) else "unparsed")
        return None

    # --- embeddings ----------------------------------------------------------------------------
    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        """Embed texts (kind: 'document' or 'query'; the model-specific prefix is added here, not at call sites)."""
        if not texts:
            return []
        prefix = self.cfg.embed_doc_prefix if kind == "document" else self.cfg.embed_query_prefix
        call = RunnableLambda(self.embeddings().embed_documents)
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = [prefix + t for t in texts[i:i + EMBED_BATCH]]
            with self._lane:
                out.extend(self._retrying(call).invoke(batch, config={"run_name": "llm.embed", "metadata": {"role": "embed"}}))
        if out and len(out[0]) != self.cfg.embed_dim:
            raise LLMError(f"embedding model '{self.cfg.embed_model}' returned {len(out[0])} dimensions but the database column is "
                           f"vector({self.cfg.embed_dim}); set FINROBOT_EMBED_DIM and re-ingest")
        return out

    # --- health --------------------------------------------------------------------------------
    def health(self) -> dict:
        """Which configured models are reachable/installed. Never raises."""
        out: dict[str, Any] = {}
        specs = {"figures": self.cfg.model_figures, "narrative": self.cfg.model_narrative, "embed": self.cfg.embed_model}
        installed: set[str] | None = None
        if any(split_spec(s)[0] == "ollama" for s in specs.values()):
            try:
                r = httpx.get(f"{self.cfg.ollama_host.rstrip('/')}/api/tags", timeout=5)
                r.raise_for_status()
                installed = {m["name"] for m in r.json().get("models", [])}
            except Exception as e:
                out["ollama"] = f"unreachable: {e}"
        for role, spec in specs.items():
            provider, name = split_spec(spec)
            if provider != "ollama":
                out[role] = f"{provider}:{name} (hosted, not probed)"
            elif installed is None:
                out[role] = f"{name}: unknown (ollama unreachable)"
            else:
                out[role] = f"{name}: " + ("ok" if name in installed or f"{name}:latest" in installed else "NOT INSTALLED")
        out["ok"] = all(not v.endswith(("NOT INSTALLED",)) and "unknown" not in v and "unreachable" not in v for k, v in out.items() if k != "ok")
        return out
