"""Program model checks on real repositories.

Skipped unless DSARP_WORK points to a folder with checkouts made by
``dsarp checkout`` (and DSARP_DATA to the detector data). Example::

    dsarp checkout configs/tika.yaml --workdir $DSARP_WORK
    DSARP_DATA=... DSARP_WORK=... pytest -m integration tests/test_real_model.py
"""

import os
import shutil
from pathlib import Path

import networkx as nx
import pytest

from dsarp.config import load_config
from dsarp.program import ProgramModel, build_model

WORK = os.environ.get("DSARP_WORK")
DATA = os.environ.get("DSARP_DATA")
CONFIGS = Path(__file__).resolve().parents[1] / "configs"
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not (WORK and DATA and shutil.which("javac")),
                                 reason="DSARP_WORK, DSARP_DATA and a JDK are needed")]


def _model(repo, tmp_path):
    if not (Path(WORK) / repo / ".git").exists():
        pytest.skip(f"no checkout of {repo} in {WORK}")
    cfg = load_config(CONFIGS / f"{repo}.yaml", DATA)
    build_model(cfg, Path(WORK) / repo, tmp_path / repo, memory="5g", history_commits=500)
    return ProgramModel(tmp_path / repo)


def test_tika_core_cycle_and_deprecated_method(tmp_path):
    m = _model("tika", tmp_path)
    g = m.package_graph()
    biggest = max(nx.strongly_connected_components(g), key=len)
    assert len(biggest) == 17
    assert all(p.startswith("org.apache.tika") for p in biggest)
    member = m.members.set_index("member").loc["org.apache.tika.mime.MimeTypes#getMimeType(java.io.File)"]
    assert member["deprecated"] and member["callers"] == 0


@pytest.mark.parametrize("repo", ["karaf", "struts"])
def test_other_repositories_build(repo, tmp_path):
    m = _model(repo, tmp_path)
    assert len(m.types) > 1000
    assert m.manifest["counts"]["build_modules"] > 10


def test_tika_triage_groups_and_quick_fixes(tmp_path):
    from dsarp.ingest import ingest, write
    from dsarp.triage import triage
    if not (Path(WORK) / "tika" / ".git").exists():
        pytest.skip("no tika checkout")
    cfg = load_config(CONFIGS / "tika.yaml", DATA)
    write(ingest(cfg), tmp_path / "tika")
    build_model(cfg, Path(WORK) / "tika", tmp_path / "tika" / "model", memory="5g", history_commits=500)
    res = triage(tmp_path / "tika", tmp_path / "tika" / "model", tmp_path / "tika" / "triage")
    top = res.groups.iloc[0]
    assert top["type"] == "cyclic_dependency" and top["level"] == "package" and top["size"] == 17
    fixes = {(f["from"], f["to"], f["fix"]) for f in res.scopes[top["group_id"]]["quick_fixes"]}
    assert ("org.apache.tika.io", "org.apache.tika.parser", "remove_unused_import") in fixes
    assert ("org.apache.tika.mime", "org.apache.tika", "only_used_by_deprecated_code") in fixes
    assert (res.smells["reason"] == "cycle exists only through test code").sum() >= 5
