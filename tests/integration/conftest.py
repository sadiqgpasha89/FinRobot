"""Integration tests run against a real Postgres with pgvector (no mocks for SQL, the pool or the LangGraph checkpointer).

    FINROBOT_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres  uv run pytest -m integration

Each session creates its own throwaway database and drops it afterwards, so the URL may point at any server you can CREATE DATABASE on.
"""
from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from finrobot.config import settings
from finrobot.data import objects, store


def pytest_collection_modifyitems(items):
    for item in items:
        if "/integration/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def database_url():
    base = os.environ.get("FINROBOT_TEST_DATABASE_URL")
    if not base:
        pytest.skip("set FINROBOT_TEST_DATABASE_URL to a Postgres server with the pgvector extension")
    name = f"finrobot_it_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(base, dbname=name)
    store.close_pool()
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def db(database_url):
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "database_url", database_url)
    store.close_pool()
    store.init_db()
    yield database_url
    store.close_pool()
    mp.undo()


@pytest.fixture
def clean(db, tmp_path, monkeypatch):
    """Empty tables, a temp output dir, and no real object store."""
    with store.conn() as c:
        c.execute("TRUNCATE company, filing, filing_chunk, financial_fact, consensus, price_daily, run, run_event RESTART IDENTITY CASCADE")
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    monkeypatch.setattr(objects, "_s3", lambda: None)
    return tmp_path
