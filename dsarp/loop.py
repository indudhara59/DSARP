"""Repair loop: verify, learn from the failures, plan again, verify again.

Round 1 verifies the recommended plans (unless they are verified already).
After every round, the groups without an accepted fix are planned again: the
planner now reads the lessons store (banned steps and combinations, risky
classes), so it proposes something else, or nothing. Groups whose new
recommendation is the same plan as before are not verified again. The loop
stops when a round adds no new lesson, when no group is left to try, or after
``rounds`` rounds. Every round is frozen before it is verified (see
``freeze``), so its results cannot be tuned afterwards.

Writes ``results/<project>/lessons/loop.json`` (per round: groups planned,
verified, accepted, lessons added).
"""

from __future__ import annotations

import json
from pathlib import Path

from .lessons import Lessons, step_key


def _result(results: Path, gid: str) -> dict | None:
    f = results / "verify" / gid / "result.json"
    return json.loads(f.read_text()) if f.exists() else None


def _rec_steps(g: dict) -> list[str]:
    p = next((x for x in g.get("plans", []) if x["name"] == g.get("recommended")), None)
    return sorted(step_key(o) for o in (p or {}).get("operations", []))


def run(results: str | Path, repo: str | Path, rounds: int = 3, time_limit: float = 60.0, backend: str = "auto",
        verifier_kwargs: dict | None = None, log=print) -> dict:
    from .plan.run import Planner, write
    from .freeze import freeze
    from .verify.run import Verifier, merge_shards
    results, repo = Path(results), Path(repo)
    pj = results / "plans" / "plans.json"
    history, tried = [], {}
    for r in range(1, rounds + 1):
        plans = json.loads(pj.read_text())
        open_groups = [g for g in plans["groups"] if not (_result(results, g["group_id"]) or {}).get("accepted")]
        if r > 1:
            before = Lessons(results / "lessons").summary()
            pl = Planner(results / "model", results / "triage", backend=backend, time_limit=time_limit,
                         log=lambda *_: None, search=plans.get("search", "auto"),
                         api_policy=plans.get("api_policy", "balanced"),
                         max_api_breaks=plans.get("max_api_breaks", -1), compat=plans.get("compat", "stubs"),
                         no_widening=plans.get("no_widening", False))
            ids = {g["group_id"] for g in open_groups}
            rows = pl.groups[pl.groups["group_id"].isin(ids)]
            new = {row["group_id"]: pl.plan_group(row) for _, row in rows.iterrows()}
            changed = {gid for gid, g in new.items()
                       if _rec_steps(g) != tried.get(gid, _rec_steps(next(x for x in plans["groups"]
                                                                          if x["group_id"] == gid)))}
            if changed:                        # keep the frozen plans file when the lessons changed nothing
                plans["groups"] = [new.get(g["group_id"], g) for g in plans["groups"]]
                write(plans, results / "plans")
            log(f"round {r}: planned {len(new)} groups again with the lessons ({before}); "
                f"{len(changed)} recommendations changed")
        todo = set()
        for g in plans["groups"]:
            if not g.get("recommended") or (_result(results, g["group_id"]) or {}).get("accepted"):
                continue
            steps = _rec_steps(g)
            if tried.get(g["group_id"]) == steps:
                continue                       # the same plan as last round: nothing new to learn
            if r == 1 and _result(results, g["group_id"]) and _result(results, g["group_id"]).get("plan") == \
                    g["recommended"]:
                tried[g["group_id"]] = steps   # verified before the loop started
                continue
            todo.add(g["group_id"])
            tried[g["group_id"]] = steps
        if not todo:
            log(f"round {r}: nothing new to verify")
            history.append({"round": r, "planned_again": 0 if r == 1 else len(open_groups), "verified": 0,
                            "accepted": 0, "lessons_added": 0})
            break
        freeze(results, round_=r)
        v = Verifier(results, repo, log=log, **(verifier_kwargs or {}))
        recs = v.run(only=todo)
        merge_shards(results)
        acc = sum(1 for x in recs if x.get("accepted"))
        history.append({"round": r, "planned_again": 0 if r == 1 else len(open_groups), "verified": len(recs),
                        "accepted": acc, "lessons_added": v.lessons.added})
        log(f"round {r}: verified {len(recs)}, accepted {acc}, new lessons {v.lessons.added}")
        if r > 1 and v.lessons.added == 0:
            break
    out = {"rounds": history, "lessons": Lessons(results / "lessons").summary(),
           "accepted_total": sum(1 for g in json.loads(pj.read_text())["groups"]
                                 if (_result(results, g["group_id"]) or {}).get("accepted"))}
    (results / "lessons").mkdir(parents=True, exist_ok=True)
    (results / "lessons" / "loop.json").write_text(json.dumps(out, indent=1))
    return out
