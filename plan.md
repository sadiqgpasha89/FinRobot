# Stack

> | Plan | As built | Why |
> |---|---|---|
> | LLM: Claude API default, Ollama optional | **Ollama only** (`gpt-oss:20b` figures, `gemma4:e4b` prose) | the user's machine and models; a hosted-model class is a small addition behind `llm/client.py` |
> | MinIO object store | **SeaweedFS** (S3 API), local-filesystem fallback | MinIO no longer publishes container images (Docker Hub, quay.io both fail); the S3 client code is unchanged |
> | Market data via `yfinance` | **CSV / manual / Alpha Vantage (official API)** | no unauthenticated price API without scraping; Alpha Vantage is unit-tested on its parser only, never called live |
> | Consensus from a provider | **manual table** (`set-consensus`) | no free source; Data-CoT says plainly when none is on file |
> | Earnings transcripts | not ingested; **earnings-release 8-K (EX-99.1)** instead | transcripts are not freely available from EDGAR |
> | pdfkit / wkhtmltopdf | **WeasyPrint** | wkhtmltopdf is not installed; the plan allowed it |
> | Human-in-the-loop at valuation | **edit-assumptions step** in the UI (`/reports/draft-assumptions`) | simpler than a graph interrupt; same outcome |
> | `evals/` ~20 golden questions | **4 live cases**, numbers recomputed from the DB | enough to catch regressions that unit tests missed (see below); extend over time |
> | Tracing / cost tracking | **not built** (event log per run in Postgres only) | Phase 6 |
> | Math agent, Function-calling assistant | implemented as **code tools**, not LLM agents | by design: arithmetic must be computed, not recalled |
> | Extras beyond the plan | period-basis tracking, inferred units, grounding check, deterministic period parsing, figure-question fast path | each was added after a real failure; see "What the live runs found" |
>
> ### What the live runs found (all fixed, each has a regression test)
> Mock-mode tests would have passed all of these. Only real-model runs exposed them:
> 1. The Router **invented a year** inside a sub-question ("Q3 2024"); Data-CoT then answered about 2024. → years are parsed in code; sub-questions may not contain a year the user did not write.
> 2. Even with the right focus period, `gpt-oss` drifted to FY2024/2023 when it had to choose fiscal years. → tools take `quarters_ago`; no year to get wrong.
> 3. The model **labelled 3.4 percentage points as "3 bps"** and the numeric check accepted it. → `calc` infers units by dimensional analysis; model-chosen units removed.
> 4. After a rejected calc it **retyped the numbers by hand**, creating a fact with no provenance. → a calc must use registered fact ids.
> 5. A real figure with the **wrong period label** ("FY2024 Q3: $2.07 billion", actually FY2026 Q3) passed. → period labels in prose must match the cited facts.
> 6. **Net debt ÷ one quarter's EBITDA = 11.0x** instead of 2.93x (2 of 6 runs), found by the live evals; every number was real and cited. → period-basis tracking in `calc`.
> 7. A small model **skipped retrieval** and wrote "[S#]" placeholders and "the documents do not say…". → the focus quarter's evidence is prefetched in code; placeholder citations fail verification.
> 8. Retrieval returned the **previous quarter's guidance as the cause** of this quarter's growth. → search defaults to documents published for the focus quarter.
> 9. A tool-call JSON error from Ollama crashed the run; a model still emitting tool calls on its last step returned nothing. → both recovered.
> 10. Report layout: orphaned table captions, text tables right-aligned, legends over data, a stray heading from a markdown rule, runs of citations. → fixed; format check added.
> 11. The UI echoed 2-decimal-rounded defaults back as "overrides", silently changing the model. → only changed fields are sent.
> 12. Streamlit bound to all interfaces by default. → forced to 127.0.0.1 (API and UI).
>
> ### Not done / next
> Golden-set expansion (more tickers, a non-calendar fiscal year such as a retailer, a bank), tracing and cost per run, a Claude/hosted provider,
> a price/consensus provider with a key, transcript ingestion, and re-checking the grounding judge on a stronger model.

