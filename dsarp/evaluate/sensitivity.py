"""Sensitivity of the recommendations to DSARP's hand-set weights and thresholds.

Two analyses, both written to ``results/<project>/evaluation/``:

1. **Score weights** (cheap, all groups). Every plan in ``plans.json`` stores
   the parts of its score (benefit, new smells, other smells removed, change
   cost, widened members, public API changed), so the plans of every group can
   be ranked again with each weight halved, doubled and set to zero. The
   result is how often the recommended plan stays the same, and how many
   groups gain or lose a recommendation.

2. **Thresholds** (re-planning, a sample of groups). Constants such as the fit
   rule (``MIN_FIT``, ``FIT_RATIO``), the API limit, the size of a package that
   may be merged and the new-package cost are changed one at a time and the
   sampled groups are planned again. Compared: whether the group still gets a
   plan, and how similar the operations of the recommended plan are (Jaccard
   similarity of the moved classes and members).

A recommendation that changes with small changes of a weight is fragile; the
tables show which parameters the results depend on.
"""

from __future__ import annotations

import importlib
import json
import math
import random
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

WEIGHT_KEYS = ("new_smell", "side_removed", "cost", "made_public", "api_break")
FACTORS = (0.0, 0.5, 2.0)
THRESHOLDS = {
    "MIN_FIT": (0.025, 0.1),
    "FIT_RATIO": (0.15, 0.6),
    "NEW_PACKAGE_COST": (1.5, 6.0),
    "MERGE_MAX_TYPES": (4, 16),
    "CORE_API_MIN_USERS": (10, 40),
}
PLAN_MODULES = ("dsarp.plan.facts", "dsarp.plan.restructure", "dsarp.plan.classlevel", "dsarp.plan.run",
                "dsarp.plan.extra", "dsarp.plan.generator", "dsarp.plan.checks")


def rescore(p: dict, w: dict) -> float | None:
    """The score of a stored plan under weights ``w`` (None for plans without a score)."""
    if "benefit" not in p:
        return None
    ch = p.get("checks") or {}
    api = ch.get("api_breaks", 0) or 0
    risky = ch.get("risky_classes", 0) or 0
    return (p["benefit"] - w["new_smell"] * (p.get("new_smells") or 0) - w.get("risky", 0) * risky / (risky + 3)
            + w["side_removed"] * (p.get("other_smells_removed") or 0)
            - w["cost"] * (p.get("cost_normalised") or 0) - w["made_public"] * (ch.get("made_public") or 0)
            - w["api_break"] * api / (api + w["api_scale"]))


def recommend(plans: list[dict], w: dict) -> str | None:
    """The plan the planner would recommend under weights ``w``: best score among plans that are not blocked
    and have a benefit and a positive score."""
    best = None
    for p in plans:
        s = rescore(p, w)
        if s is None or (p.get("checks") or {}).get("status") == "blocked" or p.get("benefit", 0) <= 0 or s <= 0:
            continue
        if best is None or s > best[0]:
            best = (s, p["name"])
    return best[1] if best else None


def weights_analysis(plans_json: dict) -> pd.DataFrame:
    from ..plan.run import WEIGHTS
    groups = [g for g in plans_json.get("groups", []) if g.get("plans")]
    base = {g["group_id"]: recommend(g["plans"], WEIGHTS) for g in groups}
    rows = []
    for key in WEIGHT_KEYS:
        for f in FACTORS:
            w = {**WEIGHTS, key: WEIGHTS[key] * f}
            same = gained = lost = changed = 0
            for g in groups:
                r = recommend(g["plans"], w)
                b = base[g["group_id"]]
                if r == b:
                    same += 1
                elif b is None:
                    gained += 1
                elif r is None:
                    lost += 1
                else:
                    changed += 1
            n = len(groups) or 1
            rows.append({"weight": key, "default": WEIGHTS[key], "factor": f, "value": round(WEIGHTS[key] * f, 4),
                         "groups": len(groups), "same_recommendation": same, "other_plan": changed,
                         "gains_a_plan": gained, "loses_its_plan": lost, "stable_share": round(same / n, 3)})
    return pd.DataFrame(rows)


@contextmanager
def patched(name: str, value):
    """Temporarily set a planning constant in every planning module that has it."""
    old = {}
    for mn in PLAN_MODULES:
        mod = importlib.import_module(mn)
        if hasattr(mod, name):
            old[mn] = getattr(mod, name)
            setattr(mod, name, value)
    try:
        yield bool(old)
    finally:
        for mn, v in old.items():
            setattr(importlib.import_module(mn), name, v)


def _ops_key(p: dict | None) -> set[str]:
    if not p:
        return set()
    out = set()
    for o in p.get("operations", []):
        target = o.get("to_package") or o.get("to_class") or o.get("provider") or ""
        subject = o.get("class") or ",".join(o.get("members") or [o.get("member", "")])
        out.add(f"{o.get('op')}:{subject}->{target}")
    return out


