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
    # the same in two shards, merged: the same groups and results
    (project / "verify" / "verification.json").unlink()
    assert main(["verify", str(project), "--repo", str(PROJECT), "--shard", "0/2"]) == 0
    assert main(["verify", str(project), "--repo", str(PROJECT), "--shard", "1/2"]) == 0
    assert (project / "verify" / "verification-shard0of2.json").exists()
    assert main(["verify", str(project), "--merge"]) == 0
    merged = json.loads((project / "verify" / "verification.json").read_text())
    assert [r["group_id"] for r in merged] == [r["group_id"] for r in rec]
    assert [r["status"] for r in merged] == [r["status"] for r in rec]
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


def test_new_errors_filters_duplicates_and_library_packages():
    before = pd.DataFrame(columns=["file", "line", "code", "message"])
    after = pd.DataFrame([
        {"file": "t/A.java", "line": "1", "code": "compiler.err.duplicate.class", "message": "duplicate class: x.ATest"},
        {"file": "m/B.java", "line": "2", "code": "compiler.err.duplicate.class", "message": "duplicate class: p.Moved"},
        {"file": "m/C.java", "line": "3", "code": "compiler.err.doesnt.exist",
         "message": "package com.fasterxml.jackson.databind does not exist"},
        {"file": "m/C.java", "line": "4", "code": "compiler.err.doesnt.exist", "message": "package p.old does not exist"}])
    new = cmp.new_errors(before, after, plan_types={"p.Moved"}, repo_packages={"p", "p.old"})
    assert list(new["line"]) == ["2", "4"]


def test_imports_simple_detects_shadowing_imports():
    from dsarp.verify.rewrite import _add_imports, _imports_simple
    text = "package a;\n\nimport org.w3c.dom.Element;\nimport static x.Y.Element;\n\nclass Z {}\n"
    assert _imports_simple(text, "Element") and not _imports_simple(text, "Node")
    out = _add_imports(text, ["b.Node"])
    assert "import b.Node;" in out and out.index("import b.Node;") > out.index("import org.w3c.dom.Element;")


def test_drop_unused_imports_after_deletion():
    from dsarp.verify.rewrite import _drop_unused_imports
    lines = ["package a;", "import b.Tika;", "import b.Used;", "import static c.D.x;", "",
             "class M { /** {@link Tika} */ Used u; }"]
    out = _drop_unused_imports(lines)
    assert "import b.Tika;" not in out and "import b.Used;" in out and "import static c.D.x;" in out


def test_line_endings_are_preserved(tmp_path):
    from dsarp.verify.rewrite import _add_imports, _read_text, _write_text
    f = tmp_path / "A.java"
    f.write_bytes(b"package a;\r\n\r\nimport b.C;\r\n\r\nclass A {}\r\n")
    text = _read_text(f)
    assert "\r\n" in text
    _write_text(f, _add_imports(text.replace("package a;", "package z;"), ["b.D"]))
    raw = f.read_bytes()
    assert b"package z;\r\n" in raw and b"import b.D;\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_report_counts_plans_without_kept_steps_as_not_compiling(project):
    from dsarp.report import build
    vdir = project / "verify"
    vdir.mkdir(exist_ok=True)
    (vdir / "verification.json").write_text(json.dumps([
        {"group_id": "G1", "compiles": True, "kept": 0, "operations": 1, "reduction": "ddmin", "compile_runs": 2,
         "dropped_operations": [{"op": "move_class", "class": "a.B", "to_package": "c"}]}]))
    page = build(project)
    assert "every step broke the build" in page and "not applicable on the code" in page
    from dsarp.outcome import outcome
    assert outcome({"recommended": "x"}, {"kept": 0, "operations": 1, "compiles": True}) == "not applicable"
    (vdir / "verification.json").unlink()


