"""Facts and measures for the smell types beyond package cycles, unstable and hub-like packages.

* **God component** (Designite, Lippert and Roock): a package with more than
  30 classes (nested classes counted).
* **Feature concentration** (Designite): Lack of Component Cohesion
  ``LCC = disconnected sets of related classes / classes > 0.2``; classes are
  related by association, aggregation, composition or inheritance (Sharma,
  Singh and Spinellis 2020). Like Designite, all classes of the package are
  counted (test classes and nested classes too). With field types, ``extends``
  and ``implements`` as the relations, DSARP reproduces 336 of the 537 related
  sets Designite reports for Tika (the Designite output is from a later Tika
  version).
* **Scattered functionality** (Designite): methods that access two or more
  other packages together, more than once. Designite's exact filter is not
  published (re-implementing the published rule agrees poorly with its output),
  so the smell is measured directly: how many methods of the reporting
  packages still use the *concern classes* (the classes of the reported
  packages they access) spread over two or more packages.
* **Cyclic dependency between classes** and **hub-like classes**: the same
  rules as for packages (:mod:`dsarp.detect`) on the class graph.

All measures work on a :class:`Snapshot`: the code after a plan, built from
the program model and the plan operations (simulation, stage 6) or from the
compiled working copy (verification, stage 8), so that both are measured the
same way.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import networkx as nx
import pandas as pd

from .. import detect as det

REL_KINDS = {"field_type", "extends", "implements"}          # association, aggregation, composition, inheritance
ACCESS_KINDS = {"call", "field_access", "new", "method_ref"}
FC_LCC = 0.2                                                  # Designite's threshold (LCC >= 0.2)
FC_MIN_TYPES = 6        # Designite never reports packages with fewer types (checked on 242 reports, 5 repositories)
GOD_CLASSES = det.PIPELINE.god_classes                        # more than 30 classes
_TEST_NAME = re.compile(r"^(?:Test(?P<a>\w+)|(?P<b>\w+?)(?:Tests?|IT|TestCase|Benchmark))$")


def _flag(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(["true", "1"])


@dataclass
class Snapshot:
    """The code after a plan, as far as the extra measures need it."""

    type_pkg: dict[str, str]                  # every type (production, test, nested) -> package
    top: dict[str, str]                       # type -> top-level type
    tests: set[str]                           # test types
    prod_top: set[str]                        # production top-level classes (after the plan)
    class_edges: set[tuple[str, str]]         # dependencies between production top-level classes
    rel_pairs: set[frozenset]                 # related type pairs (feature concentration)
    method_targets: dict[str, set[str]]       # production method -> top-level classes it accesses
    method_owner: dict[str, str]              # production method -> top-level owner (after the plan)
    generated: set[str] = field(default_factory=set)
    test_edges: set[tuple[str, str]] = field(default_factory=set)   # test top-level class -> production class

    def package_types(self, pkg: str) -> list[str]:
        return [t for t, p in self.type_pkg.items() if p == pkg and t not in self.generated]

    def class_graph(self, with_tests: bool = False) -> nx.DiGraph:
        g = nx.DiGraph()
        g.add_nodes_from(self.prod_top)
        g.add_edges_from((s, t) for s, t in self.class_edges if s != t)
        if with_tests:
            g.add_edges_from((s, t) for s, t in self.test_edges if s != t)
        return g


class ExtraFacts:
    """Repository facts for the extra smell types (built once per repository)."""

    def __init__(self, rf):
        self.rf = rf
        t = rf.model.types
        gen = _flag(t["is_generated"])
        self.type_pkg = dict(zip(t["type"], t["package"]))
        self.top = dict(zip(t["type"], t["top_level"]))
        self.tests = set(t.loc[_flag(t["is_test"]), "type"])
        self.generated = set(t.loc[gen, "type"])
        self.nested_of: dict[str, list[str]] = defaultdict(list)
        for ty, tl in self.top.items():
            if ty != tl:
                self.nested_of[tl].append(ty)
        d = rf.model.deps
        rel = d[d["kind"].isin(REL_KINDS) & (d["source_type"] != d["target_type"])]
        known = set(self.type_pkg) - self.generated
        self.rel_pairs = {frozenset((a, b)) for a, b in zip(rel["source_type"], rel["target_type"])
                          if a in known and b in known}
        acc = d[d["kind"].isin(ACCESS_KINDS) & d["source"].str.contains("#", regex=False)]
        mt: dict[str, set[str]] = defaultdict(set)
        owner: dict[str, str] = {}
        for src, st, tt in zip(acc["source"], acc["source_type"], acc["target_type"]):
            s_top, t_top = self.top.get(st), self.top.get(tt)
            if s_top in rf.prod and t_top in rf.prod and s_top != t_top:
                mt[src].add(t_top)
                owner[src] = s_top
        self.method_targets = dict(mt)
        self.method_owner = owner
        # dependencies of test classes on production classes (Arcan counts them for hub-like classes)
        self.test_rows: dict[str, list[tuple[str, str, str]]] = defaultdict(list)   # prod top -> [(test, target, kind)]
        te = set()
        for st, tt, tgt, kind in zip(d["source_type"], d["target_type"], d["target"], d["kind"]):
            a, b = self.top.get(st), self.top.get(tt)
            if a in self.tests and self.top.get(a) == a and b in rf.prod:
                te.add((a, b))
                self.test_rows[b].append((a, tgt, kind))
        self.test_edges = te
        # which test classes test which production class (same package, by name or by exclusive use)
        self._tests_of: dict[str, list[str]] | None = None

    # ------------------------------------------------------------------ tests that move with a class

    def tests_of(self, c: str) -> list[str]:
        if self._tests_of is None:
            rf = self.rf
            by_pkg_simple: dict[tuple[str, str], str] = {}
            for x in rf.prod:
                by_pkg_simple[(rf.package[x], x.rsplit(".", 1)[-1])] = x
            d = rf.model.deps
            uses: dict[str, set[str]] = defaultdict(set)
            for st, tt in zip(d["source_type"], d["target_type"]):
                a, b = self.top.get(st), self.top.get(tt)
                if a in self.tests and b in rf.prod:
                    uses[a].add(b)
            out: dict[str, list[str]] = defaultdict(list)
            for tst in self.tests:
                if self.top.get(tst) != tst:
                    continue
                pkg = self.type_pkg[tst]
                simple = tst.rsplit(".", 1)[-1]
                m = _TEST_NAME.match(simple)
                target = None
                if m:
                    target = by_pkg_simple.get((pkg, m.group("a") or m.group("b")))
                if target is None:
                    same = {u for u in uses.get(tst, ()) if rf.package.get(u) == pkg}
                    if len(same) == 1:
                        target = next(iter(same))
                if target is not None:
                    out[target].append(tst)
            self._tests_of = {k: sorted(v) for k, v in out.items()}
        return self._tests_of.get(c, [])

    def test_owner(self) -> dict[str, str]:
        """Test class -> the production class it tests (see :meth:`tests_of`)."""
        if getattr(self, "_test_owner", None) is None:
            self.tests_of("")
            self._test_owner = {t: c for c, ts in self._tests_of.items() for t in ts}
        return self._test_owner

    # ------------------------------------------------------------------ snapshots

    def snapshot(self, st, ops: list[dict] | None = None) -> Snapshot:
        """The code after a simulated plan (``st`` from :func:`dsarp.plan.simulate.apply`)."""
        type_pkg = dict(self.type_pkg)
        for ty, tl in self.top.items():
            if tl in st.package:
                type_pkg[ty] = st.package[tl]
        for o in ops or []:
            if o["op"] == "move_class":
                for tst in o.get("tests", []):
                    for x in [tst] + self.nested_of.get(tst, []):
                        type_pkg[x] = o["to_package"]
        for iface, p in st.interfaces.items():
            type_pkg[iface] = p
        owner = dict(self.method_owner)
        moves = {}
        for o in ops or []:
            if o["op"] == "move_member":
                for m in o.get("members", [o["member"]]):
                    owner[m] = o["to_class"]
                    moves[m] = o["to_class"]
        test_edges = self.test_edges
        if moves:
            olds = {self.top.get(m.split("#")[0], m.split("#")[0]) for m in moves}
            test_edges = {e for e in test_edges if e[1] not in olds}
            for c in olds:
                for t, tgt, kind in self.test_rows.get(c, []):
                    b = moves.get(tgt, c)
                    if kind != "import" or b == c:
                        test_edges.add((t, b))
        return Snapshot(type_pkg, {**self.top, **{i: i for i in st.interfaces}}, self.tests,
                        set(st.package) | set(st.interfaces), set(st.edges), self.rel_pairs,
                        self.method_targets, owner, self.generated, test_edges)


def snapshot_from_compile(res, production_files: set[str]) -> Snapshot:
    """The code as the compiler sees it after the plan (stage 8)."""
    t = res.types
    type_pkg = dict(zip(t["type"], t["package"]))
    top = dict(zip(t["type"], t["top_level"])) if "top_level" in t else {x: x for x in type_pkg}
    prod_types = set(t.loc[t["file"].isin(production_files), "type"])
    tests = set(type_pkg) - prod_types
    prod_top = {top[x] for x in prod_types if top.get(x) == x}
    d = res.deps
    st = d["source"].str.split("#").str[0]
    tt = d["target"].str.split("#").str[0]
    edges, rel, mt, owner, te = set(), set(), defaultdict(set), {}, set()
    for src, s, tg, kind in zip(d["source"], st, tt, d["kind"]):
        a, b = top.get(s), top.get(tg)
        if a is None or b is None:
            continue
        if kind in REL_KINDS and s != tg and s in type_pkg and tg in type_pkg:
            rel.add(frozenset((s, tg)))
        if a in tests and top.get(a) == a and b in prod_top:
            te.add((a, b))
        if a in prod_top and b in prod_top and a != b:
            edges.add((a, b))
            if kind in ACCESS_KINDS and "#" in src:
                mt[src].add(b)
                owner[src] = a
    return Snapshot(type_pkg, top, tests, prod_top, edges, rel, dict(mt), owner, set(), te)


# ---------------------------------------------------------------------- measures

def lcc(snap: Snapshot, pkg: str) -> dict:
    """Designite's Lack of Component Cohesion of a package (all its classes, tests and nested ones too)."""
    nodes = snap.package_types(pkg)
    if not nodes:
        return {"present": False, "classes": 0, "sets": 0, "lcc": 0.0, "note": "package no longer exists"}
    ns = set(nodes)
    g = nx.Graph()
    g.add_nodes_from(nodes)
    for pr in snap.rel_pairs:
        a, b = tuple(pr)
        if a in ns and b in ns:
            g.add_edge(a, b)
    k = nx.number_connected_components(g)
    v = k / len(nodes)
    return {"present": fc_present(len(nodes), k), "classes": len(nodes), "sets": k, "lcc": round(v, 4)}


