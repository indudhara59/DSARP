"""Plans for class-level smells: cycles between classes and hub-like classes.

Classes do not move between packages here; the dependencies between them
change. Candidate operations for a dependency ``s -> t``:

* **Remove unused import** (import-only dependency);
* **Delete dead code** (deprecated members nobody calls, as for packages);
* **Extract interface** (``s`` uses only instance methods of ``t``): ``s``
  depends on a new interface that ``t`` implements;
* **Move instance method** (feature envy): an instance method of ``s`` that
  takes a ``t`` parameter, uses no state of ``s`` and is not called inside
  ``s`` becomes an instance method of ``t`` (the parameter becomes ``this``;
  callers ``x.m(a, tt)`` become ``tt.m(a)``);
* **Move static members**: *pull* the static methods and fields of ``t`` that
  ``s`` uses into ``s``, or *push* the static members of ``s`` that use ``t``
  into ``t``. Members only move with everything they need from their class
  (static members, no nested types), overloaded names move together or not
  at all; callers elsewhere are redirected to the new owner.

The effect of every operation on the class graph is computed exactly from the
member-level dependencies of the program model (:class:`MemberIndex`), the
same way the simulation applies it. The plan model then chooses operations::

    y[j] = 1                operation j is applied
    p[e] >= 1 - sum(y[j] removing e)        a dependency stays unless an operation removes it
    p[e] >= y[j]            for dependencies that operation j creates
    o[a] >= o[b] + 1 - M * (1 - p[a,b]) + M * k[a,b]   (k: dependency left on a cycle)
    each member moves at most once
    minimise  sum(k) * BIG + cost(y)

For a **hub-like class** the same operations reduce its fan-in (interfaces
for its clients, static members pulled into their callers) or its fan-out
(static members pushed to the classes they use); the hub must get below the
detection threshold on one side, as for hub-like packages.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .. import detect as det
from . import solver as slv
from .facts import IMPORT_COST, RepoFacts, deletion_cost, inversion_cost
from .generator import Plan

MOVABLE_KINDS = {"call", "field_access", "method_ref", "import"}
_SIG = re.compile(r"\(.*\)$")


@dataclass
class MemberIndex:
    """Member-level dependency rows between production top-level classes (intra-class rows included)."""

    rows_from: dict[str, list[tuple[str, str, str, str]]]   # class -> [(source id, target id, target top, kind)]
    rows_to: dict[str, list[tuple[str, str, str, str]]]     # class -> [(source id, source top, target id, kind)]
    statics: dict[str, dict[str, list[str]]]           # class -> member name -> member ids (static methods/fields)
    nested: dict[str, set[str]]                        # class -> its nested types
    member_uses: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)  # member -> [(target, top, kind)]
    member_users: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # member -> [(source id, source top)]

    @classmethod
    def build(cls, rf: RepoFacts) -> "MemberIndex":
        d = rf.model.deps
        top = rf.top
        rows_from, rows_to = defaultdict(list), defaultdict(list)
        uses, users = defaultdict(list), defaultdict(list)
        for src, tgt, st, tt, kind in zip(d["source"], d["target"], d["source_type"], d["target_type"], d["kind"]):
            a, b = top.get(st), top.get(tt)
            if a not in rf.prod or b not in rf.prod:
                continue
            rows_from[a].append((src, tgt, b, kind))
            rows_to[b].append((src, a, tgt, kind))
            if "#" in src:
                uses[src].append((tgt, b, kind))
            if "#" in tgt:
                users[tgt].append((src, a))
        statics: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for mid, info in rf.member_info.items():
            owner = info["owner"]
            if owner in rf.prod and info["static"] and info["kind"] in ("method", "field"):
                statics[owner][mid.split("#", 1)[1].split("(")[0]].append(mid)
        t = rf.model.types
        nested = defaultdict(set)
        for ty, tl in zip(t["type"], t["top_level"]):
            if ty != tl:
                nested[tl].add(ty)
        return cls(dict(rows_from), dict(rows_to), {k: dict(v) for k, v in statics.items()}, dict(nested),
                   dict(uses), dict(users))


def index(rf: RepoFacts) -> MemberIndex:
    if "_members_index" not in rf.__dict__:
        rf.__dict__["_members_index"] = MemberIndex.build(rf)
    return rf.__dict__["_members_index"]


def _owner(mid: str) -> str:
    return mid.split("#", 1)[0]


# ------------------------------------------------------------------------ edges after member moves

def _exact_edges_for(rf: RepoFacts, classes: set[str], moves: dict[str, str]) -> set[tuple[str, str]]:
    """All dependencies of ``classes`` after the member moves, from the member rows.

    An import counts only where the dependency was import-only before (an
    unused import stays unused); other imports go with the uses they serve:
    the rewriter removes imports that are no longer used."""
    ix = index(rf)
    out = set()

    def add(a, b, kind):
        if a == b:
            return
        if kind == "import":
            e = rf.edges.get((a, b))
            if e is None or not e.import_only:
                return
        out.add((a, b))

    def target(src: str, tgt: str, b: str, kind: str) -> str:
        """New owner of a dependency target; a type reference made only to reach static members that all
        move (``Beta.twice()``) goes to their new owner."""
        if tgt in moves:
            return moves[tgt]
        if kind == "type_ref" and "#" in src and "#" not in tgt:
            via = [t for t, top, k in ix.member_uses.get(src, []) if top == b and "#" in t]
            if via and all(t in moves for t in via) and len({moves[t] for t in via}) == 1:
                return moves[via[0]]
        return b

    for c in classes:
        for src, tgt, b, kind in ix.rows_from.get(c, []):
            add(moves.get(src, c), target(src, tgt, b, kind), kind)
        for src, a, tgt, kind in ix.rows_to.get(c, []):
            add(moves.get(src, a), target(src, tgt, c, kind), kind)
    return out


def apply_member_moves(rf: RepoFacts, edges: set[tuple[str, str]], moves: dict[str, str]) -> set[tuple[str, str]]:
    """Exact class dependencies after member moves (``member id -> new top-level owner``).

    Every dependency of an old owner, a new owner or a class that uses a moved
    member is recomputed from the member-level rows; the others are unchanged."""
    if not moves:
        return set(edges)
    ix = index(rf)
    touched = {rf.top.get(_owner(m), _owner(m)) for m in moves} | set(moves.values())
    for m in moves:
        touched |= {a for _, a in ix.member_users.get(m, [])}
    out = {e for e in edges if e[0] not in touched and e[1] not in touched}
    return out | _exact_edges_for(rf, touched, moves)


# ------------------------------------------------------------------------ candidate member moves

def movable_group(rf: RepoFacts, owner: str, members: set[str]) -> set[str] | None:
    """Close ``members`` over the static members of ``owner`` they use; ``None`` if they cannot move.

    A member can move if it is a static method or field of a production class
    (not an interface or enum), is not an override, not protected by the
    configuration, does not use nested types or instance members of its
    class, and its name is not overloaded with members that stay."""
    ix = index(rf)
    if rf.kind.get(owner) != "class" or rf.protected.get(owner):
        return None
    frozen_owner = bool(rf.frozen.get(owner))
    names = ix.statics.get(owner, {})
    todo, group = list(members), set()
    while todo:
        m = todo.pop()
        if m in group:
            continue
        info = rf.member_info.get(m)
        if info is None or not info["static"] or info["kind"] not in ("method", "field") or info["override"] \
                or info["protected"]:
            return None
        if frozen_owner and info["visibility"] in ("public", "protected"):
            return None                                          # part of a widely used public API
        name = m.split("#", 1)[1].split("(")[0]
        same = names.get(name, [])
        group.add(m)
        todo += [x for x in same if x not in group]              # overloads move together
        for tgt, b, _k in ix.member_uses.get(m, []):
            if b != owner:
                continue
            if tgt in ix.nested.get(owner, set()) or _owner(tgt) in ix.nested.get(owner, set()):
                return None                                      # uses a nested type of its class
            if "#" not in tgt:
                continue                                         # the class itself (e.g. a static call on it)
            ti = rf.member_info.get(tgt)
            if ti is None:
                continue
            if not ti["static"]:
                return None                                      # uses instance members
            if ti["visibility"] == "private" or tgt.split("#", 1)[1].startswith("<"):
                todo.append(tgt)                                 # private helpers come along
    return group if len(group) <= 12 else None


def _params(mid: str) -> list[str]:
    """Parameter types of a member id ``a.B#m(int,C)`` (simple names, generics removed)."""
    m = re.search(r"\((.*)\)$", mid)
    if not m or not m.group(1).strip():
        return []
    depth, cur, out = 0, "", []
    for ch in m.group(1):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        if depth == 0 and ch not in "<>":
            cur += ch
    out.append(cur)
    return [x.strip().rsplit(".", 1)[-1].replace("...", "[]") for x in out]


