"""Candidate operations for a state of the search.

A state is a set of operations (a partial plan). Its *problem dependencies*
are the class dependencies that still keep the target smell alive, measured
on the whole repository after the operations are applied:

* cycle: dependencies between different packages of a strongly connected
  component that contains core packages;
* unstable dependency: dependencies of the central package on less stable packages;
* hub-like dependency: dependencies into and out of the hub.

Each problem dependency suggests actions: move its source or target class to
one of the candidate packages (learned affinity), remove an unused import,
delete the deprecated members it comes from, or invert it with an interface.
Every operation in the state can also be dropped again, which lets the search
repair a plan (for example a solver plan that creates new smells elsewhere).
The prior probability of an action grows with the weight of the dependencies
it removes and with the affinity of the target package.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from .. import detect as det
from ..plan.facts import IMPORT_COST, GroupFacts, RepoFacts, deletion_cost, inversion_cost, move_cost
from ..plan.simulate import State, apply


def op_key(op: dict) -> str:
    o = op["op"]
    if o == "move_class":
        return f"move:{op['class']}"            # one placement per class
    if o == "delete_member":
        return f"delete:{op['member']}"
    if o == "remove_unused_import":
        return f"import:{op['class']}>{op['import']}"
    if o == "extract_interface":
        return f"invert:{op['client']}>{op['provider']}"
    return f"{o}:{sorted(op.items())}"


@dataclass
class Action:
    key: str
    kind: str                 # add or drop
    ops: tuple                # operations added, or keys dropped
    prior: float
    label: str


class ActionSpace:
    def __init__(self, rf: RepoFacts, gf: GroupFacts, max_problem_edges: int = 60, max_actions: int = 40):
        self.rf, self.gf = rf, gf
        self.core = set(gf.core)
        self.central = gf.smell.get("central", "")
        self.max_problem_edges, self.max_actions = max_problem_edges, max_actions

    # ------------------------------------------------------------- costs

    def cost(self, op: dict) -> float:
        rf = self.rf
        o = op["op"]
        if o == "move_class":
            return move_cost(rf, op["class"], op["to_package"], op.get("affinity", rf.affinity(op["class"], op["to_package"])))
        if o == "delete_member":
            return deletion_cost(rf.member_info[op["member"]])
        if o == "remove_unused_import":
            return IMPORT_COST
        if o == "extract_interface":
            return inversion_cost(op["methods"])
        return 1.0

    # ------------------------------------------------------------- problem dependencies

    def problem_edges(self, st: State) -> list[tuple[str, str]]:
        g = st.package_graph()
        pkg = st.pkg
        t = self.gf.smell_type
        out = []
        if t == "cyclic_dependency":
            for comp in nx.strongly_connected_components(g):
                if len(comp) < 2 or not comp & self.core:
                    continue
                out += [(s, d) for s, d in st.edges
                        if s in self.rf.prod and d in self.rf.prod and pkg(s) != pkg(d)
                        and pkg(s) in comp and pkg(d) in comp]
        elif t == "unstable_dependency" and self.central in g:
            inst = det.degrees(g)["instability"].to_dict()
            worse = {q for q in g.successors(self.central) if inst[q] > inst[self.central]}
            out = [(s, d) for s, d in st.edges if s in self.rf.prod and d in self.rf.prod
                   and pkg(s) == self.central and pkg(d) in worse]
        elif t == "hub_like_dependency" and self.central in g:
            out = [(s, d) for s, d in st.edges if s in self.rf.prod and d in self.rf.prod and pkg(s) != pkg(d)
                   and self.central in (pkg(s), pkg(d))]
        weight = {k: (self.rf.edges[k].weight if k in self.rf.edges else 1) for k in out}
        return sorted(out, key=lambda k: (-weight[k], k))[: self.max_problem_edges]

    # ------------------------------------------------------------- actions

    def actions(self, ops: dict[str, dict], st: State | None = None) -> list[Action]:
        rf, gf = self.rf, self.gf
        st = st or apply(rf, list(ops.values()))
        problems = self.problem_edges(st)
        total = sum(rf.edges[k].weight for k in problems if k in rf.edges) or 1
        scores: dict[str, Action] = {}

        def add(op_list: list[dict], score: float, label: str) -> None:
            key = "+".join(sorted(op_key(o) for o in op_list))
            if all(op_key(o) in ops and ops[op_key(o)] == o for o in op_list):
                return
            if key in scores:
                scores[key].prior += score
            else:
                scores[key] = Action(key, "add", tuple(op_list), score, label)

        for k in problems:
            e = rf.edges.get(k)
            if e is None:
                continue
            share = e.weight / total
            for c in k:
                cur = st.package.get(c)
                for q, aff in gf.movable.get(c, [])[1:] + gf.movable.get(c, [])[:1]:
                    if q == cur:
                        continue
                    orig = rf.package[c]
                    op = {"op": "move_class", "class": c, "from_package": orig, "to_package": q,
                          "affinity": aff, "module": rf.module.get(c, "")}
                    if q == orig:
                        continue          # moving back = dropping the move (drop action)
                    add([op], share * (0.2 + aff), f"move {c} to {q}")
            if e.import_only:
                add([{"op": "remove_unused_import", "class": k[0], "import": k[1]}], share * 1.5,
                    f"remove unused import of {k[1]} in {k[0]}")
            dset = rf.deletion_set(e)
            if dset and all(m in rf.deletable for m in dset):
                add([{"op": "delete_member", "member": m, "owner": m.split("#")[0]} for m in sorted(dset)],
                    share * 1.2, f"delete deprecated {', '.join(sorted(dset))}")
            methods = rf.invertible(e)
            if methods:
                add([{"op": "extract_interface", "client": k[0], "provider": k[1], "methods": methods,
                      "interface_package": rf.package[k[0]]}], share * 0.6,
                    f"invert {k[0]} -> {k[1]} with an interface")
        acts = sorted(scores.values(), key=lambda a: (-a.prior, a.key))[: self.max_actions]
        for key, op in ops.items():
            acts.append(Action("drop:" + key, "drop", (key,), 0.02, f"undo {key}"))
        z = sum(a.prior for a in acts) or 1.0
        for a in acts:
            a.prior /= z
        return acts


def apply_action(ops: dict[str, dict], action: Action) -> dict[str, dict]:
    new = dict(ops)
    if action.kind == "drop":
        for k in action.ops:
            new.pop(k, None)
    else:
        for o in action.ops:
            new[op_key(o)] = o
    return new
