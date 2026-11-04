"""FinRobot command line."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import typer

app = typer.Typer(help="FinRobot: multi-agent equity research and valuation.", no_args_is_help=True, add_completion=False)


def _echo_event(e: dict) -> None:
    t = e.get("type")
    if t == "route":
        typer.echo(f"  · {e['ticker']} {e['period']}", err=True)
    elif t == "agent_start":
        typer.echo(f"  → {e['agent']}…", err=True)
    elif t == "tool":
        a = ", ".join(f"{k}={v}" for k, v in (e.get("args") or {}).items())
        typer.echo(f"      {e.get('agent', '')}: {e['tool']}({a[:90]})", err=True)
    elif t == "retry":
        typer.echo(f"  ↻ {e['agent']} retry after {len(e['issues'])} verification issue(s)", err=True)
    elif t == "agent_done":
        typer.echo(f"  ✓ {e['agent']}: {e['tool_calls']} tool calls, claims {e['eval']['claims']}, removed {e['removed']}, {e.get('seconds', '?')}s", err=True)
    elif t == "grounding":
        typer.echo(f"  ✓ grounding check: {len(e['removed'])} unsupported sentence(s) removed ({e['seconds']}s)", err=True)
        for r in e["removed"]:
            typer.echo(f"      − {r[:120]}", err=True)
    elif t == "section":
        typer.echo(f"  § {e['name']}", err=True)
    elif t == "log":
        typer.echo(f"  {e['msg']}", err=True)
    elif t == "error":
        typer.echo(f"error: {e['msg']}", err=True)


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging")) -> None:
    from finrobot.config import settings
    from finrobot.log import setup_logging
    setup_logging("DEBUG" if verbose else "WARNING" if settings.log_level.upper() == "INFO" else settings.log_level, settings.log_json)


@app.command()
def ping() -> None:
    """Check Postgres, the object store, the models and the tracing configuration."""
    from finrobot.config import settings
    from finrobot.data import objects, store
    from finrobot.llm.gateway import LLMGateway
    ok = True
    try:
        store.init_db()
        typer.echo("postgres      ok (migrations applied)")
    except Exception as e:
        ok = False
        typer.echo(f"postgres      FAIL {e}")
    typer.echo("object store  " + ("ok (s3)" if objects.available() else "unavailable -> local files in output/objects"))
    health = LLMGateway().health()
    for k, v in health.items():
        if k != "ok":
            typer.echo(f"{k:<13} {v}")
    ok &= bool(health["ok"])
    typer.echo("sec user-agent " + ("set" if settings.sec_user_agent else "MISSING (set SEC_USER_AGENT in .env)"))
    typer.echo(f"langfuse      {'enabled' if settings.langfuse_enabled else 'off (set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)'}")
    typer.echo(f"langsmith     {'enabled, project ' + settings.langsmith_project if settings.langsmith_enabled else 'off (set LANGSMITH_TRACING=true and LANGSMITH_API_KEY)'}")
    for p in settings.production_problems() if settings.environment == "production" else []:
        ok = False
        typer.echo(f"production    PROBLEM: {p}")
    raise typer.Exit(0 if ok else 1)


@app.command()
def migrate() -> None:
    """Apply database migrations (also run automatically when the API starts)."""
    from finrobot.data.migrations import migrate as run
    applied = run()
    typer.echo(f"applied: {applied}" if applied else "database is up to date")


@app.command()
def ask(question: str, ticker: str = typer.Option(None, "--ticker", "-t", help="skip company detection")) -> None:
    """Ask an analyst-style question; Data, Concept and Thesis agents answer with cited figures."""
    from finrobot import runs, service
    run_id, emit = runs.new_run("qa", {"question": question, "ticker": ticker}, _echo_event)
    result = service.run_job(run_id, emit, lambda: service.execute_qa(run_id, emit, question, ticker, flush=True))
    if result is None:
        raise typer.Exit(1)
    typer.echo("\n" + result["answer_md"])
    ev, u = result["eval"], result.get("usage", {})
    typer.echo(f"\n— verification: {'PASS' if ev['ok'] else 'FAIL'} · {ev['claims']} figures · {ev['matched_facts']} tied to data · "
               f"{ev['grounded_in_passages']} grounded in documents · {result['seconds']}s · "
               f"{u.get('input_tokens', 0)}+{u.get('output_tokens', 0)} tokens · run {run_id}", err=True)


@app.command()
def report(ticker: str, assumptions: Path = typer.Option(None, help="YAML file overriding default projection assumptions"),
           peers: str = typer.Option("", help="comma-separated peer tickers (ingest them with --facts-only first)"),
           price: float = typer.Option(None, help="share price to use (otherwise the latest stored price)")) -> None:
    """Generate the multi-section equity research PDF."""
    from finrobot import runs, service
    run_id, emit = runs.new_run("report", {"ticker": ticker, "peers": peers}, _echo_event)
    result = service.run_job(run_id, emit, lambda: service.execute_report(
        run_id, emit, ticker, assumptions_file=assumptions, peers=[p.strip().upper() for p in peers.split(",") if p.strip()], price=price, flush=True))
    if result is None:
        raise typer.Exit(1)
    typer.echo(f"report: {runs.run_dir(run_id) / result['pdf_name']}  ({result['pages']} pages)")
    typer.echo(f"verification: {'PASS' if result['eval']['ok'] else 'FAIL'} · {result['eval']['claims']} figures in prose · format: {result['format']}")


@app.command()
def ingest(ticker: str, tenk: int = typer.Option(1, help="10-K filings"), tenq: int = typer.Option(3, help="10-Q filings"),
           earnings: int = typer.Option(3, help="earnings-release 8-Ks"),
           facts_only: bool = typer.Option(False, "--facts-only", help="XBRL numbers only (for peers): no filing text, no embeddings")) -> None:
    """Download and index a company: XBRL financials, filings, earnings releases."""
    from finrobot.data.ingest import ingest as run
    run(ticker, 0 if facts_only else tenk, 0 if facts_only else tenq, 0 if facts_only else earnings, log=typer.echo)


@app.command("set-price")
def set_price(ticker: str, price: float, day: str = typer.Option(None, help="YYYY-MM-DD, default today")) -> None:
    """Record a share price by hand."""
    from finrobot.data import store
    store.init_db()
    store.upsert_prices(ticker.upper(), [(date.fromisoformat(day) if day else date.today(), price)], "manual")
    typer.echo(f"{ticker.upper()} = {price}")


@app.command("import-prices")
def import_prices(ticker: str, csv_file: Path) -> None:
    """Load daily closes from a CSV with Date and Close columns."""
    from finrobot.data import store
    from finrobot.data.providers.market import parse_price_csv
    store.init_db()
    n = store.upsert_prices(ticker.upper(), parse_price_csv(csv_file.read_text()), f"csv:{csv_file.name}")
    typer.echo(f"{n} prices loaded for {ticker.upper()}")


@app.command("fetch-prices")
def fetch_prices(ticker: str) -> None:
    """Fetch recent daily closes from Alpha Vantage (needs ALPHAVANTAGE_API_KEY)."""
    from finrobot.data import store
    from finrobot.data.providers.market import fetch_alphavantage
    store.init_db()
    n = store.upsert_prices(ticker.upper(), fetch_alphavantage(ticker.upper()), "alphavantage")
    typer.echo(f"{n} prices loaded for {ticker.upper()}")


@app.command("set-consensus")
def set_consensus(ticker: str, metric: str, fiscal_year: int, fiscal_quarter: int, mean: float,
                  source: str = typer.Option("manual", help="where the number came from"),
                  as_of: str = typer.Option(None, help="YYYY-MM-DD")) -> None:
    """Record an analyst consensus value (USD for money metrics, USD/share for EPS)."""
    from finrobot.data import store
    store.init_db()
    store.upsert_consensus(ticker.upper(), metric, fiscal_year, fiscal_quarter, mean, as_of=date.fromisoformat(as_of) if as_of else None, source=source)
    typer.echo("saved")


@app.command()
def companies() -> None:
    """List ingested companies."""
    from finrobot.data import store
    for c in store.list_companies():
        typer.echo(f"{c['ticker']:<8} {c['name']}  ({store.latest_quarter(c['ticker'])})")


@app.command()
def serve(port: int = 8010, host: str = typer.Option("127.0.0.1", envvar="FINROBOT_HOST", help="0.0.0.0 inside a container")) -> None:
    """Run the API (FastAPI). Set FINROBOT_API_KEYS before binding to anything but localhost."""
    import uvicorn

    from finrobot.config import settings
    uvicorn.run("finrobot.api.app:app", host=host, port=port, log_config=None, proxy_headers=True, timeout_graceful_shutdown=30,
                log_level=settings.log_level.lower())


@app.command()
def ui(port: int = 8510, host: str = typer.Option("127.0.0.1", envvar="FINROBOT_UI_HOST")) -> None:
    """Run the Streamlit UI (start the API first with `finrobot serve`)."""
    import subprocess

    from finrobot.ui import app as ui_app
    cmd = [sys.executable, "-m", "streamlit", "run", str(Path(ui_app.__file__)), "--server.port", str(port),
           "--server.headless", "true", "--server.address", host, "--browser.gatherUsageStats", "false"]
    sys.exit(subprocess.call(cmd))  # noqa: S603 (fixed arguments, no user input)


if __name__ == "__main__":
    app()
