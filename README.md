# FinRobot

**Live site and use case: <https://sadiqgpasha89.github.io/FinRobot/>** · [Documentation](https://sadiqgpasha89.github.io/FinRobot/docs/)

Multi-agent equity research with **verified, cited figures**. Give it a ticker: it ingests SEC filings and XBRL financials, answers analyst questions
("why did EBITDA move?") with every number tied to a source, and writes an 8-section equity research PDF with tables, charts, a DCF and a
verification appendix.

Built on **LangGraph** (orchestration, durable state, human review), **LangChain** (models, tools, embeddings behind one gateway),
**Langfuse** (production traces and quality scores) and **LangSmith** (tracing, datasets, evaluation experiments). Local models by default
(Ollama); hosted models are a one-line config change.

| | |
|---|---|
| Design | [Architecture](docs/architecture.md) · [System Design Review](docs/sdr.md) · [Architecture Decision Records](docs/adr/) |
| Operate | [Deployment runbook](docs/deployment.md) · [Observability](docs/observability.md) · [Development](docs/development.md) |
| History | [0.2.0 audit: bugs found and fixed](docs/audit-0.2.0.md) · [original build plan](plan.md) · [Medium article](docs/medium/finrobot-medium-article.md) |
| Live site | [use-case page](https://sadiqgpasha89.github.io/FinRobot/) and [docs](https://sadiqgpasha89.github.io/FinRobot/docs/), published to GitHub Pages by `.github/workflows/pages.yml` (landing page source in `landing/`) |
| Docs site | built with MkDocs; also publishable to GitLab Pages by `.gitlab-ci.yml`: [how to set it up](docs/gitlab-pages.md) · preview locally with `pip install -r requirements-docs.txt && mkdocs serve` |

## Quick start (Docker)

```bash
cp .env.example .env     # set SEC_USER_AGENT="FinRobot you@example.com" and replace every change-me-* secret
docker compose up -d --build
docker compose exec api finrobot ping                       # postgres, object store, models, tracing
docker compose exec api finrobot ingest WM                  # XBRL facts + latest 10-K, 3x 10-Q, 3x earnings releases
curl -H "x-api-key: $KEY" localhost:8010/ask -d '{"question": "How did WM EBITDA change in Q3 versus a year ago, and what drove it?"}' \
     -H 'content-type: application/json'                    # -> {"run_id": "..."}, poll GET /runs/{id}
```

The UI is on <http://localhost:8510>, the API on <http://localhost:8010> (`/docs` for OpenAPI). Models run in Ollama on the host
(`ollama pull gpt-oss:20b gemma4:e4b nomic-embed-text`), or start one in compose with `--profile ollama`.
Production notes, backups and upgrades: [docs/deployment.md](docs/deployment.md).

## Quick start (local development)

```bash
docker compose up -d postgres objects      # pgvector on :5433, S3-compatible store on :9000
uv sync                                    # Python 3.11+
cp .env.example .env                       # set SEC_USER_AGENT; for local use also: FINROBOT_ENV=development and DATABASE_URL (see the file)
uv run finrobot ping
uv run finrobot ingest WM
uv run finrobot ask "How did WM's EBITDA and operating margin change in Q3 versus a year ago, and what drove it?"
uv run finrobot report WM                  # -> output/runs/<id>/report_WM_<period>.pdf
uv run finrobot serve                      # API on 127.0.0.1:8010   |   uv run finrobot ui   # Streamlit on 127.0.0.1:8510
```

Models (Ollama): `gpt-oss:20b` for figures and fact-checks, `gemma4:e4b` for prose, `nomic-embed-text` for search. On a 16 GB GPU only one
chat model fits at a time, so a run swaps models once or twice and the API runs jobs one at a time (`FINROBOT_LLM_CONCURRENCY=1`).
Use a hosted model per role with `FINROBOT_MODEL_NARRATIVE=anthropic:claude-sonnet-5-5` (`uv sync --extra anthropic`, `ANTHROPIC_API_KEY`).

## How it works

```
question ─▶ route ─▶ Data-CoT ─▶ Concept-CoT ─▶ Thesis-CoT ─▶ merge ─▶ cited, verified answer        (LangGraph: qa_graph)
                        │  each agent = tool loop ─▶ verify ─▶ (retry once) ─▶ strip what cannot be verified
ticker ─▶ draft assumptions ─▶ [human review] ─▶ tables, charts, DCF ─▶ write 8 sections ─▶ ground ─▶ PDF   (LangGraph: report_graph, checkpointed)
```

LLMs read and write; **code** does arithmetic, chooses periods, and checks every number. See [docs/architecture.md](docs/architecture.md).

## How it stays honest

| Guard | What it prevents |
|---|---|
| Every number an agent sees is registered under an id (`F12` figures, `S3` passages) and must be cited | uncited figures |
| `compute_metric` / `calc` do all arithmetic; a calc must use registered facts, and its **unit is inferred**, not chosen by the model | mislabelled units ("3 bps" for 3.4 percentage points); numbers typed in by hand |
| Facts carry a **period basis**; `calc` rejects balance-sheet ÷ one quarter's flow, and quarterly mixed with TTM | 11.0x leverage instead of 2.93x |
| Tools take `quarters_ago`, not years; the fiscal period is parsed from the question in code | a model inventing "FY2024" |
| Router sub-questions may not contain a year the user didn't write | the same, one step earlier |
| Deterministic **Evaluator**: each unit-bearing number must match a registered fact or appear in a cited passage; period labels must match the cited facts | unsupported figures, wrong-period labels, placeholder citations |
| A failed draft is retried once with the issues, then unverifiable lines are **removed** (a LangGraph verify → retry → sanitize loop) | confident wrong answers |
| **Grounding check** (gpt-oss) on qualitative claims | "positioned well against peers" with no source |
| Report **format check**; untrusted model text is **escaped** and the PDF renderer **cannot fetch URLs** | missing sections, `NaN`, injected markup, SSRF |

## API

All endpoints except `/health` and `/ready` need `x-api-key` (or `Authorization: Bearer`) when `FINROBOT_API_KEYS` is set; production refuses to
start without it. Jobs return `202 {"run_id"}` immediately; poll `GET /runs/{id}?after=<event id>`.

| Endpoint | |
|---|---|
| `POST /ask` `{question, ticker?}` | analyst question → run |
| `GET /reports/draft-assumptions?ticker=` · `POST /reports` `{ticker, assumptions?, peers?, price?, review?}` | PDF report; `review: true` pauses for assumption review |
| `POST /runs/{id}/resume` `{assumptions}` | continue a report that is `awaiting_review` (an invalid edit pauses again with the reason) |
| `POST /runs/{id}/retry` | continue an `interrupted` / `failed` report from its last completed section |
| `POST /ingest` · `POST /prices` · `GET /companies` | data |
| `GET /runs` · `GET /runs/{id}` · `GET /runs/{id}/artifacts/{name}` | run record (events, result, token usage, trace id) and files |
| `GET /health` (liveness) · `GET /ready` (Postgres + models) · `GET /metrics` (Prometheus) | operations |

### Prices, consensus, peers

There is no free, unauthenticated price or consensus API that FinRobot is willing to scrape, so:

```bash
uv run finrobot set-price WM 203.50                    # a manual quote
uv run finrobot import-prices WM prices.csv            # CSV with Date,Close
uv run finrobot fetch-prices WM                        # official Alpha Vantage API; needs ALPHAVANTAGE_API_KEY
uv run finrobot set-consensus WM ebitda 2026 3 2.10e9 --source "FactSet"   # lets Data-CoT report a beat/miss
uv run finrobot ingest RSG --facts-only && uv run finrobot ingest WCN --facts-only
uv run finrobot report WM --peers RSG,WCN
```

Without a price the report still builds (intrinsic value only). Without consensus, Data-CoT reports actuals and changes and says no consensus
is on file; it never assumes one.

### Editing the projection

Defaults are mechanical (trailing growth fading to 3.5%, trailing margins, capex and D&A ratios, WACC 8%, terminal growth 2.5%). Override in
decimals, in a file (`finrobot report WM --assumptions assumptions.yaml`), in the UI, or interactively with `POST /reports {"review": true}`:

```yaml
wacc: 0.09
terminal_growth: 0.025
rev_growth: [0.05, 0.05, 0.045, 0.04, 0.035]   # one value per projected year, or a single number
ebitda_margin: 0.31
capex_pct: 0.10
```

## Observability

Both backends are optional and switch on with keys; with none configured a run still records its own token usage and latency.

* **Langfuse**: one trace per run (id derived from the run id), the run id as the *session*, nested spans for every graph node, model call and
  tool call, and quality **scores** per run (`verification_ok`, `figures_tied_ratio`, `lines_removed`, `agent_retries`, `format_ok`, ...).
  Self-host with `docker compose -f docker-compose.yml -f docker-compose.langfuse.yml up -d`.
* **LangSmith**: automatic LangChain/LangGraph tracing with `LANGSMITH_TRACING=true`, the same scores as feedback, and golden-set experiments:
  `python evals/run.py --langsmith`.
* `FINROBOT_TRACE_INPUTS=false` redacts prompts and outputs before they leave the process.

Details: [docs/observability.md](docs/observability.md).

## Layout

```
src/finrobot/
  llm/         LLMGateway (LangChain chat + embeddings, retries, concurrency lane), Tool -> StructuredTool, usage tracker
  agents/      react.py (tool loop subgraph), checked.py (verify/retry/sanitize subgraph), Router, Data/Concept/Thesis-CoT,
               Evaluator, grounding, Math + Function tools, section writers
  graph/       qa_graph, report_graph (checkpointed, human review), checkpoint (Postgres)
  data/        EDGAR client, XBRL -> quarterly facts, parser/chunker, Postgres store (pool, transactions), migrations, object store, ingest
  tools/       FactBook, metrics, calc, valuation (projection + DCF), charts, tables
  report/      deterministic report model, Jinja2 + WeasyPrint rendering, format check
  api/         FastAPI app          service.py  run execution shared by API and CLI       observability.py  Langfuse + LangSmith + usage
  cli.py  config.py (typed settings)  log.py (JSON logs)  runs.py (run records, artifacts)  ui/ (Streamlit)
tests/         unit tests (no GPU, network or database) + tests/integration (real Postgres)
evals/         live evals against real models; LangSmith experiments
docs/          architecture, SDR, ADRs, runbook, audit, Medium article
```

## Testing

```bash
uv run pytest                                  # 139 unit tests: no GPU, network or database
FINROBOT_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres uv run pytest -m integration
uv run python evals/run.py                     # live: real models, ingested WM; stop other Ollama clients first (one GPU slot)
```

The integration suite uses a real Postgres + pgvector (throwaway database), the real LangGraph Postgres checkpointer, a mock SEC server and a
scripted chat model, and runs both graphs end to end, including human review and crash-then-resume. CI runs all of it plus a Docker build.

## Known limitations

- **EBITDA is reported operating income + D&A**, not the company's *adjusted* EBITDA. Reports label it.
- Qualitative claims are checked by an LLM (gpt-oss), which catches most but not all unsupported interpretation. Numbers are checked
  deterministically; wording like "reinforces a resilient market position" can still slip through.
- Fourth quarters and cash-flow quarters are derived (full year − nine months, differences of cumulative periods) and marked as derived.
- Fiscal years are named by the calendar year in which they end. 52/53-week years are handled within a 15-day tolerance.
- The DCF is a mechanical extrapolation under stated assumptions: not a forecast, not a rating, not investment advice.
- Only US SEC registrants (10-K / 10-Q / earnings 8-K). Transcripts, sell-side research and alternative data are not ingested.
- About 1 in 8 runs of a simple figure question ends in "no verified answer" rather than a wrong one (a live check in 0.2.0 saw 1 miss in 6 on gpt-oss:20b, on a synthetic company).
- Jobs run in-process on a small worker pool; a restart marks running jobs `interrupted` (reports resume from their checkpoint). Scale-out is
  an ADR-documented follow-up ([ADR-0007](docs/adr/0007-in-process-job-queue.md)).
