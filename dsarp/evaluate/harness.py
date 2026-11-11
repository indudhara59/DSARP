"""Stage 9 (research): compare plan generators on the same groups.

Approaches (all use the same program model, candidate operations,
whole-repository simulation and score):

* ``solver``: constraint plans only (stage 5a), best plan by score;
* ``search_only``: tree search from an empty plan, no solver seeds, no language model;
* ``hybrid``: DSARP as used (solver + tree search seeded with the solver plans);
* ``nsga2``: multi-objective genetic algorithm, same evaluation budget as the search;
* ``random``: random subsets of candidate operations, same budget;
* ``llm_only``: one language-model answer, validated (only with ``--llm``);
* ``hybrid_fixed_affinity``: ablation, hybrid with the fixed affinity weights (``--ablation``).

Also: **weight sensitivity** of the ranking (every score weight × 0.5 and × 1.5;
how often the recommended plan stays the same) from ``plans/plans.json``.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from ..plan.run import WEIGHTS, Planner
from ..plan.run import score as plan_score
from ..search.mcts import Search
from .baselines import Evaluator, llm_only, nsga2, random_search


def _row(group, approach: str, plan, sim, sc: dict, seconds: float, extra: dict | None = None) -> dict:
    r = {"group_id": group["group_id"], "type": group["type"], "approach": approach, "seconds": round(seconds, 1)}
    if plan is None:
        return {**r, "found": False, "score": 0.0, "benefit": 0.0, "new_smells": 0.0, "operations": 0,
                "change_cost": 0.0, "target_removed": False, **(extra or {})}
    return {**r, "found": True, "score": sc["score"], "benefit": sc["benefit"], "new_smells": sc["new_smells"],
            "operations": len(plan.operations), "change_cost": plan.change_cost,
            "target_removed": bool(sim.target_removed), **(extra or {})}


class Harness:
    def __init__(self, results: str | Path, backend: str = "auto", time_limit: float = 60.0,
                 search_time: float = 120.0, budget: int = 600, llm=None, affinity: str | None = None,
                 ablation: bool = False, seed: int = 0, log=print):
        self.results = Path(results)
        self.log = log
        self.kw = dict(backend=backend, time_limit=time_limit, search_time=search_time)
        self.affinity, self.ablation, self.llm = affinity, ablation, llm
        self.budget, self.seed = budget, seed
        self.planner = Planner(self.results / "model", self.results / "triage", affinity=affinity, llm=llm,
                               search="never", log=lambda *_: None, **self.kw)

    def _scored(self, gf, plan, checker=True):
        p = self.planner
        central = gf.smell.get("central", "") if gf.smell_type != "cyclic_dependency" else ""
        sim = p.sim.run(plan.operations, gf.smell_type, gf.core, central)
        made_public = 0
        if checker:
            from ..plan.checks import summarise
            after = {o["class"]: o["to_package"] for o in plan.operations if o["op"] == "move_class"}
            made_public = summarise(p.checker.check(plan.operations, after))["made_public"]
        return sim, plan_score(plan, sim, {"made_public": made_public}, gf.smell_type)

    def run_group(self, row) -> list[dict]:
        p = self.planner
        gf, info = p.prepare(row)
        group = {"group_id": row["group_id"], "type": row["type"]}
        if gf is None:
            return [{**group, "approach": "all", "skipped": info.get("skipped")}]
        rows = []
        # solver only
        p.search = "never"
        t0 = time.time()
        res = p.plan_group(row)
        t_solver = time.time() - t0
        solver_plans = [x for x in res["plans"] if x.get("operations")]
        best = max(solver_plans, key=lambda x: x["score"], default=None)
        rows.append({**group, "approach": "solver", "seconds": round(t_solver, 1), "found": best is not None,
                     **({k: best[k] for k in ("score", "benefit", "new_smells", "change_cost")} if best else
                        {"score": 0.0, "benefit": 0.0, "new_smells": 0.0, "change_cost": 0.0}),
                     "operations": len(best["operations"]) if best else 0,
                     "target_removed": bool(best and best["simulation"]["target_removed"])})
        # hybrid: search seeded with the solver plans (solver time included)
        from ..plan.generator import Plan
        seeds = [[{k: v for k, v in o.items() if k != "check"} for o in x["operations"]] for x in solver_plans]
        t0 = time.time()
        s = Search(p.rf, gf, p.sim, llm=self.llm, checker=p.checker)
        r = s.run(time_limit=self.kw["search_time"], seeds=seeds)
        cands = [Plan(gf.group_id, x["name"], "", None, x["change_cost"], 0, None, 0, "", {},
                      [{k: v for k, v in o.items() if k != "check"} for o in x["operations"]]) for x in solver_plans]
        if r.plan:
            cands.append(r.plan)
        scored = [(self._scored(gf, c), c) for c in cands]
        hy = max(scored, key=lambda x: x[0][1]["score"], default=None)
        budget = r.evaluations
        rows.append(_row(group, "hybrid", hy[1] if hy else None, hy[0][0] if hy else None,
                         hy[0][1] if hy else {}, t_solver + time.time() - t0,
                         {"evaluations": budget, "llm_calls": r.llm_calls}))
        # search only (no seeds, no language model), same time
        t0 = time.time()
        r2 = Search(p.rf, gf, p.sim, checker=p.checker).run(time_limit=self.kw["search_time"])
        sim, sc = self._scored(gf, r2.plan) if r2.plan else (None, {})
        rows.append(_row(group, "search_only", r2.plan, sim, sc, time.time() - t0, {"evaluations": r2.evaluations}))
        # baselines with the same number of evaluations as the hybrid search (at least the budget)
        n_eval = max(budget, self.budget)
        for name, fn in (("nsga2", lambda ev: nsga2(ev, n_eval, seed=self.seed, time_limit=self.kw["search_time"] * 3)),
                         ("random", lambda ev: random_search(ev, n_eval, seed=self.seed,
                                                             time_limit=self.kw["search_time"] * 3))):
            t0 = time.time()
            ev = Evaluator(p.rf, gf, p.sim, checker=p.checker)
            plan, extra = fn(ev)
            sim, sc = self._scored(gf, plan) if plan else (None, {})
            rows.append(_row(group, name, plan, sim, sc, time.time() - t0,
                             {"evaluations": len(ev.cache), **{k: v for k, v in extra.items() if k != "skipped"}}))
        if self.llm is not None and getattr(self.llm, "name", "none") != "none":
            t0 = time.time()
            plan, extra = llm_only(Evaluator(p.rf, gf, p.sim, checker=p.checker), self.llm)
            sim, sc = self._scored(gf, plan) if plan else (None, {})
            rows.append(_row(group, "llm_only", plan, sim, sc, time.time() - t0, extra))
        return rows

    def run(self, groups: int = 10) -> pd.DataFrame:
        sel = self.planner.select(groups * 5, original_only=True)
        rows = []
        done = 0
        used = []
        for _, row in sel.iterrows():
            if done >= groups:
                break
            t0 = time.time()
            rr = self.run_group(row)
            if rr and rr[0].get("approach") == "all":
                self.log(f"{row['group_id']}: skipped ({rr[0].get('skipped')})")
                continue
            done += 1
            used.append(row)
            rows += rr
            self.log(f"{row['group_id']}: " + ", ".join(f"{r['approach']} {r.get('score', '-')}" for r in rr)
                     + f" [{time.time() - t0:.0f} s]")
        df = pd.DataFrame(rows)
        if self.ablation:
            fixed = Harness(self.results, affinity=None, llm=None, ablation=False, log=lambda *_: None,
                            budget=self.budget, seed=self.seed, **{k: v for k, v in self.kw.items()})
            fixed.planner.rf.use_affinity_model(None)
            extra = []
            for row in used:
                for r in fixed.run_group(row):
                    if r.get("approach") == "hybrid":
                        extra.append({**r, "approach": "hybrid_fixed_affinity"})
            df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
        return df


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df["approach"] != "all"].copy()
    d["target_removed"] = d["target_removed"].astype(bool)
    if d.empty:
        return pd.DataFrame()
    best = d.groupby("group_id")["score"].transform("max")
    d["best"] = (d["score"] >= best - 1e-9) & (d["score"] > 0)
    agg = d.groupby("approach").agg(groups=("group_id", "nunique"), mean_score=("score", "mean"),
                                    mean_benefit=("benefit", "mean"), removed=("target_removed", "sum"),
                                    mean_new_smells=("new_smells", "mean"), mean_operations=("operations", "mean"),
                                    mean_seconds=("seconds", "mean"), best=("best", "sum"))
    return agg.round(4).sort_values("mean_score", ascending=False).reset_index()


def sensitivity(plans: dict, factors: tuple[float, ...] = (0.5, 1.5)) -> dict:
    """How often the recommended plan stays the same when one score weight changes."""
    from types import SimpleNamespace
    stable, total, per = 0, 0, {}
    for g in plans["groups"]:
        cands = [p for p in g["plans"] if p.get("simulation") and p.get("checks")
                 and p["checks"]["status"] != "blocked"]
        if len(cands) < 2 or not g.get("recommended"):
            continue
        for w in WEIGHTS:
            if w == "cost_scale":
                continue
            for f in factors:
                weights = {w: WEIGHTS[w] * f}
                best = max(cands, key=lambda p: plan_score(
                    SimpleNamespace(change_cost=p["change_cost"]), SimpleNamespace(**p["simulation"]),
                    p["checks"], g["type"], weights)["score"])
                same = best["name"] == g["recommended"]
                stable += int(same)
                total += 1
                per.setdefault(w, []).append(same)
    return {"perturbations": total, "recommendation_unchanged": round(stable / total, 4) if total else None,
            "per_weight": {w: round(sum(v) / len(v), 4) for w, v in per.items()}}


def write(df: pd.DataFrame, results: str | Path, extra: dict | None = None) -> dict[str, Path]:
    out = Path(results) / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    paths = {"results": out / "results.csv", "summary": out / "summary.csv", "json": out / "evaluation.json"}
    df.to_csv(paths["results"], index=False)
    s = summarise(df)
    s.to_csv(paths["summary"], index=False)
    paths["json"].write_text(json.dumps({"summary": s.to_dict("records"), **(extra or {})}, indent=1, default=str))
    return paths


__all__ = ["Harness", "summarise", "sensitivity", "write", "asdict"]
