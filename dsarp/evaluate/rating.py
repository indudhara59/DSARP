"""Expert rating of the recommendations.

``sheet`` writes a rating sheet (CSV, opens in any spreadsheet program): one row
per recommended step of a random sample of groups, with what the step does,
from where to where and why, and empty columns for the rater:
``verdict`` (``sensible``, ``questionable`` or ``wrong``) and ``comment``. The
language-model review (if it ran) is kept in a separate file so raters are
not influenced by it.

``agreement`` reads the filled sheets of one or more raters and reports:
the share of steps rated sensible / questionable / wrong per rater and per
smell type, the agreement between raters (Cohen's kappa for two, Fleiss'
kappa for more), and the agreement of each rater with the language-model
review (Cohen's kappa), which shows how far the automatic review can be trusted.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

import pandas as pd

VERDICTS = ("sensible", "questionable", "wrong")


def _step_text(o: dict) -> tuple[str, str, str, str]:
    op = o["op"]
    if op == "move_class":
        return ("move class" + (" into a new package" if o.get("new_package") else ""), o["class"],
                o["from_package"], o["to_package"])
    if op == "move_member":
        what = "move instance method(s)" if o.get("instance") else "move static member(s)"
        return what, ", ".join(m.split("#", 1)[-1] for m in o.get("members", [o["member"]])), o["from_class"], o["to_class"]
    if op == "extract_interface":
        return "extract interface", o["provider"], o["client"], f"interface in {o.get('interface_package', '')}"
    if op == "delete_member":
        return "delete unused deprecated member", o["member"], o.get("owner", ""), ""
    if op == "remove_unused_import":
        return "remove unused import", o["import"], o["class"], ""
    return op, json.dumps(o)[:120], "", ""


def sheet(results: str | Path, n_groups: int = 40, seed: int = 0, per_type: bool = True) -> Path:
    r = Path(results)
    plans = json.loads((r / "plans" / "plans.json").read_text())
    vf = r / "verify" / "verification.json"
    verified = {v["group_id"]: v for v in json.loads(vf.read_text())} if vf.exists() else {}
    groups = []
    for g in plans["groups"]:
        name = (verified.get(g["group_id"]) or {}).get("plan") or g.get("recommended")
        plan = next((p for p in g["plans"] if p["name"] == name), None)
        if plan is None or (verified and verified.get(g["group_id"], {}).get("kept", 1) == 0):
            continue
        groups.append((g, plan))
    rnd = random.Random(seed)
    if per_type:                                   # stratified: every smell type is represented
        by: dict[str, list] = {}
        for x in groups:
            by.setdefault(x[0].get("kind") or x[0]["type"], []).append(x)
        for v in by.values():
            rnd.shuffle(v)
        picked, i = [], 0
        while len(picked) < min(n_groups, len(groups)):
            for v in by.values():
                if i < len(v) and len(picked) < n_groups:
                    picked.append(v[i])
            i += 1
    else:
        picked = rnd.sample(groups, min(n_groups, len(groups)))
    rows, review_rows = [], []
    for g, plan in picked:
        rv = next((t for t in (g.get("review") or {}).get("tried", []) if t.get("plan") == plan["name"]), None)
        for i, o in enumerate(plan["operations"], 1):
            what, element, frm, to = _step_text(o)
            sid = f"{g['group_id']}#{i}"
            rows.append({"step_id": sid, "smell": (g.get("kind") or g["type"]).replace("_", " "),
                         "smell_elements": ", ".join(g.get("core", [])[:6]), "step": i, "what": what,
                         "element": element, "from": frm, "to": to, "why": o.get("reason", ""),
                         "verdict": "", "comment": ""})
            sv = next((x for x in (rv or {}).get("steps", []) if x["step"] == i), None)
            if sv:
                review_rows.append({"step_id": sid, "llm_verdict": sv["verdict"], "llm_reason": sv["reason"]})
    out = r / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "rating_sheet.csv", index=False)
    if review_rows:
        pd.DataFrame(review_rows).to_csv(out / "rating_llm_review.csv", index=False)
    return out / "rating_sheet.csv"


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    if not a:
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return round((po - pe) / (1 - pe), 4) if pe < 1 else 1.0


def fleiss_kappa(table: list[list[str]]) -> float | None:
    """``table``: one list of verdicts (one per rater) per item."""
    if not table:
        return None
    m = len(table[0])
    cats = sorted({x for row in table for x in row})
    n = len(table)
    p_j = {c: sum(row.count(c) for row in table) / (n * m) for c in cats}
    p_i = [(sum(row.count(c) ** 2 for c in cats) - m) / (m * (m - 1)) for row in table]
    pbar, pe = sum(p_i) / n, sum(v * v for v in p_j.values())
    return round((pbar - pe) / (1 - pe), 4) if pe < 1 else 1.0


def agreement(sheets: list[str | Path], llm_review: str | Path | None = None) -> dict:
    raters = []
    for f in sheets:
        df = pd.read_csv(f, keep_default_na=False)
        df["verdict"] = df["verdict"].str.strip().str.lower()
        bad = df[~df["verdict"].isin(VERDICTS)]
        if len(bad):
            raise ValueError(f"{f}: {len(bad)} rows without a verdict (sensible, questionable or wrong), "
                             f"e.g. {bad['step_id'].iloc[0]}")
        raters.append(df.set_index("step_id"))
    common = sorted(set.intersection(*[set(d.index) for d in raters]))
    out = {"raters": len(raters), "steps": len(common), "per_rater": [], "per_smell": {}}
    for f, d in zip(sheets, raters):
        c = Counter(d.loc[common, "verdict"])
        out["per_rater"].append({"sheet": str(f), **{v: c.get(v, 0) for v in VERDICTS},
                                 "sensible_share": round(c.get("sensible", 0) / len(common), 4) if common else None})
    first = raters[0].loc[common]
    for smell, d in first.groupby("smell"):
        c = Counter(d["verdict"])
        out["per_smell"][smell] = {**{v: c.get(v, 0) for v in VERDICTS},
                                   "sensible_share": round(c.get("sensible", 0) / len(d), 4)}
    if len(raters) == 2:
        out["cohen_kappa"] = cohen_kappa(list(raters[0].loc[common, "verdict"]), list(raters[1].loc[common, "verdict"]))
    elif len(raters) > 2:
        out["fleiss_kappa"] = fleiss_kappa([[d.loc[s, "verdict"] for d in raters] for s in common])
    if len(raters) > 1:          # majority verdict (ties: the more critical one)
        order = {"wrong": 0, "questionable": 1, "sensible": 2}
        maj = {s: sorted(Counter(d.loc[s, "verdict"] for d in raters).most_common(),
                         key=lambda kv: (-kv[1], order[kv[0]]))[0][0] for s in common}
        c = Counter(maj.values())
        out["majority"] = {**{v: c.get(v, 0) for v in VERDICTS},
                           "sensible_share": round(c.get("sensible", 0) / len(common), 4) if common else None}
    if llm_review and Path(llm_review).exists():
        lr = pd.read_csv(llm_review, keep_default_na=False).set_index("step_id")
        both = [s for s in common if s in lr.index]
        out["llm_agreement"] = [{"sheet": str(f), "steps": len(both),
                                 "cohen_kappa": cohen_kappa(list(d.loc[both, "verdict"]),
                                                            list(lr.loc[both, "llm_verdict"])),
                                 "same_verdict": round(sum(d.loc[s, "verdict"] == lr.loc[s, "llm_verdict"]
                                                           for s in both) / len(both), 4) if both else None}
                                for f, d in zip(sheets, raters)]
    return out
