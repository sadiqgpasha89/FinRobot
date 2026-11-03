"""PostgreSQL (+pgvector) storage for filings, chunks, financial facts, prices, consensus and runs."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date
from typing import Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from finrobot.config import settings
from finrobot.data.xbrl import FactRow

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
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


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    c = psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=True)
    try:
        register_vector(c)
        yield c
    finally:
        c.close()


def init_db() -> None:
    # the extension must exist before register_vector can look up its type
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with conn() as c:
        c.execute(SCHEMA)


# --- companies / filings / chunks -----------------------------------------------------------
def upsert_company(ticker: str, cik: int, name: str, sector: str = "", fye: str = "") -> None:
    with conn() as c:
        c.execute("""INSERT INTO company(ticker,cik,name,sector,fiscal_year_end) VALUES(%s,%s,%s,%s,%s)
                     ON CONFLICT(ticker) DO UPDATE SET cik=EXCLUDED.cik,name=EXCLUDED.name,sector=EXCLUDED.sector,
                     fiscal_year_end=EXCLUDED.fiscal_year_end""", (ticker, cik, name, sector, fye))


def get_company(ticker: str) -> dict | None:
    with conn() as c:
        return c.execute("SELECT * FROM company WHERE ticker=%s", (ticker,)).fetchone()


def find_company(text: str) -> dict | None:
    """Match an ingested company by ticker, or by (partial) name."""
    t = text.strip()
    with conn() as c:
        r = c.execute("SELECT * FROM company WHERE upper(ticker)=upper(%s)", (t,)).fetchone()
        if r is None:
            r = c.execute("SELECT * FROM company WHERE name ILIKE %s ORDER BY length(name) LIMIT 1", (f"%{t}%",)).fetchone()
        return r


def list_companies() -> list[dict]:
    with conn() as c:
        return c.execute("SELECT * FROM company ORDER BY ticker").fetchall()


def filing_exists(accession: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM filing WHERE accession=%s", (accession,)).fetchone() is not None


def insert_filing(ticker: str, form: str, accession: str, filed: str, period: str, url: str, object_key: str,
                  chunks: list[tuple[int, str, str, list[float] | None]]) -> int:
    """chunks: (idx, section, text, embedding)."""
    with conn() as c:
        fid = c.execute("""INSERT INTO filing(ticker,form_type,accession,filed,period,url,object_key)
                           VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (ticker, form, accession, filed or None, period or None, url, object_key)).fetchone()["id"]
        with c.cursor() as cur:
            cur.executemany("INSERT INTO filing_chunk(filing_id,idx,section,text,embedding) VALUES(%s,%s,%s,%s,%s)",
                            [(fid, i, s, t, e) for i, s, t, e in chunks])
        return fid


def list_filings(ticker: str) -> list[dict]:
    with conn() as c:
        return c.execute("""SELECT f.*, (SELECT count(*) FROM filing_chunk WHERE filing_id=f.id) AS chunks
                            FROM filing f WHERE ticker=%s ORDER BY filed DESC""", (ticker,)).fetchall()


def _tsquery(text: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9&-]{2,}", text.lower())]
    return " | ".join(dict.fromkeys(words)) if words else ""


