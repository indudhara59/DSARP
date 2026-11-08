"""Checks against the real detector output of the five study repositories.

Skipped unless DSARP_DATA points to the data folder (the data is not part of
the repository). Run with::

    DSARP_DATA=/path/to/data pytest -m integration
"""

import os
from pathlib import Path

import pytest

from dsarp.config import load_config
from dsarp.ingest import ingest

DATA = os.environ.get("DSARP_DATA")
CONFIGS = Path(__file__).resolve().parents[1] / "configs"
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DATA, reason="DSARP_DATA not set")]

# Arcan smell counts per repository at the configured commit.
EXPECTED_ARCAN = {"tika": 278, "struts": 292, "karaf": 67, "log4j": 717, "cassandra": 0}


@pytest.mark.parametrize("repo", sorted(EXPECTED_ARCAN))
def test_every_repository_ingests(repo):
    res = ingest(load_config(CONFIGS / f"{repo}.yaml", DATA))
    arcan = res.smells[res.smells["tool"] == "arcan"] if len(res.smells) else res.smells
    assert len(arcan) == EXPECTED_ARCAN[repo]
    assert (res.smells["tool"] == "designite").sum() > 0
    classes = res.components[(res.components["kind"] == "class") & res.components["has_source"].astype(bool)]
    assert len(classes) > 500
    assert not any(w.startswith("MISMATCH") for w in res.warnings)


def test_tika_top_cycle_packages():
    res = ingest(load_config(CONFIGS / "tika.yaml", DATA))
    s = res.smells.set_index("smell_id").loc["arcan:101638"]
    assert s["type"] == "cyclic_dependency" and s["size"] == 16
