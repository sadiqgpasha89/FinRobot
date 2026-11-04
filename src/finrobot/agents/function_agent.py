"""Function-calling assistant: turns requests into artifacts (charts) via typed tools."""
from __future__ import annotations

from finrobot.agents.context import RunContext
from finrobot.llm.tools import Tool
from finrobot.tools import charts


def function_tools(ctx: RunContext) -> list[Tool]:
    def make_chart(metric: str, quarters: int = 8) -> dict:
        series = list(reversed(ctx.metrics.series(metric, min(max(int(quarters), 2), 12), up_to=ctx.period_end)))
        if len(series) < 2:
            return {"error": f"not enough data to chart {metric}"}
        labels = [f.label.split(" ", 2)[-1] for f in series]
        name = f"chart_{metric}.png"
        unit = series[0].unit
        if unit == "USD":
            ctx.artifacts[name] = charts.bars(labels, [f.value for f in series], f"{ctx.ticker} {metric.replace('_', ' ')}, quarterly")
        else:
            ctx.artifacts[name] = charts.lines(labels, {metric: [f.value for f in series]}, f"{ctx.ticker} {metric.replace('_', ' ')}", unit)
        return {"artifact": name, "periods": labels}

    return [Tool("make_chart", "Render a quarterly trend chart of a reported metric (e.g. revenue, operating_income). "
                 "Returns the artifact name; it is attached to the answer automatically.",
                 {"type": "object", "properties": {"metric": {"type": "string"}, "quarters": {"type": "integer"}}, "required": ["metric"]},
                 make_chart)]
