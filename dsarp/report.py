"""Stage 9 (users): one self-contained HTML report per repository.

Brings together the triage (what was found, what was dropped and why), the
recommended plans with their step-by-step explanation (``plans/report.md``),
the verification on the real code (``verify/``: compiles, operations dropped,
smell before and after, patch) and, if present, the evaluation of the
generators (``evaluation/``). The page has no external resources, so it can be
attached to an issue or opened offline.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pandas as pd

CSS = """
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#1d2125}
h1{font-size:1.7rem}h2{font-size:1.3rem;margin-top:2.2rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}
h3{font-size:1.05rem;margin-top:1.4rem}code{background:#f3f4f6;padding:0 .25rem;border-radius:3px;font-size:.9em}
table{border-collapse:collapse;margin:.8rem 0;font-size:.9rem}td,th{border:1px solid #ddd;padding:.3rem .6rem;text-align:left}
th{background:#f6f7f9}.ok{color:#11743b;font-weight:600}.bad{color:#b42318;font-weight:600}
.cards{display:flex;gap:1rem;flex-wrap:wrap}.card{border:1px solid #ddd;border-radius:6px;padding:.7rem 1rem;min-width:150px}
.card b{display:block;font-size:1.5rem}blockquote{border-left:3px solid #ccc;margin:0;padding-left:1rem;color:#444}
details{margin:.5rem 0}pre{background:#f6f7f9;padding:.7rem;overflow:auto;font-size:.8rem;max-height:30rem}
"""


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)


def markdown(md: str) -> str:
    """Small Markdown subset used by the explanations: headings, lists, tables, quotes, paragraphs."""
    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#"):
            level = min(len(ln) - len(ln.lstrip("#")) + 1, 4)
            out.append(f"<h{level}>{_inline(ln.lstrip('#').strip())}</h{level}>")
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.match(r"^\|[-| ]+\|$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            out.append("<table>" + "".join(
                "<tr>" + "".join(f"<{'th' if k == 0 else 'td'}>{_inline(c)}</{'th' if k == 0 else 'td'}>" for c in r)
                + "</tr>" for k, r in enumerate(rows)) + "</table>")
            continue
        elif ln.lstrip().startswith("- "):
            items = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                depth = len(lines[i]) - len(lines[i].lstrip())
                items.append((depth, lines[i].lstrip()[2:]))
                i += 1
            html_items, cur = [], 0
            for depth, txt in items:
                d = 1 if depth else 0
                while cur < d:
                    html_items.append("<ul>")
                    cur += 1
                while cur > d:
                    html_items.append("</ul>")
                    cur -= 1
                html_items.append(f"<li>{_inline(txt)}</li>")
            html_items += ["</ul>"] * cur
            out.append("<ul>" + "".join(html_items) + "</ul>")
            continue
        elif ln.startswith(">"):
            out.append(f"<blockquote>{_inline(ln[1:].strip())}</blockquote>")
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    return "\n".join(out)


def _table(df: pd.DataFrame, cols: list[str] | None = None) -> str:
    if df is None or df.empty:
        return "<p>none</p>"
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = "".join("<tr>" + "".join(f"<td>{_inline(str(v))}</td>" for v in row) + "</tr>"
                   for row in df.itertuples(index=False))
    return f"<table><tr>{head}</tr>{body}</table>"


def _read(path: Path):
    if not path.exists():
        return None
    if path.suffix == ".json":
        return json.loads(path.read_text())
    if path.suffix == ".csv":
        return pd.read_csv(path, keep_default_na=False)
    return path.read_text()


def build(results: str | Path) -> str:
    r = Path(results)
    model = _read(r / "model" / "model.json") or {}
    triage = _read(r / "triage" / "triage.json") or {}
    plans = _read(r / "plans" / "plans.json") or {"groups": []}
    report_md = _read(r / "plans" / "report.md")
    verify = _read(r / "verify" / "verification.json") or []
    evaluation = _read(r / "evaluation" / "summary.csv")
    name = model.get("repo") or r.name
    parts = [f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>DSARP report: {html.escape(name)}"
             f"</title><style>{CSS}</style></head><body>",
             f"<h1>Architectural refactoring report: {html.escape(name)}</h1>",
             f"<p>Commit <code>{html.escape(str(model.get('commit', '')))}</code>, "
             f"DSARP {html.escape(str(model.get('dsarp_version', '')))}.</p>"]
    groups = plans.get("groups", [])
    rec = sum(1 for g in groups if g.get("recommended"))
    compiled = sum(1 for v in verify if v.get("compiles") and v.get("kept", 0) > 0)
    removed = sum(1 for v in verify if v.get("target_removed"))
    decisions = triage.get("decisions", {}) if isinstance(triage, dict) else {}
    cards = [("smells reported", triage.get("smells", "") if isinstance(triage, dict) else ""),
             ("kept after triage", decisions.get("keep", "")), ("groups planned", len(groups)),
             ("with a plan", rec), ("plans that compile", f"{compiled}/{len(verify)}" if verify else "not verified"),
             ("smell removed (verified)", removed if verify else "-")]
    parts.append("<div class='cards'>" + "".join(f"<div class='card'><b>{html.escape(str(v))}</b>{k}</div>"
                                                for k, v in cards) + "</div>")
    parts.append("<h2>Overview</h2>")
    ov = []
    vmap = {v["group_id"]: v for v in verify}
    for g in groups:
        p = next((x for x in g["plans"] if x["name"] == g.get("recommended")), None)
        v = vmap.get(g["group_id"], {})
        ov.append({"rank": g["rank"], "group": g["group_id"], "smell": g["type"], "packages": len(g.get("core", [])),
                   "plan": g.get("recommended") or g.get("skipped") or "none",
                   "steps": len(p["operations"]) if p else 0, "score": p.get("score") if p else "",
                   "compiles": (("yes" if v.get("kept", 0) else "no: all steps dropped")
                                if v.get("compiles") else "no") if v else "-",
                   "kept steps": f"{v['kept']}/{v['operations']}" if v else "-",
                   "smell after (verified)": json.dumps(v.get("target_after", "")) if v else "-"})
    parts.append(_table(pd.DataFrame(ov)))
    if report_md:
        parts.append("<h2>Plans step by step</h2>")
        body = report_md.split("\n", 2)[-1]
        parts.append(markdown(body))
    if verify:
        parts.append("<h2>Verification on the code</h2>")
        parts.append("<p>Each plan was applied to a clean copy of the code and compiled. Steps that caused new "
                     "compiler errors were found by delta debugging and dropped; the smells were then detected "
                     "again on the compiled result and compared with the prediction of the simulation.</p>")
        for v in verify:
            ok = bool(v.get("compiles") and v.get("kept", 0))
            parts.append(f"<h3>{html.escape(v['group_id'])}</h3><ul>"
                         f"<li>compiles: <span class='{'ok' if ok else 'bad'}'>"
                         f"{'yes' if ok else 'no: every step broke the build and was dropped' if v.get('compiles') else 'no'}"
                         f"</span>, kept {v['kept']} of {v['operations']} steps "
                         f"({html.escape(str(v.get('reduction')))}, {v.get('compile_runs')} compiler runs)</li>"
                         f"<li>smell before: <code>{html.escape(json.dumps(v.get('target_before')))}</code>, after: "
                         f"<code>{html.escape(json.dumps(v.get('target_after')))}</code>; simulation predicted "
                         f"<code>{html.escape(json.dumps(v.get('predicted_after')))}</code></li>")
            if "build" in v:
                b = v["build"]
                parts.append(f"<li>build ({html.escape(b['tool'])}, {b['tests_selected']} tests selected): "
                             f"<span class='{'ok' if b['ok'] else 'bad'}'>{'passed' if b['ok'] else 'failed'}</span>"
                             f" (baseline {'passed' if b['baseline_ok'] else 'failed'})</li>")
            parts.append("</ul>")
            if v.get("dropped_operations"):
                parts.append("<p>Dropped steps (they broke the build):</p>" + _table(pd.DataFrame(
                    [{"op": o["op"], "element": o.get("class") or o.get("member") or o.get("client"),
                      "to": o.get("to_package", "")} for o in v["dropped_operations"]])))
            patch = _read(r / "verify" / v["group_id"] / "plan.patch")
            if patch:
                parts.append(f"<details><summary>Patch ({patch.count(chr(10))} lines, "
                             f"<code>verify/{html.escape(v['group_id'])}/plan.patch</code>)</summary>"
                             f"<pre>{html.escape(patch[:60000])}</pre></details>")
    if evaluation is not None and not evaluation.empty:
        parts.append("<h2>Evaluation of the plan generators</h2>")
        parts.append(_table(evaluation))
    parts.append("<p style='color:#666;margin-top:3rem'>Generated by DSARP.</p></body></html>")
    return "\n".join(parts)


def write(results: str | Path) -> Path:
    p = Path(results) / "report.html"
    p.write_text(build(results), encoding="utf-8")
    return p
