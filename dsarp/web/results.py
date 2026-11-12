"""Read a results folder into small JSON documents for the dashboard.

Only the standard library is used (csv, json), and every reader tolerates
missing files: a stage that hasn't run yet just leaves its part empty.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from ..outcome import ORDER, outcome, reason

csv.field_size_limit(1 << 26)


_CACHE: dict[tuple[str, str], tuple[tuple[float, int], object]] = {}


def _cached(p: Path, kind: str, load):
    """Parse a file once and keep it until it changes (result files can be large)."""
    try:
        st = p.stat()
    except OSError:
        return None
    key, stamp = (kind, str(p)), (st.st_mtime, st.st_size)
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    val = load(p)
    _CACHE[key] = (stamp, val)
    return val


def _json(p: Path):
    def load(q):
        try:
            return json.loads(q.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return _cached(p, "json", load)


def _csv(p: Path) -> list[dict]:
    def load(q):
        try:
            with q.open(newline="", encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
        except OSError:
            return []
    return _cached(p, "csv", load) or []


def _num(v, cast=float):
    try:
        return cast(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(v) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def overview(res: Path) -> dict:
    """Numbers and chart data across all stages."""
    out: dict = {"available": {}}
    manifest = _json(res / "manifest.json")
    if manifest:
        out["repo"] = {"name": manifest.get("repo"), "commit": manifest.get("commit"),
                       "url": manifest.get("repo_url"), "created": manifest.get("created_utc")}
    smells = _csv(res / "smells.csv")
    if smells:
        out["available"]["ingest"] = True
        by = Counter((s.get("tool", ""), s.get("type", "")) for s in smells)
        out["smells"] = {"total": len(smells),
                         "by_tool_type": [{"tool": t, "type": ty, "n": n} for (t, ty), n in sorted(by.items())],
                         "by_level": dict(Counter(s.get("level", "") for s in smells))}
    model = _json(res / "model" / "model.json")
    if model:
        out["available"]["model"] = True
        c = model.get("counts", {})
        out["model"] = {k: c.get(k) for k in ("java_files", "analysed_files", "types", "members", "dependencies",
                                              "packages", "build_modules", "api_types", "wiring_refs_in_repo")}
        out["model"]["build_system"] = model.get("build_system")
        h = model.get("history") or {}
        out["model"]["history_commits"] = h.get("commits_used")
        out["model"]["cochange_pairs"] = h.get("cochange_pairs")
    tri = _json(res / "triage" / "triage.json")
    if tri:
        out["available"]["triage"] = True
        out["triage"] = {"smells": tri.get("smells"), "decisions": tri.get("decisions", {}),
                         "groups": tri.get("groups"), "groups_by_type": tri.get("groups_by_type", {}),
                         "by_reason": [r for r in tri.get("by_reason", []) if r.get("decision") == "drop"],
                         "baseline": (tri.get("baseline_detection") or {}).get("summary", {})}
    aff = _json(res / "learn" / "affinity.json")
    if aff:
        out["available"]["learn"] = True
        ev = (aff.get("evaluation") or {})
        out["learn"] = {"features": aff.get("features"), "coef": aff.get("coef"), "examples": aff.get("examples"),
                        "evaluation": {k: {m: v.get(m) for m in ("top1", "mrr", "move_top1", "move_mrr", "queries",
                                                                 "move_queries")}
                                       for k, v in ev.items() if isinstance(v, dict) and "top1" in v}}
    plans = _json(res / "plans" / "plans.json")
    if plans:
        out["available"]["plan"] = True
        gs = plans.get("groups", [])
        vj = _json(res / "verify" / "verification.json") or []
        vmap = {v.get("group_id"): v for v in vj if isinstance(v, dict)}
        rows = []
        for g in gs:
            rec = next((p for p in g.get("plans", []) if p.get("name") == g.get("recommended")), None)
            rows.append({"group_id": g.get("group_id"), "type": g.get("type"), "kind": g.get("kind") or g.get("type"),
                         "rank": g.get("rank"),
                         "priority": g.get("priority"), "recommended": g.get("recommended"),
                         "score": rec.get("score") if rec else None, "benefit": rec.get("benefit") if rec else None,
                         "cost": rec.get("change_cost") if rec else None,
                         "new_smells": rec.get("new_smells") if rec else None,
                         "operations": len(rec.get("operations", [])) if rec else 0,
                         "skipped": g.get("skipped"), "reason": reason(g),
                         "outcome": outcome(g, vmap.get(g.get("group_id"))),
                         "verified_plan": (vmap.get(g.get("group_id")) or {}).get("plan"),
                         "fallback_from": (vmap.get(g.get("group_id")) or {}).get("fallback_from")})
        by_kind: dict = {}
        for r in rows:
            k = by_kind.setdefault(r["kind"], {"groups": 0, "full": 0, "partial": 0, "none": 0})
            k["groups"] += 1
            if r["recommended"]:
                k["full" if (r["benefit"] or 0) >= 1 else "partial"] += 1
            else:
                k["none"] += 1
        out["plans"] = {"groups": rows, "planned": sum(1 for r in rows if r["recommended"]), "by_kind": by_kind,
                        "backends": plans.get("backends"), "weights": plans.get("weights"),
                        "by_plan": dict(Counter(r["recommended"] for r in rows if r["recommended"])),
                        "outcomes": {k: v for k, v in sorted(Counter(r["outcome"] for r in rows).items(),
                                                            key=lambda kv: ORDER.index(kv[0]))},
                        "api_policy": plans.get("api_policy"), "frozen_classes": plans.get("frozen_classes")}
    ver = _csv(res / "verify" / "summary.csv")
    if ver:
        out["available"]["verify"] = True
        out["verify"] = [{"group_id": v.get("group_id"), "plan": v.get("plan"), "operations": _num(v.get("operations"), int),
                          "kept": _num(v.get("kept"), int), "compiles": _bool(v.get("compiles")),
                          "target_removed": _bool(v.get("target_removed")),
                          "prediction_matches": _bool(v.get("prediction_matches")),
                          "files_changed": _num(v.get("files_changed"), int), "patch_lines": _num(v.get("patch_lines"), int),
                          "seconds": _num(v.get("seconds"))} for v in ver]
    ev = _csv(res / "evaluation" / "summary.csv")
    if ev:
        out["available"]["evaluate"] = True
        out["evaluation"] = {"summary": [{k: (_num(v) if k != "approach" else v) for k, v in r.items()} for r in ev],
                             "per_group": [{"group_id": r.get("group_id"), "approach": r.get("approach"),
                                            "score": _num(r.get("score")), "new_smells": _num(r.get("new_smells"))}
                                           for r in _csv(res / "evaluation" / "results.csv")]}
    comb = _json(res / "combined" / "combined.json")
    if comb:
        out["available"]["combine"] = True
        out["combined"] = {k: comb.get(k) for k in ("plans_applied", "operations", "compiles", "removed",
                                                   "whole_repository", "files_changed", "patch_lines")}
        out["combined"]["left_out"] = len(comb.get("conflicts", [])) + len(comb.get("interfering", [])) + \
            len(comb.get("plans_dropped_by_compiler", []))
    out["available"]["report"] = (res / "report.html").exists()
    out["available"]["report_md"] = (res / "plans" / "report.md").exists()
    return out


def group(res: Path, gid: str) -> dict | None:
    """Everything about one group: plans, operations, checks, simulation, verification, patch."""
    plans = _json(res / "plans" / "plans.json") or {}
    g = next((x for x in plans.get("groups", []) if x.get("group_id") == gid), None)
    tri = next((r for r in _csv(res / "triage" / "groups.csv") if r.get("group_id") == gid), None)
    if g is None and tri is None:
        return None
    out: dict = {"group_id": gid, "triage": tri, "plan": g}
    vdir = res / "verify" / gid
    if vdir.is_dir():
        out["verification"] = _json(vdir / "verification.json")
        patch = vdir / "plan.patch"
        if patch.exists():
            text = patch.read_text(encoding="utf-8", errors="replace")
            out["patch"] = text[:400_000]
            out["patch_truncated"] = len(text) > 400_000
        recipe = vdir / "rewrite.yml"
        if recipe.exists():
            out["recipe"] = recipe.read_text(encoding="utf-8", errors="replace")[:50_000]
        out["new_errors"] = _csv(vdir / "new_errors.csv")[:200]
    md = res / "plans" / "report.md"
    if md.exists():
        text = md.read_text(encoding="utf-8", errors="replace")
        start = text.find(f": {gid}\n")
        if start >= 0:
            start = text.rfind("\n## ", 0, start) + 1
            end = text.find("\n## ", start + 4)
            out["report_md"] = text[start:end if end > 0 else None].strip()
    return out
