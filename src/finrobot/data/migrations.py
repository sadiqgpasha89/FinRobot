"""Versioned schema migrations. Every service calls `migrate()` on startup; an advisory lock makes concurrent starts safe.

Add a new (version, name, sql) at the END of MIGRATIONS and never edit an applied one.
"""
from __future__ import annotations

import psycopg

from finrobot.config import settings
from finrobot.log import get_logger

log = get_logger("migrations")
_LOCK_KEY = 726_001  # arbitrary, shared by all FinRobot processes

V1_BASELINE = f"""
CREATE TABLE IF NOT EXISTS company(
  ticker text PRIMARY KEY, cik bigint NOT NULL, name text, sector text, fiscal_year_end text);
CREATE TABLE IF NOT EXISTS filing(
  id serial PRIMARY KEY, ticker text NOT NULL REFERENCES company(ticker), form_type text NOT NULL,
  accession text UNIQUE NOT NULL, filed date, period date, url text, object_key text);
CREATE TABLE IF NOT EXISTS filing_chunk(
  id serial PRIMARY KEY, filing_id int NOT NULL REFERENCES filing(id) ON DELETE CASCADE,
  idx int NOT NULL, section text, text text NOT NULL, embedding vector({settings.embed_dim}),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED);
CREATE INDEX IF NOT EXISTS chunk_tsv ON filing_chunk USING gin(tsv);
CREATE INDEX IF NOT EXISTS chunk_filing ON filing_chunk(filing_id);
CREATE TABLE IF NOT EXISTS financial_fact(
  ticker text NOT NULL, metric text NOT NULL, period_end date NOT NULL, fiscal_year int, fiscal_q int NOT NULL,
  value double precision NOT NULL, unit text, tag text, form text, filed date, accession text, derived boolean,
  source_id text, PRIMARY KEY(ticker, metric, period_end, fiscal_q));
CREATE TABLE IF NOT EXISTS consensus(
  ticker text NOT NULL, metric text NOT NULL, fiscal_year int NOT NULL, fiscal_q int NOT NULL,
  mean double precision NOT NULL, high double precision, low double precision, n_analysts int,
  as_of date, source text, PRIMARY KEY(ticker, metric, fiscal_year, fiscal_q));
CREATE TABLE IF NOT EXISTS price_daily(
  ticker text NOT NULL, day date NOT NULL, close double precision NOT NULL, source text,
  PRIMARY KEY(ticker, day));
CREATE TABLE IF NOT EXISTS run(
  id text PRIMARY KEY, mode text NOT NULL, request jsonb, status text NOT NULL, result jsonb,
  events jsonb DEFAULT '[]'::jsonb, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());
"""

V2_INDEXES = """
CREATE INDEX IF NOT EXISTS filing_ticker_filed ON filing(ticker, filed DESC);
CREATE INDEX IF NOT EXISTS chunk_embedding ON filing_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS run_status ON run(status, created_at DESC);
"""

# Events moved out of one ever-growing jsonb value (rewritten on every append) into rows; the old column is left in place.
V3_RUN_EVENTS = """
ALTER TABLE run ADD COLUMN IF NOT EXISTS trace_id text;
ALTER TABLE run ADD COLUMN IF NOT EXISTS usage jsonb;
ALTER TABLE run ADD COLUMN IF NOT EXISTS error text;
CREATE TABLE IF NOT EXISTS run_event(
  id bigserial PRIMARY KEY, run_id text NOT NULL REFERENCES run(id) ON DELETE CASCADE, event jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS run_event_run ON run_event(run_id, id);
"""

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "baseline schema", V1_BASELINE),
    (2, "search and run indexes", V2_INDEXES),
    (3, "run events table, trace id, usage", V3_RUN_EVENTS),
]


def migrate(database_url: str | None = None) -> list[int]:
    """Apply pending migrations; returns the versions applied by this call."""
    url = database_url or settings.database_url
    applied: list[int] = []
    # the extension must exist before any connection tries to register the vector type
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version int PRIMARY KEY, name text NOT NULL, "
                      "applied_at timestamptz NOT NULL DEFAULT now())")
            done = {r[0] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
            for version, name, sql in MIGRATIONS:
                if version in done:
                    continue
                with c.transaction():
                    c.execute(sql)
                    c.execute("INSERT INTO schema_migrations(version, name) VALUES (%s, %s)", (version, name))
                applied.append(version)
                log.info("applied migration %s: %s", version, name)
        finally:
            c.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied
