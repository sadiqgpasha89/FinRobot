# Architecture

FinRobot answers analyst questions and writes equity research reports whose every figure can be traced to a filing line or a calculation.
This page describes the system as built in 0.2.0. The reasons behind the choices are in the [ADRs](adr/); the review of the whole design is
in the [System Design Review](sdr.md).

## 1. Design principle

> **The model reads and writes. Code does arithmetic, chooses periods, and checks every number.**

Everything below exists to make that sentence enforceable: a number can only reach a reader if it was *registered* (given an id by code),
*cited* by the model, and *verified* by a deterministic check. What cannot be verified is removed, not softened.

## 2. System context

```mermaid
flowchart LR
    user([Analyst]) --> ui[Streamlit UI]
    user --> cli[finrobot CLI]
    ui --> api
    cli --> core
    subgraph FinRobot
        api[FastAPI service] --> core[Run service<br/>LangGraph graphs]
        core --> pg[(Postgres + pgvector<br/>facts, chunks, runs, checkpoints)]
        core --> obj[(S3-compatible store<br/>raw filings, PDFs)]
    end
    core --> llm{{LLM gateway<br/>LangChain}}
    llm --> ollama[Ollama<br/>gpt-oss, gemma, nomic-embed]
    llm -.optional.-> hosted[Hosted models<br/>Anthropic, OpenAI, ...]
    core --> sec[SEC EDGAR<br/>XBRL, 10-K, 10-Q, 8-K]
    core -.traces + scores.-> lf[Langfuse]
    core -.traces + feedback.-> ls[LangSmith]
```

## 3. Runtime components

| Component | Responsibility | Notes |
|---|---|---|
| `api/app.py` | HTTP surface, auth, validation, job queue, metrics | stateless; jobs return a run id at once |
| `service.py` | one place that executes a run: tracing, usage, scores, persistence, final status | shared by API and CLI |
| `graph/qa_graph.py` | question answering as a LangGraph | `Runtime` context carries the LLM gateway, event sink and `RunContext` |
| `graph/report_graph.py` | report generation as a checkpointed LangGraph | human review via `interrupt`, resume after a crash |
| `agents/react.py` | tool-calling loop as a subgraph | withholds tools on the last step, recovers malformed tool calls |
| `agents/checked.py` | generate → verify → repair as a subgraph | the guardrail loop |
| `llm/gateway.py` | the only place that talks to models | per-role model choice, retries, concurrency lane, structured output |
| `data/*` | EDGAR client, XBRL normaliser, parser/chunker, store, migrations, objects | Postgres access is pooled and transactional |
| `observability.py` | Langfuse + LangSmith wiring, scores, token usage | no-op without keys |

## 4. The graphs

### 4.1 Question answering

```mermaid
flowchart TD
    S([start]) --> route
    route -->|ticker, fiscal period parsed in code| data[Data-CoT<br/>figures, comparisons]
    data -->|figure question, or no verified figure| merge
    data -->|asks why / outlook| concept[Concept-CoT<br/>causes from retrieved passages]
    concept --> thesis[Thesis-CoT<br/>story, what to watch<br/>+ grounding check]
    thesis --> merge
    merge[merge + sources<br/>+ final evaluation] --> E([end])
```

Each of the three agents is the same two-level subgraph:

```mermaid
flowchart LR
    subgraph checked [checked agent]
        direction LR
        draft --> verify
        verify -->|ok| finish
        verify -->|issues, first attempt| retry --> draft
        verify -->|issues after retry| sanitize --> finish
    end
    subgraph react [draft = tool loop]
        direction LR
        model -->|tool calls| tools --> model
        model -->|answer| done([text])
    end
    draft -.runs.-> react
```

* `verify` is the deterministic Evaluator: every unit-bearing number must equal a registered fact (within rounding) or appear in a cited
  passage; the periods a sentence names must be covered by the facts it cites; placeholder citations fail.
* `retry` puts the exact issues back into the prompt, once. `sanitize` deletes the lines that still fail. A draft about the wrong quarter
  becomes "no verified answer".
* The tool loop is a graph so each model call and tool call is its own span in Langfuse / LangSmith.

### 4.2 Report

```mermaid
flowchart TD
    S([start]) --> draft[draft assumptions<br/>from trailing data]
    draft -->|review = true| hold{{interrupt:<br/>human edits}}
    hold -->|valid edits| model
    hold -->|invalid: pause again with the reason| hold
    draft -->|review = false| model[model: tables, charts, DCF]
    model --> write[write ONE section]
    write -->|more sections| write
    write -->|all 8 written| ground[grounding check]
    ground --> render[render HTML → PDF<br/>+ format check]
    render --> E([end])
```

State is small and serialisable (assumptions, prose, a FactBook snapshot). Heavy objects (tables, charts, the `RunContext`) live in the run's
`Runtime` context and are **rebuilt deterministically from the database** when a run resumes in a new process. Because the FactBook
deduplicates on `(label, value, unit, source)`, rebuilt facts get the ids the prose already cites. After every section a checkpoint is written
to Postgres; a crash costs at most one section of model calls.

## 5. The FactBook contract