def fc_present(n: int, sets: int) -> bool:
    """Designite's feature concentration: at least 6 types and LCC = sets / types >= 0.2."""
    return n >= FC_MIN_TYPES and 5 * sets >= n


def god_size(snap: Snapshot, pkg: str) -> dict:
    """Designite's god component: more than 30 types (production and test classes, nested ones too)."""
    n = sum(1 for x, p in snap.type_pkg.items() if p == pkg and x not in snap.generated)
    return {"present": n > GOD_CLASSES, "classes": n, "limit": GOD_CLASSES}


def scattered(snap: Snapshot, methods: list[str], concern: set[str]) -> dict:
    """Methods (from the reporting packages) that use the concern classes spread over 2+ packages."""
    per_pkg: Counter = Counter()
    spread = 0
    for m in methods:
        owner = snap.method_owner.get(m)
        if owner is None:
            continue
        own_pkg = snap.type_pkg.get(owner)
        pk = {snap.type_pkg.get(c) for c in snap.method_targets.get(m, set()) & concern} - {own_pkg, None}
        if len(pk) >= 2:
            spread += 1
            per_pkg[own_pkg] += 1
    present = any(v > 1 for v in per_pkg.values())        # "more than once", as in Designite
    return {"present": present, "methods": spread, "packages": dict(per_pkg)}


