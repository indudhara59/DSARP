"""New verified operations: Move Instance Method (class cycles), from planning to the compiled code."""

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from dsarp.plan.run import Planner, kind_of

needs_jdk = pytest.mark.skipif(not shutil.which("javac"), reason="needs a JDK (javac)")


def _java(root: Path, pkg: str, name: str, body: str, imports: str = "") -> None:
    d = root / "src/main/java" / pkg.replace(".", "/")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.java").write_text(f"package {pkg};\n{imports}\npublic class {name} {{\n{body}\n}}\n")


def build_project(base: Path, make, smells: list[dict]):
    from dsarp.config import parse_config
    from dsarp.program import build_model
    from dsarp.triage import triage
    repo, res = base / "repo", base / "results"
    repo.mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion><groupId>s</groupId>"
                                  "<artifactId>x</artifactId><version>1</version></project>")
    make(repo)
    cfg = parse_config({"name": "x", "repo_url": "x", "build": "maven",
                        "detectors": [{"tool": "designite", "path": "x"}]}, Path("."))
    build_model(cfg, repo, res / "model", history_commits=0)
    rows = [{"tool": "arcan", "severity": 5.0, "matched_with": "", "confirmed_by": "", "external_members": 0,
             "generated_only": False, "size": len(s["members"]), **s, "members": json.dumps(s["members"]),
             "attributes": json.dumps(s.get("attributes", {}))} for s in smells]
    pd.DataFrame(rows).to_csv(res / "smells.csv", index=False)
    triage(res, res / "model", res / "triage", repo="x")
    return repo, res


def make_billing(root: Path) -> None:
    # Invoice keeps its Order (type-level); Order.vat(Invoice) is the only use of Invoice in Order
    _java(root, "com.b", "Invoice", "    private Order order = new Order();\n"
          "    public double total() { return 100.0; }\n    public Order order() { return order; }")
    _java(root, "com.b", "Order", "    static final double RATE = 0.19;\n"
          "    double vat(Invoice inv, int scale) { return inv.total() * RATE * scale; }\n"
          "    public int lines() { return 3; }")
    _java(root, "com.b", "Billing", "    public double run() {\n        Order o = new Order();\n"
          "        Invoice i = new Invoice();\n        return o.vat(i, 2) + o.lines();\n    }")


@pytest.fixture(scope="module")
def billing(tmp_path_factory):
    if not shutil.which("javac"):
        pytest.skip("needs a JDK (javac)")
    return build_project(tmp_path_factory.mktemp("billing"), make_billing, [
        {"smell_id": "a:1", "type": "cyclic_dependency", "level": "class", "members": ["com.b.Order", "com.b.Invoice"],
         "central": ""}])


@needs_jdk
def test_move_instance_method_breaks_the_class_cycle(billing):
    from dsarp.verify.run import Verifier
    repo, res = billing
    p = Planner(res / "model", res / "triage", backend="highs", time_limit=20, log=lambda *_: None)
    row = next(r for _, r in p.groups.iterrows() if kind_of(r["type"], r["level"]) == "cyclic_dependency:class")
    out = p.plan_group(row)
    rec = next(x for x in out["plans"] if x["name"] == out["recommended"])
    op = rec["operations"][0]
    assert op["op"] == "move_member" and op.get("instance") and op["members"] == ["com.b.Order#vat(com.b.Invoice,int)"]
    assert op["to_class"] == "com.b.Invoice" and rec["benefit"] == 1.0
    r = Verifier(res, repo, log=lambda *_: None).verify_plan(out, rec)
    assert r["compiles"] and r["kept"] == 1, r.get("new_errors_codes")
    assert r["target_removed"] and r["prediction_matches"]
    patch = (res / "verify" / out["group_id"] / "plan.patch").read_text()
    assert "i.vat(2)" in patch and "this.total() * Order.RATE * scale" in patch


def test_detector_command_and_group_matching(tmp_path):
    """A detector command runs with {repo}/{out}; its output says per group whether the smell is still there."""
    import sys
    from dsarp.verify.detectors import run_detector
    fake = tmp_path / "fake_designite.py"
    fake.write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'ArchitectureSmells.csv').write_text('Project,Package,Smell,Description\\n'\n"
        "  'x,com.a,God Component,The tool detected the smell in this component because the component contains high "
        "number of classes. Number of classes in the component are: 40\\n')\n"
        "(out / 'TypeMetrics.csv').write_text('Project,Package,Class,NOF,NOPF,NOM,NOPM,LOC,WMC,NC,DIT,LCOM,Fan-In,"
        "Fan-Out,File,Line no\\n')\n")
    groups = [{"group_id": "G1", "type": "god_component", "central": "com.a", "core": ["com.a"]},
              {"group_id": "G2", "type": "god_component", "central": "com.b", "core": ["com.b"]},
              {"group_id": "C1", "type": "cyclic_dependency", "core": ["com.a", "com.b"]}]
    r = run_detector("designite", f"{sys.executable} {fake} {{repo}} {{out}}", tmp_path, tmp_path / "out", groups=groups)
    assert r["ok"] and r["smells"] == {"god_component": 1}
    assert r["groups"] == {"G1": True, "G2": False, "C1": None}       # C1: no cycles reported at all -> unknown


