"""Math agent: exact arithmetic as tools. It is deliberately code, not an LLM — numbers must be computed, not recalled."""
from __future__ import annotations

from finrobot.agents.context import RunContext
from finrobot.llm.tools import Tool
from finrobot.tools.calc import CalcError, calc_and_register
from finrobot.tools.metrics import METRIC_NAMES, MissingData, shift


def math_tools(ctx: RunContext) -> list[Tool]:
    def compute_metric(name: str, quarters_ago: int = 0) -> dict:
        fy, fq = shift(ctx.fy, ctx.fq, -int(quarters_ago))
        try:
            f = ctx.metrics.compute(name, fy, fq)
        except MissingData as e:
            return {"error": str(e)}
        inputs = [{"id": i, "label": ctx.fb.facts[i].label, "display": ctx.fb.facts[i].display} for i in f.inputs]
        return {"id": f.id, "label": f.label, "display": f.display, "value": f.value, "unit": f.unit, "computed_from": inputs}

    def calc(expression: str, label: str = "") -> dict:
        try:
            f = calc_and_register(expression, ctx.fb, label)
        except CalcError as e:
            return {"error": str(e)}
        return {"id": f.id, "label": f.label, "display": f.display, "value": f.value, "unit": f.unit}

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    return [
        Tool("compute_metric", "Compute a standard metric from reported data, with its inputs cited. "
             f"name is one of: {', '.join(METRIC_NAMES)}. quarters_ago is relative to the focus quarter: 0 = the focus quarter, "
             "1 = the previous quarter, 4 = the same quarter a year earlier, 8 = two years earlier.",
             obj({"name": {"type": "string"}, "quarters_ago": {"type": "integer"}}, ["name"]), compute_metric),
        Tool("calc", "Exact arithmetic over registered fact ids. expression uses ids like F3 and + - * / ** parentheses. "
             "The result's unit is worked out for you: (F3 - F5) / F5 * 100 is a percent change; a difference of two percentages "
             "is in percentage points (multiply by 100 for basis points). Returns a new fact id to cite.",
             obj({"expression": {"type": "string"}, "label": {"type": "string"}}, ["expression"]), calc),
    ]