def movable_instance(rf: RepoFacts, owner: str, m: str, dest: str) -> int | None:
    """Index of the ``dest`` parameter if instance method ``m`` of ``owner`` can move to ``dest``.

    Conditions (Move Method, feature envy): a plain instance method of a class
    (no constructor, override, overridden method, abstract or protected API),
    exactly one parameter of type ``dest``, no use of instance members or
    nested types of ``owner``, not called inside ``owner`` and never used as a
    method reference; ``dest`` is a production class without a member of that name."""
    info = rf.member_info.get(m)
    if info is None or info["kind"] != "method" or info["static"] or info["override"] or info["protected"]:
        return None
    if rf.kind.get(owner) != "class" or rf.kind.get(dest) != "class" or dest not in rf.prod or rf.fixed(dest) \
            or rf.protected.get(owner):
        return None
    if rf.frozen.get(owner) and info["visibility"] in ("public", "protected"):
        return None
    name = m.split("#", 1)[1].split("(")[0]
    if name.startswith("<") or rf._is_overridden(m):
        return None
    params = _params(m)
    d_simple = dest.rsplit(".", 1)[-1]
    hits = [i for i, p in enumerate(params) if p == d_simple]
    if len(hits) != 1:
        return None
    ix = index(rf)
    for tgt, top, kind in ix.member_uses.get(m, []):
        if top != owner:
            continue
        if "#" not in tgt:
            if tgt != owner:
                return None                      # a nested type of the owner
            continue
        ti = rf.member_info.get(tgt)
        if ti is None or not ti["static"]:
            return None                          # instance state of the owner
    for src, a in ix.member_users.get(m, []):
        if a == owner:
            return None                          # called inside its class: the dependency would stay
    d = rf.model.deps
    if ((d["target"] == m) & (d["kind"] == "method_ref")).any():
        return None
    dest_names = {mid.split("#", 1)[1].split("(")[0] for mid, i in rf.member_info.items() if i["owner"] == dest}
    if name in dest_names:
        return None
    return hits[0]


