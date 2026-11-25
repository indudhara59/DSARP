"""Lessons store: what failed verifications (and developers) taught, so the planner does not repeat it.

Kept per repository in ``results/<project>/lessons/lessons*.json`` (one file per
verification shard; all are read together):

* **banned steps**: a step the compiler check rejected (delta debugging dropped
  it), or the only step of a plan that broke the project's build, its tests or
  the reflection gate; a developer's rejection (web interface) too;
* **banned combinations**: the steps of a plan that broke the build or tests or
  created new smells, when the culprit step is not known: a later plan that
  contains all of them is not recommended again;
* **risky classes**: classes a plan changed when tests failed; moving them
  again costs more in planning;
* **prediction log**: for every verified plan, what the simulation predicted and
  what was measured on the compiled code (for calibration).

The planner reads the store (``Planner``): banned steps and combinations block a
plan with the reason, risky classes add a cost. ``dsarp loop`` re-plans the
groups that have no accepted fix and verifies them again, until a round adds no
new lesson.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def step_key(o: dict) -> str:
    """A stable name for one step: operation, subject and destination."""
    target = o.get("to_package") or o.get("to_class") or o.get("provider") or o.get("import") or ""
    subject = o.get("class") or o.get("client") or ",".join(o.get("members") or [o.get("member", "")])
    return f"{o.get('op')}:{subject}->{target}"


def _moved_classes(ops: list[dict]) -> set[str]:
    out = set()
    for o in ops:
        for k in ("class", "from_class", "owner", "client"):
            if o.get(k):
                out.add(o[k])
    return out


class Lessons:
    def __init__(self, folder: str | Path, tag: str = ""):
        self.folder = Path(folder)
        self.file = self.folder / f"lessons{tag}.json"
        self.data = {"banned": {}, "banned_sets": [], "risky": {}, "log": []}
        for f in sorted(self.folder.glob("lessons*.json")):      # every shard's file
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            self.data["banned"].update(d.get("banned", {}))
            self.data["banned_sets"] += [s for s in d.get("banned_sets", []) if s not in self.data["banned_sets"]]
            for c, v in d.get("risky", {}).items():
                self.data["risky"].setdefault(c, v)
            if f == self.file:
                self.data["log"] = d.get("log", [])
        self.added = 0

    # ------------------------------------------------------------------ reading (planner)
    def banned_reason(self, ops: list[dict]) -> str | None:
        keys = [step_key(o) for o in ops]
        for k in keys:
            if k in self.data["banned"]:
                b = self.data["banned"][k]
                return f"step {k} failed before ({b['reason']}; group {b.get('group', '?')})"
        ks = set(keys)
        for s in self.data["banned_sets"]:
            if set(s["steps"]) <= ks:
                return f"this combination of {len(s['steps'])} steps failed before ({s['reason']})"
        return None

    def risky(self, ops: list[dict]) -> list[str]:
        return sorted(c for c in _moved_classes(ops) if c in self.data["risky"])

    # ------------------------------------------------------------------ writing (verification)
    def ban(self, o: dict, reason: str, group: str, source: str) -> None:
        k = step_key(o)
        if k not in self.data["banned"]:
            self.data["banned"][k] = {"reason": reason, "group": group, "source": source, "time": int(time.time())}
            self.added += 1

    def ban_set(self, ops: list[dict], reason: str, group: str, source: str) -> None:
        if len(ops) == 1:
            self.ban(ops[0], reason, group, source)
            return
        steps = sorted(step_key(o) for o in ops)
        if not any(s["steps"] == steps for s in self.data["banned_sets"]):
            self.data["banned_sets"].append({"steps": steps, "reason": reason, "group": group, "source": source})
            self.added += 1

    def mark_risky(self, ops: list[dict], tests: list[str], group: str) -> None:
        for c in _moved_classes(ops):
            if c not in self.data["risky"]:
                self.data["risky"][c] = {"tests": tests[:5], "group": group}
                self.added += 1

    def learn(self, rec: dict, kept_ops: list[dict], dropped_ops: list[dict]) -> None:
        """Turn one verification result into lessons."""
        gid = rec.get("group_id", "?")
        for o in dropped_ops:
            self.ban(o, "compile errors on the real code (delta debugging dropped it)", gid, "compile")
        st = rec.get("status")
        b = rec.get("build") or {}
        if kept_ops and st == "breaks the build":
            self.ban_set(kept_ops, "broke the project's build", gid, "build")
        elif kept_ops and st in ("breaks tests", "tests did not finish"):
            self.ban_set(kept_ops, f"{st}: {b.get('reason', '')}"[:200], gid, "tests")
            self.mark_risky(kept_ops, b.get("new_failures", []), gid)
        elif kept_ops and st == "made_worse":
            self.ban_set(kept_ops, rec.get("why_not_accepted", "new smells")[:200], gid, "new_smells")
        refl = rec.get("reflection") or {}
        if kept_ops and refl and not refl.get("ok", True):
            stale = {s.split(" in ")[0] for s in refl.get("old_names_in_strings", [])}
            hit = [o for o in kept_ops if o.get("class") in stale] or kept_ops
            self.ban_set(hit, "reflection: " + "; ".join(refl.get("old_names_in_strings", [])
                                                         + refl.get("added_lookups", []))[:200], gid, "reflection")
        if rec.get("kept"):
            self.data["log"].append({"group_id": gid, "plan": rec.get("plan"), "time": int(time.time()),
                                     "predicted_removed": rec.get("predicted_removed"),
                                     "measured_removed": rec.get("target_removed"),
                                     "prediction_matches": rec.get("prediction_matches"),
                                     "status": st, "accepted": rec.get("accepted"), "level": rec.get("level"),
                                     "api_predicted": rec.get("api_predicted"),
                                     "api_measured": (rec.get("api_breaks") or {}).get("count")})

    def reject_by_developer(self, ops: list[dict], reason: str, group: str) -> None:
        for o in ops:
            self.ban(o, f"rejected by a developer: {reason}" if reason else "rejected by a developer", group,
                     "developer")

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        own = {"banned": self.data["banned"], "banned_sets": self.data["banned_sets"], "risky": self.data["risky"],
               "log": self.data["log"]}
        self.file.write_text(json.dumps(own, indent=1))

    def summary(self) -> dict:
        return {"banned_steps": len(self.data["banned"]), "banned_combinations": len(self.data["banned_sets"]),
                "risky_classes": len(self.data["risky"]), "verified_plans_logged": len(self.data["log"])}
