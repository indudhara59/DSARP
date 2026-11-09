"""In-memory architectural smell detection on the program model.

Re-implements the published detection rules of Arcan and Designite on the
package and class dependency graphs, so that the effect of a refactoring plan
on the smells can be measured in milliseconds without re-running the tools
(check L3 of the pipeline). Calibration against the real detectors is in
:mod:`dsarp.calibrate`.

Rules (thresholds in :class:`Thresholds`):

* **Cyclic dependency**: every strongly connected component with two or more
  packages (or classes) is one cycle smell. Arcan and Designite list
  individual cycles; each of them lies inside one such component.
* **Hub-like dependency** (Arcan): fan-in and fan-out both above the median
  of all components, and their difference small compared with the total.
* **Unstable dependency** (Arcan): at least 30% of a package's dependencies
  point to packages that are less stable (higher instability ``I = Ce/(Ca+Ce)``).
  Designite reports a package with *any* less stable dependency
  (``unstable_ratio=0`` with ``strict`` comparison).
* **God component** (Designite): a package with more than 30 classes; an
  optional lines-of-code threshold approximates Arcan's size-based rule.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import networkx as nx
import pandas as pd


@dataclass
class Thresholds:
    unstable_ratio: float = 0.30        # Arcan's degree of unstable dependency
    hub_quantile: float = 0.5           # median, as in Arcan
    hub_balance: float = 0.25           # |in - out| must be below this share of in + out
    hub_min_degree: int = 1             # ignore components with no dependencies at all
    god_classes: int = 30               # Designite: more than 30 classes
    god_loc: int | None = None          # optional size threshold (lines of code)
    god_count_nested: bool = False      # Designite counts nested classes, Arcan does not
    collapse_nested: bool = True        # Arcan analyses top-level classes only


ARCAN_LIKE = Thresholds()
"""Arcan's published rules (median fan-in/fan-out, 30% unstable dependencies)."""

ARCAN_CALIBRATED = Thresholds(hub_quantile=0.9, hub_balance=0.25, unstable_ratio=0.5)
"""Best agreement with Arcan 2.x output on Tika, Karaf and Struts (see dsarp.calibrate)."""

DESIGNITE_LIKE = Thresholds(unstable_ratio=0.0, god_count_nested=True, collapse_nested=False)
"""Designite's rules: any less stable dependency; god component above 30 classes incl. nested."""

PIPELINE = Thresholds(hub_quantile=0.9, hub_balance=0.25, unstable_ratio=0.5, god_count_nested=True)
"""Used by the pipeline to measure smells before and after a plan."""


# ---------------------------------------------------------------- metrics

def degrees(g: nx.DiGraph) -> pd.DataFrame:
    """Fan-in, fan-out (distinct neighbours) and instability per node."""
    nodes = list(g.nodes)
    fan_in = [g.in_degree(n) for n in nodes]
    fan_out = [g.out_degree(n) for n in nodes]
    inst = [fo / (fi + fo) if fi + fo else 0.0 for fi, fo in zip(fan_in, fan_out)]
    return pd.DataFrame({"node": nodes, "fan_in": fan_in, "fan_out": fan_out, "instability": inst}).set_index("node")


# ---------------------------------------------------------------- rules

def cycles(g: nx.DiGraph) -> list[list[str]]:
    """Strongly connected components with at least two nodes, largest first."""
    comps = [sorted(c) for c in nx.strongly_connected_components(g) if len(c) > 1]
    return sorted(comps, key=lambda c: (-len(c), c[0]))


def hubs(g: nx.DiGraph, th: Thresholds = ARCAN_LIKE) -> pd.DataFrame:
    d = degrees(g)
    active = d[(d["fan_in"] + d["fan_out"]) >= th.hub_min_degree]
    if active.empty:
        return active.assign(hub=False)
    in_thr = active["fan_in"].quantile(th.hub_quantile)
    out_thr = active["fan_out"].quantile(th.hub_quantile)
    total = active["fan_in"] + active["fan_out"]
    is_hub = ((active["fan_in"] > in_thr) & (active["fan_out"] > out_thr)
              & ((active["fan_in"] - active["fan_out"]).abs() < th.hub_balance * total))
    return active.assign(hub=is_hub, in_threshold=in_thr, out_threshold=out_thr)


