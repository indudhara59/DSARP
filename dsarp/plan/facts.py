"""Facts about one smell group that the plan model is built from.

Everything is computed at the level of top-level classes (nested classes move
with their outer class), on production code only:

* **class edges**: aggregated dependencies between top-level classes, with
  kinds, weight, the members they come from and the members they use;
* **movable classes** and their **candidate target packages**, ranked by
  affinity, restricted to the same build module and to packages without a
  class of the same name;
* **deletable members**: deprecated methods, constructors and fields with no
  production callers (callers in tests must be updated);
* for each edge, whether it can disappear by **removing an unused import**,
  by **deleting deprecated members**, or by **dependency inversion**
  (the client uses only instance methods of the provider).

Affinity of a class to a package combines structural coupling (0.6), co-change
(0.3) and name similarity (0.1). Phase 5 replaces these fixed weights with
weights learned from refactoring history.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from ..program import ProgramModel

INVERTIBLE_KINDS = {"call", "param_type", "return_type", "field_type", "local_type", "type_ref", "import", "type_arg"}
AFFINITY_WEIGHTS = {"structural": 0.6, "cochange": 0.3, "semantic": 0.1}
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _flag(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1"])


def tokens(name: str) -> set[str]:
    out = set()
    for part in re.split(r"[.$_]", name):
        out |= {t.lower() for t in _CAMEL.findall(part) if len(t) > 2}
    return out


@dataclass
class ClassEdge:
    source: str                      # top-level class
    target: str
    weight: int
    kinds: set[str]
    source_members: set[str]         # member ids (or the class itself) the dependency comes from
    target_members: set[str]         # member ids (or the class itself) that are used

    @property
    def import_only(self) -> bool:
        return self.kinds == {"import"}


@dataclass
class RepoFacts:
    """Facts for the whole repository, computed once and shared by all groups."""

    model: ProgramModel
    top: dict[str, str]
    package: dict[str, str]          # top-level class -> package
    module: dict[str, str]           # top-level class -> build module
    file: dict[str, str]
    kind: dict[str, str]
    visibility: dict[str, str]
    api: dict[str, bool]
    protected: dict[str, bool]
    wiring: dict[str, int]
    prod: set[str]                   # production top-level classes
    edges: dict[tuple[str, str], ClassEdge]
    out_edges: dict[str, list[tuple[str, str]]]
    in_edges: dict[str, list[tuple[str, str]]]
    pkg_classes: dict[str, set[str]]
    pkg_modules: dict[str, set[str]]
    simple_names: dict[str, set[str]]   # package -> simple names of its top-level classes
    members: pd.DataFrame
    member_info: dict[str, dict]
    cochange: dict[str, dict[str, int]]
    deletable: dict[str, dict] = field(default_factory=dict)
    users: dict[str, int] = field(default_factory=dict)   # classes (production and test) that use a class

    @classmethod
    def build(cls, model: ProgramModel) -> "RepoFacts":
        t = model.types.copy()
        top = dict(zip(t["type"], t["top_level"]))
        tl = t[t["type"] == t["top_level"]]
        prod_mask = ~tl["is_test"] & ~tl["is_generated"]
        prod = set(tl.loc[prod_mask, "type"])
        package = dict(zip(tl["type"], tl["package"]))
        module = dict(zip(tl["type"], tl["build_module"].astype(str).where(tl["build_module"].astype(str) != "",
                                                                           tl["module"].astype(str))))
        d = model.deps
        d = d.assign(st=d["source_type"].map(top), tt=d["target_type"].map(top))
        d = d[d["st"].isin(prod) & d["tt"].isin(prod) & (d["st"] != d["tt"])]
        edges: dict[tuple[str, str], ClassEdge] = {}
        cnt = pd.to_numeric(d["count"], errors="coerce").fillna(1).astype(int)
        for (s, tgt), grp in d.assign(cnt=cnt).groupby(["st", "tt"]):
            edges[(s, tgt)] = ClassEdge(s, tgt, int(grp["cnt"].sum()), set(grp["kind"]),
                                        set(grp["source"]), set(grp["target"]))
        out_e, in_e = defaultdict(list), defaultdict(list)
        for k in edges:
            out_e[k[0]].append(k)
            in_e[k[1]].append(k)
        pkg_classes, pkg_modules, simple = defaultdict(set), defaultdict(set), defaultdict(set)
        for c in prod:
            p = package[c]
            pkg_classes[p].add(c)
            pkg_modules[p].add(module[c])
            simple[p].add(c[len(p) + 1:] if p else c)
        m = model.members.copy()
        for col in ("static", "deprecated", "override", "api", "protected"):
            if col in m:
                m[col] = _flag(m[col])
        m["callers"] = pd.to_numeric(m["callers"], errors="coerce").fillna(0).astype(int)
        m["test_callers"] = pd.to_numeric(m["test_callers"], errors="coerce").fillna(0).astype(int)
        member_info = m.drop_duplicates("member").set_index("member")[["kind", "owner", "visibility", "static", "deprecated", "override",
                                              "callers", "test_callers", "api", "protected"]].to_dict("index")
        co: dict[str, dict[str, int]] = defaultdict(dict)
        for a, b, n in zip(model.cochange["file_a"], model.cochange["file_b"],
                           pd.to_numeric(model.cochange["count"], errors="coerce").fillna(0).astype(int)):
            co[a][b] = n
            co[b][a] = n
        f = cls(model, top, package, module, dict(zip(tl["type"], tl["file"])), dict(zip(tl["type"], tl["kind"])),
                dict(zip(tl["type"], tl["visibility"])), dict(zip(tl["type"], tl["api"])),
                dict(zip(tl["type"], tl["protected"])),
                dict(zip(tl["type"], pd.to_numeric(tl["wiring_refs"], errors="coerce").fillna(0).astype(int))),
                prod, edges, dict(out_e), dict(in_e), dict(pkg_classes), dict(pkg_modules), dict(simple), m,
                member_info, dict(co))
        f.deletable = f._deletable()
        allu = model.deps.assign(st=model.deps["source_type"].map(top), tt=model.deps["target_type"].map(top))
        allu = allu[allu["st"].notna() & (allu["st"] != allu["tt"])]
        f.users = allu.groupby("tt")["st"].nunique().astype(int).to_dict()
        return f

    # ------------------------------------------------------------ members

    def _deletable(self) -> dict[str, dict]:
        """Deprecated methods and fields that nothing calls any more (neither production code nor
        tests) and can be deleted.

        Uses from inside the owner itself count as production uses (the
        program model's caller counts only other classes). Methods that are
        overridden in a subtype are kept (the override would no longer
        compile), and constructors are never deleted (implicit ``super()``
        calls are not visible in the dependencies)."""
        d = self.model.deps
        used_inside = set(d.loc[(d["source"] != d["target"]) & d["target"].str.contains("#", regex=False)
                                & (d["source_type"] == d["target_type"]), "target"])
        ext = d[d["kind"].isin(["extends", "implements"])]
        subtypes: dict[str, set[str]] = defaultdict(set)
        for a, b in zip(ext["source_type"], ext["target_type"]):
            if a != b:
                subtypes[b].add(a)
        out = {}
        for mid, info in self.member_info.items():
            if not info["deprecated"] or info["override"] or info["protected"]:
                continue
            if info["kind"] not in ("method", "field"):
                continue
            owner = self.top.get(info["owner"])
            if owner not in self.prod or self.kind.get(owner) in ("interface", "annotation"):
                continue
            if info["callers"] > 0 or mid in used_inside:
                continue                      # used by production code or by tests (tests would break)
            if info["kind"] == "method" and self._overridden(mid, subtypes):
                continue
            out[mid] = {"owner": owner, "test_callers": int(info["test_callers"]), "api": bool(info["api"])}
        return out

    def _is_overridden(self, mid: str) -> bool:
        if "_subtypes" not in self.__dict__:
            d = self.model.deps
            ext = d[d["kind"].isin(["extends", "implements"])]
            sub: dict[str, set[str]] = defaultdict(set)
            for a, b in zip(ext["source_type"], ext["target_type"]):
                if a != b:
                    sub[b].add(a)
            self.__dict__["_subtypes"] = sub
        return self._overridden(mid, self.__dict__["_subtypes"])

    def _overridden(self, mid: str, subtypes: dict[str, set[str]]) -> bool:
        owner, sig = mid.split("#", 1)
        todo, seen = [owner], {owner}
        while todo:
            for sub in subtypes.get(todo.pop(), ()):
                if sub in seen:
                    continue
                if f"{sub}#{sig}" in self.member_info:
                    return True
                seen.add(sub)
                todo.append(sub)
        return False

    # ------------------------------------------------------------ affinity

    def features(self, c: str, pkg: str) -> dict[str, float]:
        """Evidence that class ``c`` belongs in package ``pkg`` (``c`` itself excluded from ``pkg``).

        structural_out / structural_in: share of the class's outgoing / incoming
        dependency weight that connects it to classes of ``pkg``;
        cochange: share of its co-changes with files of ``pkg``;
        semantic: name-token overlap with ``pkg`` and its classes;
        package_name: overlap with the package name only;
        log_size: size of ``pkg`` (large packages attract classes)."""
        others = self.pkg_classes.get(pkg, set()) - {c}
        outs, ins = self.out_edges.get(c, []), self.in_edges.get(c, [])
        w_out = sum(self.edges[k].weight for k in outs)
        w_in = sum(self.edges[k].weight for k in ins)
        o_pkg = sum(self.edges[k].weight for k in outs if k[1] in others)
        i_pkg = sum(self.edges[k].weight for k in ins if k[0] in others)
        mine = self.cochange.get(self.file.get(c, ""), {})
        co_total = sum(mine.values())
        files = {self.file.get(o) for o in others}
        co_pkg = sum(n for f, n in mine.items() if f in files)
        mine_t = tokens(c[len(self.package[c]) + 1:])
        name_t = tokens(pkg.rsplit(".", 1)[-1])
        pkg_t = set(name_t)
        for o in sorted(others)[:60]:
            pkg_t |= tokens(o[len(pkg) + 1:])
        import math
        return {
            "structural_out": o_pkg / w_out if w_out else 0.0,
            "structural_in": i_pkg / w_in if w_in else 0.0,
            "structural": (o_pkg + i_pkg) / (w_out + w_in) if w_out + w_in else 0.0,
            "cochange": co_pkg / co_total if co_total else 0.0,
            "semantic": len(mine_t & pkg_t) / len(mine_t | pkg_t) if mine_t | pkg_t else 0.0,
            "package_name": len(mine_t & name_t) / len(mine_t) if mine_t else 0.0,
            "log_size": math.log1p(len(others)),
        }

    def affinity(self, c: str, pkg: str) -> float:
        """How well class ``c`` fits package ``pkg`` (0..1).

        Uses the learned model when one is attached (``self.affinity_model``,
        see :mod:`dsarp.learn.affinity`), otherwise fixed weights."""
        key = (c, pkg)
        cache = self.__dict__.setdefault("_aff_cache", {})
        if key in cache:
            return cache[key]
        f = self.features(c, pkg)
        model = self.__dict__.get("affinity_model")
        if model is not None:
            val = round(float(model.score(f)), 4)
        else:
            w = AFFINITY_WEIGHTS
            val = round(w["structural"] * f["structural"] + w["cochange"] * f["cochange"]
                        + w["semantic"] * f["semantic"], 4)
        cache[key] = val
        return val

    def use_affinity_model(self, model) -> None:
        self.__dict__["affinity_model"] = model
        self.__dict__["_aff_cache"] = {}

    # ------------------------------------------------------------ edge properties

    def deletion_set(self, e: ClassEdge) -> set[str] | None:
        """Deprecated members whose deletion removes edge ``e`` (``None`` if impossible)."""
        needed = set()
        for s in e.source_members:
            if "#" not in s:   # type-level dependency (extends, annotation, import ...)
                continue
            if s not in self.deletable:
                return None
            needed.add(s)
        type_level_kinds = {k for k in e.kinds if k != "import"}
        if not needed or (any("#" not in s for s in e.source_members) and
                          type_level_kinds & {"extends", "implements", "annotation", "type_bound"}):
            return None
        return needed

    def invertible(self, e: ClassEdge) -> list[str] | None:
        """Instance methods of the provider used by the client, if the edge can be inverted."""
        if not e.kinds <= INVERTIBLE_KINDS or self.kind.get(e.target) != "class":
            return None
        if self.protected.get(e.target) or self.protected.get(e.source):
            return None
        if self.kind.get(e.source) != "class":
            return None            # an interface or enum client: implementers would stop compiling
        for m in e.source_members:
            info = self.member_info.get(m)
            if info is not None and info["kind"] == "method" and (info["override"] or self._is_overridden(m)):
                return None        # the provider type is part of an inherited signature
        methods = []
        for m in e.target_members:
            if "#" not in m:
                if m != e.target:
                    return None    # a nested type of the provider is used
                continue
            info = self.member_info.get(m)
            if info is None or info["kind"] != "method" or info["static"]:
                return None
            methods.append(m)
        return sorted(methods) if methods else None

    def signature_types(self, methods: list[str]) -> set[str]:
        """Production classes named in the signatures of ``methods`` (return, parameter, throws types).

        An interface extracted for these methods depends on them."""
        if "_sig" not in self.__dict__:
            d = self.model.deps
            d = d[d["kind"].isin(["return_type", "param_type", "throws", "type_arg", "type_bound"])
                  & d["source"].str.contains("#", regex=False)]
            sig: dict[str, set[str]] = defaultdict(set)
            for src, tgt in zip(d["source"], d["target_type"]):
                t = self.top.get(tgt)
                if t in self.prod:
                    sig[src].add(t)
            self.__dict__["_sig"] = sig
        out: set[str] = set()
        for m in methods:
            out |= self.__dict__["_sig"].get(m, set())
        return out

    def dependents(self, c: str) -> set[str]:
        return {k[0] for k in self.in_edges.get(c, [])}


@dataclass
class GroupFacts:
    """Facts for one group: what may change and where things may go."""

    group_id: str
    smell_type: str
    core: list[str]                          # packages of the group
    packages: list[str]                      # all packages in the model (core + targets)
    movable: dict[str, list[tuple[str, float]]]   # class -> [(package, affinity)], current package first
    edges: list[ClassEdge]
    deletable: dict[str, dict]
    smell: dict                              # smell-specific data (unstable targets, hub thresholds ...)


def group_facts(rf: RepoFacts, group_id: str, smell_type: str, scope: dict, max_targets: int = 3,
                max_movable: int = 400, smell: dict | None = None) -> GroupFacts:
    """Select movable classes, targets and relevant edges for one package-level group."""
    core = [p for p in scope["core"] if p in rf.pkg_classes]
    core_set = set(core)
    neighbours = [p for p in scope.get("neighbours", []) if p in rf.pkg_classes]
    allowed_pkgs = core_set | set(neighbours)
    smell = smell or {}
    focus_pairs = smell.get("focus_pairs")   # for unstable/hub: package pairs whose edges matter

    # classes on dependencies that matter for the smell
    boundary = set()
    for (s, t), e in rf.edges.items():
        ps, pt = rf.package[s], rf.package[t]
        if ps == pt:
            continue
        if focus_pairs is not None:
            if (ps, pt) in focus_pairs:
                boundary |= {s, t}
        elif ps in core_set and pt in core_set:
            boundary |= {s, t}
    boundary = {c for c in boundary if rf.package[c] in core_set or focus_pairs is not None}

    def weight(c):
        return sum(rf.edges[k].weight for k in rf.out_edges.get(c, []) + rf.in_edges.get(c, []))
    ranked = sorted(boundary, key=lambda c: (-weight(c), c))[:max_movable]

    movable: dict[str, list[tuple[str, float]]] = {}
    for c in ranked:
        if rf.protected.get(c):
            continue
        cur = rf.package[c]
        simple = c[len(cur) + 1:]
        cands = set()
        for k in rf.out_edges.get(c, []) + rf.in_edges.get(c, []):
            other = k[1] if k[0] == c else k[0]
            q = rf.package[other]
            if q != cur and q in allowed_pkgs and rf.module[c] in rf.pkg_modules.get(q, set()) \
                    and simple not in rf.simple_names.get(q, set()):
                cands.add(q)
        scored = sorted(((q, rf.affinity(c, q)) for q in cands), key=lambda x: (-x[1], x[0]))[:max_targets]
        movable[c] = [(cur, rf.affinity(c, cur))] + scored

    pkgs_used = set(core) | {q for opts in movable.values() for q, _ in opts}
    if smell_type == "cyclic_dependency":
        pkgs_used |= cycle_context(rf, movable, core_set)
    if focus_pairs is not None:
        pkgs_used |= {p for pair in focus_pairs for p in pair}
    rel = []
    for (s, t), e in rf.edges.items():
        ps_opts = {q for q, _ in movable.get(s, [(rf.package[s], 0.0)])} & pkgs_used
        pt_opts = {q for q, _ in movable.get(t, [(rf.package[t], 0.0)])} & pkgs_used
        if ps_opts and pt_opts and not (len(ps_opts) == 1 and ps_opts == pt_opts):
            rel.append(e)
    deletable = {m: v for m, v in rf.deletable.items() if rf.package.get(v["owner"]) in pkgs_used}
    return GroupFacts(group_id, smell_type, core, sorted(pkgs_used), movable, rel, deletable, smell)


def cycle_context(rf: RepoFacts, movable: dict[str, list[tuple[str, float]]], core: set[str]) -> set[str]:
    """Packages that could lie on a cycle with the core packages after some moves.

    Every possible placement of the movable classes is added to the current
    package graph; the strongly connected components that contain core
    packages are the context. Dependencies between packages outside the
    context can never close a cycle through the core, so leaving them out of
    the plan model is safe.
    """
    import networkx as nx
    g = nx.DiGraph()
    for (s, t) in rf.edges:
        for q, _ in movable.get(s, [(rf.package[s], 0.0)]):
            for r, _ in movable.get(t, [(rf.package[t], 0.0)]):
                if q != r:
                    g.add_edge(q, r)
    out: set[str] = set()
    for comp in nx.strongly_connected_components(g):
        if comp & core:
            out |= comp
    return out


def current_package_pairs(rf: RepoFacts) -> set[tuple[str, str]]:
    return {(rf.package[s], rf.package[t]) for s, t in rf.edges if rf.package[s] != rf.package[t]}


def move_cost(rf: RepoFacts, c: str, target: str, aff: float) -> float:
    """Cost of moving class ``c`` to ``target``.

    ``1`` per move, up to ``4`` for a poor fit (low affinity to the target),
    ``0.2`` per class that uses it (each needs its imports changed, tests
    included), ``5`` if it is public API and ``2`` per framework wiring entry.
    The per-user term makes widely used classes (like ``ParseContext`` in a
    code base) expensive to move: in practice they are the last to move."""
    users = rf.users.get(c, len(rf.dependents(c)))
    cost = 1.0 + 4.0 * (1.0 - aff) + 0.2 * users
    if rf.api.get(c):
        cost += 5.0
    cost += 2.0 * rf.wiring.get(c, 0)
    return round(cost, 3)


def deletion_cost(info: dict) -> float:
    return round(0.5 + 0.5 * info["test_callers"] + (1.0 if info["api"] else 0.0), 3)


def inversion_cost(methods: list[str]) -> float:
    return round(3.0 + 0.5 * len(methods), 3)


IMPORT_COST = 0.05
EDGE_COST = 0.01   # tie-breaker: prefer fewer package dependencies
NEW_EDGE_COST = 1.5   # a package dependency that does not exist today (architecture erosion)
