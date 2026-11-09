"""Stage 3: triage, grouping, prioritisation and scope.

Takes the merged detector smells (stage 1) and the program model (stage 2)
and decides, for every smell, whether it is worth planning a refactoring:

* **dropped** with a reason when it only involves test code, generated code,
  external libraries, or elements that do not exist at the analysed commit,
  or when a reported cycle does not exist between production classes;
* **report only** for system-wide measures (dense structure);
* **kept** otherwise.

Kept smells are **grouped**: all cycles inside one strongly connected
component of the production dependency graph form one group (they must be
solved together), and the other smells are grouped by the element they are
centred on. Each group gets a **priority** and a **scope**: the elements a
plan may change (members of the group) and the places it may move them to
(neighbouring packages or classes).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import pandas as pd

from . import __version__
from .detect import PIPELINE, detect_model, summary, thresholds_dict, to_records
from .program import ProgramModel

GROUP_PREFIX = {
    "cyclic_dependency": "C", "hub_like_dependency": "H", "unstable_dependency": "U",
    "god_component": "G", "feature_concentration": "F", "scattered_functionality": "S",
    "ambiguous_interface": "A",
}
CONFIRMED_BOOST = 1.2


@dataclass
class TriageResult:
    out: Path
    smells: pd.DataFrame
    groups: pd.DataFrame
    scopes: dict
    manifest: dict


class _Index:
    """Lookups on the program model used by the triage rules."""

    def __init__(self, model: ProgramModel):
        self.model = model
        t = model.types
        self.top = dict(zip(t["type"], t["top_level"]))
        self.type_pkg = dict(zip(t["type"], t["package"]))
        self.type_prod = dict(zip(t["type"], ~t["is_test"] & ~t["is_generated"]))
        self.type_api = dict(zip(t["type"], t["api"]))
        self.type_protected = dict(zip(t["type"], t["protected"]))
        self.type_changes = dict(zip(t["type"], pd.to_numeric(t["changes"], errors="coerce").fillna(0)))
        p = model.packages
        flag = lambda col: p[col].astype(str).str.lower().isin(["true", "1"])  # noqa: E731
        self.pkg_prod = dict(zip(p["package"], ~flag("test_only") & ~flag("generated_only")))
        self.pkg_api = dict(zip(p["package"], pd.to_numeric(p["api_types"], errors="coerce").fillna(0) > 0))
        self.pkg_protected = dict(zip(p["package"], flag("protected")))
        self.pkg_changes = dict(zip(p["package"], pd.to_numeric(p["changes"], errors="coerce").fillna(0)))

        self.pkg_prod_g = model.package_graph(include_tests=False)
        self.pkg_all_g = model.package_graph(include_tests=True)
        self.cls_prod_g = model.type_graph(include_tests=False, collapse_nested=True)
        self.cls_all_g = model.type_graph(include_tests=True, collapse_nested=True)
        self.pkg_prod_scc = self._scc_index(self.pkg_prod_g)
        self.pkg_all_scc = self._scc_index(self.pkg_all_g)
        self.cls_prod_scc = self._scc_index(self.cls_prod_g)
        self.cls_all_scc = self._scc_index(self.cls_all_g)
        self.pagerank = nx.pagerank(self.pkg_prod_g, weight="weight") if self.pkg_prod_g.number_of_nodes() else {}

    @staticmethod
    def _scc_index(g: nx.DiGraph) -> dict[str, int]:
        comps = sorted((sorted(c) for c in nx.strongly_connected_components(g) if len(c) > 1),
                       key=lambda c: (-len(c), c[0]))
        return {n: i for i, c in enumerate(comps) for n in c}

    def map_member(self, name: str, level: str) -> str | None:
        if level == "package":
            return name if name in self.pkg_prod else None
        return self.top.get(name)

    def is_prod(self, name: str, level: str) -> bool:
        return bool(self.pkg_prod.get(name)) if level == "package" else bool(self.type_prod.get(name))

    def package_of(self, name: str, level: str) -> str:
        return name if level == "package" else self.type_pkg.get(name, "")


# ---------------------------------------------------------------- decisions

def _decide(row: pd.Series, ix: _Index) -> dict:
    stype, level = row["type"], row["level"]
    members = json.loads(row["members"]) if isinstance(row["members"], str) else []
    if stype == "dense_structure" or level == "system":
        return {"decision": "report_only", "reason": "system-wide measure, not a single refactoring target"}
    mapped = [m for m in (ix.map_member(x, level) for x in members) if m]
    mapped = list(dict.fromkeys(mapped))
    prod = [m for m in mapped if ix.is_prod(m, level)]
    base = {"members_in_model": len(mapped), "members_in_production": len(prod)}
    if not mapped:
        reason = ("only external libraries" if row.get("external_members", 0) == len(members)
                  else "elements not in the analysed code (other version, external or package hierarchy only)")
        return {**base, "decision": "drop", "reason": reason}
    if not prod:
        gen = all(not ix.is_prod(m, level) for m in mapped) and bool(row.get("generated_only"))
        return {**base, "decision": "drop", "reason": "generated code only" if gen else "test code only"}

    if stype == "cyclic_dependency":
        prod_scc = ix.pkg_prod_scc if level == "package" else ix.cls_prod_scc
        all_scc = ix.pkg_all_scc if level == "package" else ix.cls_all_scc
        counts = Counter(prod_scc[m] for m in prod if m in prod_scc)
        if counts and counts.most_common(1)[0][1] >= 2:
            comp = counts.most_common(1)[0][0]
            return {**base, "decision": "keep", "reason": "", "component": comp}
        all_counts = Counter(all_scc[m] for m in mapped if m in all_scc)
        if all_counts and all_counts.most_common(1)[0][1] >= 2:
            return {**base, "decision": "drop", "reason": "cycle exists only through test code"}
        return {**base, "decision": "drop", "reason": "no cycle between these elements in the analysed code"}

    if stype == "scattered_functionality":
        if len(prod) < 2:
            return {**base, "decision": "drop", "reason": "fewer than two production components involved"}
        return {**base, "decision": "keep", "reason": "", "key": ";".join(sorted(prod))}

    central = row.get("central") or (members[0] if members else "")
    c = ix.map_member(central, level) if central else None
    if not c or not ix.is_prod(c, level):
        return {**base, "decision": "drop", "reason": "central element not in production code"}
    return {**base, "decision": "keep", "reason": "", "key": c}


# ---------------------------------------------------------------- groups

def _quick_fixes(ix: _Index, packages: list[str]) -> list[dict]:
    """Package edges inside a cycle that are caused only by imports or deprecated members."""
    m = ix.model
    pkg_set = set(packages)
    d = m.deps
    prod_types = {t for t, ok in ix.type_prod.items() if ok}
    d = d[d["source_type"].isin(prod_types) & d["target_type"].isin(prod_types)]
    d = d.assign(sp=d["source_type"].map(ix.type_pkg), tp=d["target_type"].map(ix.type_pkg))
    d = d[d["sp"].isin(pkg_set) & d["tp"].isin(pkg_set) & (d["sp"] != d["tp"])]
    deprecated = set(m.members.loc[m.members["deprecated"].astype(str).str.lower().isin(["true", "1"]), "member"])
    deprecated |= set(m.types.loc[m.types["deprecated"].astype(str).str.lower().isin(["true", "1"]), "type"])
    fixes = []
    for (sp, tp), e in d.groupby(["sp", "tp"]):
        kinds = set(e["kind"])
        if kinds == {"import"}:
            fixes.append({"from": sp, "to": tp, "fix": "remove_unused_import",
                          "classes": sorted(set(e["source_type"]))})
            continue
        non_import = e[e["kind"] != "import"]
        src_members = set(non_import["source"])
        if src_members and all(s in deprecated or s.split("#")[0] in deprecated for s in src_members):
            fixes.append({"from": sp, "to": tp, "fix": "only_used_by_deprecated_code",
                          "members": sorted(src_members)})
        elif set(non_import["target"]) <= deprecated:
            fixes.append({"from": sp, "to": tp, "fix": "only_uses_deprecated_code",
                          "members": sorted(set(non_import["target"]))})
    return fixes


def _group_id(stype: str, level: str, key: str) -> str:
    h = hashlib.sha1(key.encode()).hexdigest()[:8]
    return f"{GROUP_PREFIX.get(stype, 'X')}-{level[:3]}-{h}"


def _build_groups(smells: pd.DataFrame, ix: _Index) -> tuple[pd.DataFrame, dict]:
    kept = smells[smells["decision"] == "keep"]
    groups: dict[str, dict] = {}
    pkg_comps: dict[int, list[str]] = {}
    for n, c in ix.pkg_prod_scc.items():
        pkg_comps.setdefault(c, []).append(n)
    cls_comps: dict[int, list[str]] = {}
    for n, c in ix.cls_prod_scc.items():
        cls_comps.setdefault(c, []).append(n)

    for _, s in kept.iterrows():
        stype, level = s["type"], s["level"]
        if stype == "cyclic_dependency":
            comp = int(s["component"])
            members = sorted((pkg_comps if level == "package" else cls_comps)[comp])
            key = ";".join(members)
        else:
            key = s["key"]
            members = key.split(";") if stype == "scattered_functionality" else [key]
        gid = _group_id(stype, level, key)
        g = groups.setdefault(gid, {"group_id": gid, "type": stype, "level": level, "members": members,
                                    "central": "" if stype in ("cyclic_dependency", "scattered_functionality") else key,
                                    "smell_ids": [], "tools": set(), "confirmed": False, "severity": []})
        g["smell_ids"].append(s["smell_id"])
        g["tools"].add(s["tool"])
        g["confirmed"] |= bool(isinstance(s.get("matched_with"), str) and s["matched_with"])
        if pd.notna(s.get("severity")):
            g["severity"].append(float(s["severity"]))

    rows, scopes = [], {}
    pkg_cycle_of = {n: _group_id("cyclic_dependency", "package", ";".join(sorted(pkg_comps[c])))
                    for n, c in ix.pkg_prod_scc.items()}
    for gid, g in groups.items():
        level = g["level"]
        pkgs = sorted({ix.package_of(m, level) for m in g["members"]} - {""})
        if level == "package":
            graph, core = ix.pkg_prod_g, [m for m in g["members"] if m in ix.pkg_prod_g]
        else:
            graph, core = ix.cls_prod_g, [m for m in g["members"] if m in ix.cls_prod_g]
        sub = graph.subgraph(core)
        neighbours = sorted({n for m in core for n in (*graph.predecessors(m), *graph.successors(m))} - set(core))
        core_classes = sorted(t for t, p in ix.type_pkg.items() if p in set(pkgs) and ix.type_prod.get(t)
                              and (level == "package" or ix.top.get(t) in set(core)))
        api = sum(1 for m in g["members"] if (ix.pkg_api if level == "package" else ix.type_api).get(m))
        protected = sum(1 for m in g["members"] if (ix.pkg_protected if level == "package" else ix.type_protected).get(m))
        changes = sum(ix.pkg_changes.get(p, 0) for p in pkgs) if level == "package" else \
            sum(ix.type_changes.get(m, 0) for m in g["members"])
        related = sorted({pkg_cycle_of[p] for p in pkgs if p in pkg_cycle_of} - {gid})
        fixes = _quick_fixes(ix, core) if g["type"] == "cyclic_dependency" and level == "package" else []
        rows.append({
            "group_id": gid, "type": g["type"], "level": level, "central": g["central"],
            "size": len(g["members"]), "members": json.dumps(g["members"]), "packages": json.dumps(pkgs),
            "internal_edges": sub.number_of_edges(),
            "class_edges": int(sum(a.get("class_edges", a.get("refs", 1)) for _, _, a in sub.edges(data=True))),
            "smells": len(g["smell_ids"]), "smell_ids": ";".join(g["smell_ids"]),
            "tools": ";".join(sorted(g["tools"])), "confirmed_by_both": len(g["tools"]) > 1 or g["confirmed"],
            "severity": max(g["severity"]) if g["severity"] else math.nan,
            "pagerank": round(sum(ix.pagerank.get(p, 0.0) for p in pkgs), 6),
            "changes": int(changes), "api_members": api, "protected_members": protected,
            "related_cycle_groups": ";".join(related), "quick_fixes": len(fixes),
            "within_one_package": level == "class" and len(pkgs) == 1,
        })
        scopes[gid] = {"type": g["type"], "level": level, "core": core, "packages": pkgs,
                       "neighbours": neighbours, "core_classes": core_classes, "quick_fixes": fixes}
    return pd.DataFrame(rows), scopes


def _prioritise(groups: pd.DataFrame) -> pd.DataFrame:
    """priority = severity × (0.1 + PageRank) × (1 + change activity) × confirmation boost (all normalised)."""
    if groups.empty:
        return groups.assign(priority=[], rank=[])
    g = groups.copy()
    default_sev = g.groupby("type")["severity"].transform("median")
    sev = g["severity"].fillna(default_sev).fillna(g["severity"].median()).fillna(5.0)
    sev_n = sev / sev.max() if sev.max() > 0 else sev
    pr_n = g["pagerank"] / g["pagerank"].max() if g["pagerank"].max() > 0 else g["pagerank"]
    ch = g["changes"].map(math.log1p)
    ch_n = ch / ch.max() if ch.max() > 0 else ch
    boost = g["confirmed_by_both"].map(lambda b: CONFIRMED_BOOST if b else 1.0)
    within = g["within_one_package"].map(lambda b: 0.5 if b else 1.0)
    g["priority"] = (sev_n * (0.1 + pr_n) * (1 + ch_n) * boost * within).round(6)
    g = g.sort_values(["priority", "size"], ascending=[False, False]).reset_index(drop=True)
    g["rank"] = range(1, len(g) + 1)
    return g


# ---------------------------------------------------------------- run

def triage(ingest_dir: str | Path, model_dir: str | Path, out: str | Path, repo: str = "") -> TriageResult:
    ingest_dir, out = Path(ingest_dir), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    smells = pd.read_csv(ingest_dir / "smells.csv", low_memory=False)
    model = ProgramModel(model_dir)
    ix = _Index(model)

    decisions = [_decide(r, ix) for _, r in smells.iterrows()]
    dec = pd.DataFrame(decisions)
    for col in ("component", "key", "members_in_model", "members_in_production"):
        if col not in dec:
            dec[col] = None
    smells = pd.concat([smells.reset_index(drop=True), dec], axis=1)
    groups, scopes = _build_groups(smells, ix)
    groups = _prioritise(groups)
    gid_of_smell = {sid: r["group_id"] for _, r in groups.iterrows() for sid in r["smell_ids"].split(";")}
    smells["group_id"] = smells["smell_id"].map(gid_of_smell).fillna("")

    detected = detect_model(model, include_tests=False, th=PIPELINE)
    to_records(detected).to_csv(out / "detected_baseline.csv", index=False)

    cols = ["smell_id", "tool", "type", "level", "size", "central", "severity", "confirmed_by", "decision", "reason",
            "members_in_model", "members_in_production", "group_id", "members"]
    smells[cols].to_csv(out / "smells_triaged.csv", index=False)
    groups.to_csv(out / "groups.csv", index=False)
    (out / "scopes.json").write_text(json.dumps(scopes, indent=1), encoding="utf-8")

    by_reason = (smells.groupby(["tool", "decision", "reason"]).size().reset_index(name="n")
                 .to_dict(orient="records"))
    manifest = {
        "dsarp_version": __version__,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": repo, "inputs": {"ingest": str(ingest_dir), "model": str(model_dir)},
        "smells": int(len(smells)),
        "decisions": {k: int(v) for k, v in smells["decision"].value_counts().items()},
        "by_reason": by_reason,
        "groups": int(len(groups)),
        "groups_by_type": {f"{t}:{lv}": int(n) for (t, lv), n in groups.groupby(["type", "level"]).size().items()}
        if len(groups) else {},
        "quick_fixes": int(groups["quick_fixes"].sum()) if len(groups) else 0,
        "baseline_detection": {"thresholds": thresholds_dict(PIPELINE), "summary": summary(detected)},
        "top_groups": groups.head(10)[["rank", "group_id", "type", "level", "size", "priority", "tools"]]
        .to_dict(orient="records") if len(groups) else [],
    }
    (out / "triage.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return TriageResult(out, smells, groups, scopes, manifest)
