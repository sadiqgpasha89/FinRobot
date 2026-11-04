# ADR-0001: LangGraph is the orchestration layer, at every level

* Status: Accepted · Date: 2026-11-05

## Context
Version 0.1 used LangGraph only as a straight line of five functions, with a mutable dictionary smuggling the run context between nodes, a
hand-written `while` loop for tool calling and a hand-written retry function for verification. The behaviours that matter most (the tool
loop, the verify → retry → sanitize loop, the report's per-section writing, human review of assumptions, resuming after a crash) are all
*control flow over state*, which is what a graph runtime is for. Hand-written, they were invisible to tracing and impossible to checkpoint.

## Decision
Model each of them as a LangGraph:
* `qa_graph` and `report_graph` at the top;
* `react` (model ⇄ tools) and `checked` (draft → verify → retry/sanitize → finish) as subgraphs, compiled with `checkpointer=False` so their
  non-serialisable state is never persisted even inside a checkpointed parent;
* run-scoped objects (LLM gateway, event sink, `RunContext`) travel in LangGraph's typed `Runtime` context, not in state and not in a global.

## Alternatives considered
| Option | Why not |
|---|---|
| Keep plain functions | every loop invisible to traces; no checkpointing; no interrupt |
| CrewAI / AutoGen style agent frameworks | role-play conversation as the control flow; we need explicit, testable edges and deterministic verification between agent steps |
| Temporal / Prefect | durable and mature, but a second runtime to deploy and a workflow DSL on top; LangGraph's checkpointer gives the durability we need at node granularity (ADR-0006) |
| LangChain `create_agent` prebuilt | no hook for withholding tools on the last step, recovering malformed tool calls, or emitting our run events |

## Consequences
* (+) Each model call, tool call, verification verdict and repair is a span; a "no verified answer" is traceable to the claim that failed.
* (+) Checkpointing, `interrupt` and resume come from the runtime, not from code we maintain.
* (+) Graphs are small, individually testable with a scripted model (`tests/test_react.py`, `tests/test_checked.py`).
* (−) The team must understand LangGraph semantics, e.g. an `interrupt` node re-runs from its first line on resume, so code before it must be
  idempotent (it is: it only reads the database), and a validation error inside the node must re-interrupt rather than raise (see ADR-0006).
* (−) Tight coupling to LangGraph's API, which moves quickly; versions are pinned in `uv.lock` and integration tests run on every change.
