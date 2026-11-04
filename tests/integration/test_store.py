import threading
from datetime import date

import psycopg
import pytest
from seed import TICKER, seed, seed_filings

from fakes import FakeEmbeddings
from finrobot.data import store
from finrobot.data.migrations import MIGRATIONS, migrate
from finrobot.data.xbrl import FactRow


def test_migrations_apply_once_and_are_recorded(db):
    assert migrate() == []                                            # fixture already applied everything
    with store.conn() as c:
        versions = [r["version"] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        idx = {r["indexname"] for r in c.execute("SELECT indexname FROM pg_indexes WHERE tablename IN ('filing_chunk','run')").fetchall()}
    assert versions == [m[0] for m in MIGRATIONS]
    assert {"chunk_embedding", "chunk_tsv", "run_status"} <= idx      # HNSW vector index, full-text index


def test_concurrent_migrators_serialise_on_the_advisory_lock(db):
    errors = []

    def go():
        try:
            migrate()
        except Exception as e:   # pragma: no cover
            errors.append(e)
    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []


def test_find_company_never_matches_everything(clean):
    seed()
    assert store.find_company("TST")["ticker"] == TICKER and store.find_company("tst")["ticker"] == TICKER
    assert store.find_company("Testco")["ticker"] == TICKER
    for junk in ("", " ", "%", "_", "T", "%%"):         # '' / '%' used to match every row and silently pick an arbitrary company
        assert store.find_company(junk) is None, junk


def test_replace_facts_is_all_or_nothing(clean):
    seed()
    before = len(store.get_facts(TICKER, "revenue", limit=50))
    assert before == 15
    bad = FactRow("revenue", date(2030, 3, 31), 2030, 1, None, "USD", "t", "10-Q", date(2030, 4, 30), "a", False)   # NOT NULL violation
    with pytest.raises(psycopg.errors.NotNullViolation):
        store.replace_facts(TICKER, [bad])
    assert len(store.get_facts(TICKER, "revenue", limit=50)) == before        # the old facts survived the failed refresh


def test_a_failed_chunk_insert_leaves_no_filing_row_behind(clean):
    seed()
    with pytest.raises(Exception):
        store.insert_filing(TICKER, "10-Q", "acc-bad", "2026-10-30", "2026-09-30", "u", "k", [(0, "MD&A", "text", [0.1, 0.2])])   # wrong dimension
    assert not store.filing_exists("acc-bad")      # otherwise ingest would skip this filing forever
    seed_filings()
    assert store.filing_exists("0000000-26-000000")


def test_hybrid_search_ranks_the_relevant_passage_and_honours_filters(clean):
    seed()
    emb = FakeEmbeddings()
    seed_filings(emb)
    hits = store.search_chunks(TICKER, "commodity prices recycled materials", emb.embed_query("search_query: commodity prices recycled materials"))
    assert hits and "recycled" in hits[0]["text"]
    only_10k = store.search_chunks(TICKER, "commodity prices", None, forms=["10-K"])
    assert only_10k and {h["form_type"] for h in only_10k} == {"10-K"}
    window = store.search_chunks(TICKER, "revenue pricing volume", None, filed_between=(date(2026, 10, 1), date(2026, 10, 29)))
    assert [h["form_type"] for h in window] == ["8-K"]
    assert store.search_chunks(TICKER, "", None) == []


def test_run_lifecycle_events_and_incremental_polling(clean):
    store.create_run("r1", "qa", {"question": "q"}, trace_id="t" * 32)
    for i in range(5):
        store.add_event("r1", {"type": "log", "msg": f"m{i}"})
    run = store.get_run("r1")
    assert run["status"] == "running" and [e["msg"] for e in run["events"]] == [f"m{i}" for i in range(5)] and run["trace_id"] == "t" * 32
    cursor = run["events"][2]["_id"]
    assert [e["msg"] for e in store.get_run("r1", cursor)["events"]] == ["m3", "m4"]
    store.finish_run("r1", "done", {"answer_md": "x"}, {"calls": 3}, None)
    done = store.get_run("r1")
    assert done["status"] == "done" and done["usage"] == {"calls": 3} and done["result"]["answer_md"] == "x"
    assert store.get_run("nope") is None


def test_runs_left_running_by_a_restart_are_marked_interrupted(clean):
    store.create_run("a", "report", {})
    store.create_run("b", "qa", {})
    store.finish_run("b", "done", {})
    assert store.mark_orphaned_runs() == ["a"]
    assert store.get_run("a")["status"] == "interrupted" and "restarted" in store.get_run("a")["error"] and store.get_run("b")["status"] == "done"


def test_the_pool_serves_many_threads_with_few_connections(clean):
    seed()
    results = []

    def go():
        for _ in range(10):
            results.append(store.latest_quarter(TICKER))
    threads = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [(2026, 3)] * 200
    with store.conn() as c:
        assert c.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()").fetchone()["n"] <= 12
    assert store.ping()
