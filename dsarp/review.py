"""Stage 6b (optional): design review of the recommended plans by a language model.

The solver, the simulation and the checks decide whether a plan removes a
smell safely; they cannot judge whether a developer of the project would
accept *where* code ends up (for example a media-type class moved into a
metadata package). The review asks a language model, as an experienced
architect of the project, to judge every step of the recommended plan:

* ``sensible``: the element belongs at its new place;
* ``questionable``: possible, but a developer may disagree;
* ``wrong``: the element clearly does not belong there.

The model only judges; it cannot add, change or reorder steps. A plan
rejected by the review is replaced by the next acceptable plan of the group
(by score) that the review does not reject; if none is left, the group gets
no recommendation and the reason. Answers that do not follow the expected
format are ignored (the plan stays as it is).

The model is reached through any OpenAI-compatible endpoint
(``DSARP_LLM_URL``, ``DSARP_LLM_MODEL``, ``DSARP_LLM_KEY``), for example the
University of Paderborn AI gateway (``https://ai-gateway.uni-paderborn.de/v1``)
or a model served with vLLM on a GPU node. The key is read from the
environment only and never written to any file. Answers are cached in
``plans/review_cache.json`` (keyed by the prompt) so that a re-run costs nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .search.llm import LLMError, extract_json

VERDICTS = ("sensible", "questionable", "wrong")
OVERALL = ("accept", "caution", "reject")
MAX_TRIES = 3            # plans reviewed per group at most (recommended + 2 alternatives)

SYSTEM = (
    "You are a senior software architect reviewing refactoring proposals for a Java code base. "
    "A tool proposes steps that remove an architectural smell; all steps already compile and remove the smell. "
    "Judge only whether each step puts the code where an experienced developer of this project would accept it: "
    "does the class or member belong to its new package or class (same concern, same layer, sensible name), "
    "and does the step avoid breaking a public contract that users of the library rely on? "
    "Answer with JSON only.")

FORMAT = (
    'Answer with exactly this JSON: {"steps": [{"step": 1, "verdict": "sensible|questionable|wrong", '
    '"reason": "at most 25 words"}, ...one entry per step...], "overall": "accept|caution|reject", '
    '"reason": "at most 40 words"}. Use "reject" only if at least one step is clearly wrong.')


def _simple(name: str) -> str:
    return name.rsplit(".", 1)[-1]


class Reviewer:
    def __init__(self, rf, llm, cache_file: Path | None = None, log=print):
        self.rf, self.llm, self.log = rf, llm, log
        self.cache_file = cache_file
        self.cache: dict[str, str] = {}
        if cache_file and cache_file.exists():
            try:
                self.cache = json.loads(cache_file.read_text())
            except json.JSONDecodeError:
                self.cache = {}
        self.calls = 0

    # ------------------------------------------------------------------ prompt
    def _pkg_view(self, pkg: str, n: int = 14) -> str:
        names = sorted(_simple(c) for c in self.rf.pkg_classes.get(pkg, set()))
        more = f" (+{len(names) - n} more)" if len(names) > n else ""
        return ", ".join(names[:n]) + more if names else "(new package)"

    def _class_view(self, c: str) -> str:
        rf = self.rf
        deps = sorted(rf.out_edges.get(c, []), key=lambda k: -rf.edges[k].weight)[:6]
        users = sorted(rf.in_edges.get(c, []), key=lambda k: -rf.edges[k].weight)[:6]
        return (f"uses {', '.join(_simple(t) for _, t in deps) or 'nothing in the repo'}; "
                f"used by {rf.users.get(c, 0)} classes, e.g. {', '.join(_simple(s) for s, _ in users) or '-'}")

    def _step(self, i: int, o: dict) -> str:
        op = o["op"]
        if op == "move_class":
            c = o["class"]
            return (f"{i}. Move class {c} from package {o['from_package']} to package {o['to_package']}"
                    + (" (a new package created by this plan)" if o.get("new_package") else "") + ".\n"
                    f"   {_simple(c)} {self._class_view(c)}.\n"
                    f"   Classes now in {o['from_package']}: {self._pkg_view(o['from_package'])}.\n"
                    f"   Classes in {o['to_package']}: {self._pkg_view(o['to_package'])}.")
        if op == "move_member":
            mem = o.get("members") or [o.get("member")]
            return (f"{i}. Move static member(s) {', '.join(m.split('#', 1)[-1] for m in mem)} from class "
                    f"{o['from_class']} to class {o['to_class']}.")
        if op == "extract_interface":
            return (f"{i}. Extract an interface from {o.get('provider')} with methods "
                    f"{', '.join(m.split('#', 1)[-1] for m in o.get('methods', [])[:8])}, placed in the package of "
                    f"{o.get('client')}, which then depends on the interface instead.")
        if op == "delete_member":
            return f"{i}. Delete the deprecated, unused member {o.get('member')}."
        if op == "remove_unused_import":
            return f"{i}. Remove the unused import of {o.get('import')} from {o.get('class')}."
        return f"{i}. {op} {json.dumps({k: v for k, v in o.items() if k != 'check'}, default=str)[:200]}"

    def prompt(self, group: dict, plan: dict) -> str:
        smell = group.get("kind") or group["type"]
        lines = [f"Architectural smell: {smell.replace('_', ' ').replace(':class', ' between classes')} "
                 f"involving {', '.join(group.get('core', [])[:12])}"
                 + (f" (central: {group['central']})" if group.get("central") else "") + ".",
                 f"Proposed plan ({len(plan['operations'])} steps):"]
        lines += [self._step(i + 1, o) for i, o in enumerate(plan["operations"])]
        lines.append(FORMAT)
        return "\n".join(lines)

    # ------------------------------------------------------------------ review
    def review(self, group: dict, plan: dict) -> dict | None:
        text = self.prompt(group, plan)
        key = hashlib.sha256((getattr(self.llm, "model", "") + "\n" + text).encode()).hexdigest()
        answer = self.cache.get(key)
        if answer is None:
            try:
                answer = self.llm.complete(SYSTEM, text)
            except LLMError as e:
                self.log(f"  review failed: {e}")
                return None
            self.calls += 1
            self.cache[key] = answer
            self._save_cache()
        return self.parse(answer, len(plan["operations"]))

    @staticmethod
    def parse(answer: str, n_steps: int) -> dict | None:
        try:
            data = extract_json(answer)
        except LLMError:
            return None
        if not isinstance(data, dict) or data.get("overall") not in OVERALL:
            return None
        steps = data.get("steps")
        if not isinstance(steps, list) or len(steps) != n_steps:
            return None
        out = []
        for i, s in enumerate(steps):
            if not isinstance(s, dict) or s.get("verdict") not in VERDICTS:
                return None
            out.append({"step": i + 1, "verdict": s["verdict"], "reason": str(s.get("reason", ""))[:300]})
        overall = data["overall"]
        if overall == "reject" and not any(s["verdict"] == "wrong" for s in out):
            overall = "caution"                     # a rejection must name a wrong step
        if overall == "accept" and any(s["verdict"] == "wrong" for s in out):
            overall = "reject"
        return {"overall": overall, "reason": str(data.get("reason", ""))[:400], "steps": out}

    def _save_cache(self) -> None:
        if self.cache_file:
            self.cache_file.write_text(json.dumps(self.cache, indent=0))


def _acceptable(p: dict) -> bool:
    return bool(p.get("operations")) and (p.get("checks") or {}).get("status") != "blocked" \
        and (p.get("benefit") or 0) > 0 and (p.get("score") or 0) > 0


def review_plans(results: str | Path, llm, groups: int | None = None, apply: bool = True, log=print) -> dict:
    """Review the recommended plan of every group in ``plans/plans.json`` and update it."""
    from .plan.facts import RepoFacts
    from .program import ProgramModel
    r = Path(results)
    pf = r / "plans" / "plans.json"
    plans = json.loads(pf.read_text())
    rf = RepoFacts.build(ProgramModel(r / "model"), plans.get("api_policy", "balanced"))
    rv = Reviewer(rf, llm, r / "plans" / "review_cache.json", log)
    todo = [g for g in plans["groups"] if g.get("recommended") or g.get("review")]
    if groups:
        todo = todo[:groups]
    stats = {"reviewed": 0, "accept": 0, "caution": 0, "reject": 0, "replaced": 0, "dropped": 0, "failed": 0}
    for g in todo:
        original = g.get("review", {}).get("original") or g.get("recommended")
        if not original:
            continue
        by_name = {p["name"]: p for p in g["plans"]}
        order = [by_name[original]] + [p for p in sorted(g["plans"], key=lambda p: -(p.get("score") or 0))
                                       if p["name"] != original and _acceptable(p)]
        seen, tried, chosen = set(), [], None
        for p in order:
            key = frozenset(json.dumps(o, sort_keys=True, default=str) for o in p["operations"])
            if key in seen:
                continue
            seen.add(key)
            res = rv.review(g, p)
            if res is None:
                stats["failed"] += 1
                tried.append({"plan": p["name"], "overall": None})
                chosen = p                       # no usable answer: keep this plan as the planner ranked it
                break
            tried.append({"plan": p["name"], **res})
            if res["overall"] != "reject" or not apply:
                chosen = p
                break
            if len(tried) >= MAX_TRIES:
                break
        stats["reviewed"] += 1
        last = next((t for t in tried if t.get("plan") == (chosen or {}).get("name")), tried[-1] if tried else {})
        if last.get("overall"):
            stats[last["overall"]] += 1
        g["review"] = {"model": getattr(llm, "model", getattr(llm, "name", "")), "original": original,
                       "tried": tried}
        if apply:
            if chosen is None:
                g["recommended"] = None
                why = "; ".join(f"{t['plan']}: {t.get('reason', '')}" for t in tried if t.get("overall") == "reject")
                g["no_plan_reason"] = f"the design review rejected every acceptable plan ({why})"
                stats["dropped"] += 1
            else:
                if chosen["name"] != original:
                    stats["replaced"] += 1
                g["recommended"] = chosen["name"]
                g.pop("no_plan_reason", None)
        verdict = last.get("overall") or "no answer"
        log(f"{g['group_id']}: {original} -> {verdict}"
            + (f"; now {g['recommended']}" if apply and g.get("recommended") != original else ""))
    plans["review"] = {**stats, "model": getattr(llm, "model", ""), "llm_calls": rv.calls, "applied": apply}
    pf.write_text(json.dumps(plans, indent=1, default=str))
    return plans["review"]
