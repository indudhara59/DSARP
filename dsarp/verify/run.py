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
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from ..plan.facts import RepoFacts
from ..plan.simulate import Simulator, interface_name
from ..program import ProgramModel
from . import build as bld
from . import compile as cmp
from .debug import Tester, keep_passing
from .detectors import run_detector
from .rewrite import Rewriter, openrewrite_recipe
from .workspace import Workspace


def _flag(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1"])


class Verifier:
    def __init__(self, results: str | Path, repo: str | Path, workdir: str | Path | None = None,
                 memory: str = "4g", classpath: str = "", build_tool: str = "none", run_tests: bool = True,
                 max_compiles: int = 30, detector_cmds: dict[str, str] | None = None, log=print):
        self.results, self.repo = Path(results), Path(repo)
        self.workdir = Path(workdir) if workdir else self.results / "verify" / "work"
        self.memory, self.classpath, self.log = memory, classpath, log
        self.build_tool, self.run_tests, self.max_compiles = build_tool, run_tests, max_compiles
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
        self.symbols = {t.rsplit(".", 1)[-1].split("$")[-1] for t in self.model.types["type"]} | \
            set(self.model.members["name"]) | {t.rsplit(".", 1)[-1] for t in self.model.types["package"].dropna()}

    # ------------------------------------------------------------------ baseline

    def baseline(self) -> cmp.CompileResult:
        if self._baseline is None:
            t0 = time.time()
            self._baseline = cmp.compile_tree(self.repo, self.rw.java_files, self.roots,
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
        renamed = {o["class"]: f"{o['to_package']}.{o['class'].rsplit('.', 1)[-1]}"
                   for o in ops if o["op"] == "move_class"}
        new = cmp.new_errors(self.baseline().errors, comp.errors, res.moved, renamed,
                             self.symbols | {n.rsplit(".", 1)[-1] for n in res.created_types})
        return new.empty, res, comp, new

    def _production_after(self, res, ops) -> set[str]:
        prod = {res.moved.get(f, f) for f in self.prod_files}
        for c in res.created:
            prod.add(c)
        return prod

    # ------------------------------------------------------------------ plan

    def verify_plan(self, group: dict, plan: dict) -> dict:
        gid = group["group_id"]
        out = self.results / "verify" / gid
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
                prod_after = self._production_after(res, kept_ops)
                after = cmp.detect(comp, prod_after)
                g_after, _ = cmp.package_graph(comp, prod_after)
                before = cmp.detect(self.baseline(), self.prod_files)
                g_before, _ = cmp.package_graph(self.baseline(), self.prod_files)
                central = group.get("central", "") if group["type"] != "cyclic_dependency" else ""
                core = group.get("core", [])
                tb = self.sim.target_measure(g_before, before, group["type"], core, central)
                ta = self.sim.target_measure(g_after, after, group["type"], core, central)
                predicted = self.sim.run(kept_ops, group["type"], core, central)
                rec.update({
                    "target_before": _small(tb), "target_after": _small(ta),
                    "target_removed": not ta.get("present", True),
                    "predicted_after": _small(predicted.target_after),
                    "prediction_matches": _small(predicted.target_after) == _small(ta),
                    "smells_before": dict(Counter(before["type"])), "smells_after": dict(Counter(after["type"]))})
            # patch and recipe
            patch = ws.diff()
            (out / "plan.patch").write_text(patch)
            (out / "rewrite.yml").write_text(openrewrite_recipe(kept_ops, f"dsarp.{gid.replace('-', '_')}"))
            (out / "operations.json").write_text(json.dumps({"kept": kept_ops, "dropped": rec["dropped_operations"]},
                                                            indent=1))
            rec["patch_lines"] = patch.count("\n")
            rec["files_changed"] = len(res.changed) + len(res.created)
            rec["notes"] = res.notes[:50]
            # build and tests
            if self.build_tool != "none" and kept_ops:
                changed = {o.get("class") or o.get("owner") or o.get("provider") for o in kept_ops}
                changed |= {interface_name(o) for o in kept_ops if o["op"] == "extract_interface"}
                tests = bld.select_tests(self.model, changed) if self.run_tests else []
                modules = sorted({self.file_module.get(f, "") for f in
                                  [self.rw.type_file.get(c, "") for c in changed]} - {""})
                ws.reset()
                base_b = bld.build(ws.path, modules, tests, self.build_tool)
                self.rw.apply(ws.path, kept_ops, [checks[i] for i in kept])
                after_b = bld.build(ws.path, modules, tests, self.build_tool)
                rec["build"] = {"tool": after_b.tool, "modules": modules, "tests_selected": len(tests),
                                "baseline_ok": base_b.ok, "ok": after_b.ok, "seconds": after_b.seconds,
                                "command": " ".join(after_b.command), "log_tail": after_b.log_tail[-3000:]}
            # detectors
            for tool, command in self.detector_cmds.items():
                rec.setdefault("detectors", {})[tool] = run_detector(tool, command, ws.path, out / f"{tool}-after")
        rec["seconds"] = round(time.time() - t0, 1)
        (out / "verification.json").write_text(json.dumps(rec, indent=1, default=str))
        return rec

    def run(self, groups: int | None = None, only: set[str] | None = None) -> list[dict]:
        plans = json.loads((self.results / "plans" / "plans.json").read_text())
        self.baseline()
        out = []
        todo = [g for g in plans["groups"] if g.get("recommended") and (not only or g["group_id"] in only)]
        for g in todo[:groups] if groups else todo:
            plan = next(p for p in g["plans"] if p["name"] == g["recommended"])
            rec = self.verify_plan(g, plan)
            self.log(f"{g['group_id']}: {rec['kept']}/{rec['operations']} operations compile "
                     f"({rec['reduction']}, {rec['compile_runs']} compiles); target removed: "
                     f"{rec.get('target_removed')}, as predicted: {rec.get('prediction_matches')}"
                     + (f"; build {'ok' if rec['build']['ok'] else 'FAILED'}" if "build" in rec else "")
                     + f" [{rec['seconds']:.0f} s]")
            out.append(rec)
        summary = self.results / "verify" / "summary.csv"
        pd.DataFrame([{k: v for k, v in r.items() if not isinstance(v, (dict, list))} for r in out]).to_csv(
            summary, index=False)
        (self.results / "verify" / "verification.json").write_text(json.dumps(out, indent=1, default=str))
        return out


def _small(t: dict) -> dict:
    keep = ("present", "core_packages_on_cycle", "dependencies_to_cut", "fan_in", "fan_out")
    out = {k: t[k] for k in keep if k in t}
    if "less_stable" in t:
        out["less_stable"] = len(t["less_stable"])
    return out
