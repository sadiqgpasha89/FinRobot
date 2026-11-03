"""Live evals: real models, real ingested data. Expected numbers are computed straight from the database, never from the LLM.

    python evals/run.py            # needs `finrobot ingest WM` first and Ollama free (no other client using the GPU)

Checks per case: verification passed, the answer cites the right figures for the right period (value equal to an independent
recomputation), and 'why' questions cite at least one document passage.
"""
import sys
import time

from finrobot.data import store
from finrobot.graph.qa_graph import run_qa
from finrobot.agents.evaluator import cites_in
from finrobot.tools.factbook import FactBook
from finrobot.tools.metrics import Metrics, shift

TICKER = "WM"
CASES = [
    {"q": "How did Waste Management's EBITDA and operating margin change in Q3 versus a year ago, and what drove it?",
     "period": "FY2026 Q3", "expect": [("ebitda", 0), ("ebitda", 4), ("operating_margin", 0), ("operating_margin", 4)], "needs_passage": True},
    {"q": "Why did Waste Management's free cash flow change in Q2 compared with Q1?",
     "period": "FY2026 Q2", "expect": [("fcf", 0), ("fcf", 1)], "needs_passage": True},
    {"q": "What was Waste Management's revenue growth in Q1 year over year?",
     "period": "FY2026 Q1", "expect": [("revenue_growth_yoy", 0)], "needs_passage": False},
    {"q": "How leveraged is Waste Management: what is net debt relative to EBITDA?",
     "period": "FY2026 Q3", "expect": [("net_debt_to_ttm_ebitda", 0)], "needs_passage": False},
]


def expected_value(metric: str, fy: int, fq: int, ago: int) -> float:
    m = Metrics(TICKER, FactBook(), store)
    y, q = shift(fy, fq, -ago)
    return m.compute(metric, y, q).value


def main() -> int:
    failures = 0
    for case in CASES:
        t0 = time.time()
        result, ctx = run_qa(case["q"], TICKER)
        fy, fq = int(case["period"][2:6]), int(case["period"][-1])
        cited = cites_in(result["answer_md"].split("## Sources")[0])
        cited_values = [ctx.fb.facts[i].value for i in cited if i in ctx.fb.facts]
        problems = []
        if not result["eval"]["ok"]:
            problems.append("verification failed: " + "; ".join(result["eval"]["issues"][:2]))
        if result["period"] != case["period"]:
            problems.append(f"period {result['period']} != {case['period']}")
        for metric, ago in case["expect"]:
            want = expected_value(metric, fy, fq, ago)
            if not any(abs(v - want) <= 1e-6 * max(abs(want), 1) for v in cited_values):
                problems.append(f"missing {metric} (quarters_ago={ago}) = {want:,.4f}")
        if case["needs_passage"] and not any(i.startswith("S") for i in cited):
            problems.append("no document passage cited for a 'why' question")
        status = "PASS" if not problems else "FAIL"
        failures += bool(problems)
        print(f"[{status}] {time.time() - t0:5.1f}s  {case['q'][:78]}")
        for p in problems:
            print("        -", p)
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
