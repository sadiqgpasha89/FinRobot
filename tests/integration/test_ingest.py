"""The ingest pipeline (EDGAR -> XBRL facts + parsed, chunked, embedded filings -> Postgres) against a mock SEC and a real database."""
import time

import httpx
import pytest

from fakes import FakeEmbeddings, make_gateway
from finrobot.config import settings
from finrobot.data import store
from finrobot.data.ingest import ingest
from finrobot.data.providers.edgar import EdgarClient
from finrobot.llm.gateway import LLMError
from mock_sec import FakeSEC


@pytest.fixture
def pipeline(clean, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", clean / "cache")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake = FakeSEC()
    return EdgarClient("FinRobot tests@example.com", transport=httpx.MockTransport(fake)), make_gateway()[0], fake


def test_ingest_loads_facts_and_filings_and_is_idempotent(pipeline):
    edgar, gw, fake = pipeline
    logs = []
    out = ingest("Testco", edgar=edgar, llm=gw, log=logs.append)
    assert out["ticker"] == "TST" and out["filings_added"] == 2 and out["facts"] > 0
    assert store.latest_quarter("TST") == (2026, 3)
    assert {f["form_type"] for f in store.list_filings("TST")} == {"10-Q", "8-K"}
    sections = {h["section"] for h in store.search_chunks("TST", "revenue pricing volume", None, k=10)}
    assert {"MD&A", "Earnings Release"} <= sections
    assert any("2 filings added, 0 already present" in m for m in logs)

    again = ingest("TST", edgar=edgar, llm=gw, log=logs.append)
    assert again["filings_added"] == 0 and len(store.list_filings("TST")) == 2          # nothing duplicated


def test_a_failed_embedding_leaves_nothing_half_ingested_and_the_next_run_recovers(pipeline):
    """Regression: the filing row was written before its chunks, so one failure meant that filing was skipped forever."""
    edgar, gw, _ = pipeline

    class Flaky(FakeEmbeddings):
        def embed_documents(self, texts):
            raise ConnectionError("ollama restarted")
    broken = make_gateway()[0]
    broken._embeddings = Flaky()
    with pytest.raises((ConnectionError, LLMError)):
        ingest("TST", edgar=edgar, llm=broken, log=lambda m: None)
    assert store.list_filings("TST") == []                                              # no orphan filing rows

    out = ingest("TST", edgar=edgar, llm=gw, log=lambda m: None)
    assert out["filings_added"] == 2 and all(f["chunks"] > 0 for f in store.list_filings("TST"))


def test_facts_only_ingest_skips_filings_and_embeddings(pipeline):
    edgar, gw, _ = pipeline
    out = ingest("TST", 0, 0, 0, embed=False, edgar=edgar, llm=gw, log=lambda m: None)
    assert out["filings_added"] == 0 and store.list_filings("TST") == [] and store.latest_quarter("TST") == (2026, 3)
    assert gw.embeddings().calls == []
