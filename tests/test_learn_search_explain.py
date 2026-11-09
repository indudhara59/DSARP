"""Phase 5: history moves, learned affinity, tree search, language-model clients, explanations."""

import json
import subprocess

import pandas as pd
import pytest

from dsarp.explain import allowed_names, explain, review
from dsarp.learn import affinity as af
from dsarp.learn import moves as mv
from dsarp.plan.facts import RepoFacts, group_facts
from dsarp.plan.run import Planner
from dsarp.plan.simulate import Simulator
from dsarp.program import ProgramModel
from dsarp.search.actions import ActionSpace, apply_action, op_key
from dsarp.search.llm import LLMError, NoLLM, Scripted, extract_json, from_env
from dsarp.search.mcts import Search

LOG = """@@abc|1700000000
R100\tcore/src/main/java/org/a/old/Thing.java\tcore/src/main/java/org/a/fresh/Thing.java
R095\tcore/src/main/java/org/a/x/Same.java\tother/src/main/java/org/a/x/Same.java
R080\tcore/src/main/java/org/a/x/A.java\tcore/src/main/java/org/a/y/B.java
R100\tcore/src/test/java/org/a/t/T.java\tcore/src/test/java/org/a/u/T.java
@@def|1690000000
R100\tsrc/org/b/one/Two.java\tsrc/org/b/three/Two.java
"""


def test_moves_parse_keeps_package_changes_only():
    m = mv.parse(LOG)
    assert list(m["new_class"]) == ["org.a.fresh.Thing", "org.b.three.Two"]
    assert m.iloc[0]["old_package"] == "org.a.old" and m.iloc[0]["similarity"] == 100
    assert mv.package_of("mod/src/main/java/org/x/Y.java") == "org.x"
    assert mv.package_of("src/org/x/Y.java") == "org.x"


