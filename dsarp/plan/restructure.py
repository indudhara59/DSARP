"""Plans that restructure packages: split a god component or a package with
several concerns (feature concentration), gather a scattered concern.

**Split a god component** (more than 30 classes). Every class of the package
is assigned to the package itself or to one of ``K`` new sub-packages::

    x[c,j] = 1   class c ends up in part j        (one part per class)
    size(j) <= 30                                 (new parts; the old one may keep a remainder)
    E[j,k] >= x[s,j] + x[t,k] - 1                 (a class dependency s -> t between parts)
    o[j] >= o[k] + 1 if E[j,k]                    (the parts form no cycle)
    minimise  overflow * BIG + moves + 0.3 * cut dependencies + new package dependencies

**Split a package with several concerns** (feature concentration, Designite's
LCC > 0.2). The package's classes are grouped into *units*: sets of related
classes (association, aggregation, composition, inheritance), together with
their nested classes and their tests. Units are never broken up. Each unit
stays, moves to an existing package it is related to (learned affinity), or
moves to a new sub-package; for every package that keeps or receives units::

    5 * sets - classes <= 0                       (LCC <= 0.2)
    or no worse than today for a receiving package that already has the smell

plus the same no-cycle and size rules as above.

**Gather a scattered concern** (scattered functionality): the concern
classes that the reporting packages use together are moved into one of the
concern packages; every target is tried (exhaustive search over the few
options), the simulation decides.

New sub-packages get a name from the most frequent word in their class names
(``org.apache.tika.parser.pdf.image``); the name is checked against the
existing packages.
"""

from __future__ import annotations

import keyword
import math
import time
from collections import Counter, defaultdict

import networkx as nx

from . import solver as slv
from .extra import FC_LCC, GOD_CLASSES, ExtraFacts, extra, scattered_methods
from .facts import EDGE_COST, NEW_EDGE_COST, RepoFacts, current_package_pairs, move_cost, tokens
from .generator import Plan

GENERIC = {"impl", "util", "utils", "abstract", "base", "default", "helper", "helpers", "test", "tests", "java",
           "main", "core", "common", "internal", "support", "the", "and", "for"}


# ------------------------------------------------------------------------ helpers

def split_cost(rf: RepoFacts, c: str) -> float:
    """Cost of moving ``c`` into a new sub-package of its own package (no affinity: cohesion decides)."""
    users = rf.users.get(c, len(rf.dependents(c)))
    cost = 1.0 + 0.2 * users + 2.0 * rf.wiring.get(c, 0)
    if rf.api.get(c):
        cost += 5.0
    return round(cost, 3)


def _weights(rf: RepoFacts) -> dict[str, int]:
    if getattr(rf, "_nested", None) is None:
        t = rf.model.types
        t = t[~t["is_test"].astype(bool) & ~t["is_generated"].astype(bool)]
        rf._nested = t["top_level"].value_counts().to_dict()
    return rf._nested


def _all_packages(rf: RepoFacts) -> set[str]:
    return set(rf.model.types["package"].dropna())


def name_part(rf: RepoFacts, parent: str, classes: list[str], taken: set[str], index: int) -> str:
    """A sub-package name for a part: its most frequent class-name word that is not in the parent's name."""
    parent_words = set().union(*(tokens(seg) | {seg.lower()} for seg in parent.split("."))) | GENERIC
    words: Counter = Counter()
    for c in classes:
        for w in tokens(c.rsplit(".", 1)[-1]):
            if w not in parent_words and w.isalpha():
                words[w] += 1
    existing = _all_packages(rf) | taken
    for w, _ in words.most_common():
        name = w.lower()
        if keyword.iskeyword(name) or not name.isidentifier():
            continue
        if f"{parent}.{name}" not in existing:
            return f"{parent}.{name}"
    k = index
    while f"{parent}.part{k}" in existing:
        k += 1
    return f"{parent}.part{k}"


def _plan(gid: str, name: str, sol: slv.Solution | None, cost: float, remaining: float, size: dict,
          ops: list[dict], seconds: float, status: str | None = None) -> Plan:
    return Plan(gid, name, status or (sol.status if sol else "heuristic"), sol.objective if sol else None,
                round(cost, 3), round(remaining, 3), sol.gap if sol else 0.0, round(seconds, 2),
                sol.backend if sol else "enumeration", size, ops)


