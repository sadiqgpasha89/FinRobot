# Development

## Setup

```bash
uv sync                       # Python 3.11+, locked dependencies incl. dev tools
cp .env.example .env          # set SEC_USER_AGENT; FINROBOT_ENV=development; DATABASE_URL for your Postgres
docker compose up -d postgres objects        # or any Postgres with pgvector
uv run finrobot ping
```

`make help` lists the common tasks (`make lint`, `make test`, `make test-integration`, `make cov`, `make up`).

## Tests

| Command | Needs | Runs |
|---|---|---|
| `uv run pytest` | nothing | unit tests (the default; `integration` and `live` are deselected) |
| `FINROBOT_TEST_DATABASE_URL=... uv run pytest -m integration` | Postgres with pgvector | real SQL, pool, LangGraph checkpointer, both graphs end to end, the API through its lifespan |
| `uv run pytest -m "integration or not integration" --cov` | the above | everything with coverage (CI gate: 80%) |
| `uv run python evals/run.py` | Ollama + an ingested company | live evals on real models |

The integration fixtures create a throwaway database on the server you point at and drop it afterwards. No Docker? Any Postgres works; for a disposable one:
`uv run --with pgserver python -c "import pgserver; print(pgserver.get_server('/tmp/pg').get_uri())"` (bundles Postgres and pgvector), then pass that URI.

### How the tests avoid a GPU

`tests/fakes.py` has `ScriptedChatModel`: a LangChain chat model whose replies come from a script (strings, `AIMessage`s with tool calls, exceptions, or functions of the conversation), plus
deterministic fake embeddings. `make_gateway(figures=[...], narrative=[...])` returns a real `LLMGateway` wired to them. `tests/integration/seed.py` builds a synthetic company; `tests/mock_sec.py`
is a fake SEC. Scripted replies behave like an honest model (they read tool results and cite the ids they were given), so what is under test is everything *around* the model.

### Before a release

The fakes cannot catch provider contract drift (this release's live runs found two such bugs). Run once: `ollama serve`, the models, then `evals/run.py` and a seeded live question
(see `tests/integration/seed.py`). Record the verified-answer rate.

## Adding things

* **A tool:** write a function returning a JSON-able dict, wrap it in `llm.tools.Tool(name, description, json_schema, fn)`, return it from `data_tools` / `math_tools` / `function_tools`.
  Register any number it returns with `ctx.fb.add_fact(...)` so it has an id: an unregistered number cannot be cited and will be removed by the Evaluator.
* **An agent:** a function that builds a system/user prompt and calls `checked(ctx, role, system, user, tools, who, ...)`. You get the tool loop, verification, retry and sanitize for free.
* **A graph node:** keep state small and serialisable; put run-scoped objects in the `Runtime` context; for a long step, make it one node per unit of work so it checkpoints.
* **A model provider:** `uv add langchain-<provider>` (optional extra), set `FINROBOT_MODEL_<ROLE>=<provider>:<model>`. Nothing else changes.
* **A migration:** append `(version, name, sql)` to `data/migrations.py`; additive and idempotent; never edit an applied one.

## Conventions

* Arithmetic, period selection and number checking are code. If a change lets the model decide any of them, it needs an ADR.
* Every bug fix gets a regression test named after the failure (see the audit log for examples).
* `ruff check` clean; type hints on public functions; no provider SDK imports outside `llm/gateway.py`.
