"""Checks L1 and L3: apply a plan in memory and detect the smells again.

The plan model works on a reduced problem (one group, a few candidate
targets per class, lazily added cycles). Here the plan is applied to the
**whole** production dependency graph of the repository and the same
detection rules as in stage 3 (:mod:`dsarp.detect`, ``PIPELINE`` thresholds)
are run before and after:

* **L1** (graph simulation): does the targeted smell disappear?
* **L3** (re-detection): which smells appear elsewhere, which disappear as a
  side effect? A plan that removes a cycle but creates a new one, or turns a
  package into a hub, is penalised in the ranking.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import networkx as nx
import pandas as pd

from .. import detect as det
from .facts import RepoFacts


@dataclass
class State:
    """Production code after a plan: package of each class and class dependencies."""

    package: dict[str, str]
    edges: set[tuple[str, str]]
    interfaces: dict[str, str] = field(default_factory=dict)   # new interface -> package

    def package_graph(self) -> nx.DiGraph:
        g = nx.DiGraph()
        g.add_nodes_from(set(self.package.values()) | set(self.interfaces.values()))
        weight: Counter = Counter()
        for s, t in self.edges:
            ps, pt = self.pkg(s), self.pkg(t)
            if ps != pt:
                weight[(ps, pt)] += 1
        for (a, b), w in weight.items():
            g.add_edge(a, b, weight=w, class_edges=w)
        return g

    def pkg(self, c: str) -> str:
        return self.package.get(c) or self.interfaces[c]


def initial_state(rf: RepoFacts) -> State:
    return State({c: rf.package[c] for c in rf.prod}, set(rf.edges))


def apply(rf: RepoFacts, ops: list[dict], state: State | None = None) -> State:
    """Apply plan operations to a copy of ``state`` (default: the current code)."""
    st = state or initial_state(rf)
    st = State(dict(st.package), set(st.edges), dict(st.interfaces))
    deleted = {o["member"] for o in ops if o["op"] == "delete_member"}
    for o in ops:
        if o["op"] == "move_class":
            st.package[o["class"]] = o["to_package"]
        elif o["op"] == "remove_unused_import":
            st.edges.discard((o["class"], o["import"]))
        elif o["op"] == "extract_interface":
            iface = interface_name(o)
            st.interfaces[iface] = o["interface_package"]
            st.edges.discard((o["client"], o["provider"]))
            st.edges.add((o["client"], iface))
            st.edges.add((o["provider"], iface))
    if deleted:
        for k in list(st.edges):
            e = rf.edges.get(k)
            if e is None:
                continue
            need = rf.deletion_set(e)
            if need and need <= deleted:
                st.edges.discard(k)
    return st


def interface_name(op: dict) -> str:
    """Name of the interface introduced by an ``extract_interface`` operation."""
    simple = op["provider"].rsplit(".", 1)[-1]
    return f"{op['interface_package']}.{simple}Port" if op["interface_package"] else f"{simple}Port"


def class_counts(rf: RepoFacts, st: State) -> dict[str, int]:
    """Classes per package, nested classes counted with their top-level class (as in stage 3)."""
    t = rf.model.types
    t = t[~t["is_test"].astype(bool) & ~t["is_generated"].astype(bool)]
    nested = Counter(t["top_level"])
    out: Counter = Counter()
    for c, p in st.package.items():
        out[p] += nested.get(c, 1)
    for _, p in st.interfaces.items():
        out[p] += 1
    return dict(out)


def smells(rf: RepoFacts, st: State, th: det.Thresholds = det.PIPELINE) -> pd.DataFrame:
    """Package-level smells of a state (cycles, hubs, unstable dependencies, god components)."""
    return det.detect(st.package_graph(), None, class_counts(rf, st), None, th)


def feedback_arcs(g: nx.DiGraph, nodes: set[str] | None = None) -> int:
    """Approximate size of a minimum feedback arc set (Eades, Lin and Smyth 1993):
    how many package dependencies still have to go before no cycle is left.
    Measured on the strongly connected components that touch ``nodes``."""
    comps = [c for c in nx.strongly_connected_components(g) if len(c) > 1 and (nodes is None or c & nodes)]
    total = 0
    for comp in comps:
        h = g.subgraph(comp).copy()
        left, right = [], []
        while h:
            changed = True
            while changed:
                changed = False
                for n in [n for n in h if h.out_degree(n) == 0]:
                    right.append(n)
                    h.remove_node(n)
                    changed = True
                for n in [n for n in h if h.in_degree(n) == 0]:
                    left.append(n)
                    h.remove_node(n)
                    changed = True
            if h:
                n = max(h, key=lambda v: (h.out_degree(v) - h.in_degree(v), v))
                left.append(n)
                h.remove_node(n)
        order = {n: i for i, n in enumerate(left + right[::-1])}
        total += sum(1 for a, b in g.subgraph(comp).edges if order[a] > order[b])
    return total


def _key(row) -> tuple:
    if row["type"] == "cyclic_dependency":
        return ("cyclic_dependency", frozenset(row["members"]))
    return (row["type"], row["central"])


def _cycle_pairs(df: pd.DataFrame) -> set[frozenset]:
    pairs = set()
    for mem in df.loc[df["type"] == "cyclic_dependency", "members"]:
        mem = sorted(mem)
        for i in range(len(mem)):
            for j in range(i + 1, len(mem)):
                pairs.add(frozenset((mem[i], mem[j])))
    return pairs


@dataclass
class Simulation:
    target_removed: bool
    target_before: dict
    target_after: dict
    new_smells: list[dict]
    removed_smells: list[dict]
    counts_before: dict
    counts_after: dict

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Simulator:
    """Holds the baseline so that many plans can be simulated cheaply."""

    def __init__(self, rf: RepoFacts, th: det.Thresholds = det.PIPELINE):
        self.rf, self.th = rf, th
        self.base_state = initial_state(rf)
        self.base_graph = self.base_state.package_graph()
        self.base = det.detect(self.base_graph, None, class_counts(rf, self.base_state), None, th)
        self.base_keys = {_key(r) for _, r in self.base.iterrows()}
        self.base_pairs = _cycle_pairs(self.base)

    def target_status(self, df: pd.DataFrame, smell_type: str, core: list[str], central: str) -> dict:
        if smell_type == "cyclic_dependency":
            core_set = set(core)
            best = 0
            members: list[str] = []
            for mem in df.loc[df["type"] == "cyclic_dependency", "members"]:
                n = len(core_set & set(mem))
                if n > best:
                    best, members = n, sorted(core_set & set(mem))
            return {"present": best >= 2, "core_packages_on_cycle": best, "packages": members}
        return {"present": bool(len(df[(df["type"] == smell_type) & (df["central"] == central)]))}

    def target_measure(self, g: nx.DiGraph, df: pd.DataFrame, smell_type: str, core: list[str],
                       central: str) -> dict:
        """Size of the target smell: cycle packages, less stable dependencies, or hub degree.

        For unstable and hub-like dependencies the measure is taken directly
        from the graph, so that a smell reported by a detector with other
        thresholds (e.g. Designite: any dependency on a less stable package)
        is measured the same way before and after the plan."""
        if smell_type == "cyclic_dependency":
            return {**self.target_status(df, smell_type, core, central),
                    "dependencies_to_cut": feedback_arcs(g, set(core))}
        if central not in g:
            return {"present": False, "note": "package no longer exists"}
        d = det.degrees(g)
        inst = d["instability"].to_dict()
        if smell_type == "unstable_dependency":
            targets = list(g.successors(central))
            worse = sorted(t for t in targets if inst[t] > inst[central])
            return {"present": bool(worse), "less_stable": worse, "dependencies": len(targets),
                    "instability": round(inst[central], 4),
                    "ratio": round(len(worse) / len(targets), 4) if targets else 0.0,
                    "detected": self.target_status(df, smell_type, core, central)["present"]}
        if smell_type == "hub_like_dependency":
            flagged = self.target_status(df, smell_type, core, central)["present"]
            return {"present": flagged, "fan_in": int(d.loc[central, "fan_in"]),
                    "fan_out": int(d.loc[central, "fan_out"])}
        return self.target_status(df, smell_type, core, central)

    def run(self, ops: list[dict], smell_type: str, core: list[str], central: str = "") -> Simulation:
        st = apply(self.rf, ops, self.base_state)
        g_after = st.package_graph()
        after = det.detect(g_after, None, class_counts(self.rf, st), None, self.th)
        before_t = self.target_measure(self.base_graph, self.base, smell_type, core, central)
        after_t = self.target_measure(g_after, after, smell_type, core, central)
        after_keys = {_key(r): r for _, r in after.iterrows()}
        new = []
        for k, r in after_keys.items():
            if k in self.base_keys:
                continue
            if k[0] == "cyclic_dependency":
                mem = sorted(r["members"])
                pairs = {frozenset((a, b)) for i, a in enumerate(mem) for b in mem[i + 1:]}
                fresh = pairs - self.base_pairs
                if not fresh:
                    continue          # a smaller part of a cycle that existed before
                involved = sorted({p for pr in fresh for p in pr})
                new.append({"type": k[0], "packages": involved, "size": len(mem)})
            else:
                new.append({"type": k[0], "central": k[1], "metrics": r["metrics"]})
        removed = []
        for _, r in self.base.iterrows():
            k = _key(r)
            if k in after_keys:
                continue
            if k[0] == "cyclic_dependency":
                left = max((len(set(r["members"]) & set(m)) for m in after.loc[after["type"] == k[0], "members"]),
                           default=0)
                removed.append({"type": k[0], "size": len(r["members"]), "largest_part_left": left})
            else:
                removed.append({"type": k[0], "central": k[1]})
        return Simulation(not after_t["present"], before_t, after_t, new, removed,
                          dict(Counter(self.base["type"])), dict(Counter(after["type"])))


def unstable_targets(rf: RepoFacts, central: str, th: det.Thresholds = det.PIPELINE) -> list[str]:
    g = initial_state(rf).package_graph()
    if central not in g:
        return []
    u = det.unstable(g, th)
    return list(u.loc[central, "less_stable"]) if central in u.index else []


def hub_data(rf: RepoFacts, central: str, th: det.Thresholds = det.PIPELINE) -> dict | None:
    g = initial_state(rf).package_graph()
    if central not in g:
        return None
    h = det.hubs(g, th)
    if central not in h.index:
        return None
    r = h.loc[central]
    return {"hub": central, "fan_in": int(r["fan_in"]), "fan_out": int(r["fan_out"]),
            "max_in": int(r["in_threshold"]), "max_out": int(r["out_threshold"]),
            "successors": sorted(g.successors(central)), "predecessors": sorted(g.predecessors(central))}


def neighbours_by_package(rf: RepoFacts) -> dict[str, set[str]]:
    out = defaultdict(set)
    for s, t in rf.edges:
        ps, pt = rf.package[s], rf.package[t]
        if ps != pt:
            out[ps].add(pt)
            out[pt].add(ps)
    return out