def test_new_errors_ignores_names_imported_from_libraries(tmp_path):
    (tmp_path / "A.java").write_text("package p.q;\nimport org.osgi.service.event.Event;\nclass A {}\n")
    before = pd.DataFrame(columns=["file", "line", "code", "message"])
    after = pd.DataFrame([{"file": "A.java", "line": "9", "code": "compiler.err.cant.resolve.location",
                           "message": "cannot find symbol symbol: class Event location: class p.q.A"}])
    assert len(cmp.new_errors(before, after, repo_symbols={"Event"}, repo_packages={"p", "p.q"})) == 1
    assert cmp.new_errors(before, after, repo_symbols={"Event"}, repo_packages={"p", "p.q"},
                          root=tmp_path).empty


@needs_jdk
def test_moved_public_class_keeps_a_compiling_deprecated_stub(project):
    v = Verifier(project, PROJECT, log=lambda *_: None, compat="stubs", build_tool="none")
    group = {"group_id": "G-stub", "type": "cyclic_dependency", "core": ["com.acme.core", "com.acme.util"],
             "central": ""}
    plan = {"name": "manual", "operations": [{**MOVE_ENGINE, "check": {"edits": [
        {"edit": "make_public", "elements": ["com.acme.core.Engine#runs"]}]}}]}
    rec = v.verify_plan(group, plan)
    assert rec["kept"] == 1 and rec["compat"]["stubs"] == ["com.acme.core.Engine"] and rec["compat"]["failed"] == []
    stub = (project / "verify" / "G-stub" / "compat.patch").read_text()
    assert "core/src/main/java/com/acme/core/Engine.java" in stub
    assert "@Deprecated" in stub and "public class Engine extends com.acme.util.Engine" in stub
    assert rec["api_breaks"]["count"] < rec["api_breaks_without_stubs"]
    assert rec["compat_patch_applies"] is True
    # the stub adds a dependency from the old package to the new one: measured, not hidden
    assert rec["compat"]["target_removed_with_stubs"] in (True, False)


@needs_jdk
def test_rewriter_updates_class_names_in_resources_and_osgi_exports(project, tmp_path):
    rw = Rewriter(ProgramModel(project / "model"))
    op = {"op": "move_class", "class": "com.acme.core.Engine", "from_package": "com.acme.core",
          "to_package": "com.acme.core.engine"}
    with Workspace(PROJECT, tmp_path / "ws") as ws:
        res_dir = ws.path / "core/src/main/resources"
        res_dir.mkdir(parents=True, exist_ok=True)
        (res_dir / "app.properties").write_text("engine=com.acme.core.Engine\nother=com.acme.core.EngineFactory\n")
        (ws.path / "core/core.bnd").write_text("Export-Package: com.acme.core;version=1.0, com.acme.util\n")
        res = rw.apply(ws.path, [op], [{"edits": [{"edit": "make_public", "elements": ["com.acme.core.Engine#runs"]}]}])
        props = (res_dir / "app.properties").read_text()
        assert "engine=com.acme.core.engine.Engine" in props
        assert "other=com.acme.core.EngineFactory" in props                 # a different class is left alone
        assert "com.acme.core.engine,com.acme.core;version=1.0" in (ws.path / "core/core.bnd").read_text()
        assert "core/src/main/resources/app.properties" in res.changed


@needs_jdk
def test_no_plan_is_proven_against_a_partial_baseline(project):
    (project / "baseline").mkdir(exist_ok=True)
    (project / "baseline" / "baseline.json").write_text(json.dumps(
        {"tool": "maven", "complete": False, "reasons": ["the test suite did not finish within 60 s"],
         "tests_run": 3, "failing": [], "flaky": []}))
    v = Verifier(project, PROJECT, log=lambda *_: None)
    group = {"group_id": "G-part", "type": "cyclic_dependency", "core": ["com.acme.core", "com.acme.util"],
             "central": ""}
    plan = {"name": "manual", "operations": [{**MOVE_ENGINE, "check": {"edits": [
        {"edit": "make_public", "elements": ["com.acme.core.Engine#runs"]}]}}]}
    rec = v.verify_plan(group, plan)
    assert rec["build"]["check"] == "partial baseline" and "did not finish" in rec["build"]["reason"]
    assert not rec["accepted"] and "partial" in rec["why_not_accepted"]
    (project / "baseline" / "baseline.json").unlink()


def _compile_modules(repo):
    roots = sorted(repo.glob("*/src/main/java"))
    stubs = repo.parent / "stubs"                       # the fixture uses a library class that is not there
    (stubs / "org" / "external").mkdir(parents=True, exist_ok=True)
    (stubs / "org" / "external" / "Missing.java").write_text("package org.external; public class Missing {}")
    sp = ":".join(str(r) for r in roots + [stubs])
    for r in roots:
        out = r.parents[2] / "target" / "classes"
        out.mkdir(parents=True, exist_ok=True)
        files = [str(f) for f in r.rglob("*.java")]
        subprocess.run(["javac", "-nowarn", "-implicit:none", "-sourcepath", sp, "-d", str(out), *files],
                       check=True, capture_output=True)


@needs_jdk
def test_model_is_aligned_with_the_bytecode(project, tmp_path):
    import shutil as sh
    from dsarp.align import align
    from dsarp.program import ProgramModel
    repo = tmp_path / "repo"
    sh.copytree(PROJECT, repo)
    res = tmp_path / "res"
    sh.copytree(project / "model", res / "model")
    _compile_modules(repo)
    info = align(res, repo, log=lambda *_: None)
    assert info["complete"] and len(info["class_dirs"]) == 2
    assert info["class_pairs"]["both"] > 0
    m = ProgramModel(res / "model")
    assert m.aligned and "in_bytecode" in m.deps and m.deps["in_bytecode"].any()
    raw = ProgramModel(res / "model", aligned=False)
    assert not raw.aligned
    # the class graph has exactly the pairs the bytecode shows (production classes)
    g = m.type_graph(collapse_nested=True)
    byte = set(map(tuple, pd.read_csv(res / "model" / "bytecode_edges.csv").values.tolist()))
    assert set(g.edges()) <= byte
    # a partial bytecode graph is refused
    sh.rmtree(repo / "web" / "target")
    info = align(res, repo, log=lambda *_: None)
    assert not info["complete"] and info["modules_without_classes"] == ["web"]
    assert not ProgramModel(res / "model").aligned


# ---------------------------------------------------------------- Phase B: measured API, coverage, steps

FAKE_MAVEN = r"""#!/bin/sh
# stand-in Maven: compiles every module with javac; a test goal writes one passing test report per module
SP=""
for r in */src/main/java; do SP="$SP$PWD/$r:"; done
SP="$SP$PWD/libstubs"
for r in */src/main/java; do
  m=${r%/src/main/java}; rm -rf "$m/target/classes"; mkdir -p "$m/target/classes"
  javac -nowarn -implicit:none -sourcepath "$SP" -d "$m/target/classes" $(find "$r" -name '*.java') || exit 1
done
case " $* " in *" test "*)
  mkdir -p core/target/surefire-reports
  echo '<testsuite name="t"><testcase classname="com.acme.core.EngineTest" name="t"/></testsuite>' \
    > core/target/surefire-reports/TEST-core.xml ;;
esac
case "$*" in *prepare-agent*)
  mkdir -p core/target/site/jacoco
  printf 'GROUP,PACKAGE,CLASS,INSTRUCTION_MISSED,INSTRUCTION_COVERED,LINE_MISSED,LINE_COVERED\n' > core/target/site/jacoco/jacoco.csv
  printf 'core,com.acme.core,Engine,5,5,5,5\ncore,com.acme.util,Unused,4,0,4,0\n' >> core/target/site/jacoco/jacoco.csv ;;
esac
exit 0
"""

