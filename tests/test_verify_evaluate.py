"""Phase 6: applying plans to the code, compiling, delta debugging, evaluation and report."""

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from dsarp.plan.facts import RepoFacts, group_facts
from dsarp.plan.simulate import Simulator
from dsarp.program import ProgramModel
from dsarp.verify import compile as cmp
from dsarp.verify.debug import Budget, Tester, ddmin, keep_passing
from dsarp.verify.rewrite import Rewriter, _strip_comments_and_strings, openrewrite_recipe
from dsarp.verify.run import Verifier
from dsarp.verify.workspace import Workspace

PROJECT = Path(__file__).parent / "fixtures" / "javaproj"
LEGACY = "com.acme.core.Engine#legacy(java.lang.String)"
MOVE_ENGINE = {"op": "move_class", "class": "com.acme.core.Engine", "from_package": "com.acme.core",
               "to_package": "com.acme.util"}
DELETE_LEGACY = {"op": "delete_member", "member": LEGACY, "owner": "com.acme.core.Engine"}
REMOVE_IMPORT = {"op": "remove_unused_import", "class": "com.acme.core.Engine", "import": "com.acme.util.Unused"}


# ---------------------------------------------------------------- pure helpers

def test_ddmin_finds_minimal_failing_subset():
    calls = []

    def fails(s):
        calls.append(s)
        return 3 in s and 7 in s
    assert sorted(ddmin(list(range(10)), fails)) == [3, 7]


def test_keep_passing_drops_culprits():
    kept, dropped, how = keep_passing(list(range(6)), lambda s: 4 not in s)
    assert kept == [0, 1, 2, 3, 5] and dropped == [4] and how == "ddmin"
    kept, dropped, how = keep_passing(list(range(6)), lambda s: not ({1, 4} <= set(s)) and 2 not in s)
    assert 2 in dropped and not ({1, 4} <= set(kept)) and how == "greedy"
    t = Tester(lambda s: True, max_tests=1)
    t([1])
    with pytest.raises(Budget):
        t([2])


def test_strip_comments_strings_and_recipe():
    code = 'package a;\nimport b.C;\nclass X { String t = "a/*b"; /* Foo */ Bar b; // Baz\n}'
    out = _strip_comments_and_strings(code)
    assert "Bar" in out and "Foo" not in out and "Baz" not in out and "import" not in out
    rec = openrewrite_recipe([MOVE_ENGINE, DELETE_LEGACY])
    assert "org.openrewrite.java.ChangeType" in rec and "newFullyQualifiedTypeName: com.acme.util.Engine" in rec


def test_new_errors_ignores_library_noise():
    before = pd.DataFrame([{"file": "a/X.java", "line": "3", "code": "compiler.err.cant.resolve.location",
                            "message": "cannot find symbol symbol: class Test location: class X"}])
    after = pd.DataFrame([
        {"file": "b/X.java", "line": "4", "code": "compiler.err.cant.resolve.location",
         "message": "cannot find symbol symbol: class Test location: class X"},
        {"file": "b/X.java", "line": "9", "code": "compiler.err.cant.resolve.location",
         "message": "cannot find symbol symbol: class Test location: class X"},
        {"file": "b/X.java", "line": "5", "code": "compiler.err.cant.resolve.location",
         "message": "cannot find symbol symbol: class Engine location: class X"}])
    new = cmp.new_errors(before, after, {"a/X.java": "b/X.java"})
    assert len(new) == 2                                            # one extra Test, one Engine
    new = cmp.new_errors(before, after, {"a/X.java": "b/X.java"}, repo_symbols={"Engine", "X"})
    assert list(new["line"]) == ["5"]                               # library symbol ignored


# ---------------------------------------------------------------- on the fixture project

needs_jdk = pytest.mark.skipif(not shutil.which("javac"), reason="needs a JDK (javac)")


@needs_jdk
def test_workspace_copy_reset_and_diff(tmp_path):
    with Workspace(PROJECT, tmp_path / "ws") as ws:
        f = ws.path / "core/src/main/java/com/acme/util/Unused.java"
        f.write_text(f.read_text() + "\n// edit\n")
        assert "Unused.java" in ws.diff()
        ws.reset()
        assert "// edit" not in f.read_text() and ws.diff().strip() == ""
    assert not (tmp_path / "ws").exists()


@needs_jdk
def test_rewriter_moves_class_and_fixes_references(project, tmp_path):
    rw = Rewriter(ProgramModel(project / "model"))
    checks = [{"edits": [{"edit": "make_public", "elements": ["com.acme.core.Engine#runs"]}]}]
    with Workspace(PROJECT, tmp_path / "ws") as ws:
        res = rw.apply(ws.path, [MOVE_ENGINE], checks)
        moved = ws.path / "core/src/main/java/com/acme/util/Engine.java"
        assert moved.exists() and not (ws.path / "core/src/main/java/com/acme/core/Engine.java").exists()
        text = moved.read_text()
        assert text.startswith("package com.acme.util;")
        assert "import com.acme.core.Service;" in text                    # old package class now needs an import
        assert "public int runs" in text                                  # widened by the checks
        assert "import com.acme.util.Engine;" in (ws.path / "web/src/main/java/com/acme/web/Page.java").read_text()
        assert "new com.acme.util.Engine()" in (
            ws.path / "core/src/main/java/com/acme/core/internal/Activator.java").read_text()
        assert "import com.acme.util.Engine;" in (
            ws.path / "core/src/test/java/com/acme/core/EngineTest.java").read_text()
        assert res.moved == {"core/src/main/java/com/acme/core/Engine.java":
                             "core/src/main/java/com/acme/util/Engine.java"}
        assert "core/src/main/java/com/acme/util/Engine.java" in rw.files_after(res)