def class_cycle(snap: Snapshot, core: list[str]) -> dict:
    from .simulate import feedback_arcs
    g = snap.class_graph()
    core_set = set(core) & set(g)
    best, members = 0, []
    for comp in nx.strongly_connected_components(g):
        n = len(comp & core_set)
        if n > best:
            best, members = n, sorted(comp & core_set)
    return {"present": best >= 2, "core_classes_on_cycle": best, "on_cycle": members,
            "dependencies_to_cut": feedback_arcs(g, core_set)}


def class_hub(snap: Snapshot, hub: str, th: det.Thresholds = det.ARCAN_LIKE) -> dict:
    """Arcan's hub-like class rule on the class graph with test classes as clients (as Arcan counts them)."""
    g = snap.class_graph(with_tests=True)
    if hub not in g:
        return {"present": False, "note": "class no longer exists"}
    h = det.hubs(g, th)
    r = h.loc[hub]
    return {"present": bool(r["hub"]), "fan_in": int(r["fan_in"]), "fan_out": int(r["fan_out"]),
            "max_in": int(r["in_threshold"]), "max_out": int(r["out_threshold"])}


def scattered_methods(xf: ExtraFacts, accessors: set[str], concern_pkgs: set[str]) -> tuple[list[str], set[str]]:
    """The methods behind a scattered-functionality group and the concern classes they use."""
    rf = xf.rf
    methods, classes = [], set()
    for m, targets in xf.method_targets.items():
        owner = xf.method_owner[m]
        if rf.package.get(owner) not in accessors:
            continue
        hit = {c for c in targets if rf.package.get(c) in concern_pkgs}
        if len({rf.package[c] for c in hit}) >= 2:
            methods.append(m)
            classes |= hit
    return sorted(methods), classes


def extra(rf) -> ExtraFacts:
    """The repository's :class:`ExtraFacts` (built on first use)."""
    if "_extra" not in rf.__dict__:
        rf.__dict__["_extra"] = ExtraFacts(rf)
    return rf.__dict__["_extra"]