@dataclass
class Candidate:
    key: str
    ops: list[dict]
    cost: float
    removes: set[tuple[str, str]]
    adds: set[tuple[str, str]]
    members: set[str] = field(default_factory=set)
    label: str = ""


def _move_candidate(rf: RepoFacts, owner: str, dest: str, members: set[str], edges: set[tuple[str, str]],
                    why: str) -> Candidate | None:
    grp = movable_group(rf, owner, members)
    if not grp or rf.kind.get(dest) != "class" or rf.protected.get(dest):
        return None
    ix = index(rf)
    # no member of the destination may have the name of a moved member (calls would resolve to it)
    moved_names = {m.split("#", 1)[1].split("(")[0] for m in grp}
    dest_names = {mid.split("#", 1)[1].split("(")[0] for mid, info in rf.member_info.items() if info["owner"] == dest}
    if moved_names & dest_names:
        return None
    # callers in other packages must be able to see the destination class
    if str(rf.visibility.get(dest, "public")) != "public":
        users = {a for m in grp for _, a in ix.member_users.get(m, [])} | {owner}
        if any(rf.package.get(u) != rf.package.get(dest) for u in users):
            return None
    after = apply_member_moves(rf, edges, {m: dest for m in grp})
    removes, adds = edges - after, after - edges
    if not removes:
        return None
    ix = index(rf)
    callers = {a for m in grp for _, a in ix.member_users.get(m, []) if a not in (owner, dest)}
    api = any(rf.member_info[m]["api"] for m in grp)
    cost = round(1.0 + 0.5 * len(grp) + 0.2 * len(callers) + (5.0 if api else 0.0), 3)
    op = {"op": "move_member", "member": sorted(grp)[0], "members": sorted(grp), "from_class": owner,
          "to_class": dest, "callers": sorted(callers), "reason": why, "cost": cost}
    return Candidate(f"move:{owner}>{dest}:{','.join(sorted(grp))}", [op], cost, removes, adds, set(grp),
                     f"move {len(grp)} static members of {owner} to {dest}")