An AI agent system for equity research and valuation with LLMs. Given a ticker (or a question about one), it gathers filings and market data, reasons about them with specialised agents, and produces a structured equity research report with tables, charts and a valuation.

## 0. Source material

The folder holds two diagrams and no code. This plan is derived from them.

| File | What it shows |
|---|---|
| `Finrobot.png` | **Three-layer architecture**: Data Processing → Fin-Concept → Equity Research Template |
| `Screenshot from 2026-11-01 22-41-06.png` | **Multi-agent CoT flow**: a question is decomposed to Data-CoT, Concept-CoT and Thesis-CoT agents, with Math and Function-Calling helpers, then merged into one answer |

### Layer 1 — Data Processing (bottom)
- **Sources**: SEC filings (10-K, 10-Q: business description, risks, MD&A, statements, notes), sell-side equity research (initiation, rating, estimates, price-target revisions), ticker-specific knowledge (growth, gross/EBITDA/EBIT/FCF margin assumptions, comp sets), corporate releases (guidance, press releases, investor decks, earnings transcripts), alternative sources (company sites, competitor filings, McKinsey-style industry research, Google Trends, price lists).
- **Storage and access**: PostgreSQL (OceanBase in the original), unstructured docs (pdf, doc, docx, jpg, jpeg, png, ofd), third-party data interfaces (visualization config, API integration), internet data (search engines, multi-source websites), distributed file storage (DFS, MinIO).

### Layer 2 — Fin-Concept (middle)
- **Company Overview**: fundamental metrics, recent policy, market position
- **Thesis Creating**: revenue drivers, margin expansion, market-share capture
- **Story Telling**: how quarterly results affect the thesis; risk adjusted by operating results
- **Table and Graph**: financial data computing, graph and table generation
- Supporting concepts: financial summary, business overview, possible risks, key metrics, sell-side insights, valuation / credit / cash-flow metrics, quarterly results impact, competitor data, market positioning.

### Layer 3 — Equity Research Template (top)
- **Report sections**: Company Overview, Thesis and Drives, Investment Update, Financial Summary, Risks and Mitigations, Valuation, Financial Projections, Credit & Cashflow
- **Tools**: python-pdfkit, Evaluation, Function Calls, Format Check

### Agent flow (from the CoT diagram)
Example: *"Waste Management missed Q2 EBITDA. Why?"*
1. **Data-CoT agent** answers "what is Q2 EBITDA and how big is the miss?" (actual vs consensus).
2. **Concept-CoT agent** answers "given that data, why did the miss occur?" and builds a causal chain.
3. **Thesis-CoT agent** answers "what's the best way to tell this story, given the company and industry?" and uses **Math Agent** and **Function-Calling Assistant**, which call **python-pdfkit** for output.
4. The three answers are **added together** into one final answer. The dashed arrows between agents mean they exchange context, not just run in parallel.

---

## 1. Goals and non-goals

**Goals**
1. Question mode: answer analyst-style questions about a public company, with every number cited to a source document.
2. Report mode: produce a multi-section equity research PDF for a ticker (the full Layer-3 template).
3. Numeric correctness: all arithmetic and valuation done by code, never by the LLM.
4. Reproducible, inspectable runs: every report can be traced back to its source passages and tool calls.

**Non-goals (v1)**
- Real-time or intraday trading signals, order execution, portfolio management.
- Personalised investment advice. Reports carry a "not investment advice" disclaimer.
- Non-US filings, OFD/Chinese-market sources and OceanBase (the diagram lists them; we skip them for now).

