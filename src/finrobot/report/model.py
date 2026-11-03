"""Deterministic report data: every table cell, chart point and prompt fact comes from the FactBook."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from finrobot.agents.context import RunContext
from finrobot.data import store
from finrobot.tools import charts
from finrobot.tools.factbook import Fact
from finrobot.tools.metrics import Metrics, MissingData, period_label, shift
from finrobot.tools.tables import cell, html_table, money_m
from finrobot.tools.valuation import Assumptions, YEARS, comps_row, dcf, default_assumptions, project, sensitivity

SECTIONS = ["Company Overview", "Thesis and Drivers", "Investment Update", "Financial Summary",
            "Risks and Mitigations", "Valuation", "Financial Projections", "Credit and Cashflow"]


@dataclass
class SectionData:
    key: str
    title: str
    facts: list[Fact] = field(default_factory=list)       # figures the prose may cite
    tables: list[str] = field(default_factory=list)       # HTML
    charts: list[tuple[str, str]] = field(default_factory=list)   # (artifact name, caption)
    notes: list[str] = field(default_factory=list)        # plain-text context for the writer (no figures)


@dataclass
class ReportModel:
    sections: dict[str, SectionData]
    assumptions: Assumptions
    dcf_result: dict | None
    price: dict | None
    peers: list[dict]
    period: str
    period_end: date
    warnings: list[str] = field(default_factory=list)


def _try(fn, *a):
    try:
        return fn(*a)
    except (MissingData, ZeroDivisionError, KeyError, IndexError):
        return None


def _reg(ctx: RunContext, label: str, value: float, unit: str, source: str, inputs: list[Fact] | None = None) -> Fact:
    return ctx.fb.add_fact(label, value, unit, source, derived=True, inputs=[i.id for i in (inputs or [])])


def quarter_labels(fy: int, fq: int, n: int) -> list[tuple[int, int]]:
    return [shift(fy, fq, -i) for i in reversed(range(n))]


def build_model(ctx: RunContext, assumptions: Assumptions | None = None, peers: list[str] | None = None,
                price_override: float | None = None) -> ReportModel:
    m: Metrics = ctx.metrics
    fy, fq = ctx.fy, ctx.fq
    warnings: list[str] = []
    secs = {k: SectionData(k, k) for k in SECTIONS}

    # --- price -------------------------------------------------------------------------------
    price_row = {"day": date.today(), "close": price_override, "source": "manual override"} if price_override else store.latest_price(ctx.ticker)
    price_fact = None
    if price_row:
        price_fact = _reg(ctx, f"{ctx.ticker} share price on {price_row['day']}", price_row["close"], "USD/shares", f"price ({price_row['source']})")
    else:
        warnings.append("No share price on file: valuation shows intrinsic value only. Load one with `finrobot set-price`.")

    # --- history tables (8 quarters, 4 fiscal years) -----------------------------------------
    qs = quarter_labels(fy, fq, 8)
    rows = {k: [] for k in ("revenue", "ebitda", "ebitda_margin", "operating_income", "net_income", "eps", "fcf")}
    for (y, q) in qs:
        rows["revenue"].append(_try(m.fact, "revenue", y, q))
        rows["ebitda"].append(_try(m.compute, "ebitda", y, q))
        rows["ebitda_margin"].append(_try(m.compute, "ebitda_margin", y, q))
        rows["operating_income"].append(_try(m.fact, "operating_income", y, q))
        rows["net_income"].append(_try(m.fact, "net_income", y, q))
        rows["eps"].append(_try(m.fact, "eps_diluted", y, q))
        rows["fcf"].append(_try(m.compute, "fcf", y, q))
    qhead = [f"Q{q} FY{str(y)[2:]}" for y, q in qs]
    labels = {"revenue": ("Revenue", "money"), "ebitda": ("EBITDA¹", "money"), "ebitda_margin": ("EBITDA margin", "pct"),
              "operating_income": ("Operating income", "money"), "net_income": ("Net income", "money"),
              "eps": ("Diluted EPS ($)", "eps"), "fcf": ("Free cash flow²", "money")}
    fs = secs["Financial Summary"]
    fs.tables.append(html_table([("USD millions")] + qhead, [[labels[k][0]] + [cell(f, labels[k][1]) for f in rows[k]] for k in labels],
                                "Quarterly results", notes="¹ Reported operating income + depreciation & amortization (not the company's adjusted EBITDA). "
                                "² Operating cash flow − capital expenditures. Fourth quarters are derived as full year − nine months."))
    annual_years = [fy - i for i in reversed(range(4))] if fq == 4 else [fy - 1 - i for i in reversed(range(4))]
    arows = {"Revenue": ("revenue", "money"), "EBITDA¹": ("ebitda", "money"), "Operating income": ("operating_income", "money"),
             "Net income": ("net_income", "money"), "Diluted EPS ($)": ("eps_diluted", "eps"), "Free cash flow²": ("fcf", "money")}
    atable = []
    for lab, (metric, kind) in arows.items():
        cells = []
        for y in annual_years:
            f = _try(m.compute, metric, y, 0) if metric in ("ebitda", "fcf") else _try(m.fact, metric, y, 0)
            cells.append(cell(f, kind))
            if f is not None and lab in ("Revenue", "EBITDA¹", "Free cash flow²"):
                fs.facts.append(f)
        atable.append([lab] + cells)
    fs.tables.append(html_table(["USD millions"] + [f"FY{y}" for y in annual_years], atable, "Annual results"))
    latest = [rows[k][-1] for k in ("revenue", "ebitda", "ebitda_margin", "operating_income", "net_income", "fcf")]
    prior = [_try(m.fact, "revenue", *shift(fy, fq, -4)), _try(m.compute, "ebitda", *shift(fy, fq, -4)),
             _try(m.compute, "ebitda_margin", *shift(fy, fq, -4))]
    fs.facts += [f for f in latest + prior if f] + [f for f in (_try(m.compute, "revenue_growth_yoy", fy, fq), _try(m.compute, "ebitda_growth_yoy", fy, fq),
                                                                _try(m.compute, "ebitda_margin_change_yoy", fy, fq),
                                                                _try(m.compute, "eps_growth_yoy", fy, fq)) if f]
    if all(f is not None for f in rows["revenue"]) and all(f is not None for f in rows["ebitda_margin"]):
        name = "chart_financial_summary.png"
        ctx.artifacts[name] = charts.bar_line(qhead, [f.value for f in rows["revenue"]], [f.value for f in rows["ebitda_margin"]],
                                              f"{ctx.ticker}: quarterly revenue and EBITDA margin", "Revenue ($B)", "EBITDA margin (%)")
        fs.charts.append((name, "Quarterly revenue (bars) and EBITDA margin (line)."))

    # --- company overview / key metrics ----------------------------------------------------------
    ov = secs["Company Overview"]
    kms = [("TTM revenue", _try(m.compute, "ttm_revenue", fy, fq), "money"), ("TTM EBITDA¹", _try(m.compute, "ttm_ebitda", fy, fq), "money"),
           ("TTM free cash flow²", _try(m.compute, "ttm_fcf", fy, fq), "money"), ("Net debt", _try(m.compute, "net_debt", fy, fq), "money"),
           ("Net debt / TTM EBITDA", _try(m.compute, "net_debt_to_ttm_ebitda", fy, fq), "x"),
           ("Operating margin (latest quarter)", _try(m.compute, "operating_margin", fy, fq), "pct"),
           ("Revenue growth YoY (latest quarter)", _try(m.compute, "revenue_growth_yoy", fy, fq), "pct")]
    ov.tables.append(html_table(["Key metric", period_label(fy, fq)], [[n, cell(f, k)] for n, f, k in kms if f], "Key metrics"))
    ov.facts += [f for _, f, _ in kms if f]
    co = store.get_company(ctx.ticker) or {}
    ov.notes.append(f"Company: {ctx.name} ({ctx.ticker}); SEC industry description: {co.get('sector') or 'n/a'}.")
    hist = store.price_history(ctx.ticker)
    if len(hist) >= 20:
        name = "chart_price.png"
        ctx.artifacts[name] = charts.price_line([h["day"] for h in hist], [h["close"] for h in hist], f"{ctx.ticker} share price")
        ov.charts.append((name, "Share price history from the loaded price data."))
    if price_fact:
        ov.facts.append(price_fact)

    # --- investment update: latest quarter vs year ago ---------------------------------------------
    iu = secs["Investment Update"]
    iu_rows = []
    for lab, metric, kind in (("Revenue", "revenue", "money"), ("EBITDA¹", "ebitda", "money"), ("EBITDA margin", "ebitda_margin", "pct"),
                              ("Operating margin", "operating_margin", "pct"), ("Net income", "net_income", "money"),
                              ("Diluted EPS ($)", "eps_diluted", "eps"), ("Free cash flow²", "fcf", "money")):
        get = (lambda y, q, metric=metric: m.fact(metric, y, q)) if metric in ("revenue", "net_income", "eps_diluted") else (lambda y, q, metric=metric: m.compute(metric, y, q))
        cur, prev = _try(get, fy, fq), _try(get, *shift(fy, fq, -4))
        if cur is None:
            continue
        chg = ""
        if prev is not None and kind == "money" and prev.value:
            chg = f"{(cur.value / prev.value - 1) * 100:+.1f}%"
        elif prev is not None and kind == "pct":
            chg = f"{(cur.value - prev.value) * 100:+.0f} bps"
        elif prev is not None and kind == "eps" and prev.value:
            chg = f"{(cur.value / prev.value - 1) * 100:+.1f}%"
        iu_rows.append([lab, cell(cur, kind), cell(prev, kind), chg])
        iu.facts += [f for f in (cur, prev) if f]
    iu.tables.append(html_table(["USD millions", period_label(fy, fq), period_label(*shift(fy, fq, -4)), "Change YoY"], iu_rows, "Latest quarter vs. year ago"))
    iu.facts += [f for f in (_try(m.compute, "revenue_growth_yoy", fy, fq), _try(m.compute, "ebitda_growth_yoy", fy, fq),
                             _try(m.compute, "ebitda_margin_change_yoy", fy, fq), _try(m.compute, "operating_margin_change_yoy", fy, fq)) if f]

    # --- thesis & drivers: trend facts --------------------------------------------------------------
    th = secs["Thesis and Drivers"]
    th.facts += [f for f in (_try(m.compute, "revenue_growth_yoy", fy, fq), _try(m.compute, "revenue_growth_yoy", *shift(fy, fq, -4)),
                             _try(m.compute, "ebitda_margin", fy, fq), _try(m.compute, "ebitda_margin", *shift(fy, fq, -4)),
                             _try(m.compute, "ebitda_margin_change_yoy", fy, fq), _try(m.compute, "ttm_ebitda", fy, fq),
                             _try(m.compute, "fcf_margin", fy, fq)) if f]

    # --- projections & valuation -----------------------------------------------------------------------
    a = assumptions
    result = None
    sens = None
    vl, pj = secs["Valuation"], secs["Financial Projections"]
    try:
        if a is None:
            a = default_assumptions(m, fy, fq)
        proj = project(a)
        result = dcf(a)
        sens = sensitivity(a)
    except (MissingData, ZeroDivisionError, ValueError, KeyError) as e:
        warnings.append(f"Projections and valuation skipped: {e}")
    if a is not None and result is not None:
        _build_projection(ctx, m, a, proj, pj, fy, fq, annual_years)
        _build_valuation(ctx, a, result, sens, vl, price_fact, peers or [], warnings)

    # --- credit & cashflow ------------------------------------------------------------------------------
    cr = secs["Credit and Cashflow"]
    cq = quarter_labels(fy, fq, 5)
    crows = {"Net debt": ("net_debt", "money"), "Net debt / TTM EBITDA": ("net_debt_to_ttm_ebitda", "x"),
             "Interest coverage (TTM EBIT / interest)": ("interest_coverage_ttm", "x"), "Free cash flow²": ("fcf", "money"),
             "FCF margin": ("fcf_margin", "pct"), "Capex % of revenue": ("capex_pct_revenue", "pct")}
    ctable, series = [], {}
    for lab, (metric, kind) in crows.items():
        facts = [_try(m.compute, metric, y, q) for y, q in cq]
        series[metric] = facts
        ctable.append([lab] + [cell(f, kind) for f in facts])
        cr.facts += [f for f in facts[-2:] if f]
    cr.tables.append(html_table(["USD millions"] + [f"Q{q} FY{str(y)[2:]}" for y, q in cq], ctable, "Leverage and cash generation"))
    ocf, cap = _try(m.ttm, "op_cash_flow", fy, fq), _try(m.ttm, "capex", fy, fq)
    ni = _try(m.ttm, "net_income", fy, fq)
    cr.facts += [f for f in (ocf, cap, ni) if f]
    if ocf and cap and ni and ni.value:
        cr.facts.append(_reg(ctx, f"{ctx.ticker} TTM FCF conversion (FCF / net income)", (ocf.value - cap.value) / ni.value * 100, "%",
                             "derived: TTM operating cash flow - capex over TTM net income", [ocf, cap, ni]))
    fcfs, lev = series["fcf"], series["net_debt_to_ttm_ebitda"]
    if all(f is not None for f in fcfs) and all(f is not None for f in lev):
        name = "chart_credit.png"
        ctx.artifacts[name] = charts.bar_line([f"Q{q} FY{str(y)[2:]}" for y, q in cq], [f.value for f in fcfs], [f.value for f in lev],
                                              f"{ctx.ticker}: free cash flow and leverage", "Free cash flow ($B)", "Net debt / TTM EBITDA (x)", line_unit="x")
        cr.charts.append((name, "Quarterly free cash flow (bars) and net debt to trailing EBITDA (line)."))

    return ReportModel(secs, a, result, price_row, [], ctx.period, ctx.period_end, warnings)


def _build_projection(ctx, m, a: Assumptions, proj: list[dict], pj: SectionData, fy: int, fq: int, annual_years: list[int]) -> None:
    ttm_rev = m.ttm("revenue", fy, fq)
    head = ["USD millions", "TTM base"] + [f"Year {r['year']}" for r in proj]
    ebitda_ttm = m._m_ttm_ebitda(fy, fq)
    rows = [["Revenue", money_m(a.base_revenue)] + [money_m(r["revenue"]) for r in proj],
            ["  growth", ""] + [f"{r['growth']:.1f}%" for r in proj],
            ["EBITDA", money_m(a.base_ebitda)] + [money_m(r["ebitda"]) for r in proj],
            ["  margin", f"{a.base_ebitda / a.base_revenue * 100:.1f}%"] + [f"{r['ebitda_margin']:.1f}%" for r in proj],
            ["Capex", ""] + [money_m(r["capex"]) for r in proj],
            ["Unlevered free cash flow", ""] + [money_m(r["fcff"]) for r in proj]]
    pj.tables.append(html_table(head, rows, "Driver-based projection (mechanical defaults, editable)",
                                notes="Years are 12-month periods after the trailing-twelve-month base. Unlevered FCF = EBIT × (1 − tax) + D&A − capex − change in working capital."))
    arows = [["Revenue growth, year 1 → 5", f"{a.rev_growth[0] * 100:.1f}% → {a.rev_growth[-1] * 100:.1f}%", a.basis.get("rev_growth", "user-specified")],
             ["EBITDA margin", f"{a.ebitda_margin[0] * 100:.1f}% → {a.ebitda_margin[-1] * 100:.1f}%", a.basis.get("ebitda_margin", "user-specified")],
             ["D&A % of revenue", f"{a.da_pct * 100:.1f}%", a.basis.get("da_pct", "user-specified")],
             ["Capex % of revenue", f"{a.capex_pct * 100:.1f}%", a.basis.get("capex_pct", "user-specified")],
             ["Tax rate", f"{a.tax_rate * 100:.1f}%", a.basis.get("tax_rate", "user-specified")],
             ["WACC", f"{a.wacc * 100:.1f}%", a.basis.get("wacc", "user-specified")],
             ["Terminal growth", f"{a.terminal_growth * 100:.1f}%", a.basis.get("terminal_growth", "user-specified")]]
    pj.tables.append(html_table(["Assumption", "Value", "Basis"], arows, "Assumptions", text=True))
    reg = lambda label, v, unit: _reg(ctx, f"{ctx.ticker} {label}", v, unit, "assumption/model", [ttm_rev, ebitda_ttm])
    pj.facts += [reg("assumed revenue growth, year 1", a.rev_growth[0] * 100, "%"), reg("assumed revenue growth, year 5", a.rev_growth[-1] * 100, "%"),
                 reg("assumed EBITDA margin", a.ebitda_margin[-1] * 100, "%"), reg("assumed capex % of revenue", a.capex_pct * 100, "%"),
                 reg("assumed D&A % of revenue", a.da_pct * 100, "%"), reg("assumed tax rate", a.tax_rate * 100, "%"),
                 reg("projected revenue, year 5", proj[-1]["revenue"], "USD"), reg("projected EBITDA, year 5", proj[-1]["ebitda"], "USD"),
                 reg("projected unlevered free cash flow, year 5", proj[-1]["fcff"], "USD"), ttm_rev, ebitda_ttm]
    hist_fy = [(y, _try(m.fact, "revenue", y, 0), _try(m.compute, "ebitda_margin", y, 0)) for y in annual_years]
    hist_fy = [h for h in hist_fy if h[1] is not None and h[2] is not None]
    labels = [f"FY{y}" for y, _, _ in hist_fy] + ["TTM"] + [f"Y{r['year']}" for r in proj]
    bars_ = [r.value for _, r, _ in hist_fy] + [a.base_revenue] + [r["revenue"] for r in proj]
    line_ = [mm.value for _, _, mm in hist_fy] + [a.base_ebitda / a.base_revenue * 100] + [r["ebitda_margin"] for r in proj]
    name = "chart_projection.png"
    ctx.artifacts[name] = charts.bar_line(labels, bars_, line_, f"{ctx.ticker}: revenue and EBITDA margin, history and projection", "Revenue ($B)",
                                          "EBITDA margin (%)", hatch_from=len(hist_fy) + 1)
    pj.charts.append((name, "History (solid) and mechanical projection (light bars)."))


def _build_valuation(ctx, a: Assumptions, r: dict, sens: dict, vl: SectionData, price_fact: Fact | None, peers: list[str], warnings: list[str]) -> None:
    src = "DCF model"
    reg = lambda label, v, unit: _reg(ctx, f"{ctx.ticker} DCF {label}", v, unit, src)
    f_ev, f_eq = reg("enterprise value", r["enterprise_value"], "USD"), reg("equity value", r["equity_value"], "USD")
    f_ps, f_tv = reg("value per share", r["per_share"], "USD/shares"), reg("terminal value share of EV", r["tv_share_of_ev"], "%")
    f_mult = reg("implied EV / TTM EBITDA", r["implied_ev_ebitda"], "x")
    f_wacc, f_g = reg("WACC", a.wacc * 100, "%"), reg("terminal growth", a.terminal_growth * 100, "%")
    vl.facts += [f_ev, f_eq, f_ps, f_tv, f_mult, f_wacc, f_g]
    rows = [["PV of 5-year unlevered FCF", money_m(r["pv_fcff"])], ["PV of terminal value", money_m(r["pv_terminal"])],
            ["Enterprise value", money_m(r["enterprise_value"])], ["Less: net debt", money_m(a.net_debt)],
            ["Equity value", money_m(r["equity_value"])], ["Diluted shares (millions)", f"{a.shares / 1e6:,.1f}"],
            ["Value per share ($)", f"{r['per_share']:.2f}"]]
    if price_fact:
        up = (r["per_share"] / price_fact.value - 1) * 100
        f_up = _reg(ctx, f"{ctx.ticker} DCF value vs share price", up, "%", "derived: DCF value per share over share price", [f_ps, price_fact])
        vl.facts += [price_fact, f_up]
        rows += [["Share price ($)", f"{price_fact.value:.2f}"], ["DCF value vs. price", f"{up:+.1f}%"]]
    vl.tables.append(html_table(["DCF summary", "USD millions unless stated"], rows, "Discounted cash flow"))
    head = ["WACC \\ terminal growth"] + [f"{g * 100:.1f}%" for g in sens["gs"]]
    srows = [[f"{w * 100:.1f}%"] + [f"{v:.2f}" if v is not None else "—" for v in row] for w, row in zip(sens["waccs"], sens["grid"])]
    vl.tables.append(html_table(head, srows, "Sensitivity: DCF value per share ($)"))
    lo = min(v for row in sens["grid"] for v in row if v is not None)
    hi = max(v for row in sens["grid"] for v in row if v is not None)
    vl.facts += [reg("sensitivity low", lo, "USD/shares"), reg("sensitivity high", hi, "USD/shares")]
    mid = [row[2] for row in sens["grid"]]
    name = "chart_valuation.png"
    ctx.artifacts[name] = charts.bars([f"{w * 100:.1f}%" for w in sens["waccs"]], mid, f"{ctx.ticker}: DCF value per share by WACC (terminal growth {a.terminal_growth * 100:.1f}%)",
                                      unit="USD per share", scale=1.0)
    vl.charts.append((name, "DCF value per share across WACC assumptions."))
    prows = []
    for t in peers:
        lq = store.latest_quarter(t)
        if not lq:
            warnings.append(f"Peer {t} has no data; run `finrobot ingest {t} --facts-only`.")
            continue
        pm = Metrics(t, ctx.fb, store)
        pr = store.latest_price(t)
        row = _try(comps_row, t, pm, lq[0], lq[1], pr["close"] if pr else None)
        if row:
            prows.append(row)
            vl.facts += [ctx.fb.facts[i] for i in row["fact_ids"]]
    if prows:
        fm = lambda v, s: "—" if v is None else s.format(v)
        vl.tables.append(html_table(["Peer", "TTM revenue ($M)", "Growth", "EBITDA margin", "Net debt / EBITDA", "EV / EBITDA", "P/E"],
                                    [[p["ticker"], money_m(p["revenue"]), f"{p['growth']:.1f}%", f"{p['ebitda_margin']:.1f}%",
                                      fm(p["net_debt_ebitda"], "{:.2f}x"), fm(p["ev_ebitda"], "{:.1f}x"), fm(p["pe"], "{:.1f}x")] for p in prows],
                                    "Peer operating profile", notes="Multiples appear only where a share price has been loaded for the peer."))
    elif peers:
        warnings.append("Peer table skipped: no usable peer data.")