def candidates(rf: RepoFacts, focus: list[tuple[str, str]], edges: set[tuple[str, str]],
               max_candidates: int = 400) -> list[Candidate]:
    """Operations that remove at least one of the ``focus`` dependencies."""
    out: dict[str, Candidate] = {}
    for (s, t) in focus:
        e = rf.edges.get((s, t))
        if e is None:
            continue
        if e.import_only:
            op = {"op": "remove_unused_import", "class": s, "import": t}
            out.setdefault(f"import:{s}>{t}", Candidate(f"import:{s}>{t}", [op], IMPORT_COST, {(s, t)}, set(),
                                                        label=f"remove unused import of {t} in {s}"))
            continue
        dset = rf.deletion_set(e)
        if dset and all(m in rf.deletable for m in dset):
            ops = [{"op": "delete_member", "member": m, "owner": _owner(m)} for m in sorted(dset)]
            out.setdefault(f"delete:{s}>{t}", Candidate(f"delete:{s}>{t}", ops,
                                                        sum(deletion_cost(rf.member_info[m]) for m in dset),
                                                        {(s, t)}, set(), set(dset), f"delete deprecated members of {s}"))
        methods = rf.invertible(e)
        if methods:
            op = {"op": "extract_interface", "client": s, "provider": t, "methods": methods,
                  "interface_package": rf.package[s]}
            from .simulate import interface_name
            iface = interface_name(op)
            adds = {(s, iface), (t, iface)} | {(iface, x) for x in rf.signature_types(methods) if x != t}
            out.setdefault(f"invert:{s}>{t}", Candidate(f"invert:{s}>{t}", [op], inversion_cost(methods),
                                                        {(s, t)}, adds, label=f"invert {s} -> {t}"))
        used = {m for m in e.target_members if "#" in m}
        if used and e.kinds <= MOVABLE_KINDS | {"type_ref"}:
            # the exact effect (computed from the member rows) decides whether the dependency disappears
            c = _move_candidate(rf, t, s, used, edges, f"{s} only uses these static members of {t}")
            if c:
                out.setdefault(c.key, c)
        srcs = {m for m in e.source_members if "#" in m}
        ix = index(rf)
        if srcs and not any("#" not in m and m != s for m in e.source_members if m not in srcs):
            c = _instance_candidate(rf, s, t, srcs, edges)
            if c:
                out.setdefault(c.key, c)
        type_level = any("#" not in src and kind != "import"
                         for src, _, b, kind in ix.rows_from.get(s, []) if b == t)
        if srcs and not type_level:
            c = _move_candidate(rf, s, t, srcs, edges, f"these static members of {s} are about {t}")
            if c:
                out.setdefault(c.key, c)
        if len(out) >= max_candidates:
            break
    return list(out.values())


def _instance_candidate(rf: RepoFacts, owner: str, dest: str, members: set[str],
                        edges: set[tuple[str, str]]) -> Candidate | None:
    """Move the instance methods ``members`` of ``owner`` (all its uses of ``dest``) into ``dest``."""
    idx = {}
    for m in members:
        info = rf.member_info.get(m)
        if info is None or info["static"]:
            return None
        k = movable_instance(rf, owner, m, dest)
        if k is None:
            return None
        idx[m] = k
    after = apply_member_moves(rf, edges, {m: dest for m in members})
    removes, adds = edges - after, after - edges
    if not removes:
        return None
    ix = index(rf)
    callers = {a for m in members for _, a in ix.member_users.get(m, []) if a not in (owner, dest)}
    api = any(rf.member_info[m]["api"] for m in members)
    cost = round(1.5 + 0.5 * len(members) + 0.3 * len(callers) + (5.0 if api else 0.0), 3)
    op = {"op": "move_member", "instance": True, "member": sorted(members)[0], "members": sorted(members),
          "param_index": {m: idx[m] for m in sorted(members)}, "from_class": owner, "to_class": dest,
          "callers": sorted(callers), "cost": cost,
          "reason": f"{', '.join(m.split('#', 1)[1].split('(')[0] for m in sorted(members))} work on a "
                    f"{dest.rsplit('.', 1)[-1]} and use no state of {owner.rsplit('.', 1)[-1]}"}
    return Candidate(f"method:{owner}>{dest}:{','.join(sorted(members))}", [op], cost, removes, adds, set(members),
                     f"move {len(members)} instance methods of {owner} to {dest}")


