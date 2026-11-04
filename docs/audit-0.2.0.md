# 0.2.0 audit: bugs and gaps found, fixed and proven

Method: read the whole 0.1 codebase (≈ 4.7k lines), run its 68 tests, then review every layer for correctness, data integrity, security, reliability, packaging and
observability. Findings from *reading* are marked **R**; findings from *running* (integration tests, real models, a real server) are marked **T**. Every fix has a regression
test; the test names are in the last column.

Severity: **High** = wrong or unsafe output, data loss or an exploitable weakness; **Medium** = breaks in production use or silently degrades; **Low** = hygiene.

## Security

| # | Sev | Finding | Fix | Regression test |
|---|---|---|---|---|
| 1 | High | **R** Model and filing text went through Markdown into HTML that was served *and* rendered to PDF: markup injection (`<script>`, `<img src=...>`) | HTML-escape before Markdown | `test_model_text_cannot_inject_markup_into_the_report` |
| 2 | High | **R** A table cell that merely *started with* `<` bypassed escaping; risk-table cells come straight from model output | always escape; explicit `Raw` marker for trusted cells | `test_table_cells_are_escaped_even_when_they_start_with_a_tag` |
| 3 | High | **R** WeasyPrint would fetch any URL in that HTML: server-side request forgery from the PDF renderer | URL fetcher allows only `data:` | `test_rendering_a_pdf_never_makes_a_network_request` (a local HTTP server proves zero hits) |
| 4 | High | **R** API unauthenticated (documented as "do not expose") | API keys; production refuses to start without them or with defaults/placeholders | `test_everything_but_health_and_ready_requires_a_key`, `test_production_refuses_*` |
| 5 | Medium | **R** `report.html` served as `text/html` from the API origin | CSP `default-src 'none'; sandbox`, `nosniff` | `test_report_with_review_pause_edit_and_resume` (headers) |
| 6 | Medium | **R** Reading an artifact for an unknown run id **created a directory** on disk (`run_dir` mkdir on every call) | reads never create; ids and names matched against strict patterns | `test_run_artifacts_resolve_only_inside_the_run_directory` |
| 7 | Medium | **R** Local object fallback built paths from keys without containment | resolved-path containment check | `test_object_keys_cannot_escape_the_local_store` |
| 8 | Medium | **R** No input validation: arbitrary ticker strings, unbounded peers/questions, unknown assumption keys discovered late | pydantic constraints; assumption keys validated before queueing | `test_bad_input_is_rejected_with_422_before_any_work` |
| 9 | Low | **R** Development credentials as silent defaults; secrets printable in `repr` | `SecretStr`; production gate lists offending settings | `test_secrets_never_appear_in_a_repr` |

## Correctness and data integrity

| # | Sev | Finding | Fix | Regression test |
|---|---|---|---|---|
| 10 | High | **R** `find_company("")` became `ILIKE '%%'` and returned **an arbitrary company**: a router that answered with an empty company silently analysed the wrong firm. `%`/`_` in the text were wildcards too | require ≥ 2 characters, escape wildcards, clear error otherwise | `test_find_company_never_matches_everything`, `test_a_blank_company_is_not_resolved_to_an_arbitrary_one` |
| 11 | High | **R** `insert_filing` wrote the filing row and its chunks in separate autocommits: one failure (e.g. embedding server restarting) left a filing with **no chunks**, and `filing_exists` then made every later ingest skip it forever | one transaction | `test_a_failed_chunk_insert_leaves_no_filing_row_behind`, `test_a_failed_embedding_leaves_nothing_half_ingested_and_the_next_run_recovers` |
| 12 | High | **R** `replace_facts` was `DELETE` then `INSERT` in autocommit: a failed refresh left the company with **no financial facts** | one transaction | `test_replace_facts_is_all_or_nothing` |
| 13 | Medium | **R** `parse_json` on a code fence without a brace sliced from index −1 | rewritten; used only as a fallback after provider-side structured output | `test_parse_json_tolerates_fences_prose_and_garbage` |
| 14 | Medium | **T** Score computation crashed on real QA output: `grounding_removed` is a *list* in question results and a *count* in report stats (found by the first integration test of the service layer) | `_count()` accepts both | `test_scores_summarise_verification_and_cleanup`, `test_the_service_records_the_run_events_usage_and_artifacts` |
| 15 | Medium | **R** Embedding dimension mismatches surfaced as an opaque Postgres error mid-ingest | checked in the gateway with an actionable message | `test_embeddings_are_batched_prefixed_and_dimension_checked` |
| 16 | Low | **R** Unused variable and `zip()` without `strict=` where lengths must match | cleaned | lint |

## Reliability

