"""Statistics for the evaluation: how sure are the numbers?

* **Wilson score interval** (95 %) for every rate: accepted fixes per group, per
  smell type, per evidence level. With tens of groups, a rate without its
  interval says little.
* **Placebo**: for the same groups, random plans of the same size (random
  classes of the group's packages moved to another of its packages) are
  verified through exactly the same gate. If the placebo were accepted as
  often as DSARP's plans, the gate (not the planning) would explain the
  results.
* **McNemar's exact test** on the paired groups: DSARP accepted and the
  placebo not, against the other way round.
* **Calibration**: what the simulation predicted (smell removed) against what
  was measured on the compiled code: agreement, and the Brier score of the
  predicted benefit used as a probability.

``dsarp stats results/<p> [--placebo N]`` writes ``evaluation/stats.md`` and
``evaluation/stats.json``.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from pathlib import Path


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Proportion k/n with its Wilson score interval."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for b (A yes, B no) against c (A no, B yes) discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _fmt(k: int, n: int) -> str:
    p, lo, hi = wilson(k, n)
    return f"{k}/{n} = {p:.0%} (95 % CI {lo:.0%}-{hi:.0%})"


def load_verified(results: Path, folder: str = "verify") -> list[dict]:
    f = results / folder / "verification.json"
    return json.loads(f.read_text()) if f.exists() else []


# ------------------------------------------------------------------ placebo
def placebo_plans(results: Path, groups: int = 30, seed: int = 7) -> dict:
    """Random plans of the same size as the recommended ones, on the same groups (written to placebo/plans.json)."""
    from ..plan.facts import RepoFacts
    from ..program import ProgramModel
    plans = json.loads((results / "plans" / "plans.json").read_text())
    rf = RepoFacts.build(ProgramModel(results / "model"), api_policy="off")
    rnd = random.Random(seed)
    out = []
    for g in [g for g in plans["groups"] if g.get("recommended")][:groups]:
        rec = next(p for p in g["plans"] if p["name"] == g["recommended"])
        n = max(1, sum(1 for o in rec["operations"] if o.get("op") == "move_class") or 1)
        pkgs = [p for p in (g.get("core") or []) if p in rf.pkg_classes] or \
            sorted({rf.package.get(o.get("class")) for o in rec["operations"] if o.get("class")} - {None})
        if not pkgs:
            continue
        ops = []
        for _ in range(n):
            src = rnd.choice(pkgs)
            cls = rnd.choice(sorted(rf.pkg_classes[src]))
            dst_pool = [p for p in pkgs if p != src and rf.pkg_modules.get(p) == rf.pkg_modules.get(src)] or \
                [p for p in rf.pkg_classes if p != src and rf.pkg_modules.get(p) == rf.pkg_modules.get(src)]
            if not dst_pool:
                continue
            ops.append({"op": "move_class", "class": cls, "from_package": src, "to_package": rnd.choice(dst_pool),
                        "reason": "placebo: random move", "check": {"status": "ok", "edits": [], "notes": []}})
        if ops:
            from dataclasses import asdict
            from ..plan.checks import Checker
            bare = [{k: v for k, v in o.items() if k != "check"} for o in ops]
            after = {o["class"]: o["to_package"] for o in bare}
            for o, c in zip(ops, Checker(rf).check(bare, after)):     # the same follow-up edits DSARP's plans get
                o["check"] = {k: v for k, v in asdict(c).items() if k != "index"}
            out.append({**{k: g[k] for k in g if k not in ("plans", "recommended")}, "recommended": "placebo",
                        "plans": [{"name": "placebo", "operations": ops, "status": "placebo"}]})
    pj = {"groups": out, "placebo_seed": seed, "api_policy": plans.get("api_policy"),
          "max_api_breaks": plans.get("max_api_breaks"), "compat": plans.get("compat")}
    (results / "placebo").mkdir(parents=True, exist_ok=True)
    (results / "placebo" / "plans.json").write_text(json.dumps(pj, indent=1))
    return pj


def run_placebo(results: Path, repo: Path, groups: int = 30, seed: int = 7, verifier_kwargs: dict | None = None,
                log=print) -> list[dict]:
    from ..verify.run import Verifier
    placebo_plans(results, groups, seed)
    v = Verifier(results, repo, log=log, out_name="placebo", plans_file=results / "placebo" / "plans.json",
                 learn=False, **(verifier_kwargs or {}))
    return v.run()


# ------------------------------------------------------------------ the report
def summarise(results: str | Path) -> dict:
    results = Path(results)
    ver = load_verified(results)
    plans = json.loads((results / "plans" / "plans.json").read_text())
    groups = plans["groups"]
    n_groups = len(groups)
    acc = [v for v in ver if v.get("accepted")]
    out = {"groups": n_groups, "verified": len(ver), "accepted": len(acc),
           "accepted_of_groups": wilson(len(acc), n_groups), "accepted_of_verified": wilson(len(acc), len(ver)),
           "levels": dict(Counter(v.get("level") or "?" for v in ver))}
    by_type: dict[str, list[int]] = {}
    for g in groups:
        k = g.get("kind") or g["type"]
        v = next((x for x in ver if x["group_id"] == g["group_id"]), None)
        by_type.setdefault(k, [0, 0])
        by_type[k][1] += 1
        by_type[k][0] += bool(v and v.get("accepted"))
    out["by_type"] = {k: {"accepted": a, "groups": n, "wilson": wilson(a, n)} for k, (a, n) in by_type.items()}
    # calibration: simulated prediction against the compiled code
    pairs = [(bool(v.get("predicted_removed")), bool(v.get("target_removed"))) for v in ver
             if v.get("kept") and v.get("predicted_removed") is not None]
    if pairs:
        agree = sum(a == b for a, b in pairs)
        tp = sum(a and b for a, b in pairs)
        out["calibration"] = {"pairs": len(pairs), "agreement": wilson(agree, len(pairs)),
                              "predicted_removed": sum(a for a, _ in pairs), "measured_removed": sum(b for _, b in pairs),
                              "both": tp}
        probs = []
        bymap = {g["group_id"]: g for g in groups}
        for v in ver:
            g = bymap.get(v["group_id"])
            p = next((x for x in (g or {}).get("plans", []) if x["name"] == v.get("plan")), None)
            if p and p.get("benefit") is not None and v.get("kept"):
                probs.append((min(max(float(p["benefit"]), 0.0), 1.0), 1.0 if v.get("target_removed") else 0.0))
        if probs:
            out["calibration"]["brier_benefit"] = round(sum((a - b) ** 2 for a, b in probs) / len(probs), 4)
    # confidence against acceptance (Brier score, acceptance per confidence level)
    bymap = {g["group_id"]: g for g in groups}
    conf = []
    for v in ver:
        p = next((x for x in bymap.get(v["group_id"], {}).get("plans", []) if x["name"] == v.get("plan")), None)
        c = (p or {}).get("confidence") or {}
        if c.get("value") is not None:
            conf.append((float(c["value"]), c.get("level", "?"), bool(v.get("accepted"))))
    if conf:
        lv = {}
        for _, l, a in conf:
            lv.setdefault(l, [0, 0])
            lv[l][0] += a
            lv[l][1] += 1
        out["confidence"] = {"plans": len(conf), "brier": round(sum((c - a) ** 2 for c, _, a in conf) / len(conf), 4),
                             "by_level": {k: {"accepted": a, "plans": n, "wilson": wilson(a, n)}
                                          for k, (a, n) in lv.items()}}
    # API changes measured with japicmp against the counts on the sources
    api = [((v.get("api_breaks") or {}).get("count", 0), v["api_measured"]["count"]) for v in ver
           if (v.get("api_measured") or {}).get("measured")]
    if api:
        out["api"] = {"measured_plans": len(api), "counted_on_sources": sum(a for a, _ in api),
                      "measured": sum(b for _, b in api), "equal": sum(a == b for a, b in api)}
    # placebo and McNemar on the paired groups
    pl = load_verified(results, "placebo")
    if pl:
        pmap = {v["group_id"]: bool(v.get("accepted")) for v in pl}
        dmap = {v["group_id"]: bool(v.get("accepted")) for v in ver}
        both = sorted(set(pmap) & set(dmap))
        b = sum(dmap[g] and not pmap[g] for g in both)
        c = sum(pmap[g] and not dmap[g] for g in both)
        out["placebo"] = {"groups": len(pl), "accepted": sum(pmap.values()),
                          "accepted_rate": wilson(sum(pmap.values()), len(pl)),
                          "levels": dict(Counter(v.get("level") or "?" for v in pl)),
                          "paired": len(both), "dsarp_only": b, "placebo_only": c, "mcnemar_p": mcnemar_exact(b, c)}
    return out


def write(results: str | Path) -> dict:
    results = Path(results)
    s = summarise(results)
    lines = ["# Statistics", "",
             f"- Accepted fixes: {_fmt(s['accepted'], s['groups'])} of all groups; "
             f"{_fmt(s['accepted'], s['verified'])} of verified plans",
             "- Evidence levels: " + (", ".join(f"{k}: {v}" for k, v in sorted(s["levels"].items(), reverse=True))
                                      or "none"), "", "| smell type | accepted / groups | 95 % CI |", "|---|---|---|"]
    for k, v in sorted(s["by_type"].items()):
        p, lo, hi = v["wilson"]
        lines.append(f"| {k} | {v['accepted']}/{v['groups']} | {lo:.0%}-{hi:.0%} |")
    if "calibration" in s:
        c = s["calibration"]
        p, lo, hi = c["agreement"]
        lines += ["", "## Calibration (simulation against the compiled code)", "",
                  f"- Agreement on 'smell removed': {p:.0%} (95 % CI {lo:.0%}-{hi:.0%}) over {c['pairs']} plans; "
                  f"predicted removed {c['predicted_removed']}, measured removed {c['measured_removed']}, "
                  f"both {c['both']}"]
        if "brier_benefit" in c:
            lines.append(f"- Brier score of the predicted benefit as probability of removal: {c['brier_benefit']}")
    if "confidence" in s:
        c = s["confidence"]
        lines += ["", "## Confidence against acceptance", "",
                  f"- Brier score of the plan confidence as probability of acceptance: {c['brier']} "
                  f"({c['plans']} verified plans)"]
        for k in ("high", "medium", "low"):
            if k in c["by_level"]:
                v = c["by_level"][k]
                p, lo, hi = v["wilson"]
                lines.append(f"- {k}: {v['accepted']}/{v['plans']} accepted ({p:.0%}, 95 % CI {lo:.0%}-{hi:.0%})")
    if "api" in s:
        a = s["api"]
        lines += ["", "## API changes (japicmp on accepted fixes)", "",
                  f"- {a['measured_plans']} fixes measured: {a['measured']} binary-incompatible changes measured, "
                  f"{a['counted_on_sources']} counted on the sources; equal for {a['equal']} fixes"]
    if "placebo" in s:
        pl = s["placebo"]
        p, lo, hi = pl["accepted_rate"]
        lines += ["", "## Placebo (random moves of the same size, same gate)", "",
                  f"- Placebo accepted: {pl['accepted']}/{pl['groups']} = {p:.0%} (95 % CI {lo:.0%}-{hi:.0%})",
                  f"- Paired groups: {pl['paired']}; DSARP accepted and placebo not: {pl['dsarp_only']}; "
                  f"placebo accepted and DSARP not: {pl['placebo_only']}; McNemar exact p = {pl['mcnemar_p']:.4f}"]
    out = results / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "stats.md").write_text("\n".join(lines) + "\n")
    (out / "stats.json").write_text(json.dumps(s, indent=1))
    return s
