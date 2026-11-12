"""Stage 7b (optional): design advice for groups without a verified plan.

Some smells can only be removed by a design change: new abstractions, a class
split by responsibility, a package structure that does not exist yet. DSARP
cannot verify such changes, so it recommends no plan for them and says why
(``needs a design change``, ``no safe plan``). For these groups a language
model can suggest *directions* for the developers.

Advice is clearly separated from the recommendations:

* it is stored as ``advice`` of the group (``verified: false``) and shown
  under "Design advice (not verified)" in the reports;
* it never changes plans, outcomes or any number in the evaluation;
* the model gets the facts DSARP has (elements, sizes, related classes, the
  reason why no plan was possible) and must name existing classes and packages
  by their fully qualified names and new ones by simple names only; advice
  that names a fully qualified class or package that does not exist is
  discarded (``advice_rejected``).

Answers are cached in ``plans/advice_cache.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .outcome import outcome
from .search.llm import LLMError

SYSTEM = (
    "You are a senior software architect. A tool found an architectural smell in a Java code base that cannot be "
    "removed by moving classes, members or imports alone. Suggest how the developers could redesign the code to "
    "remove it. Be concrete and short. Refer to existing classes and packages by their fully qualified names "
    "exactly as given; give new classes, interfaces or packages simple names only.")

FORMAT = ('Answer with JSON only: {"directions": [{"title": "at most 8 words", "change": "at most 60 words: what to '
          'introduce, split or merge, and which existing classes it affects", "effort": "low|medium|high", '
          '"risk": "at most 25 words"}]} with 1 to 3 directions, best first.')

_FQN = re.compile(r"\b(?:[a-z][a-z0-9_]*\.){2,}[A-Za-z_][\w$]*\b")


class Advisor:
    def __init__(self, rf, llm, cache_file: Path | None = None, log=print):
        self.rf, self.llm, self.log = rf, llm, log
        self.cache_file = cache_file
        self.cache = json.loads(cache_file.read_text()) if cache_file and cache_file.exists() else {}
        self.calls = 0
        self.known = set(rf.package.values()) | set(rf.package) | set(rf.model.types["type"])

    def _facts(self, g: dict) -> str:
        rf = self.rf
        kind = (g.get("kind") or g["type"]).replace("_", " ").replace(":class", " between classes")
        lines = [f"Smell: {kind}.", f"Elements: {', '.join(g.get('core', [])[:15])}."]
        if g.get("central"):
            lines.append(f"Central element: {g['central']}.")
        model = g.get("model") or {}
        for k in ("classes", "sets", "lcc", "size", "methods", "concern_classes", "per_package"):
            if model.get(k) not in (None, "", [], {}):
                lines.append(f"{k.replace('_', ' ')}: {json.dumps(model[k])[:400]}")
        for c in g.get("core", [])[:8]:
            if c in rf.pkg_classes:                           # a package: its classes
                names = sorted(rf.pkg_classes[c])[:25]
                lines.append(f"Classes in {c}: {', '.join(x.rsplit('.', 1)[-1] for x in names)}")
            elif c in rf.package:                             # a class: what it uses and who uses it
                outs = sorted({t for _, t in rf.out_edges.get(c, [])})[:12]
                ins = sorted({s for s, _ in rf.in_edges.get(c, [])})[:12]
                lines.append(f"{c} uses {', '.join(outs) or '-'}; used by {', '.join(ins) or '-'}")
        lines.append(f"Why no automatic fix: {g.get('no_plan_reason') or g.get('skipped') or ''}")
        return "\n".join(lines)

    def advise(self, g: dict) -> dict:
        text = self._facts(g) + "\n" + FORMAT
        key = hashlib.sha256((getattr(self.llm, "model", "") + "\n" + text).encode()).hexdigest()
        answer = self.cache.get(key)
        if answer is None:
            answer = self.llm.complete(SYSTEM, text)
            self.calls += 1
            self.cache[key] = answer
            if self.cache_file:
                self.cache_file.write_text(json.dumps(self.cache, indent=0))
        return self.parse(answer)

    def parse(self, answer: str) -> dict:
        from .search.llm import extract_json
        try:
            data = extract_json(answer)
        except LLMError:
            return {"rejected": "no JSON in the answer"}
        dirs = data.get("directions") if isinstance(data, dict) else None
        if not isinstance(dirs, list) or not dirs:
            return {"rejected": "no directions"}
        out = []
        for d in dirs[:3]:
            if not isinstance(d, dict) or not d.get("change"):
                continue
            item = {k: str(d.get(k, ""))[:500] for k in ("title", "change", "effort", "risk")}
            unknown = [n for n in _FQN.findall(" ".join(item.values())) if n not in self.known
                       and n.rsplit(".", 1)[0] not in self.known]
            if unknown:
                return {"rejected": f"names that do not exist in the code: {', '.join(sorted(set(unknown))[:5])}"}
            out.append(item)
        return {"directions": out} if out else {"rejected": "no usable direction"}


def advise_groups(results: str | Path, llm, groups: int | None = None, log=print) -> dict:
    from .plan.facts import RepoFacts
    from .program import ProgramModel
    r = Path(results)
    pf = r / "plans" / "plans.json"
    plans = json.loads(pf.read_text())
    rf = RepoFacts.build(ProgramModel(r / "model"), plans.get("api_policy", "balanced"))
    adv = Advisor(rf, llm, r / "plans" / "advice_cache.json", log)
    todo = [g for g in plans["groups"] if outcome(g, None) in ("needs a design change", "no safe plan")]
    if groups:
        todo = todo[:groups]
    stats = {"groups": 0, "advised": 0, "rejected": 0, "failed": 0}
    for g in todo:
        stats["groups"] += 1
        try:
            res = adv.advise(g)
        except LLMError as e:
            stats["failed"] += 1
            log(f"{g['group_id']}: no answer ({e})")
            continue
        if "directions" in res:
            g["advice"] = {"verified": False, "model": getattr(llm, "model", ""), **res}
            g.pop("advice_rejected", None)
            stats["advised"] += 1
            log(f"{g['group_id']}: {len(res['directions'])} directions")
        else:
            g.pop("advice", None)
            g["advice_rejected"] = res["rejected"]
            stats["rejected"] += 1
            log(f"{g['group_id']}: advice discarded ({res['rejected']})")
    plans["advice"] = {**stats, "model": getattr(llm, "model", ""), "llm_calls": adv.calls}
    pf.write_text(json.dumps(plans, indent=1, default=str))
    return plans["advice"]
