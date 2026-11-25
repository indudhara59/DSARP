"""Freeze plans before they are tested (preregistration).

``plans/preregistration.json`` records, before verification, what will be
tested: the recommended plan of every group (its steps), the target smell, and
the SHA-256 of every input (detector output, smell table, program model,
triage, plans, baseline, lessons). Verify freezes automatically when the
current plans are not frozen yet and records in every result whether its plan
was frozen, so the results cannot be tuned after the tests ran without it
showing. Each round of the repair loop is frozen separately.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

INPUTS = ("smells.csv", "components.csv", "manifest.json", "model", "triage", "learn", "baseline", "lessons",
          "plans/plans.json")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_hashes(results: Path) -> dict[str, str]:
    out = {}
    for name in INPUTS:
        p = results / name
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else []
        for f in files:
            if f.name == "preregistration.json":
                continue
            out[f.relative_to(results).as_posix()] = sha256(f)
    return out


def plans_sha(results: Path) -> str | None:
    p = Path(results) / "plans" / "plans.json"
    return sha256(p) if p.exists() else None


def freeze(results: str | Path, round_: int | None = None) -> dict:
    """Add a frozen snapshot of the current plans to ``plans/preregistration.json`` (if not frozen yet)."""
    from . import __version__
    results = Path(results)
    f = results / "plans" / "preregistration.json"
    reg = json.loads(f.read_text()) if f.exists() else {"snapshots": []}
    cur = plans_sha(results)
    if reg["snapshots"] and reg["snapshots"][-1]["plans_sha256"] == cur:
        return reg["snapshots"][-1]
    plans = json.loads((results / "plans" / "plans.json").read_text())
    meta = {}
    mf = results / "model" / "model.json"
    if mf.exists():
        meta = json.loads(mf.read_text())
    targets = []
    for g in plans["groups"]:
        p = next((x for x in g.get("plans", []) if x["name"] == g.get("recommended")), None)
        if not p:
            continue
        targets.append({"group_id": g["group_id"], "type": g["type"], "plan": p["name"],
                        "steps": [{"op": o.get("op"), "element": o.get("class") or o.get("member") or o.get("client"),
                                   "to": o.get("to_package") or o.get("to_class") or o.get("provider")}
                                  for o in p.get("operations", [])],
                        "predicted": {"benefit": p.get("benefit"), "score": p.get("score"),
                                      "api_breaks": (p.get("checks") or {}).get("api_breaks")}})
    snap = {"frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "round": round_, "dsarp": __version__,
            "commit": meta.get("commit"), "repo": meta.get("repo"), "plans_sha256": cur,
            "settings": {k: plans.get(k) for k in ("api_policy", "max_api_breaks", "compat", "no_widening", "weights",
                                                   "search")},
            "targets": targets, "inputs": input_hashes(results)}
    reg["snapshots"].append(snap)
    f.write_text(json.dumps(reg, indent=1, default=str))
    return snap


def is_frozen(results: str | Path) -> bool:
    f = Path(results) / "plans" / "preregistration.json"
    if not f.exists():
        return False
    reg = json.loads(f.read_text())
    return bool(reg["snapshots"]) and reg["snapshots"][-1]["plans_sha256"] == plans_sha(results)
