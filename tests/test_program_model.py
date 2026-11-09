"""Stage 2 on a small two-module Maven project (tests/fixtures/javaproj).

The project contains, on purpose: a package cycle (core <-> util), a
deprecated method with callers, an import used by nothing, an interface call,
an anonymous class, a static initializer, a test class, a missing external
library, a ServiceLoader file, an OSGi blueprint file and a bundle activator.
"""

import shutil
import subprocess
from pathlib import Path

import networkx as nx
import pytest

from dsarp import buildinfo, sources, wiring
from dsarp.config import parse_config
from dsarp.program import ProgramModel, build_model

PROJECT = Path(__file__).parent / "fixtures" / "javaproj"
needs_jdk = pytest.mark.skipif(not shutil.which("javac"), reason="needs a JDK (javac)")


def _cfg(**extra):
    raw = {"name": "javaproj", "repo_url": "https://example.org/javaproj.git", "build": "maven",
           "detectors": [{"tool": "arcan", "path": "x"}]}
    raw.update(extra)
    return parse_config(raw, Path("."))


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    out = tmp_path_factory.mktemp("model")
    build_model(_cfg(protected_api=["com.acme.core.Service"]), PROJECT, out, history_commits=0)
    return ProgramModel(out)


def test_discover_classifies_files():
    files = sources.discover(PROJECT).set_index("file")
    engine = files.loc["core/src/main/java/com/acme/core/Engine.java"]
    assert engine["package"] == "com.acme.core" and engine["module"] == "core"
    assert not engine["is_test"] and not engine["excluded"]
    assert files.loc["core/src/test/java/com/acme/core/EngineTest.java", "is_test"]
    assert sources.source_roots(files) == ["core/src/main/java", "core/src/test/java", "web/src/main/java"]


def test_discover_excludes_templates_and_generated(tmp_path):
    (tmp_path / "m/src/main/resources/archetype/src/main/java/x").mkdir(parents=True)
    (tmp_path / "m/src/main/resources/archetype/src/main/java/x/T.java").write_text("package ${package};")
    (tmp_path / "m/target/generated-sources/a").mkdir(parents=True)
    (tmp_path / "m/target/generated-sources/a/G.java").write_text("package a; class G {}")
    files = sources.discover(tmp_path).set_index("file")
    assert set(files["reason"]) == {"resource template", "generated"}


def test_maven_modules_and_osgi_headers():
    build = buildinfo.read_build(PROJECT)
    assert build.system == "maven"
    assert set(build.modules) == {".", "core", "web"}
    assert build.modules["web"].depends_on == ["core"]          # external dependency ignored
    assert build.modules["core"].exported_packages == ["com.acme.core", "com.acme.util"]
    assert build.modules["core"].activator == "com.acme.core.internal.Activator"


def test_gradle_modules(tmp_path):
    (tmp_path / "settings.gradle").write_text("include ':api', ':impl'\nproject(':impl').projectDir = file('core/impl')\n")
    (tmp_path / "core/impl").mkdir(parents=True)
    (tmp_path / "api").mkdir()
    (tmp_path / "core/impl/build.gradle").write_text("dependencies { implementation project(':api') }")
    build = buildinfo.read_build(tmp_path)
    assert build.system == "gradle"
    assert build.modules["core/impl"].depends_on == ["api"]


def test_wiring_references():
    w = wiring.scan(PROJECT, buildinfo.read_build(PROJECT))
    kinds = set(zip(w["kind"], w["class"]))
    assert ("service_provider", "com.acme.core.Engine") in kinds
    assert ("xml_reference", "com.acme.web.Page") in kinds
    assert ("bundle_activator", "com.acme.core.internal.Activator") in kinds