def _jaccard(a: set, b: set) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def sample_groups(plans_json: dict, n: int, seed: int = 1) -> list[str]:
    """Up to ``n`` groups with a plan, spread over the smell types."""
    by_type: dict[str, list[str]] = {}
    for g in plans_json.get("groups", []):
        if g.get("recommended"):
            by_type.setdefault(g.get("kind") or g["type"], []).append(g["group_id"])
    rnd = random.Random(seed)
    for v in by_type.values():
        rnd.shuffle(v)
    out, i = [], 0
    while len(out) < n and any(i < len(v) for v in by_type.values()):
        for v in by_type.values():
            if i < len(v) and len(out) < n:
                out.append(v[i])
        i += 1
    return out


def thresholds_analysis(results: Path, plans_json: dict, groups: list[str], time_limit: float = 30.0,
                        log=print, planner_kwargs: dict | None = None) -> pd.DataFrame:
    from ..plan.run import Planner
    kw = {"time_limit": time_limit, "search": "never", "log": lambda *_: None,
          "api_policy": plans_json.get("api_policy", "balanced"), "compat": plans_json.get("compat", "stubs"),
          **(planner_kwargs or {})}
    stored = {g["group_id"]: g for g in plans_json.get("groups", [])}

    def run_all() -> dict[str, dict]:
        pl = Planner(results / "model", results / "triage", **kw)
        rows = pl.groups[pl.groups["group_id"].isin(groups)]
        return {r["group_id"]: pl.plan_group(r) for _, r in rows.iterrows()}

    log(f"sensitivity: planning {len(groups)} groups with the default thresholds")
    base = run_all()
    rows = []
    for name, values in THRESHOLDS.items():
        for v in values:
            with patched(name, v) as found:
                if not found:
                    continue
                log(f"sensitivity: {name} = {v}")
                out = run_all()
            for gid in groups:
                b, o = base.get(gid, {}), out.get(gid, {})
                bp = next((p for p in b.get("plans", []) if p["name"] == b.get("recommended")), None)
                op = next((p for p in o.get("plans", []) if p["name"] == o.get("recommended")), None)
                rows.append({"parameter": name, "value": v, "group_id": gid,
                             "kind": (stored.get(gid) or {}).get("kind", ""),
                             "plan_before": bool(bp), "plan_after": bool(op),
                             "operations_similarity": round(_jaccard(_ops_key(bp), _ops_key(op)), 3),
                             "benefit_before": (bp or {}).get("benefit"), "benefit_after": (op or {}).get("benefit")})
    return pd.DataFrame(rows)


def summarise_thresholds(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    g = df.groupby(["parameter", "value"])
    return pd.DataFrame({
        "groups": g.size(),
        "still_a_plan": g.apply(lambda x: int((x["plan_before"] & x["plan_after"]).sum())),
        "plan_lost": g.apply(lambda x: int((x["plan_before"] & ~x["plan_after"]).sum())),
        "plan_gained": g.apply(lambda x: int((~x["plan_before"] & x["plan_after"]).sum())),
        "mean_operations_similarity": g["operations_similarity"].mean().round(3),
    }).reset_index()


def run(results: str | Path, replan_groups: int = 0, time_limit: float = 30.0, log=print) -> dict:
    results = Path(results)
    plans_json = json.loads((results / "plans" / "plans.json").read_text())
    out = results / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    wdf = weights_analysis(plans_json)
    wdf.to_csv(out / "sensitivity_weights.csv", index=False)
    summary = {"groups_with_plans": int(wdf["groups"].iloc[0]) if len(wdf) else 0,
               "weights": {f"{r.weight} x{r.factor:g}": r.stable_share for r in wdf.itertuples()}}
    lines = ["# Sensitivity of the recommendations", "",
             "## Score weights (all groups, plans ranked again)", "",
             "| weight | default | x | same plan | other plan | gains a plan | loses its plan | stable |",
             "|---|---|---|---|---|---|---|---|"]
    for r in wdf.itertuples():
        lines.append(f"| {r.weight} | {r.default} | {r.factor:g} | {r.same_recommendation} | {r.other_plan} | "
                     f"{r.gains_a_plan} | {r.loses_its_plan} | {r.stable_share:.0%} |")
    if replan_groups:
        gids = sample_groups(plans_json, replan_groups)
        tdf = thresholds_analysis(results, plans_json, gids, time_limit, log)
        tdf.to_csv(out / "sensitivity_thresholds.csv", index=False)
        ts = summarise_thresholds(tdf)
        ts.to_csv(out / "sensitivity_thresholds_summary.csv", index=False)
        summary["thresholds"] = ts.to_dict("records")
        lines += ["", f"## Thresholds ({len(gids)} sampled groups planned again)", "",
                  "| parameter | value | groups | still a plan | plan lost | plan gained | similar operations |",
                  "|---|---|---|---|---|---|---|"]
        for r in ts.itertuples():
            sim = r.mean_operations_similarity
            lines.append(f"| {r.parameter} | {r.value} | {r.groups} | {r.still_a_plan} | {r.plan_lost} | "
                         f"{r.plan_gained} | {'' if isinstance(sim, float) and math.isnan(sim) else f'{sim:.2f}'} |")
    (out / "sensitivity.md").write_text("\n".join(lines) + "\n")
    (out / "sensitivity.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary
