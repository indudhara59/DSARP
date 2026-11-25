"""What counts as a fix: the project's own build and tests, new smells, public API, patches (version 0.12)."""

import os
import shutil
import stat
import subprocess
from types import SimpleNamespace

import pandas as pd
import pytest

from dsarp.plan.api import ApiIndex
from dsarp.verify import build as bld
from dsarp.verify import compile as cmp
from dsarp.verify.run import final_status
from dsarp.verify.workspace import Workspace

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

FAKE_MVN = """#!/bin/sh
# stands in for Maven: test-compile fails when BROKEN exists; test writes a Surefire report
case " $* " in
  *" test-compile "*) [ -f BROKEN ] && { echo "[ERROR] cannot find symbol"; exit 1; }; mkdir -p target/classes; exit 0 ;;
esac
[ -f SLOW ] && sleep 5
mkdir -p target/surefire-reports
if [ -f FAILING ]; then
  cat > target/surefire-reports/TEST-com.a.OrderTest.xml <<EOF
<testsuite name="com.a.OrderTest" tests="2" failures="1"><testcase classname="com.a.OrderTest" name="ok"/>
<testcase classname="com.a.OrderTest" name="total"><failure message="expected 3"/></testcase></testsuite>
EOF
  exit 1
fi
cat > target/surefire-reports/TEST-com.a.OrderTest.xml <<EOF
<testsuite name="com.a.OrderTest" tests="2"><testcase classname="com.a.OrderTest" name="ok"/>
<testcase classname="com.a.OrderTest" name="total"/><testcase classname="com.a.OrderTest" name="off"><skipped/></testcase></testsuite>
EOF
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    m = tmp_path / "mvnw"
    m.write_text(FAKE_MVN)
    m.chmod(m.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def test_build_separates_compile_errors_test_failures_and_timeouts(project):
    base = bld.build(project, [], ["com.a.OrderTest"], "maven", test_timeout=3)
    assert base.compiled and base.ok and base.tests_run == 2 and base.failing == []
    (project / "FAILING").write_text("")
    after = bld.build(project, [], ["com.a.OrderTest"], "maven", test_timeout=3)
    assert after.compiled and not after.ok and after.failing == ["com.a.OrderTest#total"]
    c = bld.compare(base, after)
    assert c["status"] == "breaks tests" and c["new_failures"] == ["com.a.OrderTest#total"]
    # a test that failed before already is not blamed on the plan
    assert bld.compare(after, after)["status"] == "builds and tests pass"
    (project / "FAILING").unlink()
    (project / "BROKEN").write_text("")
    broken = bld.build(project, [], ["com.a.OrderTest"], "maven", test_timeout=3)
    assert not broken.compiled and bld.compare(base, broken)["status"] == "breaks the build"
    # the original code does not build here (e.g. dependencies not downloaded): only the compiler check counts
    assert bld.compare(broken, base)["status"] == "not built"
    (project / "BROKEN").unlink()
    (project / "SLOW").write_text("")
    slow = bld.build(project, [], ["com.a.OrderTest"], "maven", test_timeout=1)
    assert slow.compiled and not slow.tests_finished and not slow.ok
    assert bld.compare(base, slow)["status"] == "tests did not finish"
    assert bld.compare(slow, slow)["status"] == "builds"          # unfinished before as well: build compared
    nobuild = bld.build(project / "nothing", [], [], "auto") if (project / "nothing").mkdir() is None else None
    assert nobuild.tool == "none" and bld.compare(nobuild, nobuild)["status"] == "not built"


def test_stale_class_files_are_removed_before_building(project):
    old = project / "core" / "target" / "classes" / "com" / "a"
    old.mkdir(parents=True)
    (old / "Moved.class").write_bytes(b"\xca\xfe")
    bld.build(project, [], [], "maven")
    assert not (project / "core" / "target" / "classes" / "com" / "a" / "Moved.class").exists()


def _verified(**kw):
    r = {"kept": 2, "operations": 2, "compiles": True, "target_removed": True,
         "target_before": {"present": True, "packages": 3}, "target_after": {"present": False, "packages": 0},
         "build": {"status": "builds and tests pass", "check": "build and 4 tests"}, "patch_applies": True,
         "api_breaks": {"count": 3}, "new_smells": [], "new_smells_count": 0}
    r.update(kw)
    return r


def test_only_built_tested_harmless_plans_are_accepted_fixes():
    assert final_status(_verified(), 30) == ("removed", True, "")
    assert final_status(_verified(kept=0), 30)[:2] == ("not_applicable", False)
    st, ok, why = final_status(_verified(build={"status": "breaks tests", "reason": "1 tests fail"}), 30)
    assert (st, ok) == ("breaks tests", False) and "fail" in why
    st, ok, why = final_status(_verified(build={"status": "not built", "reason": "no pom.xml"}), 30)
    assert (st, ok) == ("removed", False) and "pom.xml" in why
    news = [{"type": "cyclic_dependency", "level": "class", "elements": ["com.a.X", "com.b.Y"]}]
    st, ok, why = final_status(_verified(new_smells=news, new_smells_count=2), 30)
    assert (st, ok) == ("made_worse", False) and "2 class cyclic dependency" in why
    st, ok, why = final_status(_verified(api_breaks={"count": 866}), 30)
    assert (st, ok) == ("removed", False) and "866 public" in why
    assert final_status(_verified(api_breaks={"count": 866}), None)[1]
    st, ok, why = final_status(_verified(patch_applies=False), 30)
    assert not ok and "patch" in why
    st, ok, _ = final_status(_verified(target_removed=False, target_after={"present": True, "packages": 2}), 30)
    assert (st, ok) == ("reduced", True)
    st, ok, _ = final_status(_verified(target_removed=False, target_after={"present": True, "packages": 3}), 30)
    assert (st, ok) == ("no_effect", False)


def test_public_api_breaks_are_counted_like_an_api_checker():
    model = SimpleNamespace(
        types=pd.DataFrame({"type": ["com.a.Order", "com.a.Order.Line", "com.a.Util", "com.a.Client", "com.a.Kind"],
                            "top_level": ["com.a.Order", "com.a.Order", "com.a.Util", "com.a.Client", "com.a.Kind"],
                            "api": [True, True, False, True, True], "kind": ["class", "class", "class", "class", "enum"],
                            "final": [False, False, False, True, False]}),
        members=pd.DataFrame({"member": ["com.a.Order#total()", "com.a.Order#id", "com.a.Order#calc(int)",
                                         "com.a.Order.Line#qty()", "com.a.Util#help()",
                                         "com.a.Client#run(com.a.Order)", "com.a.Client#stop()"],
                              "owner": ["com.a.Order", "com.a.Order", "com.a.Order", "com.a.Order.Line", "com.a.Util",
                                        "com.a.Client", "com.a.Client"],
                              "api": [True, True, False, True, False, True, True],
                              "kind": ["method", "field", "method", "method", "method", "method", "method"],
                              "visibility": ["public"] * 7}))
    idx = ApiIndex(model)
    # moving a public class: the class, its public nested class and their public members (5)
    assert idx.breaks([{"op": "move_class", "class": "com.a.Order", "to_package": "com.b"}])["count"] == 5
    assert idx.breaks([{"op": "move_class", "class": "com.a.Util", "to_package": "com.b"}])["count"] == 0
    assert idx.breaks([{"op": "move_member", "members": ["com.a.Order#total()", "com.a.Order#calc(int)"],
                        "from_class": "com.a.Order", "to_class": "com.a.Util"}])["count"] == 1
    # clients that use an extracted interface change the signatures that named the provider
    assert idx.breaks([{"op": "extract_interface", "client": "com.a.Client", "provider": "com.a.Order",
                        "methods": []}])["count"] == 1
    assert idx.breaks([{"op": "remove_unused_import"}])["count"] == 0
    # a deprecated stub at the old place covers a movable public class, not a final class or an enum
    assert idx.stubbable("com.a.Order") and not idx.stubbable("com.a.Client") and not idx.stubbable("com.a.Kind")
    st = idx.breaks([{"op": "move_class", "class": "com.a.Order"}, {"op": "move_class", "class": "com.a.Kind"}],
                    compat="stubs")
    assert (st["count"], st["covered_by_stubs"]) == (1, 1)


def _comp(types, deps):
    t = pd.DataFrame(types, columns=["type", "top_level", "package", "file"])
    d = pd.DataFrame(deps, columns=["source", "target"])
    return cmp.CompileResult(pd.DataFrame(), t, d, 0.0)


def test_new_smells_are_found_on_the_compiled_code():
    types = [("a.X", "a.X", "a", "X.java"), ("b.Y", "b.Y", "b", "Y.java"), ("c.Z", "c.Z", "c", "Z.java")]
    before = _comp(types, [("a.X#m()", "b.Y"), ("b.Y", "c.Z")])
    files = {"X.java", "Y.java", "Z.java"}
    assert cmp.new_smells(before, before, files, files, {}) == []
    after = _comp(types, [("a.X#m()", "b.Y"), ("b.Y", "c.Z"), ("c.Z#n()", "a.X")])
    news = {(n["type"], n["level"]): n["elements"] for n in cmp.new_smells(before, after, files, files, {})}
    assert news[("cyclic_dependency", "class")] == ["a.X", "b.Y", "c.Z"]
    assert news[("cyclic_dependency", "package")] == ["a", "b", "c"]
    # a moved class that stays on the cycle it was on is not new
    cyc = _comp(types, [("a.X", "b.Y"), ("b.Y", "a.X")])
    moved = _comp([("d.X", "d.X", "d", "X2.java"), ("b.Y", "b.Y", "b", "Y.java")], [("d.X", "b.Y"), ("b.Y", "d.X")])
    out = cmp.new_smells(cyc, moved, files, {"X2.java", "Y.java"}, {"a.X": "d.X"})
    assert not [n for n in out if n["level"] == "class"]


@needs_git
def test_patches_of_crlf_files_apply_to_a_clean_checkout(tmp_path):
    from dsarp.verify.rewrite import _read_text, _write_text
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q")
    (repo / ".gitattributes").write_text("* text=auto\n")
    (repo / "A.java").write_bytes(b"class A {\r\n  int x;\r\n}\r\n")
    g("add", "-A")
    g("-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "user.name=t", "-c", "user.email=t@t",
      "commit", "-qm", "base")
    with Workspace(repo, tmp_path / "w") as ws:
        p = ws.path / "A.java"
        _write_text(p, _read_text(p).replace("int x;", "int y;"))
        patch = tmp_path / "plan.patch"
        patch.write_text(ws.diff())
        ws.reset()
        assert ws.apply_check(patch)


def test_planner_blocks_plans_over_the_api_limit():
    from dsarp.plan.run import why_rejected
    plans = [{"name": "p", "operations": [{"op": "move_class"}], "score": 0.4, "benefit": 1.0,
              "checks": {"status": "blocked", "blocked": 0, "api_breaks": 120,
                         "api_limit": "it changes 120 public types and members that code outside the repository "
                                      "may use (3 types, 117 members; limit 30)"}}]
    assert "120 public types and members" in why_rejected(plans)


def test_untested_changes_are_not_accepted_fixes():
    st, ok, why = final_status(_verified(build={"status": "builds", "reason": "no tests"},
                                         test_scope="none"), 30)
    assert (st, ok) == ("removed", False) and "no test runs the changed code" in why
    assert final_status(_verified(build={"status": "builds"}), 30, allow_untested=True)[1]


def test_tests_are_selected_by_use_configuration_or_module():
    types = pd.DataFrame({
        "type": ["a.Svc", "a.Helper", "a.Other", "a.SvcTest", "a.HelperUserTest", "a.OtherTest", "b.Lonely", "b.Thing"],
        "top_level": ["a.Svc", "a.Helper", "a.Other", "a.SvcTest", "a.HelperUserTest", "a.OtherTest", "b.Lonely",
                      "b.Thing"],
        "is_test": [False, False, False, True, True, True, False, False],
        "build_module": ["core"] * 6 + ["web", "web"],
        "wiring_refs": [0, 0, 1, 0, 0, 0, 0, 0]})
    deps = pd.DataFrame({"source_type": ["a.SvcTest", "a.Helper", "a.HelperUserTest", "a.OtherTest"],
                         "target_type": ["a.Svc", "a.Svc", "a.Helper", "a.Other"]})
    model = SimpleNamespace(types=types, deps=deps)
    tests, scope = bld.select_tests_scoped(model, {"a.Svc"})
    assert tests == ["a.HelperUserTest", "a.SvcTest"] and scope == "uses the changed classes"   # two hops
    tests, scope = bld.select_tests_scoped(model, {"a.Other"})
    assert set(tests) == {"a.SvcTest", "a.HelperUserTest", "a.OtherTest"} and "configuration" in scope
    assert bld.select_tests_scoped(model, {"b.Lonely"}) == ([], "none")
    types.loc[types["type"] == "a.OtherTest", "build_module"] = "web"
    tests, scope = bld.select_tests_scoped(model, {"b.Lonely"})
    assert tests == ["a.OtherTest"] and scope.startswith("whole module")


def test_combined_plan_selection_is_exact():
    from dsarp.verify import combine as cb
    c = object.__new__(cb.Combiner)
    c.rf = SimpleNamespace(package={"a.X": "a", "a.Y": "a", "b.Z": "b", "c.W": "c"})
    c._effect = lambda g, ops: (1.0, None)
    c.new_smells = lambda ops: set()
    mv = lambda cls: {"op": "move_class", "class": cls, "from_package": cls.split(".")[0], "to_package": "z"}
    v = lambda st, sc: {"status": st, "score": sc, "kept": 1, "compiles": True, "target_removed": st == "removed"}
    # A conflicts with B and C (shared class a.X); B and C are independent: greedy by score takes A only,
    # the exact choice takes B + C
    cands = [({"group_id": "A", "core": []}, [mv("a.X"), mv("b.Z")], v("removed", 0.9)),
             ({"group_id": "B", "core": []}, [mv("a.X")], v("removed", 0.5)),
             ({"group_id": "C", "core": []}, [mv("b.Z"), mv("c.W")], v("removed", 0.4))]
    chosen, conflicts, _ = c._exact_order(cands)
    assert [g["group_id"] for g, _, _ in chosen] == ["B", "C"]
    assert conflicts and conflicts[0]["group_id"] == "A"


FAKE_SUITE = """#!/bin/sh
# full suite: module a (Ok passes, Flaky fails the first time, Bad always fails), module b (skipped if SKIPB)
case " $* " in
  *" test-compile "*) [ -f BROKEN ] && exit 1; exit 0 ;;
esac
report() { mkdir -p "$1/target/surefire-reports"; cat > "$1/target/surefire-reports/TEST-$2.xml"; }
case "$*" in
  *-Dtest=*)
    report a reruns <<XML
<testsuite name="x"><testcase classname="a.Flaky" name="t"/><testcase classname="a.Bad" name="t"><failure/></testcase></testsuite>
XML
    exit 1 ;;
esac
report a a.Tests <<XML
<testsuite name="a"><testcase classname="a.Ok" name="t"/><testcase classname="a.Flaky" name="t"><failure/></testcase>
<testcase classname="a.Bad" name="t"><error/></testcase></testsuite>
XML
[ -f SKIPB ] || report b b.Tests <<XML
<testsuite name="b"><testcase classname="b.Ok" name="t"/></testsuite>
XML
case "$*" in
  *prepare-agent*) mkdir -p a/target/site/jacoco
    printf 'GROUP,PACKAGE,CLASS,INSTRUCTION_MISSED,INSTRUCTION_COVERED,LINE_MISSED,LINE_COVERED\n' > a/target/site/jacoco/jacoco.csv
    printf 'a,a,Svc,0,10,2,8\na,a,Svc.Inner,0,5,0,2\na,a,Dead,10,0,6,0\n' >> a/target/site/jacoco/jacoco.csv ;;
esac
exit 1
"""


@pytest.fixture
def suite(tmp_path):
    (tmp_path / "pom.xml").write_text("<project><properties><maven.compiler.release>11</maven.compiler.release>"
                                      "</properties></project>")
    for m in ("a", "b"):
        d = tmp_path / m / "src" / "test" / "java"
        d.mkdir(parents=True)
        (d / "T.java").write_text("class T {}")
    m = tmp_path / "mvnw"
    m.write_text(FAKE_SUITE)
    m.chmod(m.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def test_baseline_finds_flaky_and_failing_tests(suite, tmp_path):
    from dsarp.verify import baseline as bsl
    r = bsl.run(suite, tmp_path / "out", "maven", timeout=60, log=lambda *_: None)
    assert r["complete"] and r["tests_run"] == 4 and r["java_level"] == 11
    assert r["flaky"] == ["a.Flaky#t"] and r["failing"] == ["a.Bad#t"]
    assert r["modules_with_tests"] == ["a", "b"] and r["missing_modules"] == []
    assert (tmp_path / "out" / "baseline.json").exists()
    # neither the flaky nor the failing test is blamed on a plan
    base = bsl.as_build_result(r)
    after = bld.BuildResult("maven", False, [], 1.0, tests=["a.Bad"], compiled=True, tests_run=2,
                            failing=["a.Bad#t", "a.Flaky#t"])
    assert bld.compare(base, after)["status"] == "builds and tests pass"


def test_partial_baselines_are_refused(suite, tmp_path):
    from dsarp.verify import baseline as bsl
    (suite / "SKIPB").write_text("")
    r = bsl.run(suite, tmp_path / "o1", "maven", timeout=60, log=lambda *_: None)
    assert not r["complete"] and r["missing_modules"] == ["b"] and "produced no test report" in r["reasons"][0]
    (suite / "BROKEN").write_text("")
    r = bsl.run(suite, tmp_path / "o2", "maven", timeout=60, log=lambda *_: None)
    assert not r["complete"] and "does not build" in r["reasons"][0]


def test_jdk_is_chosen_from_the_java_level(tmp_path, monkeypatch):
    from dsarp.verify import baseline as bsl
    (tmp_path / "pom.xml").write_text("<project><properties><maven.compiler.source>1.8</maven.compiler.source>"
                                      "</properties></project>")
    assert bsl.java_level(tmp_path) == 8
    j8, j17 = tmp_path / "jdk8", tmp_path / "jdk17"
    j8.mkdir()
    j17.mkdir()
    monkeypatch.setenv("DSARP_JDK_17", str(j17))
    assert bsl.java_home(8) == (str(j17), 17)                  # nearest newer JDK
    monkeypatch.setenv("DSARP_JDK_8", str(j8))
    assert bsl.java_home(8) == (str(j8), 8)
    assert bsl.build_env(tmp_path)["JAVA_HOME"] == str(j8)
    (tmp_path / "pom.xml").unlink()
    (tmp_path / "build.gradle").write_text("java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }")
    assert bsl.java_level(tmp_path) == 17


needs_jdk_here = pytest.mark.skipif(not shutil.which("javac"), reason="needs a JDK")


@needs_jdk_here
def test_safety_rules_from_failed_builds(tmp_path):
    """Moves that broke real builds in the cycle-repair-loop experiments are not planned."""
    from pathlib import Path as P
    from dsarp.config import parse_config
    from dsarp.plan.checks import Checker
    from dsarp.plan.facts import RepoFacts
    from dsarp.program import ProgramModel, build_model
    repo = tmp_path / "repo"
    src = repo / "src/main/java/com/a"
    src.mkdir(parents=True)
    (repo / "src/main/java/com/b").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion><groupId>s</groupId>"
                                  "<artifactId>s</artifactId><version>1</version></project>")
    (src / "Widget.java").write_text("package com.a;\npublic class Widget {\n    Widget(int n) {}\n"
                                     "    public int size() { return 1; }\n}\n")
    (src / "Factory.java").write_text("package com.a;\npublic class Factory {\n    public Widget make() "
                                      "{ return new Widget(3); }\n    int secret() { return 2; }\n}\n")
    (src / "Helper.java").write_text("package com.a;\npublic class Helper {\n    public int use(Factory f) "
                                     "{ return f.secret(); }\n}\n")
    (src / "Api.java").write_text("package com.a;\npublic class Api {\n    public int x() { return 1; }\n}\n")
    (repo / "src/main/java/com/b/Thing.java").write_text("package com.b;\npublic class Thing {\n    "
                                                         "public int n(com.a.Widget w) { return w.size(); }\n}\n")
    cfg = parse_config({"name": "s", "repo_url": "x", "build": "maven", "detectors": [{"tool": "designite",
                                                                                         "path": "x"}]}, P("."))
    build_model(cfg, repo, tmp_path / "model", history_commits=0)
    rf = RepoFacts.build(ProgramModel(tmp_path / "model"), api_policy="off")
    mv = lambda c, to, new=False: {"op": "move_class", "class": f"com.a.{c}", "from_package": "com.a",
                                   "to_package": to, **({"new_package": True} if new else {})}

    def blocked(op):
        c = Checker(rf).check([op], {op["class"]: op["to_package"]})[0]
        return c.status == "blocked", " | ".join(c.notes)

    b, why = blocked(mv("Widget", "com.b"))
    assert b and "non-public constructor" in why
    b, why = blocked(mv("Api", "com.a.internal", new=True))
    assert b and "internal package" in why
    b, why = blocked(mv("Helper", "com.b"))
    assert not b                                                   # widening allowed by default
    rf.no_widening = True
    b, why = blocked(mv("Helper", "com.b"))
    assert b and "widening is switched off" in why


@needs_git
def test_reflection_gate(tmp_path):
    from dsarp.verify.run import reflection_gate
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q")
    (repo / "A.java").write_text("class A { Object o = null; }\n")
    (repo / "B.java").write_text('class B { String n = "com.x.Old"; }\n')
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    res = SimpleNamespace(moved={}, changed={"A.java"}, created=[])
    op = [{"op": "move_class", "class": "com.x.Other", "from_package": "com.x", "to_package": "com.y"}]
    assert reflection_gate(repo, res, op)["ok"]
    (repo / "A.java").write_text('class A { Object o = Class.forName("com.y.Other"); }\n')
    r = reflection_gate(repo, res, op)
    assert not r["ok"] and r["added_lookups"] == ["A.java (+1)"]
    (repo / "A.java").write_text("class A { Object o = null; }\n")
    op2 = [{"op": "move_class", "class": "com.x.Old", "from_package": "com.x", "to_package": "com.y"}]
    r = reflection_gate(repo, res, op2)
    assert not r["ok"] and r["old_names_in_strings"][0].startswith("com.x.Old in B.java")
    st, ok, why = final_status(_verified(reflection=r), 30)
    assert not ok and "old class name still in a string" in why


def test_wilson_and_mcnemar_known_values():
    from dsarp.evaluate.stats import mcnemar_exact, wilson
    p, lo, hi = wilson(8, 29)                            # e.g. 8 of 29 cycles dissolved and accepted
    assert round(p, 3) == 0.276 and round(lo, 3) == 0.147 and round(hi, 3) == 0.457
    assert wilson(0, 0) == (0.0, 0.0, 0.0) and wilson(0, 10)[1] == 0.0
    assert mcnemar_exact(0, 0) == 1.0
    assert round(mcnemar_exact(10, 2), 4) == 0.0386      # binomial, two-sided
    assert mcnemar_exact(3, 3) == 1.0


def test_evidence_levels():
    from dsarp.verify.run import evidence_level
    assert evidence_level(None) == "0"
    assert evidence_level(_verified(accepted=True, status="removed", confirmed_by=["arcan"])) == "4c"
    assert evidence_level(_verified(accepted=True, status="removed")) == "4b"
    assert evidence_level(_verified(accepted=True, status="reduced")) == "4a"
    assert evidence_level(_verified(accepted=False, status="breaks the build",
                                    build={"status": "breaks the build"})) == "1"
    assert evidence_level(_verified(accepted=False, status="removed", build={"status": "not built"})) == "1"
    assert evidence_level(_verified(accepted=False, status="breaks tests", build={"status": "breaks tests"})) == "2"
    assert evidence_level(_verified(accepted=False, status="removed", build={"status": "builds"})) == "2"
    assert evidence_level(_verified(accepted=False, status="made_worse")) == "3"


def test_baseline_measures_coverage_and_untested_changes_are_not_accepted(suite, tmp_path):
    from dsarp.plan.run import touched_coverage
    from dsarp.verify import baseline as bsl
    res = tmp_path / "res"
    r = bsl.run(suite, res / "baseline", "maven", timeout=60, log=lambda *_: None)
    assert r["coverage"]["classes"] == 2 and r["coverage"]["covered_classes"] == 1
    cov = bsl.load_coverage(res)
    assert cov == {"a.Svc": 0.8333, "a.Dead": 0.0}             # nested classes counted with their top-level class
    rf = SimpleNamespace(coverage=cov)
    assert touched_coverage(rf, [{"op": "move_class", "class": "a.Dead"}]) == 0.0
    assert touched_coverage(rf, [{"op": "move_class", "class": "x.Unknown"}]) is None
    st, ok, why = final_status(_verified(coverage={"mean": 0.0, "classes": 1, "uncovered": ["a.Dead"]}), 30)
    assert not ok and "coverage 0" in why
    from dsarp.verify.run import evidence_level
    assert evidence_level(_verified(accepted=False, coverage={"mean": 0.0})) == "2"
    assert final_status(_verified(coverage={"mean": 0.5, "classes": 2}), 30)[1]


def test_every_help_text_renders():
    from dsarp.cli import build_parser
    p = build_parser()
    assert "stats" in p.format_help()
    for a in p._subparsers._group_actions:
        for name, sp in a.choices.items():
            sp.format_help()
