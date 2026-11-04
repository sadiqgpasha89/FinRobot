# ADR-0003: LangChain behind a single `LLMGateway`, models chosen per role

* Status: Accepted · Date: 2026-11-05

## Context
0.1 spoke to Ollama over raw HTTP in one module and the plan listed a hosted model as "a small addition behind `llm/client.py`". Tool calling,
structured output, retries, embeddings and tracing were each hand-rolled, and tracing was absent. The agents need two *kinds* of model: one that
is exact (figures, routing, fact-checks) and one that writes well (prose).

## Decision
* Use **LangChain** chat models, tools and embeddings (`ChatOllama` / `OllamaEmbeddings`, `init_chat_model` for hosted providers).
* Put them behind one class, `LLMGateway`, the only code that touches a model. Agents ask for a **role** (`figures`, `narrative`); settings map roles
  to `[provider:]model` strings.
* The gateway owns the concurrency lane, jittered retries on transient network errors, per-call `role`/`model` metadata, structured output (provider
  schema decoding where available, JSON-from-text fallback) and embedding-dimension checks.
* `Tool` stays FinRobot's own tiny dataclass (name, JSON schema, callable) and converts to a LangChain `StructuredTool` at the edge, so tools stay plain
  Python and every call still becomes a tool span.

## Alternatives considered
| Option | Why not |
|---|---|
| Keep raw HTTP | re-implements what LangChain maintains; no tracing integration |
| LiteLLM / provider SDKs directly | gateway-level routing is not the problem; tracing and tool-binding semantics in LangChain/LangGraph are |
| Let agents call `init_chat_model` themselves | provider details leak everywhere; no single place for the lane, retries or fakes |

## Consequences
* (+) Switching a role to Anthropic/OpenAI is an environment variable and an extra (`uv sync --extra anthropic`).
* (+) Tests inject a scripted chat model through `chat_factory`; no test needs a GPU.
* (+) Callbacks (usage, Langfuse, LangSmith) reach every model call inside nested graphs through LangChain's config propagation.
* (−) Fakes cannot catch constructor-level contract drift. A live run found `OllamaEmbeddings(keep_alive="10m")` rejected (it takes seconds as an
  int) and gpt-oss omitting optional schema fields; both are now unit-tested (`test_the_real_ollama_embeddings_client_is_built_from_settings`,
  `test_structured_schemas_require_every_field`) and `evals/` plus a live smoke remain part of the release routine.
* (−) A retry wrapper is applied per call, not per graph; non-transient provider errors are surfaced, not retried.
