# Architecture Decision Records

Each record states a decision, why it was taken, what else was considered and what it costs. Format: context → decision → alternatives →
consequences. Records are never edited after acceptance; a later record supersedes an earlier one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-langgraph-orchestration.md) | LangGraph is the orchestration layer, at every level of the system | Accepted |
| [0002](0002-verification-first-generation.md) | Numbers are registered, cited and verified by code; the LLM never does arithmetic | Accepted (carried over from 0.1) |
| [0003](0003-langchain-llm-gateway.md) | LangChain behind a single `LLMGateway`, models chosen per role | Accepted |
| [0004](0004-postgres-single-store.md) | Postgres + pgvector holds facts, chunks, runs and graph checkpoints | Accepted |
| [0005](0005-dual-observability.md) | Langfuse for production traces and scores, LangSmith for development and evaluation | Accepted |
| [0006](0006-durable-state-and-human-review.md) | Durable report runs: small serialisable state, rebuildable runtime context, `interrupt` for review | Accepted |
| [0007](0007-in-process-job-queue.md) | Jobs run on a bounded in-process worker pool | Accepted (revisit at scale-out) |
| [0008](0008-security-posture.md) | Untrusted model text is escaped, the renderer is network-isolated, the API is key-protected, production refuses defaults | Accepted |
| [0009](0009-local-first-models.md) | Local models by default; hosted providers by configuration | Accepted |
