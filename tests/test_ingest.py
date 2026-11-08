import json
from pathlib import Path

import pandas as pd
import pytest

from dsarp.cli import main
from dsarp.config import load_config
from dsarp.ingest import ingest, write

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def result():
    return ingest(load_config(FIXTURES / "sample.yaml", data_root=FIXTURES))


def test_paths_become_repository_relative(result):
    comp = result.components.set_index("name")
    page = comp.loc["com.acme.web.Page"]
    assert page["file_path"] == "web/src/main/java/com/acme/web/Page.java"
    assert page["module"] == "web"
    assert bool(comp.loc["com.acme.core.EngineTest", "is_test"])
    assert result.manifest["analysis_roots"]["designite"] == "/home/u/demo"


def test_metrics_from_both_tools_side_by_side(result):
    engine = result.components.set_index("name").loc["com.acme.core.Engine"]
    assert engine["arcan_fan_in"] == 3
    assert engine["designite_wmc"] == 9
    assert engine["tools"] == "arcan;designite"


def test_package_roles(result):
    roles = result.components.set_index("name")["role"]
    assert roles["com.acme.core"] == "source"
    assert roles["com.acme"] == "parent"      # only sub-packages have code
    assert roles["org.lib"] == "external"     # a library, not part of the repo


def test_cross_tool_match_and_flags(result):
    s = result.smells.set_index("smell_id")
    assert s.loc["arcan:100", "matched_with"].startswith("designite:")
    assert s.loc["arcan:100", "confirmed_by"] == "arcan;designite"
    assert s.loc["arcan:100", "best_cycle_overlap"] == 1.0
    lib_cycle = s.loc["arcan:102"]
    assert lib_cycle["external_members"] == 1 and lib_cycle["parent_members"] == 1
    assert result.manifest["counts"]["confirmed_by_both"] >= 2


def test_commit_mismatch_is_reported():
    cfg = load_config(FIXTURES / "sample.yaml", data_root=FIXTURES)
    cfg.commit = "f" * 40
    res = ingest(cfg)
    assert any(w.startswith("MISMATCH") for w in res.warnings)


def test_write_outputs(result, tmp_path):
    out = write(result, tmp_path / "demo")
    assert (out / "smells.csv").exists() and (out / "components.csv").exists()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert len(manifest["inputs"]) == 2
    assert all(len(h) == 64 for i in manifest["inputs"] for h in i["files"].values())
    smells = pd.read_csv(out / "smells.csv")
    assert json.loads(smells.loc[0, "members"])


def test_cli_ingest_and_summary(tmp_path, capsys):
    cfg = str(FIXTURES / "sample.yaml")
    assert main(["check", cfg, "--data-root", str(FIXTURES)]) == 0
    assert main(["ingest", cfg, "--data-root", str(FIXTURES), "--out", str(tmp_path / "r")]) == 0
    assert main(["summary", str(tmp_path / "r"), "--detail"]) == 0
    assert "demo" in capsys.readouterr().out
