"""Phase 4: solver backends, plan model, simulation, checks and the plan stage."""

import json
import shutil

import pandas as pd
import pytest

from dsarp.plan import solver as slv
from dsarp.plan.checks import Checker, summarise
from dsarp.plan.facts import RepoFacts, cycle_context, group_facts
from dsarp.plan.generator import PlanModel
from dsarp.plan.run import Planner, write
from dsarp.plan.simulate import Simulator, apply, initial_state
from dsarp.program import ProgramModel

needs_jdk = pytest.mark.skipif(not shutil.which("javac"), reason="needs a JDK (javac)")


# ---------------------------------------------------------------- solver

def _knapsack():
    m = slv.LinearModel()
    a, b, c = m.binary("a"), m.binary("b"), m.binary("c")
    m.add({a: 3, b: 4, c: 5}, lb=7)          # cover at least 7
    m.minimize({a: 2, b: 3, c: 4})
    return m


@pytest.mark.parametrize("backend", ["highs", "cpsat"])
def test_solver_backends_agree(backend):
    if backend not in slv.available_backends():
        pytest.skip(f"{backend} not installed")
    sol = slv.solve(_knapsack(), backend=backend, time_limit=10)
    assert sol.ok and sol.status == "optimal"
    assert sol.objective == pytest.approx(5.0)           # a + b
    assert sol.value("a") == 1 and sol.value("b") == 1 and sol.value("c") == 0


def test_infeasible_model():
    m = slv.LinearModel()
    x = m.binary("x")
    m.add({x: 1}, lb=2)
    m.minimize({x: 1})
    sol = slv.solve(m, backend="highs", time_limit=5)
    assert not sol.ok


def test_integer_variable_and_bounds():
    m = slv.LinearModel()
    o = m.var("o", 0, 5)
    m.add({o: 1}, lb=3)
    m.minimize({o: 1})
    sol = slv.solve(m, backend="highs", time_limit=5)
    assert sol.value("o") == 3


# ---------------------------------------------------------------- fixture project

@needs_jdk
def test_facts_and_context(project):
    rf = RepoFacts.build(ProgramModel(project / "model"))
    assert ("com.acme.core.Engine", "com.acme.util.Dates") in rf.edges
    assert rf.edges[("com.acme.core.Engine", "com.acme.util.Unused")].import_only
    # deprecated, but Dates still calls it from production code: not deletable
    assert "com.acme.core.Engine#legacy(java.lang.String)" not in rf.deletable
    scopes = json.loads((project / "triage" / "scopes.json").read_text())
    gid = next(iter(scopes))
    gf = group_facts(rf, gid, "cyclic_dependency", scopes[gid])
    assert {"com.acme.core", "com.acme.util"} <= set(gf.packages)
    assert {"com.acme.core", "com.acme.util"} <= cycle_context(rf, gf.movable, {"com.acme.core"})


@needs_jdk
def test_plan_removes_fixture_cycle(project):
    rf = RepoFacts.build(ProgramModel(project / "model"))
    scopes = json.loads((project / "triage" / "scopes.json").read_text())
    gid = next(iter(scopes))
    pm = PlanModel(rf, group_facts(rf, gid, "cyclic_dependency", scopes[gid]))
    plans = {p.name: p for p in pm.plans(time_limit=20, backend="highs")}
    assert plans["no_change"].remaining > 0
    best = plans["most_removed"]
    assert best.status == "optimal" and best.remaining == 0 and best.operations
    sim = Simulator(rf).run(best.operations, "cyclic_dependency", ["com.acme.core", "com.acme.util"])
    assert sim.target_removed and sim.target_before["present"]
    assert not [s for s in sim.new_smells if s["type"] == "cyclic_dependency"]


@needs_jdk
def test_simulation_apply_operations(project):
    rf = RepoFacts.build(ProgramModel(project / "model"))
    base = initial_state(rf)
    moved = apply(rf, [{"op": "move_class", "class": "com.acme.util.Dates", "from_package": "com.acme.util",
                        "to_package": "com.acme.core"}])
    assert moved.package["com.acme.util.Dates"] == "com.acme.core"
    assert base.package["com.acme.util.Dates"] == "com.acme.util"          # original state unchanged
    g = moved.package_graph()
    assert not g.has_edge("com.acme.util", "com.acme.core")
    inv = apply(rf, [{"op": "extract_interface", "client": "com.acme.util.Dates", "provider": "com.acme.core.Engine",
                      "methods": ["com.acme.core.Engine#legacy(java.lang.String)"],
                      "interface_package": "com.acme.util"}])
    assert ("com.acme.core.Engine", "com.acme.util.EnginePort") in inv.edges
    assert inv.package_graph().has_edge("com.acme.core", "com.acme.util")
    assert not inv.package_graph().has_edge("com.acme.util", "com.acme.core")


@needs_jdk
def test_checks_list_follow_up_edits(project):
    rf = RepoFacts.build(ProgramModel(project / "model"))
    ck = Checker(rf)
    ops = [{"op": "move_class", "class": "com.acme.core.Engine", "from_package": "com.acme.core",
            "to_package": "com.acme.util"},
           {"op": "delete_member", "member": "com.acme.core.Engine#legacy(java.lang.String)",
            "owner": "com.acme.core.Engine"},
           {"op": "move_class", "class": "com.acme.util.Dates", "from_package": "com.acme.util",
            "to_package": "com.acme.web"}]
    res = ck.check(ops, {"com.acme.core.Engine": "com.acme.util"})
    move = {e["edit"]: e for e in res[0].edits}
    assert move["change_package_declaration"]["new_file"].endswith("com/acme/util/Engine.java")
    assert "com.acme.core.EngineTest" in move["update_tests"]["classes"]
    # Engine.runs is protected and only used inside Engine: nothing to widen; Service stays in core
    assert any("Activator" in c for c in move["update_imports"]["classes"])
    assert res[1].status == "blocked" and "member still has production callers" in res[1].notes
    assert "com.acme.core.EngineTest" in res[1].edits[0]["classes"]
    assert res[2].status == "blocked"                                  # web is another build module
    assert summarise(res)["status"] == "blocked"


@needs_jdk
def test_planner_writes_results(project, tmp_path):
    planner = Planner(project / "model", project / "triage", backend="highs", time_limit=20, log=lambda *_: None)
    result = planner.run(5)
    g = result["groups"][0]
    assert g["type"] == "cyclic_dependency" and g["recommended"]
    rec = next(p for p in g["plans"] if p["name"] == g["recommended"])
    assert rec["benefit"] == 1.0 and rec["simulation"]["target_removed"]
    assert all("check" in o for o in rec["operations"])
    paths = write(result, tmp_path / "plans")
    ops = pd.read_csv(paths["operations"])
    assert ops["recommended"].any()
    assert json.loads(paths["plans"].read_text())["groups"][0]["group_id"] == g["group_id"]


@needs_jdk
def test_cli_plan(project, capsys):
    from dsarp.cli import main
    assert main(["plan", str(project), "--groups", "2", "--time-limit", "10", "--backend", "highs"]) == 0
    assert (project / "plans" / "plans.json").exists()
    assert "recommended" in capsys.readouterr().out


def test_moves_respect_exported_packages(project):
    rf = RepoFacts.build(ProgramModel(project / "model"))
    # com.acme.core.internal is not exported; Engine is used by the web module
    assert not rf.is_exported("com.acme.core.internal") and rf.is_exported("com.acme.core")
    assert not rf.visible_after_move("com.acme.core.Engine", "com.acme.core.internal")
    assert rf.visible_after_move("com.acme.core.Engine", "com.acme.util")