FAKE_JAPICMP = """
import java.io.*; import java.util.*; import java.util.zip.*;
public class FakeJapicmp {
  static Set<String> names(String jar) throws IOException {
    Set<String> s = new TreeSet<>();
    try (ZipFile z = new ZipFile(jar)) { for (Enumeration<? extends ZipEntry> e = z.entries(); e.hasMoreElements();) {
      String n = e.nextElement().getName(); if (n.endsWith(".class")) s.add(n.replace('/', '.').replace(".class", "")); } }
    return s;
  }
  public static void main(String[] a) throws IOException {
    String o = null, n = null, x = null;
    for (int i = 0; i < a.length; i++) { if (a[i].equals("-o")) o = a[++i]; else if (a[i].equals("-n")) n = a[++i];
      else if (a[i].equals("-x")) x = a[++i]; }
    Set<String> after = names(n);
    StringBuilder b = new StringBuilder("<japicmp><classes>");
    for (String c : names(o)) if (!after.contains(c))
      b.append("<class fullyQualifiedName=\\"" + c + "\\" changeStatus=\\"REMOVED\\" binaryCompatible=\\"false\\"/>");
    b.append("</classes></japicmp>");
    try (Writer w = new FileWriter(x)) { w.write(b.toString()); }
  }
}
"""


def test_japicmp_report_is_counted(tmp_path):
    from dsarp.verify.apicheck import count, pack
    xml = tmp_path / "r.xml"
    xml.write_text("""<japicmp><classes>
<class fullyQualifiedName="a.Gone" changeStatus="REMOVED" binaryCompatible="false"><methods>
 <method name="x" binaryCompatible="false" changeStatus="REMOVED"/></methods></class>
<class fullyQualifiedName="a.Kept" changeStatus="MODIFIED" binaryCompatible="false">
 <compatibilityChanges><compatibilityChange type="INTERFACE_REMOVED" binaryCompatible="false"/></compatibilityChanges>
 <methods><method name="run" binaryCompatible="false" changeStatus="REMOVED"/>
          <method name="ok" binaryCompatible="true" changeStatus="UNCHANGED"/></methods>
 <fields><field name="f" binaryCompatible="false" changeStatus="MODIFIED"/></fields></class>
<class fullyQualifiedName="a.Same" changeStatus="UNCHANGED" binaryCompatible="true"/>
</classes></japicmp>""")
    r = count(xml)
    assert r["count"] == 4 and r["removed_types"] == 1          # a removed type counts once, not per member
    assert "a.Gone removed" in r["examples"] and any("INTERFACE_REMOVED" in e for e in r["examples"])
    d = tmp_path / "classes" / "a"
    d.mkdir(parents=True)
    (d / "X.class").write_bytes(b"\xca\xfe")
    assert pack([tmp_path / "classes", tmp_path / "classes"], tmp_path / "x.jar") == 1


@pytest.fixture
def buildable(project, tmp_path, monkeypatch):
    """The fixture project with a stand-in Maven (javac) and a stand-in japicmp, its baseline run."""
    import stat
    from dsarp.verify import baseline as bsl
    repo = tmp_path / "repo"
    shutil.copytree(PROJECT, repo)
    (repo / ".gitignore").write_text("target/\n")
    lib = repo / "libstubs" / "org" / "external"
    lib.mkdir(parents=True)
    (lib / "Missing.java").write_text("package org.external; public class Missing {}")
    (repo / "mvnw").write_text(FAKE_MAVEN)
    (repo / "mvnw").chmod((repo / "mvnw").stat().st_mode | stat.S_IEXEC)
    jc = tmp_path / "japicmp"
    jc.mkdir()
    (jc / "FakeJapicmp.java").write_text(FAKE_JAPICMP)
    subprocess.run(["javac", "-d", str(jc), str(jc / "FakeJapicmp.java")], check=True)
    subprocess.run(["jar", "cfe", str(jc / "fake.jar"), "FakeJapicmp", "-C", str(jc), "FakeJapicmp.class"], check=True)
    monkeypatch.setenv("DSARP_JAPICMP_JAR", str(jc / "fake.jar"))
    res = tmp_path / "res"
    for d in ("model", "triage"):
        shutil.copytree(project / d, res / d)
    b = bsl.run(repo, res / "baseline", "maven", timeout=120, log=lambda *_: None)
    assert b["complete"] and b["coverage"]["classes"] == 2
    return repo, res


