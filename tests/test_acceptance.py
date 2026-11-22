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
