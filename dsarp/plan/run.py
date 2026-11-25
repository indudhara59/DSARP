"""Stage 5a + 6: plan the top groups of a repository, check, simulate, rank, write.

For every selected group (package cycles, unstable dependencies and hub-like
packages here; god components, feature concentration, scattered
functionality, class cycles and hub-like classes in :mod:`.restructure` and
:mod:`.classlevel`, with the same simulation, checks and score):

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
          - 0.30 * cost / (cost + 20 * (1 + size / 5))   # change cost, saturating;
                                         # size = dependencies to cut (cycles) or
                                         # to less stable packages (unstable)
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
from collections import Counter
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from ..program import ProgramModel
from . import solver as slv
from .api import api_breaks
from .checks import Checker, summarise
from .facts import GroupFacts, RepoFacts, group_facts
from .generator import Plan, PlanModel
from .simulate import Simulator, hub_data, unstable_targets

SUPPORTED = {("cyclic_dependency", "package"), ("unstable_dependency", "package"),
             ("hub_like_dependency", "package"), ("god_component", "package"), ("feature_concentration", "package"),
             ("scattered_functionality", "package"), ("cyclic_dependency", "class"), ("hub_like_dependency", "class")}
ORIGINAL = {("cyclic_dependency", "package"), ("unstable_dependency", "package"), ("hub_like_dependency", "package")}
LARGE_GROUP = 25
WEIGHTS = {"new_smell": 0.25, "side_removed": 0.05, "cost": 0.30, "cost_scale": 20.0, "made_public": 0.01,
           "api_break": 0.20, "api_scale": 20.0, "risky": 0.15, "uncovered": 0.10}
# public types and members a recommended plan may break for users of the library (None: no limit);
# --max-api-breaks overrides it. Every plan also pays a score penalty for what it breaks.
MAX_API_BREAKS = {"strict": 10, "balanced": 30, "off": None}


def kind_of(smell_type: str, level: str) -> str:
    """Planner kind of a group: the smell type, with ``:class`` for class-level smells."""
    return smell_type if level != "class" else f"{smell_type}:class"


def _benefit(sim, smell_type: str) -> float:
    b, a = sim.target_before, sim.target_after
    if not b.get("present", False):
        return 0.0
    if not a.get("present", False):
        return 1.0
    if smell_type == "god_component":
        nb, na, lim = b["classes"], a["classes"], b.get("limit", 30)
        return round(max(0.0, (nb - na) / (nb - lim)) * 0.9, 4) if nb > lim else 0.0
    if smell_type == "feature_concentration":
        lb, la = b["lcc"], a["lcc"]
        return round(max(0.0, (lb - la) / (lb - 0.2)) * 0.9, 4) if lb > 0.2 else 0.0
    if smell_type == "scattered_functionality":
        mb, ma = b["methods"], a["methods"]
        return round(max(0.0, (mb - ma) / mb) * 0.9, 4) if mb else 0.0
    if smell_type == "cyclic_dependency:class":
        nb, na = b["core_classes_on_cycle"], a["core_classes_on_cycle"]
        fb, fa = b.get("dependencies_to_cut", 0), a.get("dependencies_to_cut", 0)
        pk = (nb - na) / nb if nb else 0.0
        fs = (fb - fa) / fb if fb else 0.0
        return round(max(0.0, pk, fs) * 0.9, 4)
    if smell_type == "hub_like_dependency:class":
        tb = b.get("fan_in", 0) + b.get("fan_out", 0)
        ta = a.get("fan_in", 0) + a.get("fan_out", 0)
        return max(0.0, (tb - ta) / tb) * 0.9 if tb else 0.0
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


def _size(sim, smell_type: str) -> float:
    """Size of the target smell before the plan (dependencies to cut / remove)."""
    b = sim.target_before
    if smell_type == "cyclic_dependency":
        return float(b.get("dependencies_to_cut", 1))
    if smell_type == "unstable_dependency":
        return float(len(b.get("less_stable", [])))
    if smell_type == "god_component":
        return float(max(1, b.get("classes", 31) - b.get("limit", 30)))
    if smell_type == "feature_concentration":
        return float(max(1, b.get("sets", 1)))
    if smell_type == "scattered_functionality":
        return float(max(1, b.get("methods", 1)))
    if smell_type == "cyclic_dependency:class":
        return float(b.get("dependencies_to_cut", 1))
    return 1.0


def score(plan: Plan, sim, check_summary: dict, smell_type: str, weights: dict | None = None) -> dict:
    w = {**WEIGHTS, **(weights or {})}
    benefit = _benefit(sim, smell_type)
    new = sum(1.0 if s["type"] != "cyclic_dependency" else min(1.0 + s["size"] / 10, 3.0)
              for s in sim.new_smells)
    side = max(len(sim.removed_smells) - (1 if benefit == 1.0 else 0), 0)
    scale = w["cost_scale"] * (1.0 + _size(sim, smell_type) / 5.0)   # bigger smells justify more change
    cost_n = plan.change_cost / (plan.change_cost + scale) if plan.change_cost else 0.0
    api = check_summary.get("api_expected", check_summary.get("api_breaks", 0))
    risky = check_summary.get("risky_classes", 0)
    total = (benefit - w["new_smell"] * new + w["side_removed"] * side - w["cost"] * cost_n
             - w["made_public"] * check_summary.get("made_public", 0)
             - w["api_break"] * api / (api + w["api_scale"]) - w.get("risky", 0) * risky / (risky + 3)
             - w.get("uncovered", 0) * check_summary.get("uncovered", 0))
    return {"score": round(total, 4), "benefit": round(benefit, 4), "new_smells": round(new, 3),
            "other_smells_removed": side, "cost_normalised": round(cost_n, 4)}


class Planner:
    def __init__(self, model_dir: str | Path, triage_dir: str | Path, backend: str = "auto",
                 time_limit: float = 60.0, log=print, affinity: str | Path | None = None,
                 search: str = "auto", search_time: float = 120.0, llm=None, api_policy: str = "balanced",
                 max_api_breaks: int | None = -1, compat: str = "stubs", no_widening: bool = False):
        self.log = log
        self.compat = compat
        self.no_widening = no_widening
        # -1: the default of the API policy; None: no limit
        self.max_api_breaks = MAX_API_BREAKS.get(api_policy) if max_api_breaks == -1 else max_api_breaks
        t0 = time.time()
        self.model = ProgramModel(model_dir)
        self.rf = RepoFacts.build(self.model, api_policy)
        self.affinity = None
        if affinity:
            from ..learn.affinity import AffinityModel
            self.rf.use_affinity_model(AffinityModel.load(affinity))
            self.affinity = str(affinity)
        if search not in ("auto", "always", "never"):
            raise ValueError("search must be auto, always or never")
        self.search, self.search_time, self.llm = search, search_time, llm
        self.sim = Simulator(self.rf)
        self.checker = Checker(self.rf)
        triage_dir = Path(triage_dir)
        self.groups = pd.read_csv(triage_dir / "groups.csv", keep_default_na=False)
        self.reported_by = _reported_by(triage_dir)
        self.scopes = json.loads((triage_dir / "scopes.json").read_text())
        self.rf.detector_note = detector_note(triage_dir.parent)
        self.rf.no_widening = no_widening
        from ..lessons import Lessons
        self.lessons = Lessons(triage_dir.parent / "lessons")
        if self.lessons.summary()["banned_steps"] or self.lessons.summary()["banned_combinations"]:
            self.log("lessons: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in self.lessons.summary().items()))
        if self.rf.detector_note:
            self.log("note: " + self.rf.detector_note)
        self.backend, self.time_limit = backend, time_limit
        self.log(f"facts ready in {time.time() - t0:.1f} s: {len(self.rf.prod)} production classes, "
                 f"{len(self.rf.edges)} class dependencies, baseline smells "
                 f"{ {k: int(v) for k, v in self.sim.base['type'].value_counts().items()} }")

    def _backend(self) -> str:
        if self.backend != "auto":
            return self.backend
        return "cpsat" if "cpsat" in slv.available_backends() else "highs"

    # ------------------------------------------------------------------ groups

    def select(self, n: int, types: set[str] | None = None, original_only: bool = False) -> pd.DataFrame:
        g = self.groups
        allowed = ORIGINAL if original_only else SUPPORTED
        mask = [(t, lv) in allowed and (types is None or t in types or kind_of(t, lv) in types)
                for t, lv in zip(g["type"], g["level"])]
        g = g[mask].copy()
        g["rank"] = pd.to_numeric(g["rank"], errors="coerce")
        return g.sort_values("rank").head(n)

    def _smell_data(self, row) -> tuple[dict | None, str]:
        t, central = row["type"], row["central"]
        if t == "unstable_dependency":
            worse = unstable_targets(self.rf, central)
            if not worse:
                return None, ("no dependency on a less stable package in the production code at this commit "
                              "(the detector's view differs, e.g. it includes sub-packages, libraries or tests)")
            return {"focus_pairs": {(central, w) for w in worse}, "central": central}, ""
        if t == "hub_like_dependency":
            h = hub_data(self.rf, central)
            if h is None:
                return None, "package not in the production dependency graph"
            pairs = {(central, s) for s in h["successors"]} | {(p, central) for p in h["predecessors"]}
            return {**h, "focus_pairs": pairs, "central": central}, ""
        return {}, ""

    def prepare(self, row) -> tuple[GroupFacts | None, dict]:
        """Group facts for one group row, or ``None`` with the reason it cannot be planned."""
        gid, t = row["group_id"], row["type"]
        scope = self.scopes.get(gid, {})
        smell, reason = self._smell_data(row)
        if smell is None:
            return None, {"skipped": reason}
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
        info = {"model": {"reduced_for_highs": bool(limits),
                          "movable_classes": sum(len(v) > 1 for v in gf.movable.values()),
                          "packages": len(gf.packages), "class_edges": len(gf.edges),
                          "deletable_members": len(gf.deletable)}}
        if not gf.edges:
            return None, {**info, "skipped": "no dependencies that the plan model can change"}
        return gf, info

    # ------------------------------------------------------------------ extra smell types

    def _extra_plans(self, row, kind: str, scope: dict) -> tuple[list[Plan], dict, dict]:
        """Solver plans, group information and simulation context for the extra smell types."""
        from . import restructure as rs
        gid, central = row["group_id"], row["central"]
        tl = self.time_limit
        if kind == "god_component":
            plans, info = rs.split_god(self.rf, gid, central, tl, self._backend())
            return plans, info, {}
        if kind == "feature_concentration":
            plans, info = rs.split_fc(self.rf, gid, central, tl, self._backend())
            return plans, info, {}
        if kind == "scattered_functionality":
            members = json.loads(row["members"]) if isinstance(row["members"], str) else list(row["members"])
            plans, info = rs.consolidate(self.rf, gid, members, self.reported_by.get(gid))
            ctx = {"methods": info.get("methods_list", []), "concern_classes": info.get("concern_classes", [])}
            return plans, {k: v for k, v in info.items() if k != "methods_list"}, ctx
        if kind in ("cyclic_dependency:class", "hub_like_dependency:class"):
            from . import classlevel as cl
        if kind == "cyclic_dependency:class":
            plans, info = cl.class_cycle_plans(self.rf, gid, scope.get("core", []), tl, self._backend())
            return plans, info, {"level": "class"}
        if kind == "hub_like_dependency:class":
            plans, info = cl.class_hub_plans(self.rf, gid, central, tl, self._backend())
            return plans, info, {"level": "class"}
        raise ValueError(kind)

    def _summarise(self, checks, ops: list[dict]) -> dict:
        """Checks of a plan's steps, with the public API it breaks; over the limit, the plan is blocked."""
        summ = summarise(checks)
        api = api_breaks(self.model, ops, self.compat)
        summ["api_breaks"] = api["count"]
        f = self.lessons.api_factor()
        if f != 1.0:                                  # japicmp measured more (or fewer) changes than counted
            summ["api_expected"] = round(api["count"] * f, 1)
        summ["api_covered_by_stubs"] = api["covered_by_stubs"]
        summ["api_examples"] = api["examples"]
        lesson = self.lessons.banned_reason(ops)
        if lesson:
            summ["status"] = "blocked"
            summ["lesson"] = lesson
        cov = touched_coverage(self.rf, ops)
        if cov is not None:
            summ["coverage"] = round(cov, 3)
            summ["uncovered"] = round(1 - cov, 3)
        risky = self.lessons.risky(ops)
        if risky:
            summ["risky_classes"] = len(risky)
            summ["risky_examples"] = risky[:3]
        lim = self.max_api_breaks
        if lim is not None and api["count"] > lim:
            summ["status"] = "blocked"
            summ["api_limit"] = (f"it changes {api['count']} public types and members that code outside the "
                                 f"repository may use ({api['types']} types, {api['members']} members; limit {lim})")
        return summ

    def _evaluate(self, p: Plan, kind: str, core: list[str], central: str, ctx: dict) -> dict:
        sim = self.sim.run(p.operations, kind, core, central, ctx)
        after = {o["class"]: o["to_package"] for o in p.operations if o["op"] == "move_class"}
        checks = self.checker.check(p.operations, after)
        summ = self._summarise(checks, p.operations)
        sc = score(p, sim, summ, kind)
        entry = {k: v for k, v in asdict(p).items() if k not in ("operations", "size")}
        entry["model_size"] = {k: v for k, v in p.size.items() if k != "chosen"}
        entry.update({"simulation": sim.as_dict(), "checks": summ, **sc})
        entry["operations"] = [{**o, "check": {"status": c.status, "notes": c.notes, "edits": c.edits}}
                               for o, c in zip(p.operations, checks)]
        return entry

    def repair(self, p: Plan, kind: str, core: list[str], central: str, ctx: dict,
               max_evaluations: int = 200) -> Plan | None:
        """Greedy repair (hill climbing): drop operations while the score improves.

        Used for smell types without the tree search: removes the steps that
        cause new smells or blocked checks, keeping the rest of the plan."""
        import copy
        best = copy.deepcopy(p)
        best_score = self._evaluate(best, kind, core, central, ctx)["score"]
        evals, improved = 0, True
        while improved and len(best.operations) > 1 and evals < max_evaluations:
            improved = False
            for i in range(len(best.operations)):
                cand = copy.deepcopy(best)
                cand.operations = best.operations[:i] + best.operations[i + 1:]
                cand.change_cost = round(sum(self._op_cost(o) for o in cand.operations), 3)
                evals += 1
                sc = self._evaluate(cand, kind, core, central, ctx)["score"]
                if sc > best_score + 1e-6:
                    best, best_score, improved = cand, sc, True
                    break
        if _same_ops(best.operations, p.operations):
            return None
        best.name, best.backend = "repaired", "greedy"
        return best

    def _op_cost(self, o: dict) -> float:
        from .facts import IMPORT_COST, deletion_cost, inversion_cost, move_cost
        from .restructure import split_cost
        if o["op"] == "move_class":
            c = o["class"]
            base = split_cost(self.rf, c) if o.get("new_package") else \
                move_cost(self.rf, c, o["to_package"], o.get("affinity") or self.rf.affinity(c, o["to_package"]))
            return base + 0.3 * len(o.get("tests", []))
        if o["op"] == "delete_member":
            return deletion_cost(self.rf.member_info[o["member"]])
        if o["op"] == "remove_unused_import":
            return IMPORT_COST
        if o["op"] == "extract_interface":
            return inversion_cost(o["methods"])
        return float(o.get("cost", 1.0))

    def _plan_extra(self, row, out: dict, kind: str, scope: dict) -> dict:
        gid = row["group_id"]
        central = row["central"]
        core = scope.get("core", []) if kind != "hub_like_dependency:class" else [central]
        t0 = time.time()
        plans, info, ctx = self._extra_plans(row, kind, scope)
        out["solve_seconds"] = round(time.time() - t0, 1)
        out.update({"model": {k: v for k, v in info.items() if k not in ("skipped", "why_none")}, "context": ctx})
        if info.get("skipped"):
            out["skipped"] = info["skipped"]
            return out
        best = None
        for p in plans:
            if not p.operations:
                entry = {k: v for k, v in asdict(p).items() if k not in ("operations", "size")}
                entry["model_size"] = {k: v for k, v in p.size.items() if k != "chosen"}
                entry["operations"] = []
                out["plans"].append(entry)
                continue
            entry = self._evaluate(p, kind, core, central, ctx)
            out["plans"].append(entry)
            if entry["checks"]["status"] != "blocked" and entry["benefit"] > 0 and entry["score"] > 0 \
                    and (best is None or entry["score"] > best[0]):
                best = (entry["score"], p.name)
        # repair the best plan when it has side effects or blocked steps (no tree search for these types)
        cands = [(e, p) for e, p in zip([x for x in out["plans"] if x.get("operations")], [p for p in plans if p.operations])]
        top = max(cands, key=lambda ep: ep[0].get("score", -9)) if cands else None
        if self.search != "never" and top is not None and (top[0].get("new_smells") or
                                                           top[0]["checks"]["status"] == "blocked"):
            fixed = self.repair(top[1], kind, core, central, ctx)
            if fixed is not None:
                entry = self._evaluate(fixed, kind, core, central, ctx)
                out["plans"].append(entry)
                out["search"] = {"method": "greedy repair", "from": top[1].name}
                if entry["checks"]["status"] != "blocked" and entry["benefit"] > 0 and entry["score"] > 0 \
                        and (best is None or entry["score"] > best[0]):
                    best = (entry["score"], "repaired")
        out["recommended"] = best[1] if best else None
        if best is None:
            if plans and not any(p.operations for p in plans):
                out["skipped"] = info.get("why_none") or ("no change that the planner can make reduces the smell "
                                                          "without breaking a rule (see the model information)")
            elif any((e.get("checks") or {}).get("api_limit") or (e.get("checks") or {}).get("lesson")
                     for e in out["plans"]):
                out["no_plan_reason"] = why_rejected(out["plans"])     # plans exist, but break too much public API
            elif info.get("why_none"):
                out["no_plan_reason"] = info["why_none"]
            else:
                out["no_plan_reason"] = why_rejected(out["plans"])
        return out

    def plan_group(self, row) -> dict:
        from .confidence import annotate
        out = self._plan_group(row)
        annotate(out, self.lessons.calibration(out["type"]), getattr(self.rf, "coverage", {}) or {})
        return out

    def _plan_group(self, row) -> dict:
        gid, t = row["group_id"], row["type"]
        scope = self.scopes.get(gid, {})
        kind = kind_of(t, row["level"])
        out = {"group_id": gid, "type": t, "level": row["level"], "central": row["central"],
               "rank": int(row["rank"]), "priority": float(row["priority"] or 0), "core": scope.get("core", []),
               "quick_fixes": scope.get("quick_fixes", []), "plans": [], "recommended": None, "kind": kind}
        if (t, row["level"]) not in ORIGINAL:
            return self._plan_extra(row, out, kind, scope)
        gf, info = self.prepare(row)
        out.update(info)
        if gf is None:
            return out
        pm = PlanModel(self.rf, gf)
        t0 = time.time()
        plans = pm.plans(time_limit=self.time_limit, backend=self.backend)
        if t == "cyclic_dependency":
            from .restructure import merge_plans
            plans += merge_plans(self.rf, gid, gf.core)
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
            summ = self._summarise(checks, p.operations)
            sc = score(p, sim, summ, t)
            entry.update({"simulation": sim.as_dict(), "checks": summ, **sc})
            entry["operations"] = [{**o, "check": {"status": c.status, "notes": c.notes, "edits": c.edits}}
                                   for o, c in zip(p.operations, checks)]
            out["plans"].append(entry)
            if summ["status"] != "blocked" and sc["benefit"] > 0 and sc["score"] > 0 \
                    and (best is None or sc["score"] > best[0]):
                best = (sc["score"], p.name)
        need_search = self.search == "always" or (
            self.search == "auto" and (best is None or any(
                p.get("new_smells") or (p.get("gap") or 0) > 0.1 for p in out["plans"] if p["name"] == best[1])))
        if need_search:
            entry, out["search"] = self._search(gf, plans, t, central)
            if entry is not None:
                out["plans"].append(entry)
                sc = entry
                if sc["checks"]["status"] != "blocked" and sc["benefit"] > 0 and sc["score"] > 0 \
                        and (best is None or sc["score"] > best[0]):
                    best = (sc["score"], "search")
        out["recommended"] = best[1] if best else None
        if best is None and not out.get("skipped") and not out.get("no_plan_reason"):
            out["no_plan_reason"] = why_rejected(out["plans"])
        return out

    def _search(self, gf, plans: list[Plan], t: str, central: str) -> tuple[dict | None, dict]:
        from ..search.mcts import Search
        seeds = [p.operations for p in plans if p.operations]
        s = Search(self.rf, gf, self.sim, llm=self.llm, checker=self.checker)
        r = s.run(time_limit=self.search_time, seeds=seeds)
        info = {"evaluations": r.evaluations, "iterations": r.iterations, "seconds": r.seconds,
                "best_score": r.best_score, "llm_calls": r.llm_calls, "llm_accepted": r.llm_accepted,
                "trace": r.trace}
        if r.plan is None or any(_same_ops(r.plan.operations, p.operations) for p in plans):
            return None, info
        p = r.plan
        sim = self.sim.run(p.operations, t, gf.core, central)
        after = {o["class"]: o["to_package"] for o in p.operations if o["op"] == "move_class"}
        checks = self.checker.check(p.operations, after)
        summ = self._summarise(checks, p.operations)
        entry = {k: v for k, v in asdict(p).items() if k not in ("operations", "size")}
        entry["model_size"] = p.size
        entry.update({"simulation": sim.as_dict(), "checks": summ, **score(p, sim, summ, t)})
        entry["operations"] = [{**o, "check": {"status": c.status, "notes": c.notes, "edits": c.edits}}
                               for o, c in zip(p.operations, checks)]
        return entry, info

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
                "affinity_model": self.affinity, "search": self.search, "api_policy": self.rf.api_policy,
                "max_api_breaks": self.max_api_breaks, "compat": self.compat,
                "no_widening": self.no_widening,
                "frozen_classes": len(self.rf.frozen),
                "llm": getattr(self.llm, "name", "none") if self.llm else "none",
                "baseline_smells": {k: int(v) for k, v in self.sim.base["type"].value_counts().items()}}


