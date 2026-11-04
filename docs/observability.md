# Observability

Two optional backends, one set of join keys. With neither configured, every run still records its own token usage, latency and event log in Postgres
(`GET /runs/{id}` → `usage`, `events`).

## What every run emits

* **Run record** (Postgres): status, request, result, events, `usage` (calls, input/output tokens, model seconds, per role and model), `trace_id`, `error`.
* **Trace**: the whole graph: `route` → agents → `merge` (or `draft` → `model` → `write`×8 → `ground` → `render`), with each agent's draft/verify/retry/sanitize
  steps, each model call (`llm.figures`, `llm.narrative`, `llm.embed`) and each tool call (`compute_metric`, `calc`, `search_docs`, `make_chart`).
* **Scores** (see below) attached to the trace.
* **Logs**: JSON with `run_id` and `request_id` on every line; `x-request-id` on every HTTP response.
* **Metrics** (`/metrics`): run outcomes and durations, queue depth, HTTP latency by templated path.

## Join keys

| From | To | How |
|---|---|---|
| run id | Langfuse trace | `Langfuse.create_trace_id(seed=run_id)`; the id is also stored in `run.trace_id` |
| run id | Langfuse session | the run id **is** the session id: all traces of a run (original + resume) group together |
| run id | LangSmith root run | `uuid5(namespace, run_id)`, set as the graph's `run_id` |
| trace | logs | `run_id` in every log line |

## Scores

| Score | Type | Meaning |
|---|---|---|
| `verification_ok` | boolean | the final evaluation found no hard issues |
| `figures_checked` | number | unit-bearing numbers the Evaluator examined |
| `figures_tied_ratio` | 0–1 | share of those matched to a registered fact or a cited passage |
| `lines_removed` | number | lines deleted because they could not be verified (sanitize, grounding, dropped risk rows) |
| `agent_retries` | number | agents that needed the repair attempt (question runs) |
| `format_ok`, `pages` | boolean, number | report format check result and length (report runs) |

Dashboards worth building in Langfuse: pass rate of `verification_ok` by day; distribution of `lines_removed`; cost and tokens by `role`; latency p95 by trace name.

## Langfuse

1. Cloud or self-hosted. Self-hosted: `docker compose -f docker-compose.yml -f docker-compose.langfuse.yml up -d`, open <http://localhost:3000>, create an org and project, create an API key pair.
2. `.env`: `LANGFUSE_PUBLIC_KEY=pk-lf-...`, `LANGFUSE_SECRET_KEY=sk-lf-...`, `LANGFUSE_BASE_URL=` (your host; `http://langfuse-web:3000` from inside compose).
3. Restart the API. `finrobot ping` shows `langfuse enabled`.

## LangSmith

1. `.env`: `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY=lsv2_...`, `LANGSMITH_PROJECT=finrobot` (EU: also `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`).
2. Traces appear per run; the scores are posted as feedback on the root run.
3. Offline evaluation: `python evals/run.py --langsmith` creates/uses the `finrobot-golden-qa` dataset (expected figures are recomputed from the database at run time) and
   records an experiment with evaluators `figures_correct`, `verification_passed`, `period_correct`, `case_passed`. Compare experiments before and after a prompt, tool or model change.

## Privacy

Traces contain prompts and model output: company questions, filing excerpts, draft prose. Options, strongest first: self-host Langfuse and leave LangSmith off; set
`FINROBOT_TRACE_INPUTS=false` (prompts and outputs are replaced by `[redacted]` before leaving the process; timings, token counts and scores still flow); leave tracing off
(default without keys). Set a retention period in the vendor UI.

## Verify it works

```bash
finrobot ask "What was WM revenue growth in Q1 year over year?" -t WM       # prints the run id
```
1. Langfuse → Sessions → the run id → one trace named `finrobot.qa` with nested `route`, `data` (→ `agent:Data-CoT` → `tool_loop` → `llm.figures`, `compute_metric`), ... and scores.
2. LangSmith → project `finrobot` → root run `finrobot.qa` with the same tree and feedback.
3. `GET /runs/{id}` → `trace_id` equals the Langfuse trace id; `usage.by_role` shows tokens per model.

*What was verified offline in this repository:* the real Langfuse SDK with an in-memory exporter (`tests/test_observability.py`) confirms one trace per run, the session id, tags,
and that model calls nest under the run root; usage accounting is tested through nested graphs. *Not verified here:* delivery to a live Langfuse/LangSmith server (no credentials), which
is why step 1–3 above are part of the first-deploy checklist.
