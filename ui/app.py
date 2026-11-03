"""FinRobot Streamlit UI. Talks to the API (start it with `finrobot serve`)."""
import os
import time

import httpx
import pandas as pd
import streamlit as st

API = os.environ.get("FINROBOT_API", "http://127.0.0.1:8010")
st.set_page_config(page_title="FinRobot", page_icon="📈", layout="wide")

TEAL, GREEN = "#0e8a99", "#7aa01c"
st.markdown(f"""<style>
.fr-title {{font-size:2.1rem;font-weight:800;letter-spacing:-.01em}} .fr-title b {{color:{TEAL}}}
.fr-sub {{color:#5d6872;margin-top:-.4rem;margin-bottom:1rem}}
.badge {{display:inline-block;padding:.15rem .6rem;border-radius:99px;font-size:.78rem;font-weight:700;color:#fff}}
</style>
<div class="fr-title">Fin<b>Robot</b></div><div class="fr-sub">Multi-agent equity research with cited, verified figures</div>""", unsafe_allow_html=True)


def api(method: str, path: str, **kw):
    r = httpx.request(method, API + path, timeout=kw.pop("timeout", 30), **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(detail)
    return r


def poll(run_id: str, label: str):
    """Show agent progress until the run ends; returns the final run record."""
    box = st.status(label, expanded=True)
    with box:
        log = st.empty()
    while True:
        run = api("GET", f"/runs/{run_id}").json()
        lines = []
        for e in run["events"]:
            t = e.get("type")
            if t == "agent_start":
                lines.append(f"▶ **{e['agent']}**")
            elif t == "tool":
                lines.append(f"&nbsp;&nbsp;&nbsp;↳ `{e['tool']}`")
            elif t == "retry":
                lines.append(f"↻ {e['agent']}: retrying after verification issues")
            elif t == "agent_done":
                lines.append(f"✓ {e['agent']} · {e.get('seconds', '?')}s · {e['eval'].get('claims', 0)} figures checked")
            elif t == "grounding":
                lines.append(f"✓ grounding check: {e['agent']} · {len(e['removed'])} unsupported sentence(s) removed")
            elif t == "section":
                lines.append(f"§ **{e['name']}**")
            elif t in ("log", "error"):
                lines.append(("⚠ " if t == "error" else "") + e["msg"])
        log.markdown("\n\n".join(lines[-12:]))
        if run["status"] != "running":
            box.update(label=f"{label}: {run['status']}", state="complete" if run["status"] == "done" else "error")
            return run
        time.sleep(1.5)


def verification_badge(ev: dict) -> str:
    ok = ev.get("ok")
    color = GREEN if ok else "#c0392b"
    return (f"<span class='badge' style='background:{color}'>{'VERIFIED' if ok else 'ISSUES'}</span> "
            f"{ev.get('claims', 0)} figures · {ev.get('matched_facts', 0)} tied to filings data · {ev.get('grounded_in_passages', 0)} grounded in document passages")


# ------------------------------------------------------------------------------------------------------------- sidebar
try:
    companies = api("GET", "/companies").json()
except Exception as e:
    st.error(f"Cannot reach the API at {API}: {e}\n\nStart it with `finrobot serve`.")
    st.stop()

with st.sidebar:
    st.subheader("Companies")
    if companies:
        st.dataframe(pd.DataFrame(companies)[["ticker", "latest_quarter", "price"]], hide_index=True, use_container_width=True)
    else:
        st.caption("None ingested yet.")
    with st.form("ingest"):
        t = st.text_input("Ingest a ticker", placeholder="e.g. WM")
        facts_only = st.checkbox("Financials only (for peers)")
        if st.form_submit_button("Ingest") and t:
            st.session_state["ingest_run"] = api("POST", "/ingest", json={"ticker": t.strip(), "facts_only": facts_only}).json()["run_id"]
    if "ingest_run" in st.session_state:
        run = poll(st.session_state.pop("ingest_run"), "Ingest")
        if run["status"] == "done":
            st.rerun()
    with st.form("price"):
        pt = st.selectbox("Set a share price", [c["ticker"] for c in companies] or [""])
        pv = st.number_input("Price (USD)", min_value=0.0, step=0.01)
        if st.form_submit_button("Save price") and pv > 0 and pt:
            api("POST", "/prices", json={"ticker": pt, "price": pv})
            st.rerun()

tickers = [c["ticker"] for c in companies]
tab_ask, tab_report = st.tabs(["Ask an analyst question", "Equity research report"])

# ----------------------------------------------------------------------------------------------------------------- ask
with tab_ask:
    q = st.text_area("Question", placeholder="How did Waste Management's EBITDA and operating margin change in Q3 versus a year ago, and what drove it?", height=80)
    tk = st.selectbox("Company (optional: detected from the question if blank)", [""] + tickers, key="ask_tk")
    if st.button("Ask", type="primary", disabled=not q.strip()):
        rid = api("POST", "/ask", json={"question": q, "ticker": tk or None}).json()["run_id"]
        run = poll(rid, "Agents")
        if run["status"] == "done":
            res = run["result"]
            st.markdown(verification_badge(res["eval"]), unsafe_allow_html=True)
            st.markdown(res["answer_md"])
            for name in res.get("artifacts", []):
                if name.endswith(".png"):
                    st.image(f"{API}/runs/{rid}/artifacts/{name}")
            st.caption(f"{res['ticker']} {res['period']} · {res['seconds']}s · run {rid}")
        else:
            st.error(run["result"].get("error", "failed") if isinstance(run["result"], dict) else "failed")

# -------------------------------------------------------------------------------------------------------------- report
with tab_report:
    if not tickers:
        st.info("Ingest a company in the sidebar first.")
    else:
        rt = st.selectbox("Company", tickers, key="rep_tk")
        if st.button("1 · Load default assumptions"):
            st.session_state["draft"] = api("GET", "/reports/draft-assumptions", params={"ticker": rt}).json()
        d = st.session_state.get("draft")
        if d and d["ticker"] == rt:
            a = d["assumptions"]
            st.caption(f"{d['name']} · {d['period']}. Defaults are mechanical (trailing rates); edit anything below before generating.")
            years = [f"Year {i}" for i in range(1, 6)]
            tbl = st.data_editor(pd.DataFrame({"Revenue growth %": [round(x * 100, 2) for x in a["rev_growth"]],
                                               "EBITDA margin %": [round(x * 100, 2) for x in a["ebitda_margin"]]}, index=years), key="proj_tbl")
            c1, c2, c3, c4, c5 = st.columns(5)
            wacc = c1.number_input("WACC %", value=round(a["wacc"] * 100, 2), step=0.25)
            tg = c2.number_input("Terminal growth %", value=round(a["terminal_growth"] * 100, 2), step=0.25)
            tax = c3.number_input("Tax rate %", value=round(a["tax_rate"] * 100, 1), step=0.5)
            capex = c4.number_input("Capex % revenue", value=round(a["capex_pct"] * 100, 2), step=0.25)
            da = c5.number_input("D&A % revenue", value=round(a["da_pct"] * 100, 2), step=0.25)
            c6, c7 = st.columns(2)
            price = c6.number_input("Share price for upside (0 = use stored price)", min_value=0.0, step=0.01)
            peers = c7.text_input("Peer tickers (comma separated, ingested financials-only)")
            if st.button("2 · Generate report", type="primary"):
                # send only what the user changed: the widgets show values rounded to 2 decimals, and echoing those back would
                # silently replace the exact history-derived defaults
                shown = {"rev_growth": [round(x * 100, 2) for x in a["rev_growth"]], "ebitda_margin": [round(x * 100, 2) for x in a["ebitda_margin"]],
                         "wacc": round(a["wacc"] * 100, 2), "terminal_growth": round(a["terminal_growth"] * 100, 2),
                         "tax_rate": round(a["tax_rate"] * 100, 1), "capex_pct": round(a["capex_pct"] * 100, 2), "da_pct": round(a["da_pct"] * 100, 2)}
                now = {"rev_growth": list(tbl["Revenue growth %"]), "ebitda_margin": list(tbl["EBITDA margin %"]), "wacc": wacc, "terminal_growth": tg,
                       "tax_rate": tax, "capex_pct": capex, "da_pct": da}
                over = {k: ([v / 100 for v in now[k]] if isinstance(now[k], list) else now[k] / 100) for k in now if now[k] != shown[k]}
                rid = api("POST", "/reports", json={"ticker": rt, "assumptions": over, "peers": [p.strip() for p in peers.split(",") if p.strip()],
                                                    "price": price or None}).json()["run_id"]
                run = poll(rid, "Report agents")
                if run["status"] == "done":
                    res = run["result"]
                    st.markdown(verification_badge(res["eval"]), unsafe_allow_html=True)
                    st.write(f"{res['pages']} pages · format check {'passed' if res['format']['ok'] else 'FAILED: ' + '; '.join(res['format']['issues'])} · {res['seconds']}s")
                    for w in res.get("warnings", []):
                        st.warning(w)
                    pdf = httpx.get(f"{API}/runs/{rid}/artifacts/{res['pdf_name']}", timeout=60).content
                    st.download_button("Download PDF", pdf, file_name=res["pdf_name"], mime="application/pdf", type="primary")
                else:
                    st.error(run["result"].get("error", "failed") if isinstance(run["result"], dict) else "failed")