def test_detect_stage_runs_configured_command(tmp_path):
    import subprocess
    import sys
    import yaml
    from dsarp.cli import main
    repo = tmp_path / "work" / "demo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "A.java").write_text("class A {}\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    fake = tmp_path / "det.py"
    fake.write_text("import sys, pathlib; o = pathlib.Path(sys.argv[2]); o.mkdir(parents=True, exist_ok=True); "
                    "(o / 'seen.txt').write_text(str(sorted(p.name for p in pathlib.Path(sys.argv[1]).iterdir())))\n")
    cfg = tmp_path / "demo.yaml"
    cfg.write_text(yaml.safe_dump({"name": "demo", "repo_url": "x", "commit": head,
                                   "detectors": [{"tool": "designite", "command": f"{sys.executable} {fake} {{repo}} {{out}}"}]}))
    import sys as _s
    argv = _s.argv
    _s.argv = ["dsarp", "detect", str(cfg), "--workdir", str(tmp_path / "work"), "--data-root", str(tmp_path / "data")]
    try:
        assert main() == 0
    finally:
        _s.argv = argv
    out = tmp_path / "data" / "detected" / "demo" / "designite"
    assert "A.java" in (out / "seen.txt").read_text() and (out / "dsarp-detect.json").exists()


def test_history_finds_later_class_moves(tmp_path):
    import subprocess
    from dsarp.evaluate.history import later_moves
    repo = tmp_path / "r"
    (repo / "src/main/java/a").mkdir(parents=True)
    (repo / "src/main/java/a/X.java").write_text("package a;\npublic class X {\n  int v = 1;\n  int w = 2;\n}\n")

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "one")
    first = git("rev-parse", "HEAD")
    (repo / "src/main/java/b").mkdir(parents=True)
    git("mv", "src/main/java/a/X.java", "src/main/java/b/X.java")
    (repo / "src/main/java/b/X.java").write_text("package b;\npublic class X {\n  int v = 1;\n  int w = 2;\n}\n")
    git("add", ".")
    git("commit", "-qm", "move")
    m = later_moves(repo, first, "HEAD")
    assert list(zip(m["old_class"], m["new_class"])) == [("a.X", "b.X")]


def test_rating_agreement():
    from dsarp.evaluate.rating import cohen_kappa, fleiss_kappa
    a = ["sensible", "sensible", "wrong", "questionable", "sensible"]
    assert cohen_kappa(a, a) == 1.0
    assert cohen_kappa(a, ["sensible", "wrong", "wrong", "sensible", "sensible"]) < 1.0
    assert fleiss_kappa([[x, x, x] for x in a]) == 1.0


@needs_jdk
def test_rating_sheet_and_agreement(billing, tmp_path):
    from dsarp.evaluate.rating import agreement, sheet
    _, res = billing
    p = Planner(res / "model", res / "triage", backend="highs", time_limit=20, log=lambda *_: None)
    (res / "plans").mkdir(exist_ok=True)
    (res / "plans" / "plans.json").write_text(json.dumps(p.run(10), default=str))
    f = sheet(res, 10)
    df = pd.read_csv(f, keep_default_na=False)
    assert len(df) >= 1 and set(df.columns) >= {"step_id", "what", "element", "from", "to", "verdict"}
    df["verdict"] = "sensible"
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    df.to_csv(a, index=False)
    df.to_csv(b, index=False)
    r = agreement([a, b])
    assert r["steps"] == len(df) and r["majority"]["sensible_share"] == 1.0


def test_refactoringminer_output_is_read(tmp_path):
    import subprocess
    import sys
    from dsarp.evaluate.history import refminer
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "x"], check=True)
    fake = tmp_path / "rm.py"
    fake.write_text("import json, sys\njson.dump({'commits': [{'sha1': 'abc', 'refactorings': [{'type': 'Move Method',"
                    " 'description': 'Move Method vat(Invoice) from class Order to class Invoice'}]}]}, "
                    "open(sys.argv[1], 'w'))\n")
    refs = refminer(f"{sys.executable} {fake} {{json}}", repo, "HEAD", "HEAD", tmp_path)
    assert refs == [{"commit": "abc", "type": "Move Method",
                     "description": "Move Method vat(Invoice) from class Order to class Invoice"}]


@needs_jdk
def test_design_advice_is_kept_only_with_real_names(billing):
    from dsarp.advice import Advisor
    from dsarp.plan.facts import RepoFacts
    from dsarp.program import ProgramModel
    from dsarp.search.llm import Scripted
    _, res = billing
    rf = RepoFacts.build(ProgramModel(res / "model"))
    good = json.dumps({"directions": [{"title": "Introduce a tax policy", "change": "Introduce TaxPolicy used by "
                                       "com.b.Order and com.b.Invoice", "effort": "low", "risk": "small"}]})
    bad = json.dumps({"directions": [{"title": "x", "change": "Move com.b.Nothing into com.zz.Other", "effort": "low"}]})
    adv = Advisor(rf, Scripted([good, bad]))
    g = {"group_id": "G", "type": "cyclic_dependency", "kind": "cyclic_dependency:class",
         "core": ["com.b.Order", "com.b.Invoice"], "no_plan_reason": "test"}
    assert adv.advise(g)["directions"][0]["title"] == "Introduce a tax policy"
    assert "do not exist" in adv.advise({**g, "group_id": "H", "core": ["com.b.Order"]})["rejected"]


def test_open_source_designitejava_output_is_read(tmp_path):
    from dsarp.config import DetectorInput
    from dsarp.detectors import ADAPTERS
    (tmp_path / "designCodeSmells.csv").write_text(
        "Project Name,Package Name,Type Name,Code Smell\n"
        "x,com.b,Order,Cyclic-Dependent Modularization\nx,com.b,Hub,Hub-like Modularization\n"
        "x,com.b,Order,Insufficient Modularization\n")
    res = ADAPTERS["designite"](DetectorInput(tool="designite", path=tmp_path))
    kinds = sorted((s.smell_type.value, s.level.value, tuple(s.members)) for s in res.smells)
    assert kinds == [("cyclic_dependency", "class", ("com.b.Order",)), ("hub_like_dependency", "class", ("com.b.Hub",))]