@needs_jdk
def test_rewriter_delete_and_remove_import(project, tmp_path):
    rw = Rewriter(ProgramModel(project / "model"))
    with Workspace(PROJECT, tmp_path / "ws") as ws:
        rw.apply(ws.path, [DELETE_LEGACY, REMOVE_IMPORT])
        text = (ws.path / "core/src/main/java/com/acme/core/Engine.java").read_text()
        assert "legacy" not in text and "@Deprecated" not in text and "import com.acme.util.Unused;" not in text
        assert "public String run(" in text


@needs_jdk
def test_verifier_compiles_drops_breaking_operation_and_detects_again(project):
    v = Verifier(project, PROJECT, log=lambda *_: None)
    group = {"group_id": "G-test", "type": "cyclic_dependency", "core": ["com.acme.core", "com.acme.util"],
             "central": ""}
    plan = {"name": "manual", "operations": [REMOVE_IMPORT, DELETE_LEGACY,
                                             {"op": "move_class", "class": "com.acme.util.Dates",
                                              "from_package": "com.acme.util", "to_package": "com.acme.core"}]}
    rec = v.verify_plan(group, plan)
    assert rec["compiles"] and rec["kept"] == 2 and rec["dropped"] == 1
    assert rec["dropped_operations"][0]["op"] == "delete_member"          # callers still use legacy()
    assert rec["new_errors_full_plan"] > 0
    assert rec["target_removed"] and rec["prediction_matches"]
    out = project / "verify" / "G-test"
    assert "rename" in (out / "plan.patch").read_text()
    assert "ChangeType" in (out / "rewrite.yml").read_text()


@needs_jdk
def test_verify_cli_and_report(project, capsys):
    from dsarp.cli import main
    assert main(["plan", str(project), "--groups", "1", "--time-limit", "5", "--backend", "highs",
                 "--search", "never", "--explain"]) == 0
    assert main(["verify", str(project), "--repo", str(PROJECT)]) == 0
    rec = json.loads((project / "verify" / "verification.json").read_text())
    assert rec and rec[0]["compiles"]
    assert main(["report", str(project)]) == 0
    page = (project / "report.html").read_text()
    assert page.startswith("<!DOCTYPE html>") and "Verification on the code" in page and "<h3>" in page
    assert "Generated by DSARP" in page


@needs_jdk
def test_baselines_and_harness(project):
    from dsarp.evaluate.baselines import Evaluator, llm_only, nsga2, random_search
    from dsarp.evaluate.harness import Harness, sensitivity, summarise
    from dsarp.search.llm import Scripted
    rf = RepoFacts.build(ProgramModel(project / "model"))
    scopes = json.loads((project / "triage" / "scopes.json").read_text())
    gid = next(iter(scopes))
    gf = group_facts(rf, gid, "cyclic_dependency", scopes[gid])
    ev = Evaluator(rf, gf, Simulator(rf))
    plan, info = nsga2(ev, evaluations=80, population=10)
    assert plan is not None and info["generations"] >= 1
    assert ev.evaluate({o["op"] + str(i): o for i, o in enumerate(plan.operations)})["benefit"] >= 0
    rplan, _ = random_search(Evaluator(rf, gf, Simulator(rf)), evaluations=30)
    assert rplan is not None
    lplan, linfo = llm_only(Evaluator(rf, gf, Simulator(rf)), Scripted(['{"actions": [0, 99]}']))
    assert linfo == {"proposed": 2, "valid": 1} and lplan is not None
    h = Harness(project, backend="highs", time_limit=5, search_time=5, budget=40, log=lambda *_: None)
    df = h.run(1)
    assert {"solver", "hybrid", "search_only", "nsga2", "random"} <= set(df["approach"])
    s = summarise(df)
    assert "mean_score" in s.columns and s["groups"].max() == 1
    plans = {"groups": [{"type": "cyclic_dependency", "recommended": "a", "plans": [
        {"name": "a", "change_cost": 1.0, "checks": {"status": "ok", "made_public": 0},
         "simulation": {"target_before": {"present": True, "core_packages_on_cycle": 2, "dependencies_to_cut": 1},
                        "target_after": {"present": False}, "new_smells": [], "removed_smells": []}},
        {"name": "b", "change_cost": 30.0, "checks": {"status": "ok", "made_public": 0},
         "simulation": {"target_before": {"present": True, "core_packages_on_cycle": 2, "dependencies_to_cut": 1},
                        "target_after": {"present": False}, "new_smells": [], "removed_smells": []}}]}]}
    sens = sensitivity(plans)
    assert sens["recommendation_unchanged"] == 1.0 and sens["perturbations"] > 0


def test_markdown_subset():
    from dsarp.report import markdown
    h = markdown("## Title\n- **a** `x`\n  - b\n\n| h | i |\n|---|---|\n| 1 | 2 |\n> note")
    assert "<h3>Title</h3>" in h and "<b>a</b>" in h and "<code>x</code>" in h and "<table>" in h
    assert "<blockquote>note</blockquote>" in h and h.count("<ul>") == 2


def test_git_available():
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0
