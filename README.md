# FinRobot

Multi-agent equity research with local LLMs. Give it a ticker; it ingests SEC filings and XBRL financials, answers analyst questions
("why did EBITDA move?") with cited figures, and writes an 8-section equity research PDF with tables, charts, a DCF and a verification appendix.

Design and roadmap: [plan.md](plan.md) (see **Build status** at the top for what was built and where it deviates).

## Quick start

```bash
cp .env.example .env                  # then set SEC_USER_AGENT="FinRobot you@example.com" (SEC requires contact details)
docker compose up -d                  # postgres+pgvector on :5433, S3-compatible object store on :9000
uv sync
uv run finrobot ping                  # checks postgres, object store, ollama models
uv run finrobot ingest WM             # XBRL facts + latest 10-K, 3x 10-Q, 3x earnings releases (embeds with nomic-embed-text)

uv run finrobot ask "How did WM's EBITDA and operating margin change in Q3 versus a year ago, and what drove it?"
uv run finrobot report WM             # -> output/runs/<id>/report_WM_<period>.pdf
```

Models (Ollama): `gpt-oss:20b` for figures and fact-checks, `gemma4:e4b` for prose, `nomic-embed-text` for search. Override with
`FINROBOT_MODEL_FIGURES`, `FINROBOT_MODEL_NARRATIVE`, `FINROBOT_EMBED_MODEL`. On a 16 GB GPU only one of the two chat models fits at a time, so a run
swaps models once or twice and the API runs jobs one at a time.

### UI and API

```bash
uv run finrobot serve        # API on 127.0.0.1:8010
uv run finrobot ui           # Streamlit on 127.0.0.1:8510 (start the API first)
```

Both bind to localhost only. There is no authentication; do not expose them.

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

Without a price the report still builds; the valuation shows intrinsic value and says no price was loaded. Without consensus, Data-CoT
reports actuals and year-over-year / sequential changes and says that no consensus is on file; it never assumes one.

### Editing the projection

Defaults are mechanical (trailing growth fading to 3.5%, trailing margins, capex and D&A ratios, WACC 8%, terminal growth 2.5%). Override them
in decimals:

```yaml
# assumptions.yaml
wacc: 0.09
terminal_growth: 0.025
rev_growth: [0.05, 0.05, 0.045, 0.04, 0.035]   # one value per projected year, or a single number
ebitda_margin: 0.31
capex_pct: 0.10
```
`uv run finrobot report WM --assumptions assumptions.yaml`, or edit them in the UI's Report tab.

## How it stays honest

LLMs are used for reading and writing, never for arithmetic or for choosing periods.

| Guard | What it prevents |
|---|---|
| Every number an agent sees is registered under an id (`F12` figures, `S3` passages) and must be cited | uncited figures |
| `compute_metric` / `calc` do all arithmetic; a calc must use registered facts, and its **unit is inferred**, not chosen by the model | mislabelled units ("3 bps" for 3.4 percentage points); numbers typed in by hand |
| Facts carry a **period basis**; `calc` rejects balance-sheet ÷ one quarter's flow, and quarterly mixed with TTM | 11.0x leverage instead of 2.93x |
| Tools take `quarters_ago`, not years; the fiscal period is parsed from the question in code | a model inventing "FY2024" |
| Router sub-questions may not contain a year the user didn't write | the same, one step earlier |
| Deterministic **Evaluator**: each unit-bearing number must match a registered fact or appear in a cited passage; period labels must match the cited facts | unsupported figures, wrong-period labels, placeholder citations |
| A failed draft is retried once with the issues, then unverifiable lines are **removed** | confident wrong answers |
| **Grounding check** (gpt-oss) on qualitative claims | "positioned well against peers" with no source |
| Report **format check** | missing sections, `NaN`/placeholder text, empty charts, non-PDF output |

## Layout

```
src/finrobot/
  data/        EDGAR client, XBRL -> quarterly facts, filing parser/chunker, Postgres store (pgvector), object store, ingest
  tools/       FactBook, metrics, calc, valuation (projection + DCF), charts, tables
  agents/      Router, Data/Concept/Thesis-CoT, Math (tools), Function-calling (charts), Evaluator, grounding, section writers
  graph/       LangGraph: qa_graph (question answering) and report_graph (PDF report)
  report/      deterministic report data model, Jinja2 + WeasyPrint rendering, format check
  api/ cli.py  FastAPI service and the command line
ui/app.py      Streamlit
evals/run.py   live evals against the real models (expected numbers recomputed from the database)
tests/         unit tests (no GPU needed)
```

## Testing

```bash
uv run pytest                       # unit tests, no GPU or network
uv run python evals/run.py          # live: real models, ingested WM; stop other Ollama clients first (one GPU slot)
```
Integration paths were verified by hand against a running stack (see plan.md); the model-driven paths are not deterministic, so run the evals
after any prompt, tool or model change.

## Known limitations

- **EBITDA is reported operating income + D&A**, not the company's *adjusted* EBITDA. Reports label it; compare with the company's own
  non-GAAP figures in the earnings release.
- Qualitative claims are checked by an LLM (gpt-oss), which catches most but not all unsupported interpretation. Numbers are checked
  deterministically; wording like "reinforces a resilient market position" can still slip through.
- Fourth quarters and cash-flow quarters are derived (full year − nine months, differences of cumulative periods) and marked as derived.
  Weighted-average share counts are never differenced.
- Fiscal years are named by the calendar year in which they end. 52/53-week years are handled within a 15-day tolerance.
- The DCF is a mechanical extrapolation under stated assumptions: not a forecast, not a rating, not investment advice.
- Only US SEC registrants (10-K / 10-Q / earnings 8-K). Earnings call transcripts, sell-side research and alternative data are not ingested.
- About 1 in 8 runs of a simple figure question ends in "no verified answer" rather than a wrong one.