# ------------------------------------------------------------------------ plan model

def _plans_for(rf: RepoFacts, gid: str, nodes: list[str], cands: list[Candidate], edges: set[tuple[str, str]],
               measure, time_limit: float, backend: str) -> list[Plan]:
    """Generic model: choose candidates; ``measure(m, p_vars, back)`` adds the smell rule."""
    node_set = set(nodes)

    def build():
        m = slv.LinearModel()
        cost, back = {}, {}
        y = {}
        for c in cands:
            v = m.binary(f"y|{c.key}")
            y[c.key] = v
            cost[v] = c.cost
        by_member = defaultdict(list)
        for c in cands:
            for mem in c.members:
                by_member[mem].append(y[c.key])
        for vs in by_member.values():
            if len(vs) > 1:
                m.add({v: 1 for v in vs}, ub=1)
        # dependencies among the nodes (present after the plan)
        relevant = {e for e in edges if e[0] in node_set and e[1] in node_set}
        added = defaultdict(list)
        for c in cands:
            for e in c.adds:
                if e[0] in node_set and e[1] in node_set or e[0] not in rf.prod or e[1] not in rf.prod:
                    added[e].append(y[c.key])
        removed_by = defaultdict(list)
        for c in cands:
            for e in c.removes:
                removed_by[e].append(y[c.key])
        p = {}
        for e in relevant | set(added):
            v = m.binary(f"p|{e[0]}|{e[1]}")
            p[e] = v
            if e in relevant:
                m.add({v: 1, **{yv: 1 for yv in removed_by.get(e, [])}}, lb=1)      # stays unless removed
            for yv in added.get(e, []):
                m.add({v: 1, yv: -1}, lb=0)                                      # created by the operation
        measure(m, p, back, cost)
        meta = {"moves": y}
        return m, cost, back, meta

    def ops_of(sol, meta):
        ops = []
        for c in cands:
            if sol.value(meta["moves"][c.key]) > 0.5:
                ops += c.ops
        return ops

    from .restructure import _solve_variants
    return _solve_variants(build, gid, time_limit, backend, ops_of)


def class_cycle_plans(rf: RepoFacts, gid: str, core: list[str], time_limit: float = 60.0,
                      backend: str = "auto") -> tuple[list[Plan], dict]:
    import networkx as nx
    core_top = sorted({rf.top.get(c, c) for c in core} & rf.prod)
    edges = set(rf.edges)
    g = nx.DiGraph()
    g.add_edges_from(edges)
    comps = [c for c in nx.strongly_connected_components(g) if len(c & set(core_top)) >= 2]
    info = {"classes": len(core_top)}
    if not comps:
        return [], {**info, "skipped": "the classes are not on a cycle in the production code at this commit "
                                       "(the detector saw nested classes, tests or another version)"}
    scc = set().union(*comps)
    focus = [e for e in edges if e[0] in scc and e[1] in scc]
    cands = candidates(rf, focus, edges)
    info.update({"cycle_classes": len(scc), "dependencies": len(focus), "candidates": len(cands),
                 "by_kind": dict(_kinds(cands))})
    if not cands:
        return [], {**info, "skipped": "no dependency on the cycle can be removed by the operations DSARP "
                                       "verifies (imports, dead code, interfaces, static members)"}
    nodes = sorted(scc | {e[1] for c in cands for e in c.adds if e[0] in scc} |
                   {e[0] for c in cands for e in c.adds if e[1] in scc})

    def measure(m, p, back, cost):
        big = len(nodes) + 1
        for n in nodes:
            m.var(f"o|{n}", 0, big)
        for (a, b), v in p.items():
            if a in scc or b in scc:
                if f"o|{a}" not in m.by_name or f"o|{b}" not in m.by_name:
                    continue
                k = m.binary(f"k|{a}|{b}")
                back[k] = 1.0
                m.add({f"o|{a}": 1, f"o|{b}": -1, v: -big, k: big}, lb=1 - big)

    return _plans_for(rf, gid, nodes, cands, edges, measure, time_limit, backend), info


CLASS_HUB = det.ARCAN_LIKE
"""Arcan's rule for hub-like classes: fan-in and fan-out above the median, and balanced."""