def search_chunks(ticker: str, query_text: str, query_emb: list[float] | None, forms: list[str] | None = None,
                  sections: list[str] | None = None, k: int = 6, filed_between: tuple[date, date] | None = None) -> list[dict]:
    """Hybrid retrieval: pgvector cosine rank + full-text rank, fused with reciprocal-rank fusion."""
    where = "f.ticker = %(t)s"
    params: dict = {"t": ticker, "k": k}
    if forms:
        where += " AND f.form_type = ANY(%(forms)s)"
        params["forms"] = forms
    if sections:
        where += " AND c.section = ANY(%(secs)s)"
        params["secs"] = sections
    if filed_between:
        where += " AND f.filed BETWEEN %(f0)s AND %(f1)s"
        params["f0"], params["f1"] = filed_between
    ts = _tsquery(query_text)
    parts, joins, score = [], "", []
    if query_emb is not None:
        parts.append(f"""v AS (SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> %(q)s::vector) rk
                           FROM filing_chunk c JOIN filing f ON f.id=c.filing_id WHERE {where}
                           ORDER BY c.embedding <=> %(q)s::vector LIMIT 40)""")
        joins += " LEFT JOIN v ON v.id=c.id"
        score.append("COALESCE(1.0/(60+v.rk),0)")
        params["q"] = query_emb
    if ts:
        parts.append(f"""kw AS (SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', %(ts)s)) DESC) rk
                            FROM filing_chunk c JOIN filing f ON f.id=c.filing_id
                            WHERE {where} AND c.tsv @@ to_tsquery('english', %(ts)s)
                            ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', %(ts)s)) DESC LIMIT 40)""")
        joins += " LEFT JOIN kw ON kw.id=c.id"
        score.append("COALESCE(1.0/(60+kw.rk),0)")
        params["ts"] = ts
    if not parts:
        return []
    ids = [f"{p}.id IS NOT NULL" for p in (["v"] if query_emb is not None else []) + (["kw"] if ts else [])]
    sql = f"""WITH {', '.join(parts)}
              SELECT c.id, c.section, c.idx, c.text, f.form_type, f.accession, f.filed, f.period, {' + '.join(score)} AS score
              FROM filing_chunk c JOIN filing f ON f.id=c.filing_id {joins}
              WHERE {' OR '.join(ids)} ORDER BY score DESC, f.filed DESC LIMIT %(k)s"""
    with conn() as c:
        return c.execute(sql, params).fetchall()


# --- facts -----------------------------------------------------------------------------------
def replace_facts(ticker: str, rows: list[FactRow]) -> int:
    with conn() as c:
        c.execute("DELETE FROM financial_fact WHERE ticker=%s", (ticker,))
        with c.cursor() as cur:
            cur.executemany("""INSERT INTO financial_fact(ticker,metric,period_end,fiscal_year,fiscal_q,value,unit,tag,form,filed,
                               accession,derived,source_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT DO NOTHING""",
                            [(ticker, r.metric, r.period_end, r.fiscal_year, r.fiscal_q, r.value, r.unit, r.tag, r.form, r.filed,
                              r.accession, r.derived, r.source_id) for r in rows])
    return len(rows)


def get_facts(ticker: str, metric: str, quarterly: bool = True, limit: int = 12, up_to: date | None = None) -> list[dict]:
    """Newest first. quarterly=True gives discrete quarters (stock items: quarter ends); False gives fiscal years.
    `up_to` ignores periods ending after that date."""
    cond = "fiscal_q BETWEEN 1 AND 4" if quarterly else "fiscal_q = 0"
    cap = up_to or date.max
    with conn() as c:
        rows = c.execute(f"""SELECT * FROM financial_fact WHERE ticker=%s AND metric=%s AND {cond} AND period_end <= %s
                             ORDER BY period_end DESC LIMIT %s""", (ticker, metric, cap, limit)).fetchall()
    if not rows and not quarterly:  # balance-sheet items have no fiscal_q=0 row; use Q4 ends
        with conn() as c:
            rows = c.execute("""SELECT * FROM financial_fact WHERE ticker=%s AND metric=%s AND fiscal_q=4 AND period_end <= %s
                                ORDER BY period_end DESC LIMIT %s""", (ticker, metric, cap, limit)).fetchall()
    return rows


