from pathlib import Path

import pandas as pd

from dsarp.config import DetectorInput
from dsarp.detectors import arcan, designite
from dsarp.model import Level, SmellType

SAMPLE = Path(__file__).parent / "fixtures" / "sample"


def _by_id(smells):
    return {s.smell_id: s for s in smells}


def test_arcan_smells_members_and_centre():
    res = arcan.load(DetectorInput("arcan", SAMPLE / "arcan"))
    smells = _by_id(res.smells)
    assert res.version == "0123456789abcdef0123456789abcdef01234567"
    assert len(smells) == 3
    cycle = smells["arcan:100"]
    assert cycle.smell_type is SmellType.CYCLIC_DEPENDENCY and cycle.level is Level.PACKAGE
    assert set(cycle.members) == {"com.acme.core", "com.acme.util"}
    assert cycle.attributes["Severity"] == 6.5
    hub = smells["arcan:101"]
    assert hub.smell_type is SmellType.HUB_LIKE_DEPENDENCY and hub.level is Level.CLASS
    assert hub.central == "com.acme.core.Engine"
    assert set(hub.related) == {"com.acme.util.Dates", "com.acme.web.Page"}


def test_arcan_components_have_packages_and_paths():
    comp = arcan.load_components(SAMPLE / "arcan")
    engine = comp.set_index("name").loc["com.acme.core.Engine"]
    assert engine["package"] == "com.acme.core"
    assert engine["file_path"] == "core/src/main/java/com/acme/core/Engine.java"
    assert engine["fan_in"] == 3
    assert comp.set_index("name").loc["com.acme.core", "file_path"] == ""


def test_arcan_without_smell_files_warns(tmp_path):
    (tmp_path / "component-metrics.csv").write_text((SAMPLE / "arcan" / "component-metrics.csv").read_text())
    res = arcan.load(DetectorInput("arcan", tmp_path))
    assert res.smells == []
    assert any("smell files missing" in w for w in res.warnings)


def test_arcan_non_commit_version_warns(tmp_path):
    text = (SAMPLE / "arcan" / "component-metrics.csv").read_text()
    (tmp_path / "component-metrics.csv").write_text(text.replace("0123456789abcdef0123456789abcdef01234567", "run-42"))
    res = arcan.load(DetectorInput("arcan", tmp_path))
    assert any("not a git commit" in w for w in res.warnings)


def test_designite_parses_descriptions():
    res = designite.load(DetectorInput("designite", SAMPLE / "designite"))
    by_type = {}
    for s in res.smells:
        by_type.setdefault((s.smell_type, s.level), []).append(s)

    cycles = by_type[(SmellType.CYCLIC_DEPENDENCY, Level.PACKAGE)]
    assert len(cycles) == 1, "the same cycle reported by two packages must be merged"
    assert cycles[0].members == ["com.acme.core", "com.acme.util"]
    assert sorted(cycles[0].attributes["reported_by"]) == ["com.acme.core", "com.acme.util"]

    unstable = by_type[(SmellType.UNSTABLE_DEPENDENCY, Level.PACKAGE)][0]
    assert unstable.central == "com.acme.web" and unstable.related == ["com.acme.util"]

    fc = by_type[(SmellType.FEATURE_CONCENTRATION, Level.PACKAGE)][0]
    assert fc.groups == [["Engine", "EngineTest"], ["Helper"]]
    assert fc.attributes["lcc_lack_of_component_cohesion"] == 0.5

    scattered = by_type[(SmellType.SCATTERED_FUNCTIONALITY, Level.PACKAGE)][0]
    assert scattered.members == ["com.acme.util", "com.acme.web"]

    god = by_type[(SmellType.GOD_COMPONENT, Level.PACKAGE)][0]
    assert god.attributes["classes"] == 54

    dense = by_type[(SmellType.DENSE_STRUCTURE, Level.SYSTEM)][0]
    assert dense.attributes["dependencies"] == 2
    assert dense.attributes["average_degree"] == 4.5

    class_cycles = by_type[(SmellType.CYCLIC_DEPENDENCY, Level.CLASS)]
    assert len(class_cycles) == 1
    assert set(class_cycles[0].members) == {"com.acme.core.Engine", "com.acme.util.Dates"}


def test_designite_ignores_non_architectural_design_smells():
    res = designite.load(DetectorInput("designite", SAMPLE / "designite"))
    assert not any("Deficient" in s.description for s in res.smells)


def test_designite_components_keep_absolute_paths_for_later():
    comp = designite.load_components(SAMPLE / "designite")
    assert isinstance(comp, pd.DataFrame)
    row = comp.set_index("name").loc["com.acme.web.Page"]
    assert row["file_path_abs"].endswith("web/src/main/java/com/acme/web/Page.java")
    assert row["loc"] == 60
