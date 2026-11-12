"""Plans for god components, feature concentration, scattered functionality and class cycles,
on a generated Java project, from planning to verification on the compiled code."""

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from dsarp.plan import extra as ex
from dsarp.plan.facts import RepoFacts
from dsarp.plan.run import Planner, kind_of, score
from dsarp.program import ProgramModel

needs_jdk = pytest.mark.skipif(not shutil.which("javac"), reason="needs a JDK (javac)")


def _java(root: Path, pkg: str, name: str, body: str, imports: str = "", test: bool = False) -> None:
    d = root / ("src/test/java" if test else "src/main/java") / pkg.replace(".", "/")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.java").write_text(f"package {pkg};\n{imports}\npublic class {name} {{\n{body}\n}}\n")


def make_shop(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion><groupId>s</groupId>"
                                  "<artifactId>shop</artifactId><version>1</version></project>")
    # god component: two independent chains of related classes, 34 classes in one package
    for prefix in ("Order", "Invoice"):
        for i in range(1, 18):
            nxt = f"    private {prefix}{i + 1} next;" if i < 17 else ""
            _java(root, "com.shop.big", f"{prefix}{i}", nxt + f"\n    public int id() {{ return {i}; }}")
    # feature concentration: related sets of six and five and one class related to another package
    for prefix, n in (("Cart", 6), ("Price", 5)):
        for i in range(1, n + 1):
            nxt = f"    private {prefix}{i + 1} next;" if i < n else ""
            _java(root, "com.shop.misc", f"{prefix}{i}", nxt)
    _java(root, "com.shop.misc", "Formatter", "    private Text text;\n    public String show() { return \"\" + text; }",
          "import com.shop.text.Text;")
    _java(root, "com.shop.text", "Text", "    private TextPart part;")
    for i in range(1, 6):
        _java(root, "com.shop.text", "TextPart" if i == 1 else f"TextPart{i}",
              f"    private TextPart{i + 1} next;" if i < 5 else "")
    _java(root, "com.shop.text", "TextPart5", "    private TextPart part;")
    # scattered functionality: one concern (Fee builds on Tax) used together from com.shop.a, spread over p and q
    _java(root, "com.shop.p", "Tax", "    public static int rate() { return 19; }")
    _java(root, "com.shop.q", "Fee", "    public static int fee() { return com.shop.p.Tax.rate() / 10; }")
    _java(root, "com.shop.a", "Checkout",
          "    public int total() { return com.shop.p.Tax.rate() + com.shop.q.Fee.fee(); }\n"
          "    public int net() { return com.shop.p.Tax.rate() - com.shop.q.Fee.fee(); }")
    # class cycle: Alpha calls a static helper of Beta; Beta keeps a reference to Alpha
    _java(root, "com.shop.c", "Alpha", "    public int total() { return Beta.twice(3) + Beta.LIMIT; }")
    _java(root, "com.shop.c", "Beta", "    static final int LIMIT = 7;\n    private Alpha owner;\n"
          "    static int twice(int x) { return x * 2; }\n    public Alpha owner() { return owner; }")
    _java(root, "com.shop.c", "BetaTest", "    int t() { return Beta.twice(1); }", test=True)


SMELLS = [
    {"smell_id": "d:1", "type": "god_component", "level": "package", "members": ["com.shop.big"],
     "central": "com.shop.big"},
    {"smell_id": "d:2", "type": "feature_concentration", "level": "package", "members": ["com.shop.misc"],
     "central": "com.shop.misc"},
    {"smell_id": "d:3", "type": "scattered_functionality", "level": "package", "members": ["com.shop.p", "com.shop.q"],
     "central": "", "attributes": {"reported_by": ["com.shop.a"]}},
    {"smell_id": "a:4", "type": "cyclic_dependency", "level": "class", "members": ["com.shop.c.Alpha", "com.shop.c.Beta"],
     "central": ""},
]


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    if not shutil.which("javac"):
        pytest.skip("needs a JDK (javac)")
    from dsarp.config import parse_config
    from dsarp.program import build_model
    from dsarp.triage import triage
    base = tmp_path_factory.mktemp("shop")
    repo, res = base / "repo", base / "results"
    make_shop(repo)
    cfg = parse_config({"name": "shop", "repo_url": "x", "build": "maven",
                        "detectors": [{"tool": "designite", "path": "x"}]}, Path("."))
    build_model(cfg, repo, res / "model", history_commits=0)
    rows = [{"tool": "designite", "severity": 5.0, "matched_with": "", "confirmed_by": "", "external_members": 0,
             "generated_only": False, "size": len(s["members"]), **s, "members": json.dumps(s["members"]),
             "attributes": json.dumps(s.get("attributes", {}))} for s in SMELLS]
    pd.DataFrame(rows).to_csv(res / "smells.csv", index=False)
    triage(res, res / "model", res / "triage", repo="shop")
    return repo, res