def get_fact(ticker: str, metric: str, fiscal_year: int, fiscal_q: int) -> dict | None:
    with conn() as c:
        q = fiscal_q
        row = c.execute("""SELECT * FROM financial_fact WHERE ticker=%s AND metric=%s AND fiscal_year=%s AND fiscal_q=%s
                           ORDER BY period_end DESC LIMIT 1""", (ticker, metric, fiscal_year, q)).fetchone()
        if row is None and q == 0:  # stock items at fiscal year end
            row = c.execute("""SELECT * FROM financial_fact WHERE ticker=%s AND metric=%s AND fiscal_year=%s AND fiscal_q=4
                               ORDER BY period_end DESC LIMIT 1""", (ticker, metric, fiscal_year)).fetchone()
        return row


def latest_quarter(ticker: str) -> tuple[int, int] | None:
    with conn() as c:
        r = c.execute("""SELECT fiscal_year, fiscal_q FROM financial_fact WHERE ticker=%s AND metric='revenue' AND fiscal_q BETWEEN 1 AND 4
                         ORDER BY period_end DESC LIMIT 1""", (ticker,)).fetchone()
    return (r["fiscal_year"], r["fiscal_q"]) if r else None


# --- prices & consensus ----------------------------------------------------------------------
def upsert_prices(ticker: str, rows: list[tuple[date, float]], source: str) -> int:
    with conn() as c, c.cursor() as cur:
        cur.executemany("""INSERT INTO price_daily(ticker,day,close,source) VALUES(%s,%s,%s,%s)
                           ON CONFLICT(ticker,day) DO UPDATE SET close=EXCLUDED.close, source=EXCLUDED.source""",
                        [(ticker, d, p, source) for d, p in rows])
    return len(rows)


def latest_price(ticker: str) -> dict | None:
    with conn() as c:
        return c.execute("SELECT * FROM price_daily WHERE ticker=%s ORDER BY day DESC LIMIT 1", (ticker,)).fetchone()


def price_history(ticker: str, limit: int = 260) -> list[dict]:
    with conn() as c:
        return list(reversed(c.execute("SELECT day, close FROM price_daily WHERE ticker=%s ORDER BY day DESC LIMIT %s",
                                       (ticker, limit)).fetchall()))


def upsert_consensus(ticker: str, metric: str, fy: int, fq: int, mean: float, high: float | None = None,
                     low: float | None = None, n: int | None = None, as_of: date | None = None, source: str = "manual") -> None:
    with conn() as c:
        c.execute("""INSERT INTO consensus(ticker,metric,fiscal_year,fiscal_q,mean,high,low,n_analysts,as_of,source)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT(ticker,metric,fiscal_year,fiscal_q) DO UPDATE SET mean=EXCLUDED.mean, high=EXCLUDED.high,
                     low=EXCLUDED.low, n_analysts=EXCLUDED.n_analysts, as_of=EXCLUDED.as_of, source=EXCLUDED.source""",
                  (ticker, metric, fy, fq, mean, high, low, n, as_of, source))


def get_consensus(ticker: str, metric: str, fy: int, fq: int) -> dict | None:
    with conn() as c:
        return c.execute("SELECT * FROM consensus WHERE ticker=%s AND metric=%s AND fiscal_year=%s AND fiscal_q=%s",
                         (ticker, metric, fy, fq)).fetchone()


# --- runs ------------------------------------------------------------------------------------
def create_run(run_id: str, mode: str, request: dict) -> None:
    with conn() as c:
        c.execute("INSERT INTO run(id,mode,request,status) VALUES(%s,%s,%s,'running')", (run_id, mode, json.dumps(request)))


def add_event(run_id: str, event: dict) -> None:
    with conn() as c:
        c.execute("UPDATE run SET events = events || %s::jsonb, updated_at=now() WHERE id=%s", (json.dumps([event], default=str), run_id))


def finish_run(run_id: str, status: str, result: dict) -> None:
    with conn() as c:
        c.execute("UPDATE run SET status=%s, result=%s, updated_at=now() WHERE id=%s", (status, json.dumps(result, default=str), run_id))


def get_run(run_id: str) -> dict | None:
    with conn() as c:
        return c.execute("SELECT * FROM run WHERE id=%s", (run_id,)).fetchone()