def _move(rf: RepoFacts, xf: ExtraFacts, c: str, to: str, aff: float | None, new: bool, reason: str) -> dict:
    op = {"op": "move_class", "class": c, "from_package": rf.package[c], "to_package": to,
          "affinity": aff, "module": rf.module.get(c, ""), "reason": reason}
    if new:
        op["new_package"] = True
    tests = [t for t in xf.tests_of(c) if xf.type_pkg.get(t) == rf.package[c]]
    if tests:
        op["tests"] = tests
    return op


NEW_CYCLE_COST = 20.0


def _order_constraints(m: slv.LinearModel, nodes: list[str], cost: dict, now: set[tuple[str, str]] | None = None,
                       prefix: str = "o") -> None:
    """No new cycle among ``nodes``: every dependency ``E[a,b]`` points down a topological order.

    A dependency that exists today (``now``) may break the order (``k[a,b]``);
    that is free if the reverse dependency exists today as well (the cycle is
    old), and costs ``NEW_CYCLE_COST`` otherwise. New dependencies must follow
    the order: a plan cannot close a new cycle among these packages."""
    now = now or set()
    big = len(nodes) + 1
    for n in nodes:
        m.var(f"{prefix}|{n}", 0, big)
    node_set = set(nodes)
    for v in [v for v in m.by_name if v.startswith("E|")]:
        _, a, b = v.split("|")
        if a not in node_set or b not in node_set:
            continue
        terms = {f"{prefix}|{a}": 1, f"{prefix}|{b}": -1, v: -big}       # E = 1 -> o[a] >= o[b] + 1
        if (a, b) in now:
            k = m.binary(f"k|{a}|{b}")
            cost[k] = 0.0 if (b, a) in now else NEW_CYCLE_COST
            terms[k] = big
        m.add(terms, lb=1 - big)


def _solve_variants(build, gid: str, time_limit: float, backend: str, ops_of, names=("most_removed",)) -> list[Plan]:
    """most_removed (least remaining smell, then least change), least_change (smell fully removed),
    alternative (another set of changes with the same remaining smell), budget_50."""
    out: list[Plan] = []
    t0 = time.time()

    def run(name, full=False, forbid=None, max_remaining=None, budget=None):
        m, cost, back, meta = build()
        if full:
            if back:
                m.add({b: 1 for b in back}, ub=0, name="smell fully removed")
            m.minimize(cost)
        else:
            big = 1.0 + sum(abs(c) for c in cost.values())
            obj = {b: w * big for b, w in back.items()}
            for v, c in cost.items():
                obj[v] = obj.get(v, 0) + c
            m.minimize(obj)
            if max_remaining is not None and back:
                m.add(back, ub=max_remaining + 1e-6, name="remaining limit")
            if budget is not None:
                changes = {v: c for v, c in cost.items() if not v.startswith("E|")}
                if changes:
                    m.add(changes, ub=budget, name="budget")
        for chosen in forbid or []:
            if chosen:
                m.add({v: 1 for v in chosen if m.has(v)}, ub=len(chosen) - 1, name="diversity cut")
        s0 = time.time()
        sol = slv.solve(m, backend=backend, time_limit=max(time_limit, 1.0))
        if not sol.ok:
            return _plan(gid, name, sol, 0.0, 0.0, {**m.size, "message": sol.message}, [], time.time() - s0), []
        chosen = [v for v in meta["moves"] if sol.value(v) > 0.5]
        change = sum(c for v, c in cost.items() if sol.value(v) > 0.5 and not v.startswith("E|"))
        remaining = sum(w * round(sol.value(b)) for b, w in back.items())
        ops = ops_of(sol, meta)
        return _plan(gid, name, sol, change, remaining, {**m.size, "chosen": chosen}, ops, time.time() - s0), chosen

    best, chosen = run("most_removed")
    out.append(best)
    if best.status not in ("optimal", "feasible") or not best.operations:
        return out
    ref, ref_chosen = best, chosen
    if best.remaining == 0:
        full, fc = run("least_change", full=True)
        if full.status in ("optimal", "feasible") and full.operations and _key(full) != _key(best):
            out.append(full)
            ref, ref_chosen = full, fc
    alt, _ = run("alternative", forbid=[ref_chosen], max_remaining=ref.remaining)
    if alt.status in ("optimal", "feasible") and alt.operations and all(_key(alt) != _key(p) for p in out):
        out.append(alt)
    if ref.change_cost > 0:
        bud, _ = run("budget_50", budget=ref.change_cost * 0.5)
        if bud.status in ("optimal", "feasible") and bud.operations and all(_key(bud) != _key(p) for p in out):
            out.append(bud)
    for p in out:
        p.seconds = round(p.seconds, 2)
    out[0].size["total_seconds"] = round(time.time() - t0, 2)
    return out