## 2. Key decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Finance and LLM ecosystem |
| Agent orchestration | **LangGraph** | The flow is a graph with shared state and cross-agent exchange; has checkpointing and human-in-the-loop |
| LLM access | Provider-agnostic wrapper; default **Claude API**, with an **Ollama** local option | Local models cost nothing for dev; the hosted model is for quality on long filings. Local GPU is 16 GB, so only mid-size models fit |
| Structured DB | **PostgreSQL** + `pgvector` | One store for financial tables and embeddings; replaces OceanBase |
| Object storage | **MinIO** (S3 API) | Matches the diagram, runs locally in Docker |
| Doc parsing | `sec-edgar-downloader`/EDGAR API, `unstructured` or `pymupdf`, `pandas` | Filings are HTML/PDF with tables |
| Market data | `yfinance` for prototyping; pluggable provider interface (FMP / Polygon / Alpha Vantage) | Free to start, swappable later |
| Charts | `matplotlib` (static, reproducible in PDF) | Needs to embed in the PDF |
| PDF rendering | Jinja2 → HTML → PDF (`pdfkit`/wkhtmltopdf as in the diagram, or WeasyPrint if wkhtmltopdf is a pain to install) | Template-driven; matches the "python-pdfkit" box |
| API | FastAPI | Simple job-based endpoints |
| UI | Streamlit first, a real frontend only if needed | Cheapest way to drive the pipeline |
| Packaging | `uv` + `pyproject.toml`, `docker compose` for Postgres and MinIO | |

## 3. Architecture

```
                    ┌────────────────────────────────────────────┐
  CLI / API / UI ──►│  Orchestrator (LangGraph)                  │
                    │  router: question mode │ report mode       │
                    └───────┬────────────────────────┬───────────┘
                            │                        │
        ┌───────────────────▼───────┐     ┌──────────▼───────────────────┐
        │ Multi-agent CoT           │     │ Report builder               │
        │  Data-CoT ⇄ Concept-CoT   │     │  section agents (8)          │
        │       ⇄ Thesis-CoT        │     │  → template → PDF            │
        │  + Math agent             │     │  → Format check + Evaluation │
        │  + Function-calling asst. │     └──────────┬───────────────────┘
        └───────────┬───────────────┘                │
                    └────────────┬───────────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │ Tool layer (typed functions)  │
                 │ get_financials · get_filing   │
                 │ search_docs · compute_metric  │
                 │ make_chart · make_table · DCF │
                 └───────────────┬───────────────┘
                                 ▼
        ┌──────────────────────────────────────────────────┐
        │ Data layer: ingest → parse → chunk/embed → store │
        │ Postgres(+pgvector) · MinIO · provider adapters  │
        └──────────────────────────────────────────────────┘
```

### Agent responsibilities

| Agent | Input | Output | Tools |
|---|---|---|---|
| **Router** | User request | mode, ticker, period, sub-questions | none |
| **Data-CoT** | Sub-question about facts | Figures + citations (e.g. actual vs consensus, size of miss) | `get_financials`, `get_consensus`, `search_docs` |
| **Concept-CoT** | Data-CoT output + filings/transcripts | Causal explanation, ranked drivers, each with a source | `search_docs`, `get_transcript` |
| **Thesis-CoT** | Both of the above + company/industry context | Narrative, thesis, risk framing | `get_peers`, `search_docs` |
| **Math agent** | Expressions / tables | Exact computed values | `compute_metric`, `dcf`, `comps_table` (pure Python) |
| **Function-calling assistant** | Structured request | Charts, tables, PDF artefacts | `make_chart`, `make_table`, `render_pdf` |
| **Section agents** (report mode) | Shared state | One report section each | Same tools |
| **Evaluator** | Draft output | Pass/fail + issues per check | Numeric tie-out, citation check, format check |

**Rules that make this reliable**
- The LLM **never does arithmetic**. It requests a tool call; the Math agent returns the number.
- Every claim carries a `source_id` (filing + section/page, or API + timestamp). The Evaluator rejects uncited figures.
- Cross-agent exchange (the dashed arrows) is a shared `State` object: `facts[]`, `explanations[]`, `narrative`, `artifacts[]`. Agents read and append; a bounded number of rounds (default 2) before merge.

