"""Stage 5a + 6: plan the top groups of a repository, check, simulate, rank, write.

For every selected group (package cycles, unstable dependencies and hub-like
packages; other types are reported with their quick fixes only):

1. build the group facts and the plan model (:mod:`.facts`, :mod:`.generator`);
2. solve the trade-off plans (most removed, least change, alternative, budgets);
3. **L1/L3** apply each plan to the whole repository and detect again
   (:mod:`.simulate`);
4. **L2** check Java preconditions and list follow-up edits (:mod:`.checks`);
5. **L4** score and rank the plans; the best plan that is not blocked is the
   recommendation.

Scoring (explicit weights; phase 5 replaces them with learned ones)::

    score = benefit                      # share of the target smell removed (0..1)
          - 0.25 * new smells            # cycles / hubs / unstable deps introduced elsewhere
          + 0.05 * other smells removed
          - 0.30 * cost / (cost + 20)    # change cost, saturating
          - 0.01 * elements made public

A plan is recommended only if it is not blocked by the checks, removes part
of the target smell and has a positive score; otherwise the group is reported
without a recommendation (the candidates are still listed).

Large groups (more than ``LARGE_GROUP`` packages) are planned with fewer
movable classes and candidate targets when only HiGHS is available, so that
the solver can make progress within the time limit; CP-SAT handles the full
model better (use it on the cluster).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from ..program import ProgramModel
from . import solver as slv
from .checks import Checker, summarise
from .facts import RepoFacts, group_facts
from .generator import Plan, PlanModel
from .simulate import Simulator, hub_data, unstable_targets

SUPPORTED = {("cyclic_dependency", "package"), ("unstable_dependency", "package"),
             ("hub_like_dependency", "package")}
LARGE_GROUP = 25
WEIGHTS = {"new_smell": 0.25, "side_removed": 0.05, "cost": 0.30, "cost_scale": 20.0, "made_public": 0.01}


def _benefit(sim, smell_type: str) -> float:
    b, a = sim.target_before, sim.target_after
    if not b.get("present", False):
        return 0.0
    if not a.get("present", False):
        return 1.0
    if smell_type == "cyclic_dependency":
        # share of core packages taken off the cycle, or share of the cycle-breaking
        # work done (fewer dependencies left to cut), whichever is larger; a cycle
        # that still exists counts at most 0.9
        nb, na = b["core_packages_on_cycle"], a["core_packages_on_cycle"]
        fb, fa = b.get("dependencies_to_cut", 0), a.get("dependencies_to_cut", 0)
        pk = (nb - na) / nb if nb else 0.0
        fs = (fb - fa) / fb if fb else 0.0
        return round(max(0.0, pk, fs) * 0.9, 4)
    if smell_type == "unstable_dependency":
        nb, na = len(b.get("less_stable", [])), len(a.get("less_stable", []))
        return max(0.0, (nb - na) / nb) * (1.0 if na == 0 else 0.9) if nb else 0.0
    if smell_type == "hub_like_dependency":
        tb = b.get("fan_in", 0) + b.get("fan_out", 0)
        ta = a.get("fan_in", 0) + a.get("fan_out", 0)
        return max(0.0, (tb - ta) / tb) * 0.9 if tb else 0.0
    return 0.0


def score(plan: Plan, sim, check_summary: dict, smell_type: str) -> dict:
    w = WEIGHTS
    benefit = _benefit(sim, smell_type)
    new = sum(1.0 if s["type"] != "cyclic_dependency" else min(1.0 + s["size"] / 10, 3.0)
              for s in sim.new_smells)
    side = max(len(sim.removed_smells) - (1 if benefit == 1.0 else 0), 0)
    cost_n = plan.change_cost / (plan.change_cost + w["cost_scale"]) if plan.change_cost else 0.0
    total = (benefit - w["new_smell"] * new + w["side_removed"] * side - w["cost"] * cost_n
             - w["made_public"] * check_summary.get("made_public", 0))
    return {"score": round(total, 4), "benefit": round(benefit, 4), "new_smells": round(new, 3),
            "other_smells_removed": side, "cost_normalised": round(cost_n, 4)}


class Planner:
    def __init__(self, model_dir: str | Path, triage_dir: str | Path, backend: str = "auto",
                 time_limit: float = 60.0, log=print):
        self.log = log
        t0 = time.time()
        self.model = ProgramModel(model_dir)
        self.rf = RepoFacts.build(self.model)
        self.sim = Simulator(self.rf)
        self.checker = Checker(self.rf)
        triage_dir = Path(triage_dir)
        self.groups = pd.read_csv(triage_dir / "groups.csv", keep_default_na=False)
        self.scopes = json.loads((triage_dir / "scopes.json").read_text())
        self.backend, self.time_limit = backend, time_limit
        self.log(f"facts ready in {time.time() - t0:.1f} s: {len(self.rf.prod)} production classes, "
                 f"{len(self.rf.edges)} class dependencies, baseline smells {dict(self.sim.base['type'].value_counts())}")

    def _backend(self) -> str:
        if self.backend != "auto":
            return self.backend
        return "cpsat" if "cpsat" in slv.available_backends() else "highs"

    # ------------------------------------------------------------------ groups

    def select(self, n: int, types: set[str] | None = None) -> pd.DataFrame:
        g = self.groups
        mask = [(t, lv) in SUPPORTED and (types is None or t in types) for t, lv in zip(g["type"], g["level"])]
        g = g[mask].copy()
        g["rank"] = pd.to_numeric(g["rank"], errors="coerce")
        return g.sort_values("rank").head(n)

    def _smell_data(self, row) -> tuple[dict | None, str]:
        t, central = row["type"], row["central"]
        if t == "unstable_dependency":
            worse = unstable_targets(self.rf, central)
            if not worse:
                return None, "not an unstable dependency in the production dependency graph"
            return {"focus_pairs": {(central, w) for w in worse}, "central": central}, ""
        if t == "hub_like_dependency":
            h = hub_data(self.rf, central)
            if h is None:
                return None, "package not in the production dependency graph"
            pairs = {(central, s) for s in h["successors"]} | {(p, central) for p in h["predecessors"]}
            return {**h, "focus_pairs": pairs, "central": central}, ""
        return {}, ""

    def plan_group(self, row) -> dict:
        gid, t = row["group_id"], row["type"]
        scope = self.scopes.get(gid, {})
        out = {"group_id": gid, "type": t, "level": row["level"], "central": row["central"],
               "rank": int(row["rank"]), "priority": float(row["priority"] or 0), "core": scope.get("core", []),
               "quick_fixes": scope.get("quick_fixes", []), "plans": [], "recommended": None}
        smell, reason = self._smell_data(row)
        if smell is None:
            out["skipped"] = reason
            return out
        if t == "hub_like_dependency":
            scope = {**scope, "core": [row["central"]],
                     "neighbours": sorted(set(scope.get("neighbours", [])) | {p for pr in smell["focus_pairs"] for p in pr})}
        limits = {}
        if len(scope.get("core", [])) > LARGE_GROUP and self._backend() == "highs":
            limits = {"max_movable": 150, "max_targets": 2}
        gf = group_facts(self.rf, gid, t, scope, smell=smell, **limits)
        if t == "hub_like_dependency":
            S = set(gf.packages)
            smell["fixed_out"] = sum(1 for s in smell["successors"] if s not in S)
            smell["fixed_in"] = sum(1 for p in smell["predecessors"] if p not in S)
        out["model"] = {"reduced_for_highs": bool(limits), "movable_classes": sum(len(v) > 1 for v in gf.movable.values()),
                        "packages": len(gf.packages), "class_edges": len(gf.edges),
                        "deletable_members": len(gf.deletable)}
        if not gf.edges:
            out["skipped"] = "no dependencies that the plan model can change"
            return out
        pm = PlanModel(self.rf, gf)
        t0 = time.time()
        plans = pm.plans(time_limit=self.time_limit, backend=self.backend)
        out["solve_seconds"] = round(time.time() - t0, 1)
        central = row["central"] if t != "cyclic_dependency" else ""
        best = None
        for p in plans:
            entry = {k: v for k, v in asdict(p).items() if k not in ("operations", "size")}
            entry["model_size"] = {k: v for k, v in p.size.items() if k != "chosen"}
            if p.name == "no_change" or not p.operations:
                entry["operations"] = []
                out["plans"].append(entry)
                continue
            sim = self.sim.run(p.operations, t, gf.core, central)
            after = {o["class"]: o["to_package"] for o in p.operations if o["op"] == "move_class"}
            checks = self.checker.check(p.operations, after)
            summ = summarise(checks)
            sc = score(p, sim, summ, t)
            entry.update({"simulation": sim.as_dict(), "checks": summ, **sc})
            entry["operations"] = [{**o, "check": {"status": c.status, "notes": c.notes, "edits": c.edits}}
                                   for o, c in zip(p.operations, checks)]
            out["plans"].append(entry)
            if summ["status"] != "blocked" and sc["benefit"] > 0 and sc["score"] > 0 \
                    and (best is None or sc["score"] > best[0]):
                best = (sc["score"], p.name)
        out["recommended"] = best[1] if best else None
        return out

    def run(self, n: int = 10, types: set[str] | None = None) -> dict:
        sel = self.select(n, types)
        results = []
        for _, row in sel.iterrows():
            t0 = time.time()
            r = self.plan_group(row)
            rec = next((p for p in r["plans"] if p["name"] == r["recommended"]), None)
            self.log(f"{r['group_id']} {r['type']} rank {r['rank']}: "
                     + (r.get("skipped") or
                        f"{len(r['plans'])} plans, recommended {r['recommended']} "
                        + (f"(score {rec['score']}, benefit {rec['benefit']}, {len(rec['operations'])} operations)"
                           if rec else ""))
                     + f" [{time.time() - t0:.0f} s]")
            results.append(r)
        return {"groups": results, "backends": slv.available_backends(), "weights": WEIGHTS,
                "baseline_smells": {k: int(v) for k, v in self.sim.base["type"].value_counts().items()}}


def write(result: dict, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"plans": out / "plans.json", "operations": out / "operations.csv", "summary": out / "summary.csv"}
    paths["plans"].write_text(json.dumps(result, indent=1, default=_json))
    rows, summ = [], []
    for g in result["groups"]:
        for p in g["plans"]:
            summ.append({"group_id": g["group_id"], "type": g["type"], "rank": g["rank"], "plan": p["name"],
                         "recommended": p["name"] == g["recommended"], "status": p["status"],
                         "operations": len(p["operations"]), "change_cost": p["change_cost"],
                         "remaining_in_model": p["remaining"], "score": p.get("score"),
                         "benefit": p.get("benefit"), "new_smells": p.get("new_smells"),
                         "checks": (p.get("checks") or {}).get("status"), "gap": p["gap"], "seconds": p["seconds"]})
            for i, o in enumerate(p["operations"]):
                rows.append({"group_id": g["group_id"], "plan": p["name"], "recommended": p["name"] == g["recommended"],
                             "step": i + 1, "op": o["op"],
                             "element": o.get("class") or o.get("member") or o.get("client", ""),
                             "from": o.get("from_package") or o.get("owner") or "",
                             "to": o.get("to_package") or o.get("import") or o.get("provider", ""),
                             "check": o["check"]["status"], "notes": "; ".join(o["check"]["notes"])})
        if not g["plans"]:
            summ.append({"group_id": g["group_id"], "type": g["type"], "rank": g["rank"], "plan": "",
                         "status": g.get("skipped", "")})
    pd.DataFrame(rows).to_csv(paths["operations"], index=False)
    pd.DataFrame(summ).to_csv(paths["summary"], index=False)
    return paths


def _json(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "item"):
        return o.item()
    return str(o)
