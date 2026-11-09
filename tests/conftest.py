"""Shared fixtures: the small Java project built into a program model and triaged."""

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

PROJECT = Path(__file__).parent / "fixtures" / "javaproj"


@pytest.fixture(scope="session")
def project(tmp_path_factory):
    if not shutil.which("javac"):
        pytest.skip("needs a JDK (javac)")
    from dsarp.config import parse_config
    from dsarp.program import build_model
    from dsarp.triage import triage
    root = tmp_path_factory.mktemp("plan")
    cfg = parse_config({"name": "javaproj", "repo_url": "x", "build": "maven",
                        "detectors": [{"tool": "arcan", "path": "x"}]}, Path("."))
    build_model(cfg, PROJECT, root / "model", history_commits=0)
    smells = pd.DataFrame([{
        "smell_id": "arcan:1", "tool": "arcan", "type": "cyclic_dependency", "level": "package",
        "members": json.dumps(["com.acme.core", "com.acme.util"]), "severity": 6.0, "matched_with": "",
        "confirmed_by": "", "external_members": 0, "generated_only": False, "central": "", "size": 2}])
    smells.to_csv(root / "smells.csv", index=False)
    triage(root, root / "model", root / "triage", repo="javaproj")
    return root