## 4. Data model (Postgres)

- `company(ticker, cik, name, sector, industry)`
- `filing(id, ticker, form_type, period, filed_at, url, object_key)`
- `filing_chunk(id, filing_id, section, page, text, embedding vector)`
- `financial_fact(ticker, period, metric, value, unit, source_id)` — normalised IS/BS/CF lines (XBRL "company facts")
- `consensus(ticker, period, metric, mean, high, low, n_analysts, as_of)`
- `transcript_chunk(...)`, `research_note(...)` — same shape as `filing_chunk`
- `price_daily(ticker, date, ohlcv)`
- `run(id, mode, request, status, state_json, created_at)` and `artifact(run_id, kind, object_key)`

Prefer SEC **XBRL company facts** for statement numbers (structured, free) and use text parsing only for MD&A, risks and notes.

## 5. Repository layout

```
FinRobot/
├── plan.md
├── pyproject.toml
├── docker-compose.yml          # postgres+pgvector, minio
├── .env.example
├── src/finrobot/
│   ├── config.py
│   ├── llm/                    # provider wrapper (claude, ollama)
│   ├── data/
│   │   ├── providers/          # edgar.py, market.py, transcripts.py, search.py
│   │   ├── ingest.py  parse.py  chunk.py  store.py
│   ├── tools/                  # typed functions exposed to agents
│   │   ├── financials.py  retrieval.py  metrics.py  valuation.py
│   │   ├── charts.py  tables.py  pdf.py
│   ├── agents/
│   │   ├── router.py  data_cot.py  concept_cot.py  thesis_cot.py
│   │   ├── math_agent.py  function_agent.py  sections/  evaluator.py
│   ├── graph/                  # LangGraph definitions: qa_graph.py, report_graph.py
│   ├── templates/              # Jinja2 HTML + CSS for the report
│   ├── api/                    # FastAPI app
│   └── cli.py
├── ui/app.py                   # Streamlit
├── evals/                      # golden questions, report checks
└── tests/
```

## 6. Phased build

Each phase ends with something runnable. Do them in order.

### Phase 0 — Scaffold (½ day)
- `uv init`, `pyproject.toml`, `.env.example`, `docker compose` for Postgres+pgvector and MinIO, git commit.
- LLM wrapper with one `complete()` and one `tool_call()` interface; smoke test against Claude and Ollama.
- **Done when**: `docker compose up` works and `finrobot ping` gets a reply from the configured model.

### Phase 1 — Data layer (2–3 days)
- EDGAR provider: resolve ticker→CIK, download 10-K/10-Q, fetch XBRL company facts.
- Parse filings into sections (Business, Risk Factors, MD&A, Financial Statements) and chunk; embed to pgvector.
- Market provider: price history, basic fundamentals, consensus estimates (best-effort from free source; log the gap if unavailable).
- Transcript provider: start with manually dropped PDFs/text into MinIO; automate later.
- **Done when**: `finrobot ingest WM` populates the DB and `search_docs("commodity price headwinds", ticker="WM")` returns relevant cited chunks.

### Phase 2 — Tool layer (2 days)
- Typed, unit-tested functions: `get_financials`, `search_docs`, `compute_metric` (growth, margins, EBITDA, FCF, ratios), `dcf`, `comps_table`, `make_chart`, `make_table`.
- Metrics are pure Python and tested against hand-computed fixtures.
- **Done when**: the tool functions pass tests and can be called from a script with no LLM.

### Phase 3 — Multi-agent CoT Q&A (3–4 days) ← first real milestone
- Router + Data-CoT + Concept-CoT + Thesis-CoT in LangGraph with shared state, Math agent, and merge node.
- Evaluator with citation and numeric tie-out checks.
- CLI: `finrobot ask "WM missed Q2 EBITDA, why?"`.
- **Done when**: the WM-style question returns actual vs consensus, the size of the miss, ranked drivers with citations, and a short narrative, and an uncited number is rejected by the Evaluator.

