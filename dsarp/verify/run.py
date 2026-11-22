"""Stage 8: verify the recommended plans on the real code.

For each group with a recommended plan:

1. create a clean working copy of the checkout at the analysed commit;
2. apply the plan (:mod:`.rewrite`) and compile all Java files
   (:mod:`.compile`); errors that were not there before are caused by the plan;
3. if there are new errors, **delta debugging** (:mod:`.debug`) finds the
   operations that cause them; the plan is reduced to the operations that
   compile, and the dropped operations are reported with their errors;
4. detect the smells again **on the compiled result** and compare with the
   original code and with the prediction of the simulation (stage 6);
5. optionally build the affected modules with Maven/Gradle and run the
   selected tests (:mod:`.build`), and re-run Arcan/Designite (:mod:`.detectors`);
6. write the patch (``git diff``), an OpenRewrite recipe for the moves and the
   results.
"""

from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from ..outcome import quality as _quality
from ..plan.facts import RepoFacts
from ..plan.simulate import EXTRA_KINDS, Simulator, interface_name
from ..program import ProgramModel
from . import build as bld
from ..plan.api import api_breaks
from . import compile as cmp
from .debug import Tester, keep_passing
from .detectors import run_detector
from .rewrite import Rewriter, openrewrite_recipe
from .workspace import Workspace