def class_hub_plans(rf: RepoFacts, gid: str, hub: str, time_limit: float = 60.0,
                    backend: str = "auto", th: det.Thresholds = CLASS_HUB) -> tuple[list[Plan], dict]:
    import networkx as nx
    from .extra import extra
    hub = rf.top.get(hub, hub)
    edges = set(rf.edges)
    test_edges = extra(rf).test_edges
    g = nx.DiGraph()
    g.add_nodes_from(rf.prod)
    g.add_edges_from(edges)
    g.add_edges_from(test_edges)              # Arcan counts test classes as clients
    if hub not in g:
        return [], {"skipped": "class not in the production code at this commit"}
    h = det.hubs(g, th)
    r = h.loc[hub]
    info = {"fan_in": int(r["fan_in"]), "fan_out": int(r["fan_out"]), "max_in": int(r["in_threshold"]),
            "max_out": int(r["out_threshold"])}
    if not bool(r["hub"]):
        return [], {**info, "skipped": "not a hub-like class by Arcan's rule (fan-in and fan-out above the median "
                                       "and balanced, test classes counted as clients) at this commit"}
    focus = [e for e in edges if hub in e]
    cands = candidates(rf, focus, edges)
    info["candidates"] = len(cands)
    info["by_kind"] = dict(_kinds(cands))
    if not cands:
        return [], {**info, "skipped": "no dependency of the class can be removed by the operations DSARP "
                                       "verifies (imports, dead code, interfaces, static members)"}
    nodes = sorted({hub} | {x for e in focus for x in e})
    can_in = len({e for c in cands for e in c.removes if e[1] == hub})
    can_out = len({e for c in cands for e in c.removes if e[0] == hub})
    tot = info["fan_in"] + info["fan_out"]
    info["why_none"] = (
        f"by Arcan's rule the class stops being a hub if fan-in drops from {info['fan_in']} to {info['max_in']}, "
        f"fan-out from {info['fan_out']} to {info['max_out']}, or the two differ by at least "
        f"{th.hub_balance:.0%} of {tot}; the verifiable operations can remove at most {can_in} incoming and "
        f"{can_out} outgoing dependencies, which is not enough (the rest are uses of its type: fields, "
        "parameters, subclassing, construction)")

    def measure(m, p, back, cost):
        ins = [v for (a, b), v in p.items() if b == hub]
        outs = [v for (a, b), v in p.items() if a == hub]
        fixed_in = sum(1 for (a, b) in edges if b == hub and (a, b) not in p) + \
            sum(1 for (a, b) in test_edges if b == hub)
        fixed_out = sum(1 for (a, b) in edges if a == hub and (a, b) not in p)
        # the hub disappears if fan-in or fan-out drops to the threshold, or if the two become unbalanced
        # (|in - out| >= balance * (in + out)); z picks the way, t is how far the plan stays from it
        big = 4 * (len(ins) + len(outs) + fixed_in + fixed_out + 1)
        t = m.var("hub_excess", 0, big)
        back[t] = 1.0
        z = [m.binary(f"hub_way|{k}") for k in range(4)]
        m.add({v: 1 for v in z}, lb=1, ub=1)
        bal = th.hub_balance
        IN = {v: 1.0 for v in ins}
        OUT = {v: 1.0 for v in outs}
        # 0: fan-out <= max_out
        m.add({**OUT, t: -1.0, z[0]: big}, ub=info["max_out"] - fixed_out + big)
        # 1: fan-in <= max_in
        m.add({**IN, t: -1.0, z[1]: big}, ub=info["max_in"] - fixed_in + big)
        # 2: in - out >= bal * (in + out)   <=>  (1 - bal) in - (1 + bal) out >= 0
        terms = {**{v: (1 - bal) for v in ins}, **{v: -(1 + bal) for v in outs}, t: 1.0, z[2]: -big}
        m.add(terms, lb=-(1 - bal) * fixed_in + (1 + bal) * fixed_out - big + 1e-3)
        # 3: out - in >= bal * (in + out)
        terms = {**{v: (1 + bal) * -1 for v in ins}, **{v: (1 - bal) for v in outs}, t: 1.0, z[3]: -big}
        m.add(terms, lb=(1 + bal) * fixed_in - (1 - bal) * fixed_out - big + 1e-3)

    return _plans_for(rf, gid, nodes, cands, edges, measure, time_limit, backend), info


def _kinds(cands: list[Candidate]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for c in cands:
        out[c.ops[0]["op"]] += 1
    return out
