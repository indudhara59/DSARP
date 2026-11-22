"""Outcome categories of a group (standard library only; used by the report and the web interface)."""

from __future__ import annotations

DESIGN = ("design change", "no dependency on the cycle can be removed", "Arcan's rule", "cannot verify",
          "have no related package")
NOT_A_SMELL = ("at this commit", "in the production code", "the detector also counted")
ORDER = ("removed", "reduced", "makes it worse", "breaks the build", "breaks tests", "tests did not finish",
         "no effect", "not applicable", "planned (not verified)", "no safe plan", "needs a design change",
         "not a smell at this commit")


def quality(r: dict) -> tuple:
    """Order of verified results: something kept, smell removed, improvement, as simulated, share kept."""
    tb, ta = r.get("target_before") or {}, r.get("target_after") or {}
    improved = any(isinstance(v, (int, float)) and isinstance(ta.get(k), (int, float)) and ta[k] < v
                   for k, v in tb.items() if k != "present")
    failed = r.get("status") in ("breaks the build", "breaks tests", "tests did not finish", "made_worse")
    return (bool(r.get("accepted")), not failed, r.get("kept", 0) > 0 and bool(r.get("compiles")),
            bool(r.get("target_removed")), improved,
            bool(r.get("prediction_matches")), r.get("kept", 0) / max(1, r.get("operations", 1)),
            r.get("score") or 0)


def verified_status(v: dict) -> str:
    st = v.get("status")
    if st is None:
        st = ("not_applicable" if not v.get("kept") else "removed" if v.get("target_removed") else
              "reduced" if quality(v)[2] else "no_effect")
    return {"not_applicable": "not applicable", "no_effect": "no effect", "made_worse": "makes it worse"}.get(st, st)


def accepted(v: dict | None) -> bool:
    """A verified plan that removed or reduced the smell, built and passed its tests, created no new smell, applies
    as a patch and stays within the public-API limit."""
    return bool(v and v.get("accepted"))


def reason(g: dict) -> str:
    return g.get("skipped") or g.get("no_plan_reason") or ""


def outcome(g: dict, v: dict | None) -> str:
    """removed / reduced / no effect / not applicable (verified), planned (not verified), or why there is no plan."""
    if g.get("recommended"):
        return verified_status(v) if v else "planned (not verified)"
    why = reason(g)
    if any(k in why for k in DESIGN):
        return "needs a design change"
    if any(k in why for k in NOT_A_SMELL):
        return "not a smell at this commit"
    return "no safe plan"