def _plan(res, kind):
    p = Planner(res / "model", res / "triage", backend="highs", time_limit=20, log=lambda *_: None)
    row = next(r for _, r in p.groups.iterrows() if kind_of(r["type"], r["level"]) == kind)
    return p, row, p.plan_group(row)


def _rec(out):
    return next(x for x in out["plans"] if x["name"] == out["recommended"])


# ---------------------------------------------------------------------- measures

@needs_jdk
def test_measures_match_designite_rules(shop):
    _, res = shop
    rf = RepoFacts.build(ProgramModel(res / "model"))
    xf = ex.extra(rf)
    from dsarp.plan.simulate import initial_state
    snap = xf.snapshot(initial_state(rf), [])
    assert ex.god_size(snap, "com.shop.big") == {"present": True, "classes": 34, "limit": 30}
    f = ex.lcc(snap, "com.shop.misc")
    assert f["sets"] == 3 and f["classes"] == 12 and f["present"]
    assert not ex.lcc(snap, "com.shop.text")["present"]
    methods, classes = ex.scattered_methods(xf, {"com.shop.a"}, {"com.shop.p", "com.shop.q"})
    assert len(methods) == 2 and classes == {"com.shop.p.Tax", "com.shop.q.Fee"}
    assert ex.scattered(snap, methods, classes)["present"]
    cyc = ex.class_cycle(snap, ["com.shop.c.Alpha", "com.shop.c.Beta"])
    assert cyc["present"] and cyc["core_classes_on_cycle"] == 2
    assert xf.tests_of("com.shop.c.Beta") == ["com.shop.c.BetaTest"]


# ---------------------------------------------------------------------- plans

@needs_jdk
def test_god_component_is_split_into_a_cohesive_sub_package(shop):
    _, res = shop
    _, _, out = _plan(res, "god_component")
    rec = _rec(out)
    assert rec["benefit"] == 1.0 and not rec["simulation"]["new_smells"]
    targets = {o["to_package"] for o in rec["operations"]}
    assert len(targets) == 1 and targets.pop() in ("com.shop.big.order", "com.shop.big.invoice")
    moved = {o["class"].rsplit(".", 1)[1].rstrip("0123456789") for o in rec["operations"]}
    assert len(moved) == 1                                        # one whole related chain moves
    assert all(o.get("new_package") for o in rec["operations"])


@needs_jdk
def test_feature_concentration_moves_the_unrelated_class(shop):
    _, res = shop
    _, _, out = _plan(res, "feature_concentration")
    rec = _rec(out)
    assert rec["benefit"] == 1.0
    assert [(o["class"], o["to_package"]) for o in rec["operations"]] == [("com.shop.misc.Formatter", "com.shop.text")]
    assert rec["simulation"]["target_after"]["lcc"] <= 0.2


@needs_jdk
def test_scattered_functionality_is_gathered(shop):
    _, res = shop
    _, _, out = _plan(res, "scattered_functionality")
    assert out["context"]["concern_classes"] == ["com.shop.p.Tax", "com.shop.q.Fee"]
    rec = _rec(out)
    assert rec["benefit"] == 1.0 and len(rec["operations"]) == 1
    assert rec["operations"][0]["class"] in ("com.shop.p.Tax", "com.shop.q.Fee")


@needs_jdk
def test_class_cycle_is_broken_by_moving_static_members(shop):
    _, res = shop
    _, _, out = _plan(res, "cyclic_dependency:class")
    rec = _rec(out)
    assert rec["benefit"] == 1.0
    ops = rec["operations"]
    assert ops[0]["op"] == "move_member" and ops[0]["from_class"] == "com.shop.c.Beta"
    assert set(ops[0]["members"]) == {"com.shop.c.Beta#twice(int)", "com.shop.c.Beta#LIMIT"}


@needs_jdk
def test_score_for_extra_kinds(shop):
    from types import SimpleNamespace
    sim = SimpleNamespace(target_before={"present": True, "lcc": 0.5, "sets": 5},
                          target_after={"present": True, "lcc": 0.35}, new_smells=[], removed_smells=[])
    plan = SimpleNamespace(change_cost=0.0)
    sc = score(plan, sim, {}, "feature_concentration")
    assert sc["benefit"] == pytest.approx(0.45)


# ---------------------------------------------------------------------- verification on the compiled code

@needs_jdk
@pytest.mark.parametrize("kind", ["god_component", "feature_concentration", "scattered_functionality",
                                  "cyclic_dependency:class"])
