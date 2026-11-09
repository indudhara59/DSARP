"""Agreement between the in-memory detector and the real detectors.

For each smell type, the smells a detector reported are compared with what
:mod:`dsarp.detect` finds on the program model (test code included, as the
detectors analysed it):

* **Cycles**: a detector cycle is *covered* when all its members that exist
  in the model lie in one detected strongly connected component (recall);
  a detected component is *confirmed* when it contains at least two members
  of some detector cycle (precision).
* **Hub-like, unstable, god component**: the central elements are compared
  as sets (precision, recall, F1).

A small grid over the thresholds is evaluated too, so the thesis can report
how sensitive the agreement is and which thresholds fit each detector best.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace

import pandas as pd

from .detect import ARCAN_LIKE, DESIGNITE_LIKE, PIPELINE, Thresholds, detect_model, thresholds_dict

TOOL_THRESHOLDS = {"arcan": ARCAN_LIKE, "designite": DESIGNITE_LIKE}


def _prf(found: set, truth: set) -> dict:
    tp = len(found & truth)
    p = tp / len(found) if found else None
    r = tp / len(truth) if truth else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"detected": len(found), "reported": len(truth), "both": tp,
            "precision": None if p is None else round(p, 4), "recall": None if r is None else round(r, 4),
            "f1": None if f is None else round(f, 4)}


def _cycle_agreement(reported: list[list[str]], detected: list[list[str]], nodes: set[str]) -> dict:
    where = {n: i for i, comp in enumerate(detected) for n in comp}
    mappable = covered = 0
    confirmed: set[int] = set()
    for members in reported:
        inside = [m for m in members if m in nodes]
        if len(inside) < 2:
            continue
        mappable += 1
        comps = {where.get(m) for m in inside}
        if len(comps) == 1 and None not in comps:
            covered += 1
        for c in comps - {None}:
            if sum(1 for m in inside if where.get(m) == c) >= 2:
                confirmed.add(c)
    rep_nodes = {m for members in reported for m in members if m in nodes}
    det_nodes = {m for comp in detected for m in comp}
    union = rep_nodes | det_nodes
    return {"reported": len(reported), "mappable": mappable, "detected": len(detected),
            "recall": round(covered / mappable, 4) if mappable else None,
            "precision": round(len(confirmed) / len(detected), 4) if detected else None,
            "node_jaccard": round(len(rep_nodes & det_nodes) / len(union), 4) if union else None}


def _reported(smells: pd.DataFrame, tool: str, stype: str, level: str) -> pd.DataFrame:
    s = smells[(smells["tool"] == tool) & (smells["type"] == stype) & (smells["level"] == level)]
    return s


def agreement(model, smells: pd.DataFrame, tool: str, th: Thresholds) -> dict:
    """Agreement of in-memory detection (with ``th``) with one detector's smells."""
    det = detect_model(model, include_tests=True, th=th)
    pkg_nodes = set(model.packages["package"])
    cls_nodes = set(model.types["type"])
    out = {}
    for level, nodes in (("package", pkg_nodes), ("class", cls_nodes)):
        rep = _reported(smells, tool, "cyclic_dependency", level)
        if len(rep):
            out[f"cyclic_dependency:{level}"] = _cycle_agreement(
                [json.loads(m) for m in rep["members"]],
                list(det[(det["type"] == "cyclic_dependency") & (det["level"] == level)]["members"]), nodes)
    for stype, level, nodes in (("hub_like_dependency", "package", pkg_nodes), ("hub_like_dependency", "class", cls_nodes),
                                ("unstable_dependency", "package", pkg_nodes), ("god_component", "package", pkg_nodes)):
        rep = _reported(smells, tool, stype, level)
        if not len(rep):
            continue
        truth = {c for c in rep["central"].fillna("") if c in nodes}
        found = set(det[(det["type"] == stype) & (det["level"] == level)]["central"])
        out[f"{stype}:{level}"] = _prf(found, truth)
    return out


GRID = {
    "unstable_ratio": [0.0, 0.2, 0.3, 0.4, 0.5],
    "hub_quantile": [0.5, 0.75, 0.9],
    "hub_balance": [0.25, 0.5, 1.0],
    "god_classes": [20, 30, 40],
    "god_loc": [None, 5000, 10000, 20000, 40000],
}


def _score(result: dict, key: str) -> float:
    r = result.get(key)
    return -1.0 if not r or r.get("f1") is None else r["f1"]


def calibrate(model, smells: pd.DataFrame) -> dict:
    """Default agreement per detector plus a threshold search per smell type."""
    report: dict = {}
    for tool, base in TOOL_THRESHOLDS.items():
        if not (smells["tool"] == tool).any():
            continue
        tool_report = {"thresholds": thresholds_dict(base), "agreement": agreement(model, smells, tool, base),
                       "agreement_pipeline_thresholds": agreement(model, smells, tool, PIPELINE)}
        best = {}
        # unstable dependency
        if "unstable_dependency:package" in tool_report["agreement"]:
            scores = [(r, _score(agreement(model, smells, tool, replace(base, unstable_ratio=r)),
                                 "unstable_dependency:package")) for r in GRID["unstable_ratio"]]
            best["unstable_ratio"] = max(scores, key=lambda x: x[1])
            tool_report["search_unstable_ratio"] = scores
        # hub-like (package and class level together)
        hub_keys = [k for k in tool_report["agreement"] if k.startswith("hub_like_dependency")]
        if hub_keys:
            scores = []
            for q, b in itertools.product(GRID["hub_quantile"], GRID["hub_balance"]):
                a = agreement(model, smells, tool, replace(base, hub_quantile=q, hub_balance=b))
                scores.append(((q, b), sum(max(_score(a, k), 0) for k in hub_keys) / len(hub_keys)))
            best["hub_quantile, hub_balance"] = max(scores, key=lambda x: x[1])
            tool_report["search_hub"] = scores
        if "god_component:package" in tool_report["agreement"]:
            scores = [(g, _score(agreement(model, smells, tool, replace(base, god_classes=g)), "god_component:package"))
                      for g in GRID["god_classes"]]
            best["god_classes"] = max(scores, key=lambda x: x[1])
            tool_report["search_god_classes"] = scores
            if tool == "arcan":  # Arcan's rule is size based
                scores = [(loc, _score(agreement(model, smells, tool, replace(base, god_classes=10**9, god_loc=loc)),
                                       "god_component:package")) for loc in GRID["god_loc"] if loc]
                best["god_loc"] = max(scores, key=lambda x: x[1])
                tool_report["search_god_loc"] = scores
        tool_report["best"] = best
        report[tool] = tool_report
    return report
