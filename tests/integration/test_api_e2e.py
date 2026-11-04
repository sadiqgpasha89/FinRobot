"""The HTTP API through its real lifespan (migrations, checkpointer, job pool) against a real database."""
import time

import pytest
from fastapi.testclient import TestClient
from seed import TICKER, seed, seed_filings
from test_qa_e2e import QUESTION, scripted
from test_report_e2e import gateway as report_gateway

from fakes import make_gateway
from finrobot.api.app import create_app
from finrobot.config import settings
from finrobot.data import store
from finrobot.graph.checkpoint import reset_checkpointer


@pytest.fixture
def client(clean, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", "")
    reset_checkpointer()
    seed()
    seed_filings()
    with TestClient(create_app()) as c:
        yield c


def wait(client, run_id, until=("done", "failed", "awaiting_review", "interrupted"), timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] in until:
            return run
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} still {run['status']}")


def test_ask_runs_in_the_background_and_is_fully_recorded(client):
    gw, _ = scripted()
    client.app.state.llm = gw
    r = client.post("/ask", json={"question": QUESTION, "ticker": TICKER})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    run = wait(client, run_id)
    assert run["status"] == "done" and run["result"]["eval"]["ok"] and "factbook" not in run["result"]
    assert run["usage"]["calls"] == 7 and run["error"] is None
    assert client.get(f"/runs/{run_id}?full=true").json()["result"]["factbook"]["facts"]

    cursor = run["events"][3]["_id"]
    later = client.get(f"/runs/{run_id}", params={"after": cursor}).json()["events"]
    assert later and all(e["_id"] > cursor for e in later) and len(later) == len(run["events"]) - 4    # clients poll incrementally

    assert any(x["id"] == run_id and x["status"] == "done" for x in client.get("/runs").json())
    import re
    counted = re.search(r'finrobot_runs_total\{mode="qa",status="done"\} (\d+)', client.get("/metrics").text)
    assert counted and int(float(counted.group(1))) >= 1                                 # process-global counter: other tests may add to it
    assert client.get(f"/runs/{run_id}/artifacts/answer.md").status_code == 200


def test_report_with_review_pause_edit_and_resume(client):
    gw, _ = report_gateway()
    client.app.state.llm = gw
    run_id = client.post("/reports", json={"ticker": TICKER, "review": True}).json()["run_id"]
    paused = wait(client, run_id)
    assert paused["status"] == "awaiting_review" and paused["result"]["review"]["assumptions"]["wacc"] == 0.08

    bad = client.post(f"/runs/{run_id}/resume", json={"assumptions": {"wacc": 0.01}})
    assert bad.status_code == 202
    again = wait(client, run_id)
    assert again["status"] == "awaiting_review" and "wacc must be greater" in again["result"]["review"]["error"]

    assert client.post(f"/runs/{run_id}/resume", json={"assumptions": {"nonsense": 1}}).status_code == 422
    assert client.post(f"/runs/{run_id}/resume", json={"assumptions": {"wacc": 0.095}}).status_code == 202
    done = wait(client, run_id)
    assert done["status"] == "done" and done["result"]["assumptions"]["wacc"] == 0.095 and done["result"]["format"]["ok"]
    assert done["usage"]["calls"] > 8

    pdf = client.get(f"/runs/{run_id}/artifacts/{done['result']['pdf_name']}")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["content-type"] == "application/pdf"
    html = client.get(f"/runs/{run_id}/artifacts/report.html")
    assert "default-src 'none'" in html.headers["content-security-policy"] and html.headers["x-content-type-options"] == "nosniff"

    assert client.post(f"/runs/{run_id}/resume", json={}).status_code == 409                # only a run awaiting review can be resumed
    assert client.post(f"/runs/{run_id}/retry").status_code == 409                          # ... and only an interrupted/failed one retried


def test_a_failed_report_can_be_retried_from_its_checkpoint(client):
    crashing, _ = report_gateway(fail_on_call=2)
    client.app.state.llm = crashing
    run_id = client.post("/reports", json={"ticker": TICKER}).json()["run_id"]
    failed = wait(client, run_id)
    assert failed["status"] == "failed" and "crashed" in failed["error"]

    healthy, models = report_gateway()
    client.app.state.llm = healthy
    assert client.post(f"/runs/{run_id}/retry").status_code == 202
    done = wait(client, run_id)
    assert done["status"] == "done" and len(models["narrative"].seen) == 7           # the first section was kept: 8 calls - 1


def test_unknown_companies_and_bad_state_are_clean_errors(client):
    assert client.post("/reports", json={"ticker": "NOPE"}).status_code == 404
    assert client.get("/reports/draft-assumptions", params={"ticker": "NOPE"}).status_code == 404
    assert client.get("/reports/draft-assumptions", params={"ticker": TICKER}).json()["assumptions"]["wacc"] == 0.08
    assert client.post("/prices", json={"ticker": "NOPE", "price": 10}).status_code == 404
    assert client.post("/prices", json={"ticker": TICKER, "price": 123.45}).status_code == 200
    assert store.latest_price(TICKER)["close"] == 123.45
    assert client.get("/companies").json()[0]["price"] == 123.45


def test_a_full_queue_answers_429_and_records_the_rejected_run(client, monkeypatch):
    monkeypatch.setattr(client.app.state.runner, "submit", lambda *a, **k: False)
    r = client.post("/ask", json={"question": QUESTION, "ticker": TICKER})
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    rejected = client.get("/runs").json()[0]
    assert rejected["status"] == "failed" and rejected["error"] == "queue full"


def test_readiness_reflects_the_database_and_the_models(client):
    gw, _ = make_gateway()
    client.app.state.llm = gw
    gw.health = lambda: {"ok": False, "figures": "x: NOT INSTALLED"}
    r = client.get("/ready")
    assert r.status_code == 503 and r.json()["postgres"] is True and r.json()["models"]["ok"] is False
    gw.health = lambda: {"ok": True}
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["object_store"] in ("s3", "local-fallback")


def test_runs_in_flight_when_the_service_restarts_are_marked_interrupted(clean, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", "")
    store.init_db()
    store.create_run("20260101-000000-orph1", "report", {"ticker": "TST"})
    with TestClient(create_app()) as c:                         # starting the service reconciles the run table
        run = c.get("/runs/20260101-000000-orph1").json()
        assert run["status"] == "interrupted" and "restarted" in run["error"]
        assert c.post("/runs/20260101-000000-orph1/retry").status_code == 202            # resumable from its checkpoint (there is none here)
        assert wait(c, "20260101-000000-orph1")["status"] == "failed"