def test_moves_mined_from_git(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.org")
    git("config", "user.name", "t")
    f = tmp_path / "src/main/java/org/a/old/Thing.java"
    f.parent.mkdir(parents=True)
    f.write_text("package org.a.old;\n\npublic class Thing {\n  int a;\n  int b;\n  int c;\n}\n")
    git("add", ".")
    git("commit", "-qm", "add")
    new = tmp_path / "src/main/java/org/a/fresh/Thing.java"
    new.parent.mkdir(parents=True)
    new.write_text(f.read_text().replace("org.a.old", "org.a.fresh"))
    f.unlink()
    git("add", "-A")
    git("commit", "-qm", "move")
    m = mv.mine(tmp_path)
    assert list(m["old_class"]) == ["org.a.old.Thing"] and list(m["new_class"]) == ["org.a.fresh.Thing"]


def _synthetic(n=60, seed=1):
    import numpy as np
    rng = np.random.default_rng(seed)
    rows = []
    for q in range(n):
        for j in range(4):
            pos = j == 0
            rows.append({"query": f"r:current:{q}", "repo": "r" if q % 2 else "s", "source": "current",
                         "class": f"C{q}", "package": f"p{j}", "group": f"g{q % 7}", "label": int(pos),
                         "hard_negative": 0, "weight": 1.0,
                         "structural_out": rng.uniform(0.4, 1) if pos else rng.uniform(0, 0.5),
                         "structural_in": rng.uniform(0, 1), "cochange": rng.uniform(0, 1),
                         "semantic": rng.uniform(0.3, 1) if pos else rng.uniform(0, 0.4),
                         "package_name": 0.0, "log_size": rng.uniform(0, 3)})
    return pd.DataFrame(rows)


def test_affinity_model_fits_saves_and_ranks(tmp_path):
    df = _synthetic()
    m = af.fit(df)
    assert m.coef[af.FEATURES.index("structural_out")] > 0 and m.coef[af.FEATURES.index("semantic")] > 0
    m.save(tmp_path / "a.json")
    m2 = af.AffinityModel.load(tmp_path / "a.json")
    f = df.iloc[0][af.FEATURES].to_dict()
    assert m2.score(f) == pytest.approx(m.score(f))
    ev = af.evaluate(df)
    assert ev["learned"]["top1"] > 0.8 and ev["learned"]["top1"] >= ev["structural_only"]["top1"]
    loro = af.evaluate_leave_one_repo_out(df)
    assert set(loro) == {"r", "s"}


def test_affinity_dataset_and_model_on_fixture(project):
    rf = RepoFacts.build(ProgramModel(project / "model"))
    moves = pd.DataFrame([{"commit": "x", "time": 1, "similarity": 100, "old_file": "", "new_file": "",
                           "class_name": "Dates", "old_package": "com.acme.core", "new_package": "com.acme.util",
                           "old_class": "com.acme.core.Dates", "new_class": "com.acme.util.Dates"}])
    df = af.build_dataset(rf, moves, repo="javaproj")
    mv_rows = df[df["source"] == "move"]
    assert set(mv_rows["package"]) >= {"com.acme.util", "com.acme.core"}
    assert mv_rows.loc[mv_rows["package"] == "com.acme.core", "hard_negative"].item() == 1
    f = rf.features("com.acme.util.Dates", "com.acme.core")
    assert 0 < f["structural"] <= 1
    model = af.fit(df)
    before = rf.affinity("com.acme.util.Dates", "com.acme.core")
    rf.use_affinity_model(model)
    after = rf.affinity("com.acme.util.Dates", "com.acme.core")
    assert 0 <= after <= 1 and (after != before or model.coef)


def test_llm_helpers(monkeypatch):
    assert extract_json('Sure:\n```json\n{"actions": [1, 2]}\n```') == {"actions": [1, 2]}
    assert extract_json("[3]") == [3]
    with pytest.raises(LLMError):
        extract_json("no json here")
    with pytest.raises(LLMError):
        NoLLM().complete("s", "p")
    monkeypatch.setenv("DSARP_LLM", "openai")
    monkeypatch.delenv("DSARP_LLM_URL", raising=False)
    with pytest.raises(LLMError):
        from_env()
    assert isinstance(from_env("none"), NoLLM)


def _group(project):
    rf = RepoFacts.build(ProgramModel(project / "model"))
    scopes = json.loads((project / "triage" / "scopes.json").read_text())
    gid = next(iter(scopes))
    return rf, group_facts(rf, gid, "cyclic_dependency", scopes[gid])


def test_action_space_on_fixture(project):
    rf, gf = _group(project)
    acts = ActionSpace(rf, gf).actions({})
    assert acts and abs(sum(a.prior for a in acts) - 1) < 1e-9
    moves = [a for a in acts if a.ops[0]["op"] == "move_class"]
    assert moves
    state = apply_action({}, moves[0])
    assert op_key(moves[0].ops[0]) in state
    drops = [a for a in ActionSpace(rf, gf).actions(state) if a.kind == "drop"]
    assert drops and apply_action(state, drops[0]) == {}


def test_search_removes_fixture_cycle(project):
    rf, gf = _group(project)
    r = Search(rf, gf, Simulator(rf)).run(iterations=200, time_limit=30)
    assert r.plan is not None and r.best_score > 0
    sim = Simulator(rf).run(r.plan.operations, "cyclic_dependency", gf.core)
    assert sim.target_removed


def test_search_uses_and_validates_llm_suggestions(project):
    rf, gf = _group(project)
    llm = Scripted(['{"actions": [0, 999, "x"], "reason": "first candidate"}', "not json"])
    r = Search(rf, gf, Simulator(rf), llm=llm).run(iterations=20, time_limit=10, llm_rounds=1)
    assert r.llm_calls == 2
    assert any(t["event"] == "llm_proposal" for t in r.trace)


def test_planner_with_search_and_explanation(project):
    planner = Planner(project / "model", project / "triage", backend="highs", time_limit=10,
                      search="always", search_time=10, log=lambda *_: None)
    result = planner.run(2)
    g = result["groups"][0]
    assert g["recommended"] and "search" in g
    rep = explain({**result, "repo": "javaproj"}, planner.rf)
    md = rep["markdown"]
    assert "# Refactoring plan: javaproj" in md and "### Step 1" in md and "**Effect:**" in md
    plan = next(p for p in g["plans"] if p["name"] == g["recommended"])
    allowed = allowed_names(g, plan)
    good = f"Moving {plan['operations'][0].get('class') or plan['operations'][0].get('member')} breaks the cycle."
    assert review(good, allowed) == []
    assert review("Also move com.acme.invented.Ghost.", allowed) == ["com.acme.invented.Ghost"]


def test_explanation_summary_is_reviewed(project):
    planner = Planner(project / "model", project / "triage", backend="highs", time_limit=10,
                      search="never", log=lambda *_: None)
    result = planner.run(1)
    bad = explain(result, planner.rf, Scripted(["Move com.acme.nowhere.Phantom to fix it."]))
    assert bad["groups"][0]["summary"] is None and bad["groups"][0]["summary_rejected"]
    good = explain(result, planner.rf, Scripted(["The core and util packages depend on each other."]))
    assert good["groups"][0]["summary"] and "> The core" in good["markdown"]


def test_cli_learn_and_explain(project, capsys):
    from dsarp.cli import main
    assert main(["learn", str(project), "--workdir", str(project / "nowhere")]) == 0
    assert (project / "learn" / "affinity.json").exists()
    assert main(["plan", str(project), "--groups", "1", "--time-limit", "5", "--backend", "highs",
                 "--search", "never", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "learned affinity" in out and "report.md" in out
    assert main(["explain", str(project)]) == 0
    assert (project / "plans" / "report.md").read_text().startswith("# Refactoring plan")
