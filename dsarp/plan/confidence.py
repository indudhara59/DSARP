"""Confidence of a plan and of each of its steps.

Three factors, each between 0 and 1:

* **calibration**: how often plans of this smell type were accepted when they were verified (the lessons
  log), with a uniform prior: (1 + accepted) / (2 + verified). Without any verification it is 0.5.
* **agreement**: the share of the plan's steps that other candidate plans of the same group (other planners:
  CP-SAT, HiGHS, merge plans, search) also contain. A step that several planners chose independently is less
  likely to be an artefact of one objective.
* **coverage**: the mean line coverage (baseline run, JaCoCo) of the classes the plan changes; a change to
  code that no test runs cannot be checked for behaviour.

Agreement and coverage are softened (0.5 + 0.5 x), so that one weak factor halves the confidence instead of
zeroing it; an unknown factor counts 0.75. Confidence = calibration x agreement' x coverage'. It is a ranking
aid and a statement of evidence, not a probability (it becomes one only as far as the calibration is good;
``dsarp stats`` reports the calibration of these values against the verification).
"""

from __future__ import annotations

from ..lessons import step_key

UNKNOWN = 0.75


def soft(x: float | None) -> float:
    return UNKNOWN if x is None else 0.5 + 0.5 * x


def level(v: float) -> str:
    return "high" if v >= 0.5 else "medium" if v >= 0.25 else "low"


def _classes(o: dict) -> list[str]:
    return [o[k] for k in ("class", "from_class", "owner", "client", "provider") if o.get(k)]


def annotate(group: dict, calibration: tuple[float, int], coverage: dict[str, float]) -> None:
    """Add ``confidence`` to every plan with operations and to each of its steps (in place)."""
    plans = [p for p in group.get("plans", []) if p.get("operations")]
    keys = [{step_key(o) for o in p["operations"]} for p in plans]
    cal, n = calibration
    for i, p in enumerate(plans):
        others = [k for j, k in enumerate(keys) if j != i]
        shared = []
        for o in p["operations"]:
            k = step_key(o)
            agree = sum(1 for ks in others if k in ks)
            o["planners_agreeing"] = f"{agree + 1}/{len(plans)}"
            cov = [coverage[c] for c in _classes(o) if c in coverage]
            c = sum(cov) / len(cov) if cov else None
            a = (agree / len(others)) if others else None
            o["confidence"] = round(cal * soft(a) * soft(c), 3)
            shared.append(1.0 if agree else 0.0)
        agreement = (sum(shared) / len(shared)) if others and shared else None
        cov_plan = (p.get("checks") or {}).get("coverage")
        v = round(cal * soft(agreement) * soft(cov_plan), 3)
        p["confidence"] = {"value": v, "level": level(v), "calibration": cal, "verified_before": n,
                           "agreement": None if agreement is None else round(agreement, 3),
                           "coverage": cov_plan, "candidate_plans": len(plans)}