def touched_classes(ops: list[dict]) -> set[str]:
    out = set()
    for o in ops:
        for k in ("class", "from_class", "owner", "client", "provider"):
            if o.get(k):
                out.add(o[k])
    return out


def touched_coverage(rf, ops: list[dict]) -> float | None:
    """Mean line coverage (baseline run) of the classes a plan changes; None when coverage was not measured."""
    cov = getattr(rf, "coverage", None) or {}
    vals = [cov[c] for c in touched_classes(ops) if c in cov]
    return sum(vals) / len(vals) if vals else None


def why_rejected(plans: list[dict]) -> str:
    """Why no candidate plan was recommended, from the evaluated candidates (with numbers)."""
    cands = [p for p in plans if p.get("operations") and "score" in p]
    if not cands:
        return "the plan models found no change that reduces the smell within the rules (no candidate plan)"
    useful = [p for p in cands if (p.get("benefit") or 0) > 0]
    if not useful:
        return (f"none of the {len(cands)} candidate plans removes any part of the smell when simulated on the "
                f"whole repository")
    p = max(useful, key=lambda x: (x.get("score", -9), x.get("benefit", 0)))
    parts = []
    if (p.get("checks") or {}).get("api_limit"):
        parts.append(p["checks"]["api_limit"])
    if (p.get("checks") or {}).get("lesson"):
        parts.append(p["checks"]["lesson"])
    if (p.get("checks") or {}).get("status") == "blocked" and (p.get("checks") or {}).get("blocked"):
        notes = [n for o in p["operations"] for n in (o.get("check") or {}).get("notes", [])
                 if (o.get("check") or {}).get("status") == "blocked"]
        parts.append(f"{p['checks']['blocked']} of its steps are blocked" + (f" ({notes[0]})" if notes else ""))
    new = (p.get("simulation") or {}).get("new_smells") or []
    if new:
        kinds = Counter(n.get("type", "?") for n in new)
        parts.append("it would create " + ", ".join(f"{v} new {k.replace('_', ' ')}" for k, v in kinds.items()))
    if (p.get("score") or 0) <= 0:
        parts.append(f"its score is {p.get('score')} (benefit {p.get('benefit')}, new smells "
                     f"{p.get('new_smells')}, normalised change cost {p.get('cost_normalised')})")
    return (f"the best of {len(cands)} candidate plans ({p['name']}, {len(p['operations'])} steps, removes "
            f"{round(100 * p.get('benefit', 0))}% of the smell) is not safe to recommend: " + "; ".join(parts))