### Phase 4 — Report generation (4–5 days)
- Eight section agents (Company Overview, Thesis and Drives, Investment Update, Financial Summary, Risks and Mitigations, Valuation, Financial Projections, Credit & Cashflow).
- Projections: driver-based (revenue growth, margin assumptions from the ticker-specific knowledge layer) feeding a DCF and comps valuation, with a sensitivity table. Assumptions are explicit and editable.
- Jinja2 template + CSS, charts embedded, PDF render, **Format Check** (page count, section presence, no broken images or `NaN`).
- CLI: `finrobot report WM --period latest`.
- **Done when**: one command yields a 6–10 page PDF where each table figure ties back to `financial_fact` rows.

### Phase 5 — API and UI (2 days)
- FastAPI: `POST /ask`, `POST /reports` (async job), `GET /runs/{id}`, `GET /artifacts/{id}`.
- Streamlit page: ticker input, live agent trace, download PDF, show citations.
- Human-in-the-loop: pause before valuation so the user can edit assumptions.

### Phase 6 — Evaluation and hardening (ongoing)
- `evals/`: ~20 golden questions with known answers across 3–5 tickers; check numeric accuracy, citation coverage, and report structure.
- Track tokens, latency and cost per run; add tracing (LangSmith or OpenTelemetry).
- Caching for filings and embeddings; retries and rate limiting for providers.

### Later / stretch
- Alternative data (Google Trends, industry reports), sell-side report ingestion, image/OCR for scanned docs, multi-company comparison reports, scheduled earnings-day reports.

## 7. Evaluation

| Check | Method |
|---|---|
| Numeric accuracy | Tie each number in the output to a `financial_fact` row or tool result; zero tolerance for unmatched |
| Citation coverage | Every factual sentence has a `source_id`; the target is 100% for figures |
| Causal-claim grounding | Each driver in Concept-CoT output must have a supporting chunk (e.g. the "50 bps" and "40 bps" items must appear in the transcript/filing) |
| Report structure | All 8 sections present, charts rendered, PDF opens, no `NaN`/`None`/placeholder text |
| Regression | Golden set run on each PR; compare to previous scores |

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| LLM hallucinates numbers | Tools-only arithmetic, Evaluator tie-out, reject uncited figures |
| Filing HTML is messy, tables break | Use XBRL for statements; parse text only for narrative sections; spot-check parsers per ticker |
| Free consensus data is thin or unreliable | Define a `consensus` interface, mark source and as-of date, degrade gracefully when missing |
| Long filings exceed context | Section-level retrieval with pgvector, not whole-doc prompting |
| Local model too weak on long reasoning | Per-agent model config; use the stronger hosted model for Concept/Thesis, the local one for routing and extraction |
| Scope creep from the diagram | Hold v1 to SEC + market data + transcripts; the rest is Phase "Later" |
| Regulatory / advice concerns | Disclaimer in every report; no buy/sell execution; label ratings as model output |

## 9. Open questions for you

1. **LLM**: Claude API, local Ollama, or both? (This determines the model-per-agent config in Phase 0.)
2. **Coverage**: US large caps only to start, and which 3–5 tickers do you want as the test set (WM from the diagram is a natural one)?
3. **Data budget**: free sources only (EDGAR + yfinance), or do you have an FMP/Polygon key for consensus and transcripts?
4. **Primary deliverable**: is the PDF report the main product, or the question-answering mode?
5. **Interface**: CLI plus Streamlit enough, or do you want a web app?

## 10. Suggested first step

Phases 0–3 give a working multi-agent Q&A on real filings in about a week, and they de-risk the hardest parts (data parsing and grounded reasoning) before the report template work. Start there.
