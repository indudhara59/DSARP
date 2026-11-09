"""The plan model: where should every element live so that the smell disappears?

Decision variables (all binary unless noted):

* ``x[c,p]``   class ``c`` is placed in package ``p`` (its current package or
  a candidate target): a change means **Move Class**;
* ``d[m]``     deprecated member ``m`` is deleted: **Remove dead code**;
* ``r[e]``     an import-only dependency ``e`` is removed: **Remove unused import**;
* ``z[e]``     dependency ``e`` disappears because all members it comes from
  are deleted (``z[e] <= d[m]`` for each of them);
* ``i[e]``     dependency ``e`` is inverted through an interface owned by the
  client's package: **Extract Interface + Dependency Inversion**;
* ``E[q,r]``   package ``q`` depends on package ``r`` after the plan;
* ``o[p]``     (integer) topological position of package ``p``;
* ``b[q,r]``   package dependency ``q -> r`` is allowed to break the order
  (a dependency still on a cycle), penalised or forbidden.

Constraints:

* each movable class is placed in exactly one package;
* a class dependency ``c -> t`` that is not removed creates the package
  dependency ``E[pkg(c), pkg(t)]`` (linearised: ``E[q,r] >= x[c,q] + x[t,r] - 1 - removed``);
* an inverted dependency creates the reverse package dependency and needs
  both classes to stay where they are; at most one removal mechanism per edge;
* **cycles**: ``o[q] >= o[r] + 1`` for every dependency ``E[q,r]`` unless
  ``b[q,r]`` (big-M ordering); no ``b`` means no cycle remains;
* **unstable dependency**: the dependencies from the package to its less
  stable packages must disappear (``E[u,l] <= b[u,l]``);
* **hub-like dependency**: fan-in or fan-out of the hub must drop to the
  threshold (the side is chosen by the solver; the excess is penalised);
* **size**: no package grows beyond the god-component threshold (30 classes,
  nested classes included) or beyond its current size if it is already larger;
* **new package dependencies** (pairs that do not exist today) cost extra,
  because they erode the architecture even when they create no smell.

Java validity (no name clashes, same build module, protected API never moves,
only deprecated members without production callers are deleted) is built
into the candidates (:mod:`.facts`).

Objective: least total change cost. Several plans are produced:

* ``least_change``: the smell disappears completely, at minimum cost;
* ``alternative``: the next-best complete plan (a diversity cut forbids the
  first plan's exact set of changes);
* ``budget_50`` / ``budget_25``: the most smell removed for half or a
  quarter of the cost of the complete plan (epsilon-constraint method).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..detect import PIPELINE
from . import solver as slv
from .facts import (EDGE_COST, IMPORT_COST, NEW_EDGE_COST, GroupFacts, RepoFacts, current_package_pairs,
                    deletion_cost, inversion_cost, move_cost)


@dataclass
class Plan:
    group_id: str
    name: str
    status: str
    objective: float | None
    change_cost: float
    remaining: float                 # broken order / remaining smell (0 = smell fully removed in the model)
    gap: float | None
    seconds: float
    backend: str
    size: dict
    operations: list[dict] = field(default_factory=list)


GOD_CLASSES = PIPELINE.god_classes   # same threshold as detection (stage 3)


def _key(*parts: str) -> str:
    return "|".join(parts)


class PlanModel:
    """Builds the linear model for one group; :meth:`solve` adds plan-specific limits."""

    def __init__(self, rf: RepoFacts, gf: GroupFacts):
        self.rf, self.gf = rf, gf

    def build(self, cycles: list[list[str]] | None = None) -> tuple[slv.LinearModel, dict[str, float], dict[str, float], dict]:
        rf, gf = self.rf, self.gf
        m = slv.LinearModel()
        cost: dict[str, float] = {}
        back: dict[str, float] = {}
        meta: dict = {"moves": {}, "deletions": {}, "imports": {}, "inversions": {}}
        S = set(gf.packages)
        if getattr(rf, "_pairs", None) is None:
            rf._pairs = current_package_pairs(rf)

        def edge_cost(q: str, r: str) -> float:
            return EDGE_COST if (q, r) in rf._pairs else NEW_EDGE_COST

        # placements
        place: dict[str, list[tuple[str, str | None]]] = {}
        for c, opts in gf.movable.items():
            if len(opts) == 1:
                continue
            names = []
            for q, aff in opts:
                v = m.binary(_key("x", c, q))
                names.append(v)
                if q != rf.package[c]:
                    cost[v] = move_cost(rf, c, q, aff)
                    meta["moves"][v] = (c, rf.package[c], q, aff)
                place.setdefault(c, []).append((q, v))
            m.add({v: 1 for v in names}, lb=1, ub=1, name=f"place {c}")

        def placements(c: str) -> list[tuple[str, str | None]]:
            return place.get(c, [(rf.package[c], None)])

        def cur_var(c: str) -> str | None:
            return next((v for q, v in place.get(c, []) if q == rf.package[c]), None)

        # deletions
        for mid, info in gf.deletable.items():
            v = m.binary(_key("d", mid))
            cost[v] = deletion_cost(info)
            meta["deletions"][v] = mid

        # edges
        for e in gf.edges:
            removal = []
            if e.import_only:
                v = m.binary(_key("r", e.source, e.target))
                cost[v] = IMPORT_COST
                removal.append(v)
                meta["imports"][v] = (e.source, e.target)
            else:
                dset = rf.deletion_set(e)
                if dset and all(mid in gf.deletable for mid in dset):
                    z = m.binary(_key("z", e.source, e.target))
                    for mid in dset:
                        m.add({z: 1, _key("d", mid): -1}, ub=0)
                    removal.append(z)
                methods = rf.invertible(e)
                ps, pt = rf.package[e.source], rf.package[e.target]
                if methods and ps != pt and ps in S and pt in S:
                    iv = m.binary(_key("i", e.source, e.target))
                    cost[iv] = inversion_cost(methods)
                    removal.append(iv)
                    meta["inversions"][iv] = (e.source, e.target, methods)
                    for c in (e.source, e.target):
                        cv = cur_var(c)
                        if cv:
                            m.add({iv: 1, cv: -1}, ub=0)
                    ev = m.binary(_key("E", pt, ps))
                    cost.setdefault(ev, edge_cost(pt, ps))
                    m.add({ev: 1, iv: -1}, lb=0)
            if len(removal) > 1:
                m.add({v: 1 for v in removal}, ub=1)
            for q, xv in placements(e.source):
                for r, yv in placements(e.target):
                    if q == r or q not in S or r not in S:
                        continue
                    ev = m.binary(_key("E", q, r))
                    cost.setdefault(ev, edge_cost(q, r))
                    terms = {ev: 1.0}
                    fixed = 0
                    for v in (xv, yv):
                        if v is None:
                            fixed += 1
                        else:
                            terms[v] = terms.get(v, 0) - 1.0
                    for v in removal:
                        terms[v] = terms.get(v, 0) + 1.0
                    m.add(terms, lb=fixed - 1)

        self._size_constraints(m, place, S, meta)
        self._smell_constraints(m, back, S, cycles or [])
        return m, cost, back, meta

    def _size_constraints(self, m: slv.LinearModel, place: dict, S: set[str], meta: dict) -> None:
        """No package grows into a god component: classes per package stay below the
        threshold (or below today's size, if the package is already too big)."""
        rf = self.rf
        if getattr(rf, "_nested", None) is None:
            t = rf.model.types
            t = t[~t["is_test"].astype(bool) & ~t["is_generated"].astype(bool)]
            rf._nested = t["top_level"].value_counts().to_dict()
        n = rf._nested
        size = {q: sum(n.get(c, 1) for c in rf.pkg_classes.get(q, ())) for q in S}
        movers = defaultdict(dict)
        for c, opts in place.items():
            for q, v in opts:
                movers[q][v] = n.get(c, 1)
        for iv, (client, _, _) in meta["inversions"].items():     # a new interface is a new class
            movers[rf.package[client]][iv] = 1
        for q in S:
            if not movers.get(q):
                continue
            fixed = size[q] - sum(n.get(c, 1) for c in place if rf.package[c] == q)
            cap = max(GOD_CLASSES, size[q])
            if fixed + sum(movers[q].values()) <= cap:
                continue
            m.add(movers[q], ub=cap - fixed, name=f"size {q}")

    def _smell_constraints(self, m: slv.LinearModel, back: dict[str, float], S: set[str],
                           cycles: list[list[str]]) -> None:
        gf = self.gf
        e_vars = [v for v in m.by_name if v.startswith("E|")]
        if gf.smell_type == "cyclic_dependency":
            # b[q,r]: dependency q -> r may stay on a cycle (counted as remaining smell).
            # For every known cycle, at least one of its dependencies must be gone or marked.
            for v in e_vars:
                _, q, r = v.split("|")
                b = m.binary(_key("b", q, r))
                back[b] = 1.0
                m.add({b: 1, v: -1}, ub=0)
            for cyc in cycles:
                terms: dict[str, float] = {}
                for k in range(len(cyc)):
                    q, r = cyc[k], cyc[(k + 1) % len(cyc)]
                    ev = _key("E", q, r)
                    if not m.has(ev):
                        break
                    terms[ev] = terms.get(ev, 0) + 1
                    terms[_key("b", q, r)] = terms.get(_key("b", q, r), 0) - 1
                else:
                    m.add(terms, ub=len(cyc) - 1)
        elif gf.smell_type == "unstable_dependency":
            for q, r in gf.smell.get("focus_pairs", []):
                v = _key("E", q, r)
                if m.has(v):
                    b = m.binary(_key("b", q, r))
                    back[b] = 1.0
                    m.add({v: 1, b: -1}, ub=0)
        elif gf.smell_type == "hub_like_dependency":
            # The hub disappears when its fan-in OR its fan-out drops to the threshold.
            # y = 1: reduce fan-out, y = 0: reduce fan-in; t = dependencies still above the threshold.
            h = gf.smell["hub"]
            outs = [v for v in e_vars if v.split("|")[1] == h]
            ins = [v for v in e_vars if v.split("|")[2] == h]
            y = m.binary("hub_side")
            big = len(outs) + len(ins) + gf.smell["fixed_out"] + gf.smell["fixed_in"] + 1
            t = m.var("hub_excess", 0, big)
            back[t] = 1.0
            for side, vs, fixed, limit, sign in (("out", outs, gf.smell["fixed_out"], gf.smell["max_out"], 1),
                                                 ("in", ins, gf.smell["fixed_in"], gf.smell["max_in"], -1)):
                # t >= sum(vs) + fixed - limit - big * (side not chosen)
                terms = {v: 1.0 for v in vs}
                terms[t] = -1.0
                if sign == 1:
                    terms[y] = big            # y = 1 chooses "out": constraint active when y = 1
                    m.add(terms, ub=limit - fixed + big)
                else:
                    terms[y] = -big           # y = 0 chooses "in"
                    m.add(terms, ub=limit - fixed)

    # ------------------------------------------------------------------ plans

    def plans(self, time_limit: float = 60.0, backend: str = "auto", alternatives: int = 1,
              budgets: tuple[float, ...] = (0.5, 0.25)) -> list[Plan]:
        """Trade-off plans for the group.

        1. ``no_change``: what remains if nothing is changed (reference point);
        2. ``most_removed``: least remaining smell, then least change;
        3. ``least_change``: smell removed completely at least cost (only if
           ``most_removed`` shows that this is possible);
        4. ``alternative``: next-best plan with the same remaining smell;
        5. ``budget_50`` / ``budget_25``: least remaining smell for a share
           of the cost of plan 2 (epsilon-constraint).
        """
        out: list[Plan] = []
        base = self._solve("no_change", time_limit, backend, budget=0.0)
        out.append(base)
        best = self._solve("most_removed", time_limit, backend, budget=None)
        out.append(best)
        if best.status not in ("optimal", "feasible"):
            return out
        reference = best
        if best.remaining == 0 and best.operations:
            full = self._solve("least_change", time_limit, backend, full=True)
            if full.status in ("optimal", "feasible") and not _same(full, best):
                out.append(full)
                reference = full
        seen = [self._change_vars(reference)]
        for k in range(alternatives):
            alt = self._solve("alternative" if k == 0 else f"alternative_{k + 1}", time_limit, backend,
                              max_remaining=reference.remaining, forbid=seen)
            if alt.status not in ("optimal", "feasible") or not alt.operations:
                break
            out.append(alt)
            seen.append(self._change_vars(alt))
        for frac in budgets:
            budget = reference.change_cost * frac
            if budget <= 0:
                continue
            p = self._solve(f"budget_{int(frac * 100)}", time_limit, backend, budget=budget)
            if p.status in ("optimal", "feasible") and p.operations and not any(_same(p, q) for q in out):
                out.append(p)
        return out

    def _change_vars(self, plan: Plan) -> list[str]:
        return plan.size.get("chosen", [])

    def _solve(self, name: str, time_limit: float, backend: str, full: bool = False,
               budget: float | None = None, max_remaining: float | None = None,
               forbid: list[list[str]] | None = None) -> Plan:
        """``full``: no smell may remain, minimise change. Otherwise minimise the
        remaining smell first and change second, optionally within ``budget``."""
        m, cost, back, meta = self.build(self._initial_cycles())
        self._configure(m, cost, back, budget, max_remaining, full, forbid)
        sol, rounds = self._solve_lazy(m, cost, back, budget, max_remaining, full, forbid, backend, time_limit)
        plan = Plan(self.gf.group_id, name, sol.status, sol.objective, 0.0, 0.0, sol.gap, round(sol.seconds, 2),
                    sol.backend, {**self._last_size, "cycle_rounds": rounds})
        if not sol.ok:
            plan.size["message"] = sol.message
            return plan
        plan.change_cost = round(sum(c for v, c in cost.items() if sol.value(v) > 0.5 and not v.startswith("E|")), 3)
        plan.remaining = round(sum(w * round(sol.value(b)) for b, w in back.items()), 3)
        chosen = [v for v in cost if not v.startswith("E|") and sol.value(v) > 0.5]
        plan.size["chosen"] = chosen
        plan.operations = self._operations(sol, meta)
        return plan

    # ------------------------------------------------------------------ lazy cycles

    def _possible_graph(self):
        import networkx as nx
        if getattr(self, "_pg", None) is None:
            m, _, _, _ = self.build([])
            g = nx.DiGraph()
            g.add_edges_from(tuple(v.split("|")[1:]) for v in m.by_name if v.startswith("E|"))
            self._pg = g
        return self._pg

    def _initial_cycles(self, limit: int = 3000) -> list[list[str]]:
        """Cycles of length two and three among the possible package dependencies."""
        if self.gf.smell_type != "cyclic_dependency":
            return []
        import networkx as nx
        if getattr(self, "_cycles", None) is None:
            out = []
            for c in nx.simple_cycles(self._possible_graph(), length_bound=3):
                out.append(c)
                if len(out) >= limit:
                    break
            self._cycles = out
        return list(self._cycles)

    @staticmethod
    def _configure(m, cost, back, budget, max_remaining, full, forbid) -> None:
        if full:
            if back:
                m.add({b: 1 for b in back}, ub=0, name="smell fully removed")
            m.minimize(cost)
        else:
            if budget is not None:
                changes = {v: c for v, c in cost.items() if not v.startswith("E|")}
                if changes:
                    m.add(changes, ub=budget, name="change budget")
            if max_remaining is not None and back:
                m.add(back, ub=max_remaining + 1e-6, name="remaining smell limit")
            weight = 1.0 + sum(cost.values())   # remaining smell dominates the change cost
            obj = {b: w * weight for b, w in back.items()}
            for v, c in cost.items():
                obj[v] = obj.get(v, 0) + c
            m.minimize(obj)
        for chosen in forbid or []:
            if chosen:
                m.add({v: 1 for v in chosen if m.has(v)}, ub=len(chosen) - 1, name="diversity cut")

    def _solve_lazy(self, m, cost, back, budget, max_remaining, full, forbid, backend, time_limit,
                    max_rounds: int = 40):
        """Solve; while the result still has cycles that are not counted as remaining,
        add them as constraints and solve again."""
        import time as _t

        import networkx as nx
        start = _t.time()
        rounds = 0
        while True:
            rounds += 1
            left = max(time_limit - (_t.time() - start), 1.0)
            sol = slv.solve(m, backend=backend, time_limit=left)
            self._last_size = dict(m.size)
            if not sol.ok or self.gf.smell_type != "cyclic_dependency":
                break
            g = nx.DiGraph()
            g.add_edges_from(tuple(v.split("|")[1:]) for v in m.by_name
                             if v.startswith("E|") and sol.value(v) > 0.5
                             and (not m.has("b|" + v[2:]) or sol.value("b|" + v[2:]) < 0.5))
            new = []
            for comp in nx.strongly_connected_components(g):
                if len(comp) < 2:
                    continue
                sub = g.subgraph(comp)
                for start_node in list(comp)[:10]:
                    try:
                        cyc = [u for u, _ in nx.find_cycle(sub, start_node)]
                    except nx.NetworkXNoCycle:
                        continue
                    if cyc not in new:
                        new.append(cyc)
            if not new or rounds >= max_rounds or _t.time() - start > time_limit:
                if new:
                    sol.status = "feasible"
                    sol.message = f"stopped with {len(new)} unhandled cycles after {rounds} rounds"
                break
            self._cycles = getattr(self, "_cycles", []) + new
            m, cost, back, _ = self.build(self._cycles)
            self._configure(m, cost, back, budget, max_remaining, full, forbid)
        sol.seconds = round(_t.time() - start, 2)
        return sol, rounds

    def _operations(self, sol: slv.Solution, meta: dict) -> list[dict]:
        ops = []
        for v, (c, src, dst, aff) in meta["moves"].items():
            if sol.value(v) > 0.5:
                ops.append({"op": "move_class", "class": c, "from_package": src, "to_package": dst,
                            "affinity": aff, "module": self.rf.module.get(c, "")})
        for v, mid in meta["deletions"].items():
            if sol.value(v) > 0.5:
                ops.append({"op": "delete_member", "member": mid, "owner": mid.split("#")[0]})
        for v, (s, t) in meta["imports"].items():
            if sol.value(v) > 0.5:
                ops.append({"op": "remove_unused_import", "class": s, "import": t})
        for v, (s, t, methods) in meta["inversions"].items():
            if sol.value(v) > 0.5:
                ops.append({"op": "extract_interface", "client": s, "provider": t, "methods": methods,
                            "interface_package": self.rf.package[s]})
        return ops


def _same(a: Plan, b: Plan) -> bool:
    key = lambda p: sorted(str(sorted(o.items())) for o in p.operations)  # noqa: E731
    return key(a) == key(b)