def detector_note(results_dir: Path) -> str:
    """A note for the reasons of skipped groups when a detector analysed build output besides the sources."""
    n = 0
    try:
        man = json.loads((results_dir / "manifest.json").read_text())
        n = sum(v.get("duplicated_classes", 0) or v.get("build_rows", 0) for v in (man.get("build_output") or {}).values())
    except (OSError, ValueError):
        pass
    if not n and (results_dir / "components.csv").exists():          # results ingested by an older version
        from .. import paths
        c = pd.read_csv(results_dir / "components.csv", keep_default_na=False, low_memory=False)
        c = c[c["kind"] == "class"]
        n = sum(paths.is_build_output_path(f, p) for f, p in zip(c["file_path"], c["package"]) if f)
    if not n:
        return ""
    return (f"Designite also analysed build output (copies in target/ or WEB-INF/), "
            f"so {n} classes were counted more than once")


def _reported_by(triage_dir: Path) -> dict[str, set[str]]:
    """For scattered functionality: the packages whose methods use the group's packages together."""
    st, sm = triage_dir / "smells_triaged.csv", triage_dir.parent / "smells.csv"
    if not st.exists() or not sm.exists():
        return {}
    a = pd.read_csv(st, keep_default_na=False, usecols=["smell_id", "type", "group_id"])
    a = a[a["type"] == "scattered_functionality"]
    if a.empty:
        return {}
    b = pd.read_csv(sm, keep_default_na=False, usecols=["smell_id", "attributes"])
    attrs = dict(zip(b["smell_id"], b["attributes"]))
    out: dict[str, set[str]] = {}
    for sid, gid in zip(a["smell_id"], a["group_id"]):
        try:
            rep = json.loads(attrs.get(sid) or "{}").get("reported_by", [])
        except json.JSONDecodeError:
            rep = []
        if rep:
            out.setdefault(gid, set()).update(rep)
    return out


def _same_ops(a: list[dict], b: list[dict]) -> bool:
    key = lambda ops: sorted(str(sorted(o.items())) for o in ops)  # noqa: E731
    return key(a) == key(b)


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
                         "checks": (p.get("checks") or {}).get("status"), "gap": p["gap"], "seconds": p["seconds"],
                         "confidence": (p.get("confidence") or {}).get("value"),
                         "coverage": (p.get("checks") or {}).get("coverage")})
            for i, o in enumerate(p["operations"]):
                rows.append({"group_id": g["group_id"], "plan": p["name"], "recommended": p["name"] == g["recommended"],
                             "step": i + 1, "op": o["op"],
                             "element": o.get("class") or o.get("member") or o.get("client", ""),
                             "from": o.get("from_package") or o.get("owner") or "",
                             "to": o.get("to_package") or o.get("import") or o.get("provider", ""),
                             "check": o["check"]["status"], "notes": "; ".join(o["check"]["notes"]),
                             "planners_agreeing": o.get("planners_agreeing", ""),
                             "confidence": o.get("confidence")})
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