def _key(p: Plan) -> list:
    return sorted((o.get("class", ""), o.get("to_package", "")) for o in p.operations)


# ------------------------------------------------------------------------ god component

def split_god(rf: RepoFacts, gid: str, pkg: str, time_limit: float = 60.0, backend: str = "auto",
              max_parts: int = 4) -> tuple[list[Plan], dict]:
    """Split a package with more than 30 classes (see :func:`split_units`)."""
    w = _weights(rf)
    size = sum(w.get(c, 1) for c in rf.pkg_classes.get(pkg, ()))
    if size <= GOD_CLASSES:
        return [], {"classes": size, "limit": GOD_CLASSES,
                    "skipped": f"{pkg} has {size} production classes (limit {GOD_CLASSES}) at this commit; "
                               "the detector also counted test or generated classes"}
    k = min(max_parts, max(1, math.ceil(size / GOD_CLASSES)))
    strict, info = split_units(rf, gid, pkg, "god", time_limit, backend, max_new=k)
    relaxed, _ = split_units(rf, gid, pkg, "god", time_limit, backend, max_new=k, strict=False)
    seen = [_key(p) for p in strict]
    for p in relaxed:
        if p.operations and _key(p) not in seen:
            p.name = "relaxed_" + p.name
            strict.append(p)
            seen.append(_key(p))
    info["variants"] = "strict: new sub-packages must be cohesive (LCC <= 0.2); relaxed: they may not be"
    return strict, info


# ------------------------------------------------------------------------ feature concentration

def fc_units(rf: RepoFacts, pkg: str) -> tuple[list[dict], dict]:
    """Units of a package: related classes with their nested classes and tests (never split)."""
    xf = extra(rf)
    types = [t for t, p in xf.type_pkg.items() if p == pkg and t not in xf.generated]
    ts = set(types)
    rel = nx.Graph()
    rel.add_nodes_from(types)
    for pr in xf.rel_pairs:
        a, b = tuple(pr)
        if a in ts and b in ts:
            rel.add_edge(a, b)
    unit = nx.Graph(rel)
    for t in types:
        top = xf.top.get(t, t)
        if top in ts and top != t:
            unit.add_edge(t, top)
    for c in [t for t in types if t in rf.prod]:
        for tst in xf.tests_of(c):
            if tst in ts:
                unit.add_edge(c, tst)
    comp_of = {}
    for i, comp in enumerate(nx.connected_components(rel)):
        for t in comp:
            comp_of[t] = i
    units = []
    for comp in nx.connected_components(unit):
        comp = sorted(comp)
        prod = sorted(t for t in comp if t in rf.prod)
        tests = sorted(t for t in comp if t in xf.tests and xf.top.get(t) == t)
        units.append({"types": comp, "n": len(comp), "sets": len({comp_of[t] for t in comp}), "classes": prod,
                      "tests": tests, "fixed": (not prod) or any(rf.protected.get(c) for c in prod)})
    k = nx.number_connected_components(rel)
    return units, {"classes": len(types), "sets": k, "lcc": round(k / len(types), 4) if types else 0.0}


def split_fc(rf: RepoFacts, gid: str, pkg: str, time_limit: float = 60.0, backend: str = "auto",
             max_new: int = 4) -> tuple[list[Plan], dict]:
    """Split a package with unrelated sets of classes (see :func:`split_units`)."""
    return split_units(rf, gid, pkg, "fc", time_limit, backend, max_new=max_new)