```mermaid
flowchart LR
    db[(financial_fact)] -->|Metrics / get_financials| fb[FactBook<br/>F1 F2 ... S1 S2 ...]
    chunks[(filing_chunk)] -->|search_docs| fb
    fb -->|ids + display strings| agent[LLM agent]
    agent -->|text citing F# / S#| ev{Evaluator}
    fb --> ev
    ev -->|numbers tie out| out[answer / report]
    ev -->|unsupported or uncited| fix[retry, then remove]
```

| Id | Meaning | Created by |
|---|---|---|
| `F#` | a number: a reported XBRL fact, a derived metric (with its input ids), a calculation (expression kept), a price, a consensus value | `Metrics`, `calc`, report model |
| `S#` | a retrieved passage from a filing or earnings release | `search_docs` |

A fact carries a unit (`USD`, `%`, `pp`, `bps`, `x`, ...) and a **period basis** (`instant`, `q`, `ttm`, `fy`). `calc` infers the result's unit
and refuses combinations that silently mislead (a balance-sheet figure over one quarter's flow).

## 6. LLM gateway

```mermaid
flowchart LR
    agents[agents, router,<br/>grounding, writers] -->|role: figures / narrative| gw[LLMGateway]
    gw -->|lane + retry + metadata| cm[LangChain chat model<br/>ChatOllama or init_chat_model]
    gw -->|embed documents / query| em[LangChain embeddings]
    gw -->|structured output<br/>json_schema or tool calling| cm
    cm --> prov[Ollama / Anthropic / OpenAI / ...]
```

* Roles map to models by `[provider:]model` strings (`gpt-oss:20b`, `anthropic:claude-sonnet-5-5`). Agents never import a provider SDK.
* One concurrency lane (`FINROBOT_LLM_CONCURRENCY`, default 1 for a single GPU slot); transient network errors retry with jittered backoff.
* Every call carries `role` and `model` metadata, which Langfuse and LangSmith show on the span and the usage tracker aggregates.
* Embedding dimensions are checked against the pgvector column before anything is written.

## 7. Data model

```mermaid
erDiagram
    company ||--o{ filing : has
    filing ||--o{ filing_chunk : "split into"
    company ||--o{ financial_fact : reports
    company ||--o{ consensus : "has (manual)"
    company ||--o{ price_daily : "has"
    run ||--o{ run_event : emits
    run {
        text id PK
        text mode "qa | report | ingest"
        text status "running | awaiting_review | done | failed | interrupted"
        jsonb request
        jsonb result
        jsonb usage
        text trace_id
        text error
    }
    filing_chunk {
        int id PK
        text section
        text text
        vector embedding "HNSW cosine index"
        tsvector tsv "GIN full-text index"
    }
    financial_fact {
        text metric PK
        date period_end PK
        int fiscal_q PK "1-4 quarter, 0 = fiscal year"
        float value
        bool derived
        text source_id "XBRL tag, period, accession"
    }
```

LangGraph's checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) live in the same database, keyed by `thread_id = run id`.
Retrieval is hybrid: pgvector cosine rank and Postgres full-text rank fused with reciprocal-rank fusion.

## 8. Run lifecycle

```mermaid
stateDiagram-v2
    [*] --> running: POST /ask, /reports, /ingest
    running --> done
    running --> failed: exception (error recorded)
    running --> awaiting_review: report with review = true
    awaiting_review --> running: POST /runs/ID/resume
    running --> interrupted: service restarted mid-run
    interrupted --> running: POST /runs/ID/retry (reports resume from the last checkpoint)
    failed --> running: POST /runs/ID/retry (reports)
    done --> [*]
```

## 9. Observability join keys

```mermaid
flowchart LR
    rid[run id<br/>20261105-025723-f4819] --> row[(run row<br/>events, usage, trace_id)]
    rid -->|seed| lfid[Langfuse trace id<br/>32 hex, derived]
    rid -->|uuid5| lsid[LangSmith root run id]
    rid -->|session| lfs[Langfuse session]
    lfid --> scores[scores:<br/>verification_ok, figures_tied_ratio,<br/>lines_removed, agent_retries, format_ok]
    lsid --> fb[feedback: same scores]
```

Given a run id you can open its trace in either tool, and given a trace you can find the run row, its events and its artifacts. Details in
[observability.md](observability.md).

## 10. Failure behaviour

| Failure | Behaviour |
|---|---|
| Model returns an unverifiable figure | one retry with the issues, then the line is removed; a wrong-period answer becomes "no verified answer" |
| Provider emits a malformed tool call | nudged to retry inside the tool loop |
| Transient network error to the model | retried with jittered backoff (`FINROBOT_LLM_MAX_RETRIES`) |
| Ollama / hosted model down | run fails with the error recorded; `/ready` returns 503 |
| Postgres down at start | the API waits (up to 90 s) instead of crashing; `/ready` reports it afterwards |
| Object store down | local file fallback; reconnects after a 30 s cool-down; `/ready` shows `local-fallback` |
| Service restarts mid-run | run marked `interrupted`; reports resume from the last completed section |
| Invalid reviewer edit | the run pauses again with the reason; nothing is lost |
| Langfuse / LangSmith unreachable | tracing is best effort; runs are unaffected |
| Queue full | `429` with `Retry-After`; the rejected run is recorded as failed |
| Ingest interrupted | filing + chunks are one transaction; the next ingest picks up where it stopped |