def unstable(g: nx.DiGraph, th: Thresholds = ARCAN_LIKE) -> pd.DataFrame:
    """Per package: share of dependencies on less stable packages."""
    d = degrees(g)
    inst = d["instability"].to_dict()
    rows = []
    for n in g.nodes:
        targets = list(g.successors(n))
        if not targets:
            continue
        worse = [t for t in targets if inst[t] > inst[n]]
        ratio = len(worse) / len(targets)
        smelly = ratio > 0 if th.unstable_ratio == 0 else ratio >= th.unstable_ratio
        rows.append({"node": n, "instability": inst[n], "dependencies": len(targets),
                     "less_stable": worse, "ratio": ratio, "unstable": smelly and bool(worse)})
    return pd.DataFrame(rows, columns=["node", "instability", "dependencies", "less_stable", "ratio", "unstable"]).set_index("node")


def gods(class_counts: dict[str, int], loc: dict[str, int] | None, th: Thresholds = ARCAN_LIKE) -> list[str]:
    out = [p for p, n in class_counts.items() if n > th.god_classes]
    if th.god_loc is not None and loc:
        out += [p for p, n in loc.items() if n > th.god_loc and p not in out]
    return sorted(out)


# ---------------------------------------------------------------- all smells

def detect(pkg_graph: nx.DiGraph, cls_graph: nx.DiGraph | None, class_counts: dict[str, int],
           loc: dict[str, int] | None = None, th: Thresholds = ARCAN_LIKE) -> pd.DataFrame:
    """All smells in one table: ``type, level, members, central, related, metrics``."""
    rows = []
    for c in cycles(pkg_graph):
        sub = pkg_graph.subgraph(c)
        rows.append(("cyclic_dependency", "package", c, "", [], {"edges": sub.number_of_edges()}))
    if cls_graph is not None:
        for c in cycles(cls_graph):
            rows.append(("cyclic_dependency", "class", c, "", [], {"edges": cls_graph.subgraph(c).number_of_edges()}))
    for level, g in (("package", pkg_graph), ("class", cls_graph)):
        if g is None:
            continue
        h = hubs(g, th)
        for n, r in h[h["hub"]].iterrows():
            neigh = sorted(set(g.predecessors(n)) | set(g.successors(n)))
            rows.append(("hub_like_dependency", level, [n] + neigh, n, neigh,
                         {"fan_in": int(r["fan_in"]), "fan_out": int(r["fan_out"])}))
    u = unstable(pkg_graph, th)
    for n, r in u[u["unstable"]].iterrows():
        rows.append(("unstable_dependency", "package", [n] + list(r["less_stable"]), n, list(r["less_stable"]),
                     {"instability": round(float(r["instability"]), 4), "ratio": round(float(r["ratio"]), 4)}))
    for p in gods(class_counts, loc, th):
        rows.append(("god_component", "package", [p], p, [], {"classes": int(class_counts.get(p, 0)),
                                                            "loc": int((loc or {}).get(p, 0))}))
    return pd.DataFrame([{"type": t, "level": lv, "members": m, "central": c, "related": r, "metrics": x}
                         for t, lv, m, c, r, x in rows],
                        columns=["type", "level", "members", "central", "related", "metrics"])


def summary(smells: pd.DataFrame) -> dict:
    """Numbers used to compare a codebase before and after a plan."""
    out = {f"{t}:{lv}": int(n) for (t, lv), n in smells.groupby(["type", "level"]).size().items()}
    cyc = smells[smells["type"] == "cyclic_dependency"]
    out["packages_in_cycles"] = int(sum(len(m) for m in cyc[cyc["level"] == "package"]["members"]))
    out["classes_in_cycles"] = int(sum(len(m) for m in cyc[cyc["level"] == "class"]["members"]))
    out["total"] = int(len(smells))
    return out


def detect_model(model, include_tests: bool = False, th: Thresholds = ARCAN_LIKE,
                 class_level: bool = True) -> pd.DataFrame:
    """Run :func:`detect` on a :class:`dsarp.program.ProgramModel`."""
    pkg = model.package_graph(include_tests=include_tests)
    cls = model.type_graph(include_tests=include_tests, collapse_nested=th.collapse_nested) if class_level else None
    types = model.types
    if not include_tests:
        types = types[~types["is_test"]]
    types = types[~types["is_generated"]]
    top = types[types["outer"].astype(str) == ""]
    counts = (types if th.god_count_nested else top).groupby("package").size().to_dict()
    lines = (top["end_line"].astype(int) - top["line"].astype(int) + 1).clip(lower=0)
    loc = lines.groupby(top["package"]).sum().to_dict()
    return detect(pkg, cls, counts, loc, th)


def to_records(smells: pd.DataFrame) -> pd.DataFrame:
    """JSON-encode list and dict columns for writing to CSV."""
    out = smells.copy()
    for col in ("members", "related", "metrics"):
        out[col] = out[col].map(json.dumps)
    return out


def thresholds_dict(th: Thresholds) -> dict:
    return asdict(th)