def split_units(rf: RepoFacts, gid: str, pkg: str, mode: str, time_limit: float = 60.0, backend: str = "auto",
                max_new: int = 4, targets_per_unit: int = 3, strict: bool = True) -> tuple[list[Plan], dict]:
    """Move whole units (related classes with their nested classes and tests) out of ``pkg``.

    ``mode="fc"``: the package's LCC must drop to 0.2 (remaining smell: how far it is above).
    ``mode="god"``: the package must drop to 30 classes (remaining: classes above); its LCC may not get worse.
    In both modes every receiving package keeps LCC <= 0.2 (or no worse than today) and at most 30
    classes, new sub-packages are cohesive (LCC <= 0.2) and no new package cycle is closed.
    With ``strict=False`` a new sub-package may be less cohesive (each set above the limit costs 2):
    the split then appears as a plan with a feature-concentration side effect, and the score decides."""
    xf = extra(rf)
    units, info = fc_units(rf, pkg)
    w = _weights(rf)
    for u in units:
        u["size"] = sum(w.get(c, 1) for c in u["classes"])
    info["size"] = sum(u["size"] for u in units)
    if mode == "fc" and (not units or info["lcc"] <= FC_LCC):
        return [], {**info, "skipped": f"LCC of {pkg} is {info['lcc']} at this commit (limit {FC_LCC}): "
                                      "its classes are related enough"}
    if getattr(rf, "_pairs", None) is None:
        rf._pairs = current_package_pairs(rf)
    unit_of = {c: i for i, u in enumerate(units) for c in u["classes"]}
    # candidate existing packages per unit (same module, visible, no name clash), by learned affinity
    all_types_by_pkg: dict[str, set[str]] = defaultdict(set)
    for t, p in xf.type_pkg.items():
        if t not in xf.generated:
            all_types_by_pkg[p].add(t)
    simple_in: dict[str, set[str]] = {p: {t.rsplit(".", 1)[-1] for t in ts if xf.top.get(t) == t}
                                      for p, ts in all_types_by_pkg.items()}
    opts: dict[int, list[tuple[str, float]]] = {}
    for i, u in enumerate(units):
        if u["fixed"]:
            continue
        mods = {rf.module.get(c) for c in u["classes"]}
        cands = set()
        for c in u["classes"]:
            for k in rf.out_edges.get(c, []) + rf.in_edges.get(c, []):
                other = k[1] if k[0] == c else k[0]
                q = rf.package.get(other)
                if q and q != pkg:
                    cands.add(q)
        scored = []
        for q in cands:
            if not mods <= rf.pkg_modules.get(q, set()):
                continue
            if any(t.rsplit(".", 1)[-1] in simple_in.get(q, set()) for t in u["classes"] + u["tests"]):
                continue
            if not all(rf.visible_after_move(c, q) for c in u["classes"]):
                continue
            aff = sum(rf.affinity(c, q) for c in u["classes"]) / len(u["classes"])
            scored.append((q, aff))
        opts[i] = sorted(scored, key=lambda x: (-x[1], x[0]))[:targets_per_unit]
    stand_alone = [i for i, u in enumerate(units) if not u["fixed"] and 5 * u["sets"] <= u["n"]]
    n_new = max_new if not strict else min(max_new, max(1, len(stand_alone)))
    new_parts = [f"#{j}" for j in range(1, n_new + 1)]
    targets = sorted({q for o in opts.values() for q, _ in o})
    # related-type links between a unit and a candidate package (moving there merges sets)
    links = defaultdict(set)
    for pr in xf.rel_pairs:
        a, b = tuple(pr)
        for x, y in ((a, b), (b, a)):
            if xf.type_pkg.get(x) == pkg and xf.type_pkg.get(y) in targets:
                for i, u in enumerate(units):
                    if x in u["types"]:
                        links[i].add(xf.type_pkg[y])
    # current state of the candidate packages
    q_state = {}
    for q in targets:
        ts = all_types_by_pkg.get(q, set())
        g = nx.Graph()
        g.add_nodes_from(ts)
        for pr in xf.rel_pairs:
            a, b = tuple(pr)
            if a in ts and b in ts:
                g.add_edge(a, b)
        q_state[q] = {"n": len(ts), "sets": nx.number_connected_components(g) if ts else 0,
                      "size": sum(w.get(c, 1) for c in rf.pkg_classes.get(q, ()))}
    # dependencies between units (production classes) and with the classes that never move
    unit_deps = Counter()
    for (s, t), e in rf.edges.items():
        if rf.package.get(s) == pkg and rf.package.get(t) == pkg:
            a, b = unit_of.get(s), unit_of.get(t)
            if a is not None and b is not None and a != b:
                unit_deps[(a, b)] += 1
    ext_deps = Counter()       # unit <-> other packages (for cycles through the targets)
    for (s, t) in rf.edges:
        ps, pt = rf.package.get(s), rf.package.get(t)
        if ps == pkg and pt in targets and s in unit_of:
            ext_deps[(unit_of[s], "out", pt)] += 1
        if pt == pkg and ps in targets and t in unit_of:
            ext_deps[(unit_of[t], "in", ps)] += 1
    info.update({"units": len(units), "movable_units": len(opts), "candidate_packages": targets,
                 "new_parts": len(new_parts)})

    def options(i):
        if i not in opts:
            return [pkg]
        return [pkg] + [q for q, _ in opts[i]] + new_parts

    def build():
        m = slv.LinearModel()
        cost, back, meta = {}, {}, {"moves": {}}
        for i, u in enumerate(units):
            vs = []
            for j in options(i):
                v = m.binary(f"x|{i}|{j}")
                vs.append(v)
                if j == pkg:
                    continue
                if j.startswith("#"):
                    c_ = sum(split_cost(rf, c) for c in u["classes"])
                else:
                    c_ = sum(move_cost(rf, c, j, rf.affinity(c, j)) for c in u["classes"])
                cost[v] = round(c_ + 0.3 * len(u["tests"]), 3)
                meta["moves"][v] = (i, j)
            m.add({v: 1 for v in vs}, lb=1, ub=1)
        stay = {f"x|{i}|{pkg}": i for i in range(len(units))}
        if mode == "fc":
            # LCC of the package itself (soft: remaining smell)
            slack = m.var("lcc_slack", 0, 5 * sum(u["sets"] for u in units))
            back[slack] = 1.0
            m.add({**{v: 5 * units[i]["sets"] - units[i]["n"] for v, i in stay.items()}, slack: -1}, ub=0)
        else:
            # size of the package itself (soft: remaining smell); its LCC must not get worse
            over = m.var("over", 0, info["size"])
            back[over] = 1.0
            m.add({**{v: units[i]["size"] for v, i in stay.items()}, over: -1}, ub=GOD_CLASSES)
            k0, n0 = info["sets"], info["classes"]
            if 5 * k0 <= n0:
                m.add({v: 5 * units[i]["sets"] - units[i]["n"] for v, i in stay.items()}, ub=0, name="lcc kept")
            else:
                m.add({v: n0 * units[i]["sets"] - k0 * units[i]["n"] for v, i in stay.items()}, ub=0,
                      name="lcc not worse")
        for j in new_parts:
            terms = {f"x|{i}|{j}": 5 * u["sets"] - u["n"] for i, u in enumerate(units) if m.has(f"x|{i}|{j}")}
            if not strict:
                sj = m.var(f"incoherent|{j}", 0, 5 * sum(u["sets"] for u in units))
                cost[sj] = 2.0 / 5
                terms[sj] = -1
            m.add(terms, ub=0)
        for q in targets:
            st = q_state[q]
            terms = {}
            for i, u in enumerate(units):
                v = f"x|{i}|{q}"
                if not m.has(v):
                    continue
                added = u["sets"] - (1 if q in links.get(i, ()) else 0)
                if 5 * st["sets"] <= st["n"]:
                    terms[v] = 5 * added - u["n"]
                else:
                    terms[v] = st["n"] * added - st["sets"] * u["n"]
            if terms:
                ub = st["n"] - 5 * st["sets"] if 5 * st["sets"] <= st["n"] else 0
                m.add(terms, ub=ub, name=f"lcc {q}")
            sz = {f"x|{i}|{q}": u["size"] for i, u in enumerate(units) if m.has(f"x|{i}|{q}")}
            if sz:
                m.add(sz, ub=max(GOD_CLASSES, st["size"]) - st["size"], name=f"size {q}")
        for j in new_parts:
            m.add({f"x|{i}|{j}": u["size"] for i, u in enumerate(units) if m.has(f"x|{i}|{j}")}, ub=GOD_CLASSES)
        # package dependencies created by the placement, and no cycles among the packages involved
        for (a_u, b_u), n in unit_deps.items():
            for a in options(a_u):
                for b in options(b_u):
                    if a == b:
                        continue
                    e = m.binary(f"E|{a}|{b}")
                    cost.setdefault(e, EDGE_COST if (a, b) in rf._pairs else NEW_EDGE_COST * 0.3)
                    m.add({e: 1, f"x|{a_u}|{a}": -1, f"x|{b_u}|{b}": -1}, lb=-1)
        for (i, d, q), n in ext_deps.items():
            for j in options(i):
                if j == q:
                    continue
                a, b = (j, q) if d == "out" else (q, j)
                e = m.binary(f"E|{a}|{b}")
                cost.setdefault(e, EDGE_COST if (a, b) in rf._pairs else NEW_EDGE_COST * 0.3)
                m.add({e: 1, f"x|{i}|{j}": -1}, lb=0)
        nodes = [pkg] + targets + new_parts
        _order_constraints(m, nodes, cost, rf._pairs)
        return m, cost, back, meta

    def ops_of(sol, meta):
        by_part: dict[str, list[int]] = defaultdict(list)
        for v, (i, j) in meta["moves"].items():
            if sol.value(v) > 0.5:
                by_part[j].append(i)
        names, taken = {}, set()
        for n_, j in enumerate(sorted([j for j in by_part if j.startswith("#")], key=lambda j: -len(by_part[j]))):
            names[j] = name_part(rf, pkg, [c for i in by_part[j] for c in units[i]["classes"]], taken, n_ + 1)
            taken.add(names[j])
        ops = []
        for j, idx in by_part.items():
            to = names.get(j, j)
            for i in idx:
                u = units[i]
                listed = ", ".join(c.rsplit(".", 1)[-1] for c in u["classes"][:4]) + (" ..." if len(u["classes"]) > 4 else "")
                why = (f"related classes {listed} form a separate concern of {pkg}" if mode == "fc" else
                       f"split {pkg} ({info['size']} classes, limit {GOD_CLASSES}): {listed} move as one related set")
                for c in u["classes"]:
                    aff = None if j.startswith("#") else rf.affinity(c, j)
                    op = _move(rf, xf, c, to, aff, j.startswith("#"), why)
                    op["tests"] = [t for t in u["tests"] if t in set(xf.tests_of(c))] or op.get("tests", [])
                    if not op["tests"]:
                        op.pop("tests")
                    ops.append(op)
                # tests of the unit that no single class claims move with its first class
                claimed = {t for o in ops for t in o.get("tests", [])}
                rest = [t for t in u["tests"] if t not in claimed]
                if rest and ops:
                    ops[-1].setdefault("tests", []).extend(rest)
        return ops

    plans = _solve_variants(build, gid, time_limit, backend, ops_of)
    return plans, info


