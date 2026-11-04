"""Renders the diagrams used in the Medium article to PNG (Medium does not render Mermaid).

    python docs/medium/build_diagrams.py            # needs Graphviz's `dot` and matplotlib

Structure diagrams use Graphviz; sequence diagrams are drawn with matplotlib. The same flows exist as Mermaid in docs/architecture.md and
docs/sdr.md (rendered by GitHub); keep the two in step when a flow changes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

OUT = Path(__file__).parent / "images"
INK, MUTED, TEAL, GREEN, AMBER, RED, LINE = "#1f2937", "#6b7280", "#0e8a99", "#7aa01c", "#d97706", "#c0392b", "#cbd5e1"
TINT = {"teal": "#e3f3f5", "green": "#eef5dc", "amber": "#fdf1dc", "gray": "#f3f4f6", "red": "#fbe9e7", "white": "#ffffff"}
FONT = "DejaVu Sans"

DOT_HEAD = f'''digraph G {{
  graph [fontname="{FONT}", bgcolor="white", pad=0.35, nodesep=0.35, ranksep=0.55, dpi=190, compound=true, splines=spline];
  node  [fontname="{FONT}", fontsize=12, shape=box, style="rounded,filled", fillcolor="{TINT['gray']}", color="{LINE}", fontcolor="{INK}", penwidth=1.3, margin="0.18,0.10"];
  edge  [fontname="{FONT}", fontsize=10, color="{MUTED}", fontcolor="{MUTED}", arrowsize=0.8, penwidth=1.2];
'''


def dot(name: str, body: str) -> None:
    src = OUT / f"{name}.dot"
    src.write_text(DOT_HEAD + body + "\n}\n")
    subprocess.run(["dot", "-Tpng", str(src), "-o", str(OUT / f"{name}.png")], check=True)  # noqa: S603, S607
    src.unlink()


def node(i: str, label: str, tint: str = "gray", **kw: str) -> str:
    extra = "".join(f', {k}="{v}"' for k, v in kw.items())
    return f'  {i} [label="{label}", fillcolor="{TINT[tint]}"{extra}];\n'


def cluster(i: str, label: str, tint: str, inner: str) -> str:
    return (f'  subgraph cluster_{i} {{ label="{label}"; labeljust=l; fontname="{FONT}"; fontsize=13; fontcolor="{INK}"; style="rounded,filled"; '
            f'fillcolor="{TINT[tint]}"; color="{LINE}"; margin=14;\n{inner}  }}\n')


# ------------------------------------------------------------------------------------------------------------------ structure
def architecture() -> None:
    b = ""
    b += '  rankdir=TB; newrank=true;\n'
    b += cluster("clients", "Clients", "white", node("ui", "Streamlit UI", "white") + node("cli", "CLI", "white") + node("http", "HTTP clients", "white"))
    b += cluster("svc", "Service (FastAPI, one image)", "gray",
                 node("api", "API\\nauth · validation · 202 + run id", "white") + node("svcn", "Run service\\ntrace · usage · scores · status", "white")
                 + node("queue", "Bounded job queue\\n(one GPU lane)", "white"))
    b += cluster("lg", "Orchestration: LangGraph", "teal",
                 node("qa", "qa_graph\\nroute → Data → Concept → Thesis → merge", "white") + node("rep", "report_graph\\ndraft ⏸ → model → write×8 → ground → render", "white")
                 + node("chk", "checked agent  (subgraph)\\ndraft → verify → retry → sanitize", "white") + node("react", "tool loop  (subgraph)\\nmodel ⇄ tools", "white"))
    b += cluster("code", "Deterministic layer (code, not model)", "green",
                 node("fb", "FactBook\\nF# facts · S# passages", "white") + node("tools", "Tools\\ncompute_metric · calc · search_docs\\nDCF · charts · tables", "white")
                 + node("ev", "Evaluator\\nnumbers tie to facts\\nperiods match", "white"))
    b += cluster("llm", "LLM gateway: LangChain", "amber",
                 node("gw", "LLMGateway\\nroles: figures · narrative\\nretry · lane · structured output", "white") + node("mdl", "Ollama (default)\\nAnthropic / OpenAI (optional)", "white"))
    b += cluster("data", "Data", "gray",
                 node("pg", "Postgres + pgvector\\nfacts · chunks · runs · checkpoints", "white", shape="cylinder") + node("s3", "S3-compatible store\\nraw filings · PDFs", "white", shape="cylinder")
                 + node("sec", "SEC EDGAR\\nXBRL · 10-K · 10-Q · 8-K", "white"))
    b += cluster("obs", "Observability (optional)", "white",
                 node("lf", "Langfuse\\ntraces · sessions · scores", "teal") + node("ls", "LangSmith\\ntraces · feedback · experiments", "teal") + node("pr", "Prometheus\\n/metrics", "teal"))
    for a, c in (("ui", "api"), ("http", "api"), ("cli", "svcn"), ("api", "queue"), ("queue", "svcn"), ("svcn", "qa"), ("svcn", "rep"), ("qa", "chk"), ("rep", "chk"),
                 ("chk", "react"), ("react", "tools"), ("react", "gw"), ("tools", "fb"), ("chk", "ev"), ("ev", "fb"), ("gw", "mdl"), ("tools", "pg"), ("rep", "pg"), ("svcn", "pg"),
                 ("svcn", "s3"), ("sec", "pg")):
        b += f"  {a} -> {c};\n"
    for target in ("lf", "ls"):
        b += f'  svcn -> {target} [style=dashed];\n'
    b += '  api -> pr [style=dashed];\n'
    dot("architecture", b)


def qa_graph() -> None:
    b = "  rankdir=TB;\n"
    b += node("s", "question", "white", shape="oval")
    b += node("route", "route\\nticker · fiscal period parsed in code", "teal")
    b += node("data", "Data-CoT\\nfigures · comparisons · consensus", "teal")
    b += node("concept", "Concept-CoT\\ncauses, only from retrieved passages", "teal")
    b += node("thesis", "Thesis-CoT\\nstory · what to watch · grounding check", "teal")
    b += node("merge", "merge\\n+ sources + final evaluation", "green")
    b += node("e", "cited, verified answer", "white", shape="oval")
    b += '  s -> route -> data;\n'
    b += '  data -> concept [label="  asks why / outlook"];\n'
    b += '  data -> merge [label="  figure question,\\n  or no verified figure", style=dashed];\n'
    b += '  concept -> thesis -> merge -> e;\n'
    dot("qa-graph", b)


def verify_loop() -> None:
    b = "  rankdir=LR;\n"
    inner = (node("draft", "draft\\n(tool loop)", "teal") + node("verify", "verify\\n(deterministic Evaluator)", "green") + node("retry", "retry once\\nissues go back into the prompt", "amber")
             + node("sanitize", "sanitize\\ndelete unverifiable lines", "amber") + node("finish", "finish", "white"))
    b += cluster("checked", "Every agent is this loop", "white", inner)
    b += node("loop", "model ⇄ tools\\n• tools withheld on the last step\\n• malformed tool call → nudge\\n• tool error → returned to the model", "white")
    b += '  draft -> verify;\n  verify -> finish [label="  ok"];\n  verify -> retry [label="  issues,\\n  first attempt"];\n  retry -> draft;\n'
    b += '  verify -> sanitize [label="  issues after retry"];\n  sanitize -> finish;\n  draft -> loop [style=dashed, label="  is"];\n'
    b += node("np", "wrong quarter after retry\\n→ “no verified answer”", "red")
    b += '  finish -> np [style=dashed, label="  wrong period"];\n'
    dot("verify-loop", b)


def report_graph() -> None:
    b = "  rankdir=LR;\n"
    b += node("s", "ticker", "white", shape="oval")
    b += node("draft", "draft\\nassumptions from\\ntrailing data", "teal")
    b += node("hold", "⏸ interrupt\\nhuman reviews / edits", "amber")
    b += node("model", "model\\ntables · charts · DCF", "teal")
    b += node("write", "write ONE section\\n(checked agent)", "teal")
    b += node("ground", "ground\\nqualitative claims", "teal")
    b += node("render", "render\\nHTML → PDF\\n+ format check", "green")
    b += node("pg", "Postgres\\ncheckpoint after every step", "white", shape="cylinder")
    b += '  s -> draft -> hold [label="  review = true"];\n'
    b += '  hold -> hold [label="  invalid edit:\\n  pause again\\n  with the reason", color="#d97706", fontcolor="#d97706"];\n'
    b += '  hold -> model [label="  valid edits"];\n  draft -> model [label="  review = false", style=dashed];\n'
    b += '  model -> write -> write [label="  next of 8"];\n  write -> ground [label="  all written"];\n  ground -> render;\n'
    b += '  write -> pg [style=dotted, arrowhead=none];\n  draft -> pg [style=dotted, arrowhead=none];\n'
    dot("report-graph", b)


def join_keys() -> None:
    b = "  rankdir=LR;\n"
    b += node("rid", "run id\\n20261105-025723-f4819", "amber")
    b += node("row", "run row (Postgres)\\nevents · usage · trace_id", "white", shape="cylinder")
    b += node("lfid", "Langfuse trace id\\ncreate_trace_id(seed = run id)", "teal")
    b += node("lfs", "Langfuse session\\n= run id", "teal")
    b += node("lsid", "LangSmith root run\\nuuid5(run id)", "teal")
    b += node("sc", "scores\\nverification_ok · figures_tied_ratio\\nlines_removed · agent_retries · format_ok", "green")
    b += '  rid -> row;\n  rid -> lfid;\n  rid -> lfs;\n  rid -> lsid;\n  lfid -> sc;\n  lsid -> sc [label="  as feedback"];\n  row -> lfid [style=dashed, label="  trace_id", constraint=false];\n'
    dot("join-keys", b)


# ------------------------------------------------------------------------------------------------------------------ sequence diagrams
def sequence(name: str, title: str, actors: list[tuple[str, str]], steps: list[tuple], width: float = 13.0) -> None:
    """steps: ('msg', a, b, text) | ('ret', a, b, text) | ('self', a, text) | ('note', a, b, text, tint) | ('open', label) | ('close',) | ('gap',)"""
    xs = {k: i * (width / (len(actors) - 0.4)) + 0.9 for i, (k, _) in enumerate(actors)}
    row = 0.62
    h = 1.6 + row * (sum(1 for s in steps if s[0] != "close") + 1)
    fig, ax = plt.subplots(figsize=(width, h), dpi=170)
    ax.set_xlim(0, width + 0.9)
    ax.set_ylim(-h + 1.2, 1.2)
    ax.axis("off")
    ax.text(0.15, 0.95, title, fontsize=15, fontweight="bold", color=INK, va="center")
    for k, label in actors:
        ax.add_patch(FancyBboxPatch((xs[k] - 0.78, -0.55), 1.56, 0.62, boxstyle="round,pad=0.02,rounding_size=0.1", fc=TINT["teal"], ec=TEAL, lw=1.4))
        ax.text(xs[k], -0.24, label, ha="center", va="center", fontsize=9.5, color=INK, fontweight="bold")
    y, n, frames = -1.05, 0, []
    body = []
    for s in steps:
        if s[0] == "open":
            frames.append((s[1], y + row * 0.45, None))
            y -= row * 0.55
            continue
        if s[0] == "close":
            label, top, _ = frames.pop()
            body.append(("frame", label, top, y + row * 0.25))
            y -= row * 0.2
            continue
        body.append((*s, y))
        y -= row
    bottom = y + row * 0.4
    for k, _ in actors:
        ax.plot([xs[k], xs[k]], [-0.55, bottom], color=LINE, lw=1.1, ls=(0, (4, 4)), zorder=0)
    for s in body:
        kind = s[0]
        if kind == "frame":
            _, label, top, bot = s
            x0, x1 = min(xs.values()) - 0.9, max(xs.values()) + 0.9
            ax.add_patch(FancyBboxPatch((x0, bot), x1 - x0, top - bot, boxstyle="round,pad=0.0,rounding_size=0.08", fc="none", ec=AMBER, lw=1.2, ls="--", zorder=1))
            ax.text(x0 + 0.1, top - 0.1, label, fontsize=8.5, color=AMBER, va="top", fontweight="bold")
        elif kind in ("msg", "ret"):
            _, a, b2, text, yy = s
            n += 1 if kind == "msg" else 0
            x0, x1 = xs[a], xs[b2]
            ax.annotate("", xy=(x1, yy), xytext=(x0, yy), arrowprops=dict(arrowstyle="->", color=INK if kind == "msg" else MUTED, lw=1.3, ls="-" if kind == "msg" else "--"))
            ax.text((x0 + x1) / 2, yy + 0.09, (f"{n}. " if kind == "msg" else "") + text, ha="center", va="bottom", fontsize=8.4, color=INK if kind == "msg" else MUTED)
        elif kind == "self":
            _, a, text, yy = s
            n += 1
            x = xs[a]
            ax.plot([x, x + 0.45, x + 0.45, x], [yy + 0.12, yy + 0.12, yy - 0.12, yy - 0.12], color=INK, lw=1.2)
            ax.annotate("", xy=(x, yy - 0.12), xytext=(x + 0.2, yy - 0.12), arrowprops=dict(arrowstyle="->", color=INK, lw=1.2))
            ax.text(x + 0.55, yy, f"{n}. {text}", ha="left", va="center", fontsize=8.4, color=INK)
        elif kind == "note":
            _, a, b2, text, tint, yy = s
            x0, x1 = min(xs[a], xs[b2]) - 0.7, max(xs[a], xs[b2]) + 0.7
            ax.add_patch(FancyBboxPatch((x0, yy - 0.22), x1 - x0, 0.44, boxstyle="round,pad=0.01,rounding_size=0.06", fc=TINT[tint], ec=LINE, lw=1.0, zorder=3))
            ax.text((x0 + x1) / 2, yy, text, ha="center", va="center", fontsize=8.4, color=INK, zorder=4, fontweight="bold" if tint in ("red", "amber") else "normal")
    fig.savefig(OUT / f"{name}.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def seq_ask() -> None:
    actors = [("c", "Client"), ("a", "API"), ("q", "Job queue"), ("s", "Run service"), ("g", "qa_graph"), ("l", "LLM gateway"), ("d", "Postgres"), ("t", "Langfuse /\nLangSmith")]
    steps = [
        ("msg", "c", "a", "POST /ask  (x-api-key)"), ("msg", "a", "d", "INSERT run (running)"), ("msg", "a", "q", "submit(job)"), ("ret", "a", "c", "202 {run_id}"),
        ("msg", "q", "s", "execute_qa(run_id)"), ("msg", "s", "t", "open trace  id = f(run id), session = run id"),
        ("msg", "s", "g", "invoke(question, callbacks)"), ("msg", "g", "l", "route  (structured output)"), ("msg", "g", "d", "resolve company, fiscal period"),
        ("open", "per agent: Data-CoT → Concept-CoT → Thesis-CoT"),
        ("msg", "g", "l", "draft with tools"), ("msg", "l", "d", "register facts F# and passages S#"),
        ("self", "g", "verify · retry once · sanitize"), ("msg", "g", "d", "INSERT run_event"),
        ("close",),
        ("ret", "g", "s", "answer + FactBook + evaluation"), ("msg", "s", "t", "scores: verification_ok, figures_tied_ratio, ..."),
        ("msg", "s", "d", "UPDATE run (done, result, usage, trace_id)"), ("msg", "c", "a", "GET /runs/{id}?after=cursor"), ("ret", "a", "c", "new events, status, result"),
    ]
    sequence("seq-ask", "Asking a question", actors, steps)


def seq_report() -> None:
    actors = [("c", "Client"), ("a", "API"), ("s", "Run service"), ("g", "report_graph"), ("k", "Checkpointer\n(Postgres)"), ("l", "LLM gateway")]
    steps = [
        ("msg", "c", "a", "POST /reports {review: true}"), ("ret", "a", "c", "202 {run_id}"), ("msg", "s", "g", "invoke(thread_id = run id)"),
        ("self", "g", "draft assumptions from trailing data"), ("msg", "g", "k", "checkpoint"), ("ret", "g", "s", "interrupt(draft)  → status awaiting_review"),
        ("msg", "c", "a", "POST /runs/{id}/resume {wacc: 0.01}"), ("ret", "g", "s", "invalid: WACC ≤ terminal growth → interrupt again, with the reason"),
        ("msg", "c", "a", "POST /runs/{id}/resume {wacc: 0.09}"), ("self", "g", "model: tables, charts, DCF"),
        ("open", "one section per step (8)"), ("msg", "g", "l", "write (tool loop · verify · retry · sanitize)"), ("msg", "g", "k", "checkpoint: prose, FactBook snapshot"), ("close",),
        ("note", "g", "k", "process dies during section 3", "red"),
        ("self", "a", "restart: running runs → interrupted"), ("msg", "c", "a", "POST /runs/{id}/retry"), ("msg", "s", "g", "invoke(None, thread_id)  in a NEW process"),
        ("msg", "g", "k", "load last checkpoint (sections 1–2 done)"), ("self", "g", "rebuild context + model from the database"),
        ("msg", "g", "l", "write sections 3 … 8 only"), ("self", "g", "ground · render PDF · format check"), ("ret", "g", "s", "result + artifacts → done"),
    ]
    sequence("seq-report", "A report with review, a crash and a resume", actors, steps, width=12.5)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for fn in (architecture, qa_graph, verify_loop, report_graph, join_keys, seq_ask, seq_report):
        fn()
        print("built", fn.__name__)


if __name__ == "__main__":
    main()
