"""Stage 3: in-memory detection rules, calibration and triage."""

import json
import shutil
from pathlib import Path

import networkx as nx
import pandas as pd
import pytest

from dsarp import detect
from dsarp.calibrate import agreement
from dsarp.config import parse_config
from dsarp.detect import ARCAN_LIKE, DESIGNITE_LIKE, Thresholds
from dsarp.program import ProgramModel, build_model
from dsarp.triage import triage

PROJECT = Path(__file__).parent / "fixtures" / "javaproj"
needs_jdk = pytest.mark.skipif(not shutil.which("javac"), reason="needs a JDK (javac)")


# ---------------------------------------------------------------- rules on small graphs

def _g(edges):
    g = nx.DiGraph()
    g.add_edges_from(edges)
    return g


def test_cycles_are_strongly_connected_components():
    g = _g([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("d", "e"), ("e", "d"), ("f", "a")])
    assert detect.cycles(g) == [["a", "b", "c"], ["d", "e"]]


def test_hub_needs_both_directions_and_balance():
    # h has 4 in and 4 out; s has many in but no out; o has many out but no in
    edges = [(f"i{k}", "h") for k in range(4)] + [("h", f"o{k}") for k in range(4)]
    edges += [(f"x{k}", "s") for k in range(5)] + [("o", f"y{k}") for k in range(5)]
    h = detect.hubs(_g(edges), Thresholds(hub_quantile=0.5))
    assert list(h[h["hub"]].index) == ["h"]


def test_unstable_dependency_ratio():
    # p (I=0.5) depends on u (I=0.67, less stable) and s (I=0, stable): ratio 0.5
    g = _g([("p", "u"), ("p", "s"), ("x", "p"), ("y", "p"), ("u", "z"), ("u", "z2"), ("s2", "s")])
    arcan = detect.unstable(g, Thresholds(unstable_ratio=0.3))
    assert arcan.loc["p", "unstable"] and arcan.loc["p", "less_stable"] == ["u"]
    strict = detect.unstable(g, Thresholds(unstable_ratio=0.6))
    assert not strict.loc["p", "unstable"]


def test_god_component_thresholds():
    assert detect.gods({"a": 31, "b": 30}, None, Thresholds()) == ["a"]
    assert detect.gods({"a": 2}, {"a": 50000}, Thresholds(god_loc=40000)) == ["a"]


def test_summary_counts():
    s = detect.detect(_g([("a", "b"), ("b", "a")]), None, {"a": 40, "b": 1})
    out = detect.summary(s)
    assert out["cyclic_dependency:package"] == 1 and out["packages_in_cycles"] == 2
    assert out["god_component:package"] == 1


# ---------------------------------------------------------------- on the fixture project

@pytest.fixture(scope="module")
def model_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("model")
    cfg = parse_config({"name": "javaproj", "repo_url": "x", "build": "maven",
                        "detectors": [{"tool": "arcan", "path": "x"}]}, Path("."))
    build_model(cfg, PROJECT, out, history_commits=0)
    return out


def _smells(rows):
    base = {"severity": None, "matched_with": "", "confirmed_by": "", "external_members": 0,
            "generated_only": False, "central": "", "size": 0}
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["members"] = df["members"].map(json.dumps)
    df["size"] = df["members"].map(lambda m: len(json.loads(m)))
    return df


SMELLS = [
    {"smell_id": "arcan:1", "tool": "arcan", "type": "cyclic_dependency", "level": "package",
     "members": ["com.acme.core", "com.acme.util"], "severity": 6.0, "matched_with": "designite:x:1"},
    {"smell_id": "designite:x:1", "tool": "designite", "type": "cyclic_dependency", "level": "package",
     "members": ["com.acme.util", "com.acme.core"]},
    {"smell_id": "arcan:2", "tool": "arcan", "type": "cyclic_dependency", "level": "package",
     "members": ["com.acme.web", "com.acme.core"], "severity": 3.0},           # no such cycle in the code
    {"smell_id": "arcan:3", "tool": "arcan", "type": "cyclic_dependency", "level": "package",
     "members": ["org.lib", "javax.x"], "external_members": 2},                 # libraries only
    {"smell_id": "arcan:4", "tool": "arcan", "type": "hub_like_dependency", "level": "class",
     "members": ["com.acme.core.EngineTest"], "central": "com.acme.core.EngineTest"},   # test code
    {"smell_id": "designite:x:2", "tool": "designite", "type": "unstable_dependency", "level": "package",
     "members": ["com.acme.web", "com.acme.core"], "central": "com.acme.web"},
    {"smell_id": "designite:x:3", "tool": "designite", "type": "dense_structure", "level": "system",
     "members": ["com.acme.web", "com.acme.core"]},
]


@needs_jdk
def test_triage_decisions_and_groups(model_dir, tmp_path):
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _smells(SMELLS).to_csv(ingest / "smells.csv", index=False)
    res = triage(ingest, model_dir, tmp_path / "triage", repo="javaproj")
    s = res.smells.set_index("smell_id")
    assert s.loc["arcan:1", "decision"] == "keep" and s.loc["designite:x:1", "decision"] == "keep"
    assert s.loc["arcan:1", "group_id"] == s.loc["designite:x:1", "group_id"]       # same cycle, one group
    assert s.loc["arcan:2", "reason"] == "no cycle between these elements in the analysed code"
    assert s.loc["arcan:3", "reason"] == "only external libraries"
    assert s.loc["arcan:4", "reason"] == "test code only"
    assert s.loc["designite:x:3", "decision"] == "report_only"

    g = res.groups.set_index("group_id")
    cyc = g.loc[s.loc["arcan:1", "group_id"]]
    assert json.loads(cyc["members"]) == ["com.acme.core", "com.acme.util"]
    assert cyc["confirmed_by_both"] and cyc["rank"] == 1
    scope = res.scopes[s.loc["arcan:1", "group_id"]]
    assert "com.acme.web" in scope["neighbours"] and "com.acme.core.internal" in scope["neighbours"]
    assert "com.acme.core.EngineTest" not in scope["core_classes"]
    # util -> core also uses Engine as a parameter type, so it is not a quick fix
    assert scope["quick_fixes"] == []
    for name in ("smells_triaged.csv", "groups.csv", "scopes.json", "triage.json", "detected_baseline.csv"):
        assert (tmp_path / "triage" / name).exists()


@needs_jdk
def test_detect_model_and_agreement(model_dir):
    m = ProgramModel(model_dir)
    d = detect.detect_model(m)
    cyc = d[(d["type"] == "cyclic_dependency") & (d["level"] == "package")]
    assert [sorted(x) for x in cyc["members"]] == [["com.acme.core", "com.acme.util"]]
    a = agreement(m, _smells(SMELLS), "arcan", ARCAN_LIKE)
    assert a["cyclic_dependency:package"]["recall"] == 0.5          # arcan:2 is not a real cycle
    assert a["cyclic_dependency:package"]["precision"] == 1.0
    assert DESIGNITE_LIKE.unstable_ratio == 0.0


@needs_jdk
def test_cli_triage(model_dir, tmp_path, capsys):
    from dsarp.cli import main
    res = tmp_path / "javaproj"
    res.mkdir()
    _smells(SMELLS).to_csv(res / "smells.csv", index=False)
    assert main(["triage", str(res), "--model", str(model_dir)]) == 0
    out = capsys.readouterr().out
    assert "groups" in out and "only external libraries" in out
