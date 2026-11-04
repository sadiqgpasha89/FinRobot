# ADR-0004: Postgres + pgvector holds facts, chunks, runs and graph checkpoints

* Status: Accepted · Date: 2026-11-05

## Context
The system needs relational data (XBRL facts, prices, consensus), vector + full-text retrieval over filing chunks, run records with event logs, and
(0.2.0) durable LangGraph checkpoints. Data volume is small (a company is a few thousand fact rows and a few hundred chunks).

## Decision
One Postgres with the pgvector extension for all of it:
* hybrid retrieval in a single SQL statement (HNSW cosine rank + GIN full-text rank, reciprocal-rank fusion);
* run events as **rows** (`run_event`) rather than appending to one growing `jsonb` value, which was rewritten on every event;
* LangGraph checkpoints via `PostgresSaver` on the same connection pool;
* a process-wide **connection pool** (`psycopg_pool`) with health checks; multi-statement writes (`replace_facts`, `insert_filing`) are **transactions**;
* versioned **migrations** (`schema_migrations`, advisory-locked) applied on every service start.

## Alternatives considered
| Option | Why not |
|---|---|
| Dedicated vector DB (Qdrant, Pinecone) | another system to run for a few hundred chunks per company; loses transactional consistency with filings |
| SQLite | no pgvector, weak concurrent writers (API + worker + ingest) |
| Alembic | heavier than needed for a handful of idempotent migrations; revisit if the schema grows |

## Consequences
* (+) One backup, one failure domain, atomic ingest: a failed embedding can no longer leave a filing row without chunks (which previously made the filing
  skipped forever).
* (+) Integration tests run the real thing; CI uses the `pgvector/pgvector` image.
* (−) Embedding dimension is part of the schema (`vector(768)`); changing the embedding model means a migration and re-ingest. The gateway refuses a
  mismatched dimension with an actionable error.
* (−) Postgres is a hard dependency; the API waits for it at start and `/ready` reports it.
