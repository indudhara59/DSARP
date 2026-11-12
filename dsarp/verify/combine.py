"""Stage 8b: one combined plan for the whole repository.

Every group is planned and verified on its own (stages 5-8). A user applies
many plans together, and plans can touch the same classes or undo each other.
This stage builds the set of plans to apply together:

1. **Candidates**: the verified plan of every group whose smell was removed or
   reduced on the code, with only the operations that compiled.
2. **No conflicts**: a plan is skipped if it changes a class (moves it, moves
   its members, extracts an interface from it, ...) that an already selected
   plan changes.
3. **No interference**: the combination is simulated on the whole repository;
   a plan is added only if every selected group keeps the effect it has on
   its own and no new smells appear beyond those of the plans themselves.
   Plans are added in order of their verified outcome (removed first) and score.
4. **Verification**: all selected operations are applied to one clean copy of
   the code and compiled; if the compiler reports new errors, delta debugging
   drops whole plans until the rest compiles. Every selected group is then
   measured on the combined, compiled code, and the smells of the whole
   repository are counted before and after.

Output in ``combined/``: ``combined.json`` (selection, conflicts, per-group and
whole-repository results), ``plan.patch`` and ``rewrite.yml``.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from ..outcome import verified_status
from ..plan.run import _benefit
from .debug import Tester, keep_passing
from .run import Verifier, _renamed
from .workspace import Workspace
from . import compile as cmp
from .rewrite import openrewrite_recipe


def _key(o) -> str:
    if isinstance(o, list):
        o = [{k: x for k, x in i.items() if k != "check"} for i in o]
    return json.dumps(o, sort_keys=True, default=str)


def touched(ops: list[dict]) -> set[str]:
    """Classes an operation list changes (moved, members moved, interface extracted, members deleted)."""
    out = set()
    for o in ops:
        for k in ("class", "from_class", "to_class", "client", "provider", "owner"):
            if o.get(k):
                out.add(o[k])
    return out


def packages_of(rf, ops: list[dict]) -> set[str]:
    out = set()
    for o in ops:
        for k in ("from_package", "to_package"):
            if o.get(k):
                out.add(o[k])
        for c in touched([o]):
            if c in rf.package:
                out.add(rf.package[c])
    return out


class Combiner:
    def __init__(self, verifier: Verifier, log=print):
        self.v, self.log = verifier, log
        self.rf, self.sim = verifier.rf, verifier.sim
        r = verifier.results
        self.plans = json.loads((r / "plans" / "plans.json").read_text())
        self.verified = {x["group_id"]: x for x in json.loads((r / "verify" / "verification.json").read_text())}

    # ------------------------------------------------------------------ selection
    def candidates(self) -> list[tuple[dict, list[dict], dict]]:
        out = []
        for g in self.plans["groups"]:
            v = self.verified.get(g["group_id"])
            if not v or verified_status(v) not in ("removed", "reduced"):
                continue
            ops_file = self.v.results / "verify" / g["group_id"] / "operations.json"
            if not ops_file.exists():
                continue
            ops = json.loads(ops_file.read_text()).get("kept", [])
            plan = next((p for p in g["plans"] if p["name"] == v.get("plan")), None)
            checks = {}
            for o in (plan or {}).get("operations", []):
                checks[_key({k: x for k, x in o.items() if k != "check"})] = o.get("check", {})
            if ops:
                out.append((g, [{**o, "check": checks.get(_key(o), {})} for o in ops], v))
        out.sort(key=lambda x: (verified_status(x[2]) != "removed", -(x[2].get("score") or 0)))
        return out

    def _effect(self, g: dict, ops: list[dict]) -> float:
        kind = g.get("kind", g["type"])
        core = g.get("core", [])
        central = g.get("central", "") if g["type"] != "cyclic_dependency" else ""
        if kind == "hub_like_dependency:class":
            core = [central]
        s = self.sim.run(ops, kind, core, central, g.get("context") or {})
        return _benefit(s, kind), s

    def new_smells(self, ops: list[dict]) -> set[tuple]:
        """New smells of an operation list, measured the same way whatever the plans' smell types: package-level
        smells, feature concentration in the packages it touches, and class pairs newly on one cycle."""
        key = _key(ops)
        cache = self.__dict__.setdefault("_new_cache", {})
        if key in cache:
            return cache[key]
        sim = self.sim.run(ops, "feature_concentration", [], "", {})
        out = {(n["type"], n.get("central") or "", tuple(sorted(n.get("packages") or n.get("classes") or [])))
               for n in sim.new_smells}
        if any(o["op"] in ("move_member", "extract_interface", "delete_member", "remove_unused_import") for o in ops):
            from ..plan.extra import extra
            from ..plan.simulate import apply
            st = apply(self.rf, ops, self.sim.base_state)
            snap = extra(self.rf).snapshot(st, ops)
            out |= {("class_cycle", "", tuple(sorted(n.get("classes") or n.get("members") or [])))
                    for n in self.sim._new_class_cycles(snap, ops)}
        cache[key] = out
        return out

    def select(self) -> dict:
        chosen, combined, conflicts, interfering = [], [], [], []
        own: dict[str, float] = {}
        t0 = time.time()
        for g, ops, v in self.candidates():
            gid = g["group_id"]
            clash = touched(ops) & touched(combined)
            if clash:
                conflicts.append({"group_id": gid, "classes": sorted(clash)[:5],
                                  "with": [c["group_id"] for c in chosen if touched(c["ops"]) & clash]})
                continue
            b_alone, s_alone = self._effect(g, ops)
            trial = combined + ops
            pk = packages_of(self.rf, ops)
            ok, why = True, ""
            b_trial, s_trial = self._effect(g, trial)
            if b_trial + 1e-9 < b_alone:
                ok, why = False, f"its own effect drops from {b_alone:.2f} to {b_trial:.2f} with the other plans"
            for c in chosen:
                if not ok:
                    break
                if not (pk & (packages_of(self.rf, c["ops"]) | set(c["group"].get("core", [])) |
                              {self.rf.package.get(x, "") for x in c["group"].get("core", [])})):
                    continue
                b_c, _ = self._effect(c["group"], trial)
                if b_c + 1e-9 < own[c["group_id"]]:
                    ok, why = False, f"it reduces the effect of {c['group_id']} from {own[c['group_id']]:.2f} to {b_c:.2f}"
            if ok:
                extra_new = self.new_smells(trial) - self.new_smells(combined) - self.new_smells(ops)
                if extra_new:
                    ok, why = False, (f"together with the selected plans it creates {len(extra_new)} new smells "
                                      f"that neither creates on its own (e.g. {extra_new.pop()[0].replace('_', ' ')})")
            if not ok:
                interfering.append({"group_id": gid, "reason": why})
                continue
            chosen.append({"group_id": gid, "group": g, "ops": ops, "status": verified_status(v)})
            combined = trial
            own[gid] = b_alone
        self.log(f"selected {len(chosen)} plans ({len(combined)} operations); {len(conflicts)} conflicts, "
                 f"{len(interfering)} interfering [{time.time() - t0:.0f} s]")
        return {"chosen": chosen, "conflicts": conflicts, "interfering": interfering}

    # ------------------------------------------------------------------ verification
    def run(self, max_compiles: int = 12, detector_cmds: dict[str, str] | None = None,
            build_tool: str = "none") -> dict:
        sel = self.select()
        chosen = sel["chosen"]
        out_dir = self.v.results / "combined"
        out_dir.mkdir(parents=True, exist_ok=True)
        base = self.v.baseline()
        result = {"selected": [c["group_id"] for c in chosen],
                  "conflicts": sel["conflicts"], "interfering": sel["interfering"]}
        if not chosen:
            (out_dir / "combined.json").write_text(json.dumps(result, indent=1, default=str))
            return result
        units = list(range(len(chosen)))

        def ops_of(idx):
            return [{k: x for k, x in o.items() if k != "check"} for i in idx for o in chosen[i]["ops"]]

        def checks_of(idx):
            return [o.get("check", {}) for i in idx for o in chosen[i]["ops"]]

        with Workspace(self.v.repo, self.v.workdir / "combined", self.v.meta.get("commit")) as ws:
            runs = {"n": 0}

            def passes(idx):
                runs["n"] += 1
                ok, *_ = self.v._attempt(ws, ops_of(idx), checks_of(idx), out_dir / "attempt")
                return ok

            t0 = time.time()
            ok, res, comp, new = self.v._attempt(ws, ops_of(units), checks_of(units), out_dir / "attempt")
            runs["n"] += 1
            kept, dropped = units, []
            if not ok:
                tester = Tester(passes, max_compiles)
                tester.cache[tuple(units)] = False
                kept, dropped, _ = keep_passing(units, tester)
                ok, res, comp, new = self.v._attempt(ws, ops_of(kept), checks_of(kept),
                                                     out_dir / "attempt")
            all_ops = ops_of(kept)
            per_group = []
            for i in kept:
                c = chosen[i]
                m = self.v.measure(c["group"], res, comp, [{k: x for k, x in o.items() if k != "check"}
                                                           for o in c["ops"]], all_ops)
                per_group.append({"group_id": c["group_id"], "type": c["group"].get("kind", c["group"]["type"]),
                                  "alone": c["status"], "operations": len(c["ops"]),
                                  "target_before": m["target_before"], "target_after": m["target_after"],
                                  "removed": m["target_removed"]})
            prod_after = self.v._production_after(res, all_ops)
            whole = {"before": _whole(base, self.v.prod_files), "after": _whole(comp, prod_after)}
            # the real detectors on the original and on the combined code (same tool version for both)
            for tool, command in (detector_cmds or {}).items():
                from .detectors import run_detector
                groups = [chosen[i]["group"] for i in kept]
                after_d = run_detector(tool, command, ws.path, out_dir / f"{tool}-after", groups=groups)
                with Workspace(self.v.repo, self.v.workdir / "combined-base", self.v.meta.get("commit")) as wb:
                    before_d = run_detector(tool, command, wb.path, out_dir / f"{tool}-before", groups=groups)
                result.setdefault("detectors", {})[tool] = {"before": before_d, "after": after_d}
                for p in per_group:
                    rep = (after_d.get("groups") or {}).get(p["group_id"])
                    p.setdefault("detectors", {})[tool] = None if rep is None else not rep
                self.log(f"{tool}: {before_d.get('total')} smells before, {after_d.get('total')} after")
            # build the affected modules and run the tests that use a changed class
            if build_tool != "none":
                from . import build as bld
                changed = touched(all_ops)
                tests = bld.select_tests(self.v.model, changed)
                modules = sorted({self.v.file_module.get(self.v.rw.type_file.get(c, ""), "") for c in changed} - {""})
                after_b = bld.build(ws.path, modules, tests, build_tool)
                ws.reset()
                base_b = bld.build(ws.path, modules, tests, build_tool)
                result["build"] = {"tool": after_b.tool, "modules": modules, "tests_selected": len(tests),
                                   "baseline_ok": base_b.ok, "ok": after_b.ok, "log_tail": after_b.log_tail[-3000:]}
                self.v.rw.apply(ws.path, all_ops, checks_of(kept))
            patch = ws.diff()
            (out_dir / "plan.patch").write_text(patch)
            (out_dir / "rewrite.yml").write_text(openrewrite_recipe(all_ops, "dsarp.combined"))
            result.update({
                "compiles": ok, "compile_runs": runs["n"], "seconds": round(time.time() - t0, 1),
                "plans_applied": [chosen[i]["group_id"] for i in kept],
                "plans_dropped_by_compiler": [chosen[i]["group_id"] for i in dropped],
                "operations": len(all_ops), "patch_lines": patch.count("\n"),
                "files_changed": len(res.changed) + len(res.created),
                "groups": per_group,
                "removed": sum(1 for p in per_group if p["removed"]),
                "whole_repository": whole})
        (out_dir / "combined.json").write_text(json.dumps(result, indent=1, default=str))
        (out_dir / "operations.json").write_text(json.dumps(all_ops, indent=1, default=str))
        return result


def _whole(comp, prod_files: set[str]) -> dict:
    """Smells of the whole repository on compiled code: package-level ones plus feature concentration and
    god components (Designite's rules)."""
    from ..plan import extra as ex
    counts = Counter(cmp.detect(comp, prod_files)["type"])
    snap = ex.snapshot_from_compile(comp, prod_files)
    pkgs = {p for t, p in snap.type_pkg.items() if t in snap.prod_top}
    counts["feature_concentration"] = sum(1 for p in pkgs if ex.lcc(snap, p)["present"])
    counts["god_component"] = sum(1 for p in pkgs if ex.god_size(snap, p)["present"])
    return dict(counts)