def _flag(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1"])


class Verifier:
    def __init__(self, results: str | Path, repo: str | Path, workdir: str | Path | None = None,
                 memory: str = "4g", classpath: str = "", build_tool: str = "auto", run_tests: bool = True,
                 max_compiles: int = 30, detector_cmds: dict[str, str] | None = None, log=print,
                 max_api_breaks: int | None = -1):
        self.results, self.repo = Path(results), Path(repo)
        self.workdir = Path(workdir) if workdir else self.results / "verify" / "work"
        self.memory, self.classpath, self.log = memory, classpath, log
        self.build_tool, self.run_tests, self.max_compiles = build_tool, run_tests, max_compiles
        self._base_builds: dict[tuple, object] = {}      # builds of the original code, per modules and tests
        if max_api_breaks == -1:                          # the limit the plans were made with
            from ..plan.run import MAX_API_BREAKS
            pf = self.results / "plans" / "plans.json"
            meta = json.loads(pf.read_text()) if pf.exists() else {}
            max_api_breaks = meta.get("max_api_breaks", MAX_API_BREAKS.get(meta.get("api_policy", "balanced")))
        self.max_api_breaks = max_api_breaks
        self.detector_cmds = detector_cmds or {}
        self.model = ProgramModel(self.results / "model")
        self.meta = json.loads((self.results / "model" / "model.json").read_text())
        self.rw = Rewriter(self.model)
        f = self.model.files
        f = f[~_flag(f["excluded"])]
        self.roots = sorted(set(f["source_root"].dropna()) - {""})
        self.prod_files = set(f.loc[~_flag(f["is_test"]) & ~_flag(f["is_generated"]), "file"])
        self.file_module = dict(zip(f["file"], f["module"]))
        self.rf = RepoFacts.build(self.model)
        self.sim = Simulator(self.rf)
        self._baseline: cmp.CompileResult | None = None
        self.packages = set(self.model.types["package"].dropna())
        self.symbols = {t.rsplit(".", 1)[-1].split("$")[-1] for t in self.model.types["type"]} | \
            set(self.model.members["name"]) | {t.rsplit(".", 1)[-1] for t in self.model.types["package"].dropna()}

    # ------------------------------------------------------------------ baseline

    def baseline(self) -> cmp.CompileResult:
        if self._baseline is None:
            t0 = time.time()
            # compile a clean copy at the analysed commit (the checkout itself may have local changes)
            with Workspace(self.repo, self.workdir / "baseline", self.meta.get("commit")) as ws:
                self._baseline = cmp.compile_tree(ws.path, self.rw.java_files, self.roots,
                                                  self.results / "verify" / "baseline", self.classpath, self.memory)
            self.log(f"baseline compile: {len(self._baseline.errors)} errors (missing libraries) "
                     f"in {time.time() - t0:.0f} s")
        return self._baseline

    # ------------------------------------------------------------------ attempts

    def _attempt(self, ws: Workspace, ops: list[dict], checks: list[dict], out: Path):
        ws.reset()
        res = self.rw.apply(ws.path, ops, checks)
        files = self.rw.files_after(res)
        comp = cmp.compile_tree(ws.path, files, self.roots, out, self.classpath, self.memory)
        renamed = _renamed(ops)
        new = cmp.new_errors(self.baseline().errors, comp.errors, res.moved, renamed,
                             self.symbols | {n.rsplit(".", 1)[-1] for n in res.created_types},
                             set(renamed.values()) | set(res.created_types), self.packages, ws.path)
        return new.empty, res, comp, new

    def _production_after(self, res, ops) -> set[str]:
        prod = {res.moved.get(f, f) for f in self.prod_files}
        for c in res.created:
            prod.add(c)
        return prod

    # ------------------------------------------------------------------ plan

    def verify_plan(self, group: dict, plan: dict, sub: str | None = None) -> dict:
        gid = group["group_id"]
        out = self.results / "verify" / gid / sub if sub else self.results / "verify" / gid
        out.mkdir(parents=True, exist_ok=True)
        ops = [{k: v for k, v in o.items() if k != "check"} for o in plan["operations"]]
        checks = [o.get("check", {}) for o in plan["operations"]]
        t0 = time.time()
        rec = {"group_id": gid, "type": group["type"], "plan": plan["name"], "operations": len(ops)}
        with Workspace(self.repo, self.workdir / gid, self.meta.get("commit")) as ws:
            compiles = {"n": 0}

            def passes(idx: list[int]) -> bool:
                compiles["n"] += 1
                ok, *_ = self._attempt(ws, [ops[i] for i in idx], [checks[i] for i in idx], out / "attempt")
                return ok

            ok, res, comp, new = self._attempt(ws, ops, checks, out / "attempt")
            compiles["n"] += 1
            kept = list(range(len(ops)))
            dropped: list[int] = []
            method = "all"
            first_errors = new
            if not ok:
                tester = Tester(passes, self.max_compiles)
                tester.cache[tuple(kept)] = False
                kept, dropped, method = keep_passing(kept, tester)
                ok, res, comp, new = self._attempt(ws, [ops[i] for i in kept], [checks[i] for i in kept],
                                                   out / "attempt")
                compiles["n"] += 1
            first_errors.to_csv(out / "new_errors_full_plan.csv", index=False)
            new.to_csv(out / "new_errors.csv", index=False)
            kept_ops = [ops[i] for i in kept]
            rec.update({"compiles": ok, "compile_runs": compiles["n"], "kept": len(kept), "dropped": len(dropped),
                        "reduction": method, "new_errors_full_plan": len(first_errors),
                        "new_errors_codes": dict(Counter(first_errors["code"]).most_common(5)),
                        "dropped_operations": [ops[i] for i in dropped]})
            # detection on the compiled code vs the simulation
            if kept_ops:
                rec.update(self.measure(group, res, comp, kept_ops))
            # patch and recipe
            patch = ws.diff()
            (out / "plan.patch").write_text(patch)
            (out / "rewrite.yml").write_text(openrewrite_recipe(kept_ops, f"dsarp.{gid.replace('-', '_')}"))
            (out / "operations.json").write_text(json.dumps({"kept": kept_ops, "dropped": rec["dropped_operations"]},
                                                            indent=1))
            rec["patch_lines"] = patch.count("\n")
            rec["files_changed"] = len(res.changed) + len(res.created)
            rec["notes"] = res.notes[:50]
            rec["api_breaks"] = api_breaks(self.model, kept_ops) if kept_ops else {"count": 0}
            ws.reset()
            if kept_ops:
                rec["patch_applies"] = ws.apply_check(out / "plan.patch")
            # the project's own build and the tests that use a changed class, on the original code and after
            if kept_ops:
                changed = {o.get("class") or o.get("owner") or o.get("from_class") or o.get("provider")
                           for o in kept_ops} - {None}
                changed |= {interface_name(o) for o in kept_ops if o["op"] == "extract_interface"}
                tests = bld.select_tests(self.model, changed) if self.run_tests else []
                modules = sorted({self.file_module.get(f, "") for f in
                                  [self.rw.type_file.get(c, "") for c in changed]} - {""})
                if self.build_tool == "none":
                    rec["build"] = {"status": "not built", "check": "compiler only",
                                    "reason": "the build was switched off (--build none): only the compiler check "
                                              "of the sources was done"}
                    self.rw.apply(ws.path, kept_ops, [checks[i] for i in kept])
                else:
                    key = (tuple(modules), tuple(tests))
                    if key not in self._base_builds:                 # the original code (workspace is clean)
                        self._base_builds[key] = bld.build(ws.path, modules, tests, self.build_tool)
                    self.rw.apply(ws.path, kept_ops, [checks[i] for i in kept])
                    after_b = bld.build(ws.path, modules, tests, self.build_tool)
                    rec["build"] = {"modules": modules, "tests_selected": len(tests),
                                    **bld.compare(self._base_builds[key], after_b)}
            # detectors
            for tool, command in self.detector_cmds.items():
                if not kept_ops:
                    continue
                d = run_detector(tool, command, ws.path, out / f"{tool}-after", groups=[group])
                rec.setdefault("detectors", {})[tool] = d
                if d.get("ok") and d.get("groups", {}).get(gid) is False:
                    rec.setdefault("confirmed_by", []).append(tool)
        rec["seconds"] = round(time.time() - t0, 1)
        rec["status"], rec["accepted"], rec["why_not_accepted"] = final_status(rec, self.max_api_breaks)
        (out / "verification.json").write_text(json.dumps(rec, indent=1, default=str))
        return rec

    def measure(self, group: dict, res, comp, kept_ops: list[dict], all_ops: list[dict] | None = None) -> dict:
        """The group's smell on the compiled code (``comp``), before and after, and the simulation's prediction.

        ``all_ops`` (default ``kept_ops``) are the operations applied to the code, used for renamed classes
        and members when several plans are applied together."""
        all_ops = all_ops if all_ops is not None else kept_ops
        prod_after = self._production_after(res, all_ops)
        after = cmp.detect(comp, prod_after)
        g_after, _ = cmp.package_graph(comp, prod_after)
        before = cmp.detect(self.baseline(), self.prod_files)
        g_before, _ = cmp.package_graph(self.baseline(), self.prod_files)
        central = group.get("central", "") if group["type"] != "cyclic_dependency" else ""
        core = group.get("core", [])
        kind = group.get("kind", group["type"])
        ctx = group.get("context") or {}
        if kind in EXTRA_KINDS:
            from ..plan.extra import snapshot_from_compile
            if kind == "hub_like_dependency:class":
                core = [central]
            snap_b = snapshot_from_compile(self.baseline(), self.prod_files)
            snap_a = snapshot_from_compile(comp, prod_after)
            ren = _renamed(all_ops)
            ctx_a = {**ctx, "methods": [_rename_member(m, ren) for m in ctx.get("methods", [])],
                     "concern_classes": [ren.get(c, c) for c in ctx.get("concern_classes", [])]}
            tb = self.sim.extra_measure(snap_b, kind, core, central, ctx)
            ta = self.sim.extra_measure(snap_a, kind, core, central, ctx_a)
        else:
            tb = self.sim.target_measure(g_before, before, group["type"], core, central)
            ta = self.sim.target_measure(g_after, after, group["type"], core, central)
        predicted = self.sim.run(kept_ops, kind, core, central, ctx)
        news = cmp.new_smells(self.baseline(), comp, self.prod_files, prod_after, _renamed(all_ops))
        return {"new_smells": news, "new_smells_count": sum(len(n["elements"]) for n in news),
                "target_before": _small(tb), "target_after": _small(ta), "target_removed": not ta.get("present", True),
                "predicted_after": _small(predicted.target_after),
                "prediction_matches": _small(predicted.target_after) == _small(ta),
                "smells_before": dict(Counter(before["type"])), "smells_after": dict(Counter(after["type"]))}

    def verify_group(self, g: dict) -> dict:
        """Verify the recommended plan; if it does not fully work on the code, also verify up to
        ``FALLBACK_PLANS`` next-best acceptable plans and keep the best verified result."""
        ranked = [next(p for p in g["plans"] if p["name"] == g["recommended"])]
        seen = {frozenset(json.dumps(o, sort_keys=True, default=str) for o in ranked[0]["operations"])}
        for p in sorted(g["plans"], key=lambda p: -(p.get("score") or 0)):
            key = frozenset(json.dumps(o, sort_keys=True, default=str) for o in p.get("operations", []))
            if p["name"] != g["recommended"] and _acceptable(p) and key not in seen:
                ranked.append(p)
                seen.add(key)
        for old in (self.results / "verify" / g["group_id"]).glob("try_*"):
            shutil.rmtree(old, ignore_errors=True)
        tried = []
        for i, plan in enumerate(ranked[:1 + FALLBACK_PLANS]):
            r = self.verify_plan(g, plan, sub=None if i == 0 else f"try_{plan['name']}")
            r["score"] = plan.get("score")
            tried.append(r)
            if _complete(r):
                break
        best = max(range(len(tried)), key=lambda i: (_quality(tried[i]), -i))
        rec = dict(tried[best])
        rec["tried"] = [{"plan": t["plan"], "compiles": t.get("compiles"), "kept": t.get("kept"),
                         "operations": t.get("operations"), "target_removed": t.get("target_removed"),
                         "status": t.get("status"), "accepted": t.get("accepted")}
                        for t in tried]
        rec["recommended_plan"] = g["recommended"]
        if best:
            rec["fallback_from"] = g["recommended"]
            top = self.results / "verify" / g["group_id"]
            src = top / f"try_{rec['plan']}"
            for f in src.iterdir():
                if f.is_file():
                    shutil.copy2(f, top / f.name)
        rec["status"], rec["accepted"], rec["why_not_accepted"] = final_status(rec, self.max_api_breaks)
        (self.results / "verify" / g["group_id"] / "verification.json").write_text(
            json.dumps(rec, indent=1, default=str))
        return rec

    def run(self, groups: int | None = None, only: set[str] | None = None) -> list[dict]:
        plans = json.loads((self.results / "plans" / "plans.json").read_text())
        self.baseline()
        out = []
        todo = [g for g in plans["groups"] if g.get("recommended") and (not only or g["group_id"] in only)]
        for g in todo[:groups] if groups else todo:
            rec = self.verify_group(g)
            yn = {True: "yes", False: "no", None: "-"}
            self.log(f"{g['group_id']}: plan {rec['plan']}"
                     + (f" (fallback from {rec['fallback_from']})" if rec.get("fallback_from") else "")
                     + f": {rec['kept']}/{rec['operations']} operations compile "
                     f"({rec['reduction']}, {rec['compile_runs']} compiles); smell removed: "
                     f"{yn[rec.get('target_removed')]}; result equals simulation: {yn[rec.get('prediction_matches')]}"
                     + f"; {rec.get('build', {}).get('status', 'not built')}"
                     + f"; status {rec['status']}" + (" (accepted fix)" if rec.get("accepted") else "")
                     + f" [{rec['seconds']:.0f} s]")
            out.append(rec)
        # keep earlier results of groups that were not verified in this run
        prev_file = self.results / "verify" / "verification.json"
        if only and prev_file.exists():
            done = {r["group_id"] for r in out}
            out = [r for r in json.loads(prev_file.read_text()) if r["group_id"] not in done] + out
        summary = self.results / "verify" / "summary.csv"
        pd.DataFrame([{k: v for k, v in r.items() if not isinstance(v, (dict, list))} for r in out]).to_csv(
            summary, index=False)
        (self.results / "verify" / "verification.json").write_text(json.dumps(out, indent=1, default=str))
        return out


FALLBACK_PLANS = 2       # further plans verified when the recommended one does not fully work


def _acceptable(p: dict) -> bool:
    return bool(p.get("operations")) and (p.get("checks") or {}).get("status") != "blocked" \
        and (p.get("benefit") or 0) > 0 and (p.get("score") or 0) > 0


def _complete(r: dict) -> bool:
    """The plan works on the code as planned: an accepted fix with every step kept."""
    return bool(r.get("accepted")) and r.get("kept") == r.get("operations")


BUILD_FAILURES = ("breaks the build", "breaks tests", "tests did not finish")


def _improved(r: dict) -> bool:
    tb, ta = r.get("target_before") or {}, r.get("target_after") or {}
    return any(isinstance(v, (int, float)) and isinstance(ta.get(k), (int, float)) and ta[k] < v
               for k, v in tb.items() if k != "present")


def final_status(r: dict, max_api_breaks: int | None = None) -> tuple[str, bool, str]:
    """(status, accepted, why not accepted) of a verified plan.

    Status: ``not_applicable`` (no step could be applied), a build failure (``breaks the build``,
    ``breaks tests``, ``tests did not finish``), ``made_worse`` (new smells on the compiled code),
    ``removed``, ``reduced`` or ``no_effect``. A plan is an **accepted fix** only if it removed or reduced the
    smell, the project's own build compiled it, the selected tests that passed before still pass, it created no
    new smell, its patch applies to a clean checkout, and it breaks at most ``max_api_breaks`` public types and
    members."""
    if not r.get("kept"):
        return "not_applicable", False, "no step of the plan could be applied without compile errors"
    b = r.get("build") or {}
    if b.get("status") in BUILD_FAILURES:
        return b["status"], False, b.get("reason", "")
    if r.get("new_smells_count"):
        kinds = ", ".join(f"{len(n['elements'])} {n['level']} {n['type'].replace('_', ' ')}"
                          for n in r.get("new_smells", []))
        return "made_worse", False, f"new smells on the compiled code: {kinds}"
    st = "removed" if r.get("target_removed") else "reduced" if _improved(r) else "no_effect"
    if st == "no_effect":
        return st, False, "the smell is unchanged on the compiled code"
    why = []
    if b.get("status") in (None, "not built"):
        why.append(b.get("reason") or "not built with the project's build")
    if r.get("patch_applies") is False:
        why.append("the patch does not apply to a clean checkout")
    api = (r.get("api_breaks") or {}).get("count", 0)
    if max_api_breaks is not None and api > max_api_breaks:
        why.append(f"it changes {api} public types and members (limit {max_api_breaks})")
    return st, not why, "; ".join(why)


def _renamed(ops: list[dict]) -> dict[str, str]:
    """Old -> new fully qualified names of the classes a plan moves (tests moved along included)."""
    out = {}
    for o in ops:
        if o["op"] == "move_class":
            for c in [o["class"]] + list(o.get("tests", [])):
                out[c] = f"{o['to_package']}.{c.rsplit('.', 1)[-1]}"
    return out


def _rename_member(m: str, renamed: dict[str, str]) -> str:
    owner, _, rest = m.partition("#")
    for old in sorted(renamed, key=len, reverse=True):
        if owner == old or owner.startswith(old + "."):
            return renamed[old] + owner[len(old):] + ("#" + rest if rest else "")
    return m


def _small(t: dict) -> dict:
    keep = ("present", "core_packages_on_cycle", "dependencies_to_cut", "fan_in", "fan_out", "classes", "sets",
            "lcc", "methods", "core_classes_on_cycle")
    out = {k: t[k] for k in keep if k in t}
    if "less_stable" in t:
        out["less_stable"] = len(t["less_stable"])
    return out