@needs_jdk
def test_accepted_fix_gets_measured_api_coverage_and_step_patches(buildable):
    repo, res = buildable
    v = Verifier(res, repo, log=lambda *_: None, compat="none")
    group = {"group_id": "G-b", "type": "cyclic_dependency", "core": ["com.acme.core", "com.acme.util"],
             "central": ""}
    plan = {"name": "manual", "operations": [REMOVE_IMPORT, {**MOVE_ENGINE, "check": {"edits": [
        {"edit": "make_public", "elements": ["com.acme.core.Engine#runs"]}]}}]}
    rec = v.verify_plan(group, plan)
    assert rec["build"]["status"] == "builds and tests pass", rec["build"]
    assert rec["accepted"], rec["why_not_accepted"]
    # japicmp on the compiled classes: the moved public class is gone from its old package
    am = rec["api_measured"]
    assert am["measured"] and am["count"] >= 1 and "com.acme.core.Engine removed" in am["examples"]
    assert rec["coverage"]["classes"] >= 1 and rec["coverage"]["mean"] == 0.5
    # one patch per step, each made from the clean checkout, and each compiles
    st = rec["steps"]
    assert st["count"] == 2 and st["all_compile"] and st["checkpoints"] == [1, 2]
    out = res / "verify" / "G-b" / "steps"
    one, two = (out / "step_01.patch").read_text(), (out / "step_02.patch").read_text()
    assert "Unused" in one and "rename" not in one
    assert "rename" in two and "import com.acme.util.Unused" not in two
    # the measured count decides acceptance when it is over the limit
    v2 = Verifier(res, repo, log=lambda *_: None, compat="none", max_api_breaks=0, step_patches=False)
    rec2 = v2.verify_plan({**group, "group_id": "G-b2"}, plan)
    assert not rec2["accepted"] and "measured with japicmp" in rec2["why_not_accepted"]
    # with the compatibility stubs compiled in, the old name is still there for code outside the repository
    v3 = Verifier(res, repo, log=lambda *_: None, compat="stubs", step_patches=False)
    rec3 = v3.verify_plan({**group, "group_id": "G-b3"}, plan)
    assert rec3["compat"]["stubs"] == ["com.acme.core.Engine"]
    assert rec3["api_measured"]["measured"] and rec3["api_measured"]["count"] < am["count"]
    assert "com.acme.core.Engine removed" not in rec3["api_measured"]["examples"]


def test_confidence_combines_calibration_agreement_and_coverage():
    from dsarp.plan.confidence import annotate
    a = {"op": "move_class", "class": "p.A", "to_package": "q"}
    b = {"op": "move_class", "class": "p.B", "to_package": "q"}
    g = {"plans": [{"name": "x", "operations": [dict(a), dict(b)], "checks": {"coverage": 1.0}},
                   {"name": "y", "operations": [dict(a)], "checks": {}},
                   {"name": "no_change", "operations": []}]}
    annotate(g, (0.8, 10), {"p.A": 1.0})
    x, y = g["plans"][0], g["plans"][1]
    assert x["confidence"]["agreement"] == 0.5 and x["confidence"]["value"] == round(0.8 * 0.75 * 1.0, 3)
    assert y["confidence"]["agreement"] == 1.0 and y["confidence"]["value"] == round(0.8 * 1.0 * 0.75, 3)
    assert x["operations"][0]["planners_agreeing"] == "2/2" and x["operations"][1]["planners_agreeing"] == "1/2"
    assert x["operations"][0]["confidence"] > x["operations"][1]["confidence"]
    assert "confidence" not in g["plans"][2]


def test_lessons_calibrate_api_and_acceptance(tmp_path):
    from dsarp.lessons import Lessons
    ls = Lessons(tmp_path)
    assert ls.api_factor() == 1.0 and ls.calibration("cyclic_dependency") == (0.5, 0)
    for i in range(3):
        ls.learn({"group_id": f"G{i}", "type": "cyclic_dependency", "kept": 1, "accepted": i < 2,
                  "api_breaks": {"count": 1}, "api_measured": {"measured": True, "count": 3}}, [], [])
    assert ls.api_factor() == round(10 / 4, 3)
    assert ls.calibration("cyclic_dependency") == (round(3 / 5, 3), 3)
    ls.save()
    other = Lessons(tmp_path, tag="-2")                      # another shard reads the log too
    assert other.calibration()[1] == 3
