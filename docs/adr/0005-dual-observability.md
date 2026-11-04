# ADR-0005: Langfuse for production traces and scores, LangSmith for development and evaluation

* Status: Accepted · Date: 2026-11-05

## Context
Two different questions need answers. In production: *what did this run do, what did it cost, did its verification pass, and how is quality trending?*
In development: *did this prompt/tool/model change make the golden set better or worse?* Financial text in traces is sensitive, and some deployments
cannot send it to a third-party SaaS.

## Decision
Support both, optional, joined by the FinRobot run id:
* **Langfuse** (self-hostable, `docker-compose.langfuse.yml`) is the production trace store. Each run is one trace whose id is derived from the run id
  (`create_trace_id(seed=run_id)`); the run id is the Langfuse *session*; spans come from the LangChain callback handler; verification outcomes are
  posted as **scores**.
* **LangSmith** auto-traces LangChain/LangGraph with `LANGSMITH_TRACING=true`, receives the same scores as feedback (root run id = `uuid5(run id)`), and
  runs **dataset experiments** (`evals/run.py --langsmith`) with one evaluator per check.
* A small internal `UsageTracker` callback records tokens, calls and latency per role into the run row regardless of either backend.
* `FINROBOT_TRACE_INPUTS=false` redacts prompts and outputs before they leave the process; scores and timings still flow.

## Alternatives considered
| Option | Why not |
|---|---|
| LangSmith only | strong for evals, but cannot be self-hosted by default; some users need traces to stay inside their network |
| Langfuse only | excellent ops tracing and scores; LangSmith's dataset/experiment workflow and native LangGraph views are what the eval loop uses |
| OpenTelemetry only | no LLM-aware UI, no scores, no datasets; Langfuse already speaks OTel underneath |
| Both always on | privacy and cost; each must be opt-in by key |

## Consequences
* (+) A run id opens the trace in either tool; scores make "verification pass rate" a dashboard rather than a log search.
* (+) Verified offline: the real Langfuse SDK with an in-memory span exporter (`tests/test_observability.py`) proves one trace per run, session id, nesting.
* (−) Not yet verified against a live Langfuse/LangSmith server in this repository's CI (no credentials in CI); `docs/observability.md` lists the manual check.
* (−) Two vendors' SDKs and two sets of env vars; both are inert without keys.
