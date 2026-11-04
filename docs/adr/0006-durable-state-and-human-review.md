# ADR-0006: Durable report runs: small serialisable state, rebuildable runtime context, `interrupt` for review

* Status: Accepted · Date: 2026-11-05

## Context
A report is minutes of model work across eight sections. A crash or deploy used to lose all of it, and the "edit the assumptions" step was an API
convention outside the workflow. The plan (0.1) wanted a graph interrupt at valuation and settled for a UI step as simpler.

## Decision
* Compile `report_graph` with a Postgres checkpointer; `thread_id = run id`.
* Keep the graph **state small and serialisable**: assumptions (dict), prose, stats, extra tables, a FactBook snapshot. Keep heavy, non-serialisable
  objects (`RunContext`, `ReportModel`, chart bytes) in the run's `Runtime` context and **rebuild them deterministically** from the database after a resume
  (`get_ctx`, `get_model`). Fact ids stay stable because the FactBook deduplicates on `(label, value, unit, source)`.
* Write **one section per graph step**, so every section is a checkpoint.
* `draft` calls `interrupt()` when `review=true`. The resume value is validated **inside a loop that re-interrupts with the error**; raising instead is
  unrecoverable, because LangGraph keeps the failed resume value for the task and ignores a corrected one (found by an integration test).
* The API exposes `POST /runs/{id}/resume` (review) and `/retry` (continue from the last checkpoint); startup marks runs left `running` as `interrupted`.

## Alternatives considered
| Option | Why not |
|---|---|
| State holds everything (model, charts, context) | checkpoint serialisation of arbitrary objects is brittle across versions; large blobs per step |
| Own job table + restart-from-scratch | pays for all eight sections again after any failure |
| Temporal | see ADR-0001; a second runtime for one workflow |
| Keep the UI-only assumptions step | not available to API clients, not part of the run's history |

## Consequences
* (+) Verified by tests on both the in-memory and the real Postgres checkpointer: the review pause performs no model calls; a run that crashed at
  section 3 resumes and makes exactly the remaining 6 model calls; an invalid edit pauses again instead of poisoning the run.
* (−) Resume correctness depends on `build_model` being deterministic for the same inputs (it reads the database; a price loaded between crash and
  resume yields a new price fact id). Acceptable: the prose never cites a price that was not in the rebuilt model.
* (−) Checkpoints add one blob write per step; trivial next to the model calls.