# ------------------------------------------------------------------------ scattered functionality

def consolidate(rf: RepoFacts, gid: str, concern: list[str], accessors: set[str] | None) -> tuple[list[Plan], dict]:
    xf = extra(rf)
    concern_set = {p for p in concern if p in rf.pkg_classes}
    if len(concern_set) < 2:
        return [], {"skipped": "fewer than two of the reported packages have production classes at this commit"}
    acc = accessors or (set(rf.pkg_classes) - concern_set)
    methods, classes = scattered_methods(xf, acc, concern_set)
    per_pkg = Counter(rf.package[xf.method_owner[mt]] for mt in methods)
    info = {"methods": len(methods), "concern_classes": sorted(classes), "reporting_packages": sorted(acc)[:20],
            "per_package": dict(per_pkg.most_common(10)), "methods_list": methods}
    if not any(v > 1 for v in per_pkg.values()):
        return [], {**info, "skipped": "no package has more than one method that uses classes of these "
                                       "packages together at this commit"}
    plans = []
    for q in sorted(concern_set):
        t0 = time.time()
        movers = sorted(c for c in classes if rf.package[c] != q)
        ops, blocked = [], []
        simple_q = rf.simple_names.get(q, set())
        for c in movers:
            if rf.protected.get(c) or rf.module.get(c) not in rf.pkg_modules.get(q, set()) \
                    or c.rsplit(".", 1)[-1] in simple_q or not rf.visible_after_move(c, q):
                blocked.append(c)
                continue
            ops.append(_move(rf, xf, c, q, rf.affinity(c, q), False,
                             f"used together with {q} by {len(methods)} methods"))
        cost = sum(move_cost(rf, o["class"], q, o["affinity"]) for o in ops)
        p = _plan(gid, "gather_in_" + q.rsplit(".", 1)[-1], None, cost, float(len(blocked)),
                  {"blocked_classes": blocked, "candidates": len(movers)}, ops, time.time() - t0,
                  status="optimal" if not blocked else "feasible")
        if ops:
            plans.append(p)
    plans.sort(key=lambda p: (p.remaining, p.change_cost))
    if plans:
        plans[0].name = "most_removed"
    return plans, info