def test_recommended_plans_compile_and_remove_the_smell(shop, kind):
    from dsarp.verify.run import Verifier
    repo, res = shop
    _, _, out = _plan(res, kind)
    rec = _rec(out)
    v = Verifier(res, repo, log=lambda *_: None)
    r = v.verify_plan(out, rec)
    assert r["compiles"] and r["kept"] == r["operations"], r.get("new_errors_codes")
    assert r["target_removed"] and r["prediction_matches"], (r["target_after"], r["predicted_after"])
    if kind == "cyclic_dependency:class":
        patch = (res / "verify" / out["group_id"] / "plan.patch").read_text()
        assert "Alpha.twice(1)" in patch                              # the test now calls the new owner


# ---------------------------------------------------------------------- review and API policy

def test_review_parse_rules():
    from dsarp.review import Reviewer
    ok = Reviewer.parse('{"steps": [{"step": 1, "verdict": "wrong", "reason": "x"}], "overall": "accept"}', 1)
    assert ok["overall"] == "reject"                       # a wrong step cannot be accepted
    ok = Reviewer.parse('{"steps": [{"step": 1, "verdict": "sensible"}], "overall": "reject"}', 1)
    assert ok["overall"] == "caution"                      # a rejection must name a wrong step
    assert Reviewer.parse('{"steps": [], "overall": "accept"}', 1) is None          # wrong number of steps
    assert Reviewer.parse("no json here", 1) is None


@needs_jdk
def test_review_replaces_rejected_plan_and_caches(shop, tmp_path):
    from dsarp.review import review_plans
    from dsarp.search.llm import Scripted
    _, res = shop
    p = Planner(res / "model", res / "triage", backend="highs", time_limit=20, log=lambda *_: None)
    out = p.run(50)
    (res / "plans").mkdir(exist_ok=True)
    (res / "plans" / "plans.json").write_text(json.dumps(out, default=str))
    g = next(g for g in out["groups"] if g.get("recommended"))
    n = len(next(x for x in g["plans"] if x["name"] == g["recommended"])["operations"])
    reject = json.dumps({"steps": [{"step": i + 1, "verdict": "wrong", "reason": "does not belong"} for i in range(n)],
                         "overall": "reject", "reason": "wrong place"})
    llm = Scripted([reject] + [json.dumps({"steps": [{"step": i + 1, "verdict": "sensible"} for i in range(k)],
                                           "overall": "accept"}) for k in range(1, 40)])
    llm.model = "scripted"
    review_plans(res, llm, groups=1)
    after = json.loads((res / "plans" / "plans.json").read_text())
    ga = next(x for x in after["groups"] if x["group_id"] == g["group_id"])
    assert ga["review"]["original"] == g["recommended"] and ga["review"]["tried"][0]["overall"] == "reject"
    assert ga["recommended"] != g["recommended"] or ga["recommended"] is None
    assert (res / "plans" / "review_cache.json").exists()
    calls = llm.calls
    review_plans(res, llm, groups=1)                        # the same prompts again: answered from the cache
    assert llm.calls == calls


@needs_jdk
def test_widely_used_public_api_is_frozen(shop):
    _, res = shop
    rf = RepoFacts.build(ProgramModel(res / "model"), "balanced")
    assert all(rf.users.get(c, 0) >= 20 for c in rf.frozen)
    rf_off = RepoFacts.build(ProgramModel(res / "model"), "off")
    assert rf_off.frozen == {}
    # a class never moves to a package it has nothing to do with
    c = sorted(rf.prod)[0]
    far = next(q for q in rf.pkg_classes if q != rf.package[c] and rf.evidence(c, q) == 0)
    assert not rf.fits(c, far)


@needs_jdk
def test_combined_plan_applies_all_groups_together(shop):
    from dsarp.verify.combine import Combiner
    from dsarp.verify.run import Verifier
    repo, res = shop
    p = Planner(res / "model", res / "triage", backend="highs", time_limit=20, log=lambda *_: None)
    out = p.run(50)
    (res / "plans").mkdir(exist_ok=True)
    (res / "plans" / "plans.json").write_text(json.dumps(out, default=str))
    v = Verifier(res, repo, log=lambda *_: None)
    v.run()
    r = Combiner(v, log=lambda *_: None).run()
    assert r["compiles"] and len(r["plans_applied"]) >= 3, r
    assert r["removed"] >= 3
    wb, wa = r["whole_repository"]["before"], r["whole_repository"]["after"]
    assert wa.get("god_component", 0) < wb.get("god_component", 0)
    assert (res / "combined" / "plan.patch").read_text().strip()