@needs_jdk
def test_types_members_and_flags(model):
    t = model.types.set_index("type")
    assert len(t) == 7
    assert t.loc["com.acme.core.Service", "kind"] == "interface"
    assert t.loc["com.acme.core.Service", "protected"]
    assert not t.loc["com.acme.core.internal.Activator", "api"]       # not in Export-Package
    assert not t.loc["com.acme.core.EngineTest", "api"]               # test code
    assert t.loc["com.acme.core.Engine", "wiring_refs"] == 1
    m = model.members.set_index("member")
    legacy = m.loc["com.acme.core.Engine#legacy(java.lang.String)"]
    assert legacy["deprecated"] and legacy["callers"] == 2 and legacy["test_callers"] == 1
    assert m.loc["com.acme.core.internal.Activator#<clinit>()", "kind"] == "initializer"


@needs_jdk
def test_dependency_kinds(model):
    d = model.deps
    kinds = set(zip(d["source"], d["target"], d["kind"]))
    assert ("com.acme.core.Engine", "com.acme.core.Service", "implements") in kinds
    assert ("com.acme.core.Engine", "com.acme.util.Unused", "import") in kinds
    assert ("com.acme.web.Page#render()", "com.acme.core.Service#run(java.util.List)", "call") in kinds
    assert ("com.acme.web.Page#render()", "com.acme.util.Dates#PREFIX", "field_access") in kinds
    # anonymous class body: the call is attributed to the enclosing method
    assert ("com.acme.util.Dates#usesEngine(com.acme.core.Engine)", "com.acme.core.Engine#legacy(java.lang.String)",
            "call") in kinds
    assert not any(s.startswith("com.acme.util.Dates#usesEngine") and t == "com.acme.util.Dates" for s, t, _ in kinds)
    # Unused is only imported: no other kind of dependency on it
    unused = d[d["target_type"] == "com.acme.util.Unused"]
    assert set(unused["kind"]) == {"import"}


@needs_jdk
def test_graphs(model):
    pg = model.package_graph()
    cycles = [sorted(c) for c in nx.strongly_connected_components(pg) if len(c) > 1]
    assert cycles == [["com.acme.core", "com.acme.util"]]
    assert "com.acme.core.EngineTest" not in model.type_graph()
    assert "com.acme.core.EngineTest" in model.type_graph(include_tests=True)
    assert pg["com.acme.web"]["com.acme.core"]["class_edges"] >= 1
    assert list(model.module_dependencies().edges()) == [("web", "core")]


@needs_jdk
def test_unresolved_library_is_recorded_not_fatal(model):
    assert model.manifest["unresolved_types_top"].get("Missing") == 1
    assert int(model.manifest["extractor"]["compiler_errors"]) >= 1


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin"})


@pytest.fixture
def git_project(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(PROJECT, repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    for i in range(3):  # Engine and Dates change together three times
        for f in ("core/src/main/java/com/acme/core/Engine.java", "core/src/main/java/com/acme/util/Dates.java"):
            p = repo / f
            p.write_text(p.read_text() + f"\n// change {i}\n")
        _git(repo, "commit", "-q", "-am", f"change {i}")
    return repo


def test_cochange_history(git_project):
    from dsarp.history import mine
    h = mine(git_project, "HEAD", max_files_per_commit=5)
    assert h.commits_used == 3 and h.commits_skipped == 1      # the initial commit is too large
    top = h.cochange.iloc[0]
    assert {top["file_a"], top["file_b"]} == {"core/src/main/java/com/acme/core/Engine.java",
                                              "core/src/main/java/com/acme/util/Dates.java"}
    assert top["count"] == 3


def test_checkout_from_local_repository(git_project, tmp_path):
    from dsarp.checkout import checkout, head
    commit = head(git_project)
    cfg = _cfg(repo_url=git_project.as_uri(), commit=commit)
    dest = checkout(cfg, tmp_path / "work")
    assert head(dest) == commit
    assert checkout(cfg, tmp_path / "work") == dest   # reusing an existing clone works


@needs_jdk
def test_model_cli(tmp_path, capsys):
    from dsarp.cli import main
    cfg = tmp_path / "p.yaml"
    cfg.write_text("name: javaproj\nrepo_url: x\nbuild: maven\ndetectors:\n  - tool: arcan\n    path: x\n")
    assert main(["model", str(cfg), "--repo", str(PROJECT), "--out", str(tmp_path / "m"), "--history", "0"]) == 0
    assert "types: 7" in capsys.readouterr().out