| # | Sev | Finding | Fix | Regression test |
|---|---|---|---|---|
| 17 | High | **R** A new Postgres connection (plus a vector-type lookup) for **every** query: hundreds per report, no limit under concurrency | process-wide `psycopg_pool` with health checks | `test_the_pool_serves_many_threads_with_few_connections` |
| 18 | High | **R** Runs killed by a restart stayed `running` forever; no way to resume | `mark_orphaned_runs()` at start; report runs resume from checkpoints | `test_runs_left_running_by_a_restart_are_marked_interrupted`, `test_runs_in_flight_when_the_service_restarts_*` |
| 19 | Medium | **R** Run events were appended into one ever-growing `jsonb` value, rewriting it on every event (O(n²)); clients re-downloaded the whole log each poll | `run_event` table + cursor (`?after=`) | `test_run_lifecycle_events_and_incremental_polling` |
| 20 | Medium | **R** API crashed on start if Postgres was not up yet (container start order) | wait up to 90 s; `/ready` afterwards | `test_startup_waits_for_a_database_that_is_not_up_yet` |
| 21 | Medium | **R** Object-store failure was cached **forever** (never reconnected); TLS impossible (`secure=False`); **T** the default client took ~6 s to fail, longer than a readiness probe | cool-down retry, `S3_SECURE`, 2 s connect timeout (0.35 s measured) | `test_object_store_reconnects_after_a_cooldown` |
| 22 | Medium | **R** EDGAR cache written non-atomically (a crash left a truncated file trusted forever); the ticker list never refreshed | temp file + atomic rename; 7-day refresh | `test_a_truncated_cache_file_is_never_left_behind`, `test_the_ticker_list_is_cached_on_disk_and_refreshed_when_stale` |
| 23 | Medium | **R** No retry on transient model errors; one network blip failed a run | jittered retry on transient errors only | `test_transient_errors_are_retried_and_others_are_not` |
| 24 | Medium | **R** Unbounded job queue | bounded; `429` + `Retry-After`; rejected run recorded | `test_the_job_queue_is_bounded_and_drains`, `test_a_full_queue_answers_429_*` |
| 25 | Low | **R** `@app.on_event("startup")` (deprecated); a module-level mutable holder dict carried the run context between graph nodes | lifespan; LangGraph `Runtime` context | all API/graph tests |

## Packaging and deployment

| # | Sev | Finding | Fix | Check |
|---|---|---|---|---|
| 26 | High | **R** Template and output paths were computed from the *source checkout* (`parents[2]`, `ROOT/src/finrobot/templates`): a wheel or container install could not find its templates and would write into site-packages | package-relative templates; output/cache under the working directory unless configured | wheel built, installed into a clean venv and a PDF rendered from `/tmp` |
| 27 | Medium | **R** The Streamlit UI lived outside the package, so it was not in the wheel | moved to `finrobot/ui` | wheel contents checked |
| 28 | Medium | **R** No Dockerfile, no CI; compose used `latest` images, development passwords and an unauthenticated S3 | pinned images (tags verified to exist), required secrets, authenticated S3 entrypoint, multi-stage non-root image, CI with lint/unit/integration/docker | `docker compose config`; CI yaml parses |
| 29 | Medium | **T** `LANGSMITH_TRACING=` left blank (as in `.env.example`) crashed settings parsing | empty variables mean "unset" | `test_tracing_backends_switch_on_only_with_their_keys` |
| 30 | Low | **R** `.env.example` placeholders could reach production | `change-me` is rejected by the production gate | `test_production_refuses_the_placeholders_shipped_in_env_example` |
| 31 | Low | **R** Dependencies unpinned in practice (no dev group, no extras), no lint config | `uv.lock` committed, `ruff`, extras for hosted providers | CI `uv lock --check` |

## Observability gaps (Phase 6 of the original plan, "not built")

| # | Finding | Fix |
|---|---|---|
| 32 | No tracing; hand-written tool loop and retry invisible | LangGraph subgraphs; Langfuse + LangSmith; per-run scores; token/latency accounting |
| 33 | Unstructured logs without correlation ids | JSON logs with `run_id` and `request_id`; Prometheus metrics; `/health` vs `/ready` |
| 34 | Quality only checkable by hand | verification outcomes become scores; `evals/run.py --langsmith` |

## Found while verifying the refactor on real infrastructure (T)

These slipped past fakes and unit tests and were caught by integration tests or live model runs:

| # | Finding | Fix | Regression test |
|---|---|---|---|
| 35 | `OllamaEmbeddings(keep_alive="10m")` rejected by the real constructor (an int of seconds): every ingest would have failed | `keep_alive=600` | `test_the_real_ollama_embeddings_client_is_built_from_settings` |
| 36 | gpt-oss omitted the router's `company` field: a schema without a `required` list lets a model skip fields | all fields required in every structured-output schema | `test_structured_schemas_require_every_field` |
| 37 | A reviewer's invalid edit raised inside the interrupted node; LangGraph keeps the failed resume value for that task, so **every later resume replayed the same bad edit** and the run could never continue | validate in a loop that re-interrupts with the reason | `test_the_reviewer_can_edit_the_assumptions_and_an_invalid_edit_asks_again` (both checkpointers) |
| 38 | Agent timing read 0.0 s: the clock started after the first draft | start before the draft | `test_agent_done_reports_the_wall_time_of_the_whole_agent` |
| 39 | Langfuse root observation and the LangGraph run shared one name, so two different spans were called `finrobot.qa` | root named `finrobot.<mode>.request` | `test_langfuse_receives_one_trace_per_run_*` |
| 40 | WeasyPrint 70 changed its URL-fetcher API; a plain function (the documented older form) raised `AttributeError` inside the renderer | use `URLFetcher(allowed_protocols=...)`, function fallback for older versions | `test_rendering_a_pdf_never_makes_a_network_request` |

## Not changed, on purpose

* The numeric guard rails, evaluator, calc, metrics, XBRL normaliser and valuation logic: they are the product and have regression tests from live failures. They were only *moved* behind the new graph structure.
* The documented ~1-in-8 "no verified answer" rate with gpt-oss:20b: an intended fail-closed behaviour. A live check in 0.2.0 saw 1 in 6 (Data-CoT, synthetic company).

## Open items (not bugs)

Single-replica job execution; shared-secret API keys; no automated dependency scan; manual prices/consensus; the unverified deployment artifacts listed in the
[SDR](sdr.md#verification-status).
