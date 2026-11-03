"""Static charts (PNG bytes) for the report and UI. Palette follows the FinRobot logo."""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

TEAL, GREEN, INK, GREY, RED = "#0e8a99", "#7aa01c", "#1b1f23", "#9aa4ad", "#c0392b"


def _style(ax, title: str) -> None:
    ax.set_title(title, loc="left", fontsize=10.5, fontweight="bold", color=INK, pad=10)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#d5dade")
    ax.tick_params(colors="#55606a", labelsize=8)
    ax.grid(axis="y", color="#eef1f3", linewidth=0.8)
    ax.set_axisbelow(True)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def bar_line(labels: list[str], bars: list[float], line: list[float | None], title: str, bar_name: str, line_name: str,
             bar_scale: float = 1e9, bar_unit: str = "$B", line_unit: str = "%", hatch_from: int | None = None) -> bytes:
    """Bars (left axis) with a line (right axis). `hatch_from` marks projected periods from that index on."""
    fig, ax = plt.subplots(figsize=(6.4, 2.9))
    vals = [b / bar_scale for b in bars]
    colors = [TEAL if hatch_from is None or i < hatch_from else "#8cc7cf" for i in range(len(vals))]
    ax.bar(labels, vals, color=colors, width=0.62, label=bar_name)
    _style(ax, title)
    ax.set_ylabel(bar_unit, fontsize=8, color="#55606a")
    ax.tick_params(axis="x", rotation=0)
    ax2 = ax.twinx()
    xs = [i for i, v in enumerate(line) if v is not None]
    ax2.plot([labels[i] for i in xs], [line[i] for i in xs], color=GREEN, marker="o", ms=3.5, lw=1.8, label=line_name)
    ax2.set_ylabel(line_unit, fontsize=8, color="#55606a")
    ax2.spines["top"].set_visible(False)
    ax2.tick_params(colors="#55606a", labelsize=8)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7.5, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=2)
    return _png(fig)


def bars(labels: list[str], values: list[float], title: str, unit: str = "$B", scale: float = 1e9, negative_red: bool = True) -> bytes:
    fig, ax = plt.subplots(figsize=(6.4, 2.7))
    v = [x / scale for x in values]
    ax.bar(labels, v, color=[RED if (negative_red and x < 0) else TEAL for x in v], width=0.62)
    _style(ax, title)
    ax.set_ylabel(unit, fontsize=8, color="#55606a")
    ax.axhline(0, color="#d5dade", lw=0.8)
    return _png(fig)


def lines(x: list, series: dict[str, list[float]], title: str, unit: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(6.4, 2.7))
    palette = [TEAL, GREEN, "#e08e0b", "#6c5ce7"]
    for i, (name, ys) in enumerate(series.items()):
        ax.plot(x, ys, color=palette[i % 4], lw=1.8, marker="o", ms=3, label=name)
    _style(ax, title)
    ax.set_ylabel(unit, fontsize=8, color="#55606a")
    if len(series) > 1:
        ax.legend(fontsize=7.5, frameon=False)
    return _png(fig)


def price_line(days: list, closes: list[float], title: str) -> bytes:
    fig, ax = plt.subplots(figsize=(6.4, 2.4))
    ax.plot(days, closes, color=TEAL, lw=1.6)
    _style(ax, title)
    ax.set_ylabel("USD", fontsize=8, color="#55606a")
    fig.autofmt_xdate(rotation=0, ha="center")
    return _png(fig)
