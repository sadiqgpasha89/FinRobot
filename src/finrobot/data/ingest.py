"""Ingest pipeline: EDGAR -> XBRL facts + parsed/chunked/embedded filings -> Postgres, raw files -> object store."""
from __future__ import annotations

from typing import Callable

from finrobot.data import objects, store
from finrobot.data.parse import Section, chunk_sections, html_to_text, split_sections
from finrobot.data.providers.edgar import EdgarClient
from finrobot.data.xbrl import normalise
from finrobot.llm.gateway import LLMGateway


def ingest(query: str, n_10k: int = 1, n_10q: int = 3, n_earnings: int = 3, embed: bool = True,
           log: Callable[[str], None] = print, edgar: EdgarClient | None = None, llm: LLMGateway | None = None) -> dict:
    edgar = edgar or EdgarClient()
    llm = llm or LLMGateway()
    store.init_db()
    ticker, cik, name = edgar.resolve(query)
    subs = edgar.submissions(cik)
    store.upsert_company(ticker, cik, name, subs.get("sicDescription", ""), subs.get("fiscalYearEnd", ""))
    log(f"{ticker} — {name} (CIK {cik})")

    rows = normalise(edgar.company_facts(cik))
    store.replace_facts(ticker, rows)
    log(f"  {len(rows)} XBRL fact rows ({len({r.metric for r in rows})} metrics)")

    wanted: list = []
    all_f = edgar.filings(cik, forms=("10-K", "10-Q"), earnings_8k=True)
    for form, n in (("10-K", n_10k), ("10-Q", n_10q), ("8-K", n_earnings)):
        wanted += [f for f in all_f if f.form == form][:n]

    added = skipped = 0
    for f in wanted:
        if store.filing_exists(f.accession):
            skipped += 1
            continue
        if f.form == "8-K":
            doc = edgar.earnings_exhibit(cik, f)
            if doc is None:
                log(f"  8-K {f.filed}: no EX-99.1 found, skipped")
                continue
            docname, raw = doc
        else:
            docname, raw = edgar.document(cik, f)
        text = html_to_text(raw)
        sections = split_sections(text, f.form) if f.form != "8-K" else [Section("Earnings Release", text)]
        chunks = chunk_sections(sections)
        embs = llm.embed([c.text for c in chunks], "document") if embed else [None] * len(chunks)
        key = f"raw/{ticker}/{f.acc_nodash}/{docname}"
        where = objects.put(key, raw, "text/html")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{f.acc_nodash}/{docname}"
        store.insert_filing(ticker, f.form, f.accession, f.filed, f.report_date, url, where,
                            [(c.idx, c.section, c.text, e) for c, e in zip(chunks, embs, strict=True)])
        added += 1
        log(f"  {f.form} {f.filed}: {len(chunks)} chunks ({', '.join(sorted({c.section for c in chunks}))})")
    log(f"done: {added} filings added, {skipped} already present")
    return {"ticker": ticker, "cik": cik, "name": name, "facts": len(rows), "filings_added": added}
