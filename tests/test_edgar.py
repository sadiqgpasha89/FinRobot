import time

import httpx
import pytest

from finrobot.config import settings
from finrobot.data.providers.edgar import EdgarClient, EdgarError
from mock_sec import CIK, FakeSEC


@pytest.fixture
def sec(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake = FakeSEC()
    return EdgarClient("FinRobot tests@example.com", transport=httpx.MockTransport(fake)), fake


def test_a_declared_user_agent_is_mandatory_and_sent(sec, monkeypatch):
    monkeypatch.setattr(settings, "sec_user_agent", "")
    with pytest.raises(EdgarError, match="SEC_USER_AGENT"):
        EdgarClient()
    client, _ = sec
    assert client._client.headers["user-agent"] == "FinRobot tests@example.com"


def test_tickers_and_names_resolve_to_a_registrant(sec):
    client, _ = sec
    assert client.resolve("tst") == ("TST", CIK, "Testco Inc")
    assert client.resolve("Testco") == ("TST", CIK, "Testco Inc")
    assert client.resolve("Testco, Inc.")[0] == "TST"
    with pytest.raises(EdgarError, match="No SEC registrant"):
        client.resolve("Nonexistent Holdings")


def test_the_ticker_list_is_cached_on_disk_and_refreshed_when_stale(sec, tmp_path):
    import os
    client, fake = sec
    client.resolve("TST")
    client.resolve("OTH")
    assert sum(u.endswith("company_tickers.json") for u in fake.requests) == 1
    path = settings.cache_dir / "company_tickers.json"
    old = time.time() - 8 * 86400
    os.utime(path, (old, old))                                    # a week old: the SEC adds tickers, so it must be refreshed
    EdgarClient("FinRobot tests@example.com", transport=httpx.MockTransport(fake)).resolve("TST")
    assert sum(u.endswith("company_tickers.json") for u in fake.requests) == 2


def test_a_truncated_cache_file_is_never_left_behind(sec):
    client, fake = sec
    client.resolve("TST")
    assert not list(settings.cache_dir.glob("*.tmp"))             # writes go through a temp file and an atomic rename


def test_rate_limits_are_retried_then_reported(sec):
    client, fake = sec
    fake.fail_next = [429, 503]
    assert client.resolve("TST")[0] == "TST"                       # two throttled responses, then success
    fake.fail_next = [429] * 10
    with pytest.raises(EdgarError, match="rate limited"):
        client.submissions(CIK)
    fake.fail_next = [404]
    with pytest.raises(EdgarError, match="404"):
        client.submissions(CIK)


def test_filings_include_only_earnings_8ks_and_the_requested_forms(sec):
    client, _ = sec
    filings = client.filings(CIK, forms=("10-K", "10-Q"), earnings_8k=True)
    assert [(f.form, f.accession) for f in filings] == [("10-Q", "0001234567-26-000100"), ("8-K", "0001234567-26-000200")]
    assert client.filings(CIK, forms=("10-Q",)) [0].acc_nodash == "000123456726000100"
    assert [f.form for f in client.filings(CIK, forms=("10-Q",), earnings_8k=False)] == ["10-Q"]


def test_the_earnings_release_is_found_in_the_filing_index(sec):
    client, _ = sec
    filings = client.filings(CIK, earnings_8k=True)
    name, raw = client.earnings_exhibit(CIK, filings[1])
    assert name == "tst-ex991.htm" and b"Third quarter results" in raw
