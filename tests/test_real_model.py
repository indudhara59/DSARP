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
