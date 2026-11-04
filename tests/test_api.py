import threading

import pytest
from fastapi.testclient import TestClient

from finrobot.api.app import JobRunner, create_app
from finrobot.config import settings


@pytest.fixture
def client():
    return TestClient(create_app())   # no lifespan: these tests need no database


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", "key-one, key-two")


def test_health_is_open_and_every_response_carries_a_request_id(client, keyed):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.headers["x-request-id"]
    assert client.get("/health", headers={"x-request-id": "abc123"}).headers["x-request-id"] == "abc123"


def test_everything_but_health_and_ready_requires_a_key(client, keyed):
    for method, path in (("get", "/companies"), ("get", "/runs"), ("get", "/metrics"), ("post", "/ask"), ("get", "/runs/x/artifacts/a.pdf")):
        r = getattr(client, method)(path)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer", path
    assert client.get("/companies", headers={"x-api-key": "wrong"}).status_code == 401


def test_a_valid_key_is_accepted_as_header_or_bearer_token(client, keyed):
    assert client.get("/metrics", headers={"x-api-key": "key-two"}).status_code == 200
    r = client.get("/metrics", headers={"authorization": "Bearer key-one"})
    assert r.status_code == 200 and "finrobot_http_requests_total" in r.text


def test_without_configured_keys_the_api_is_open_for_local_development(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", "")
    assert client.get("/metrics").status_code == 200


@pytest.mark.parametrize("path,body", [
    ("/ask", {"question": "hi"}),                                                  # too short
    ("/ask", {"question": "What was revenue?", "ticker": "DROP TABLE;"}),          # not a ticker
    ("/ask", {"question": "x" * 2001}),
    ("/reports", {"ticker": "WM", "peers": ["OK", "bad ticker"]}),
    ("/reports", {"ticker": "WM", "assumptions": {"made_up": 1}}),                 # unknown assumption: rejected before any DB work
    ("/reports", {"ticker": "WM", "price": -5}),
    ("/reports", {"ticker": "WM", "peers": [f"P{i}" for i in range(11)]}),
    ("/ingest", {"ticker": "WM; rm -rf /"}),
    ("/prices", {"ticker": "WM", "price": 0}),
])
def test_bad_input_is_rejected_with_422_before_any_work(client, path, body):
    assert client.post(path, json=body).status_code == 422


def test_unknown_or_malformed_run_ids_and_artifact_paths_are_404(client):
    for path in ("/runs/..%2F..%2Fetc", "/runs/has space", "/runs/x/artifacts/..%2Fsecret", "/runs/20260101-000000-zzzzz/artifacts/nope.pdf"):
        assert client.get(path).status_code == 404, path


def test_metrics_use_templated_paths_so_run_ids_do_not_explode_label_cardinality(client):
    client.get("/runs/1111/artifacts/a.pdf")
    client.get("/runs/2222/artifacts/b.pdf")
    text = client.get("/metrics").text
    assert 'path="/runs/{run_id}/artifacts/{name}"' in text and "1111" not in text and "2222" not in text


def test_production_refuses_to_start_with_development_defaults(monkeypatch):
    import asyncio

    from finrobot.api.app import lifespan
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_keys", "")
    with pytest.raises(RuntimeError, match="refusing to start in production.*FINROBOT_API_KEYS"):
        async def go():
            async with lifespan(create_app()):
                pass
        asyncio.run(go())


def test_the_job_queue_is_bounded_and_drains():
    runner = JobRunner(workers=1, max_queue=1)
    gate, started = threading.Event(), threading.Event()
    done = []

    def blocking():
        started.set()
        gate.wait(5)
        done.append(1)

    assert runner.submit("qa", blocking)          # running
    started.wait(5)
    assert runner.submit("qa", lambda: done.append(2))   # queued
    assert not runner.submit("qa", lambda: done.append(3))   # full -> the API answers 429
    gate.set()
    runner.pool.shutdown(wait=True)
    assert done == [1, 2] and runner._pending == 0


def test_startup_waits_for_a_database_that_is_not_up_yet(monkeypatch):
    """Containers start in any order: the API must retry instead of crashing on the first refused connection."""
    import psycopg

    from finrobot.api import app as api_app
    attempts = []

    def flaky_init():
        attempts.append(1)
        if len(attempts) < 3:
            raise psycopg.OperationalError("connection refused")
    monkeypatch.setattr(api_app.store, "init_db", flaky_init)
    monkeypatch.setattr(api_app.time, "sleep", lambda s: None)
    api_app._wait_for_db(timeout=30)
    assert len(attempts) == 3

    monkeypatch.setattr(api_app.store, "init_db", lambda: (_ for _ in ()).throw(psycopg.OperationalError("never up")))
    monkeypatch.setattr(api_app.time, "time", iter([0, 1, 100, 200]).__next__)   # deadline passes -> the error propagates
    with pytest.raises(psycopg.OperationalError, match="never up"):
        api_app._wait_for_db(timeout=10)
