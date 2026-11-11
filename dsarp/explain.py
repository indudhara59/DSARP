"""Stage 7: explain the recommended plans step by step.

For every group the report says **what** to change (operation, element, from
where to where), **why** (which package dependencies of the smell each step
removes or adds, and the evidence for the target package: dependencies,
co-changes, names), **how** (the edits found by the checks: package
declaration and file location, imports to update, members to make public,
tests and framework wiring to update) and **what to expect** (the smell before
and after, side effects, the alternatives).

The facts come from the program model and the simulation, so the report is
complete without a language model. If a model is configured, an *explainer*
writes a short summary for developers and a *reviewer* checks it: every
qualified Java name in the summary must belong to the plan or the group,
otherwise the summary is dropped and only the factual report is kept.
"""

from __future__ import annotations

import re
from pathlib import Path

from .plan.facts import RepoFacts
from .plan.simulate import State, apply, initial_state
from .search.llm import LLMError, NoLLM

TITLES = {
    "cyclic_dependency": "Cyclic dependency",
    "unstable_dependency": "Unstable dependency",
    "hub_like_dependency": "Hub-like dependency",
    "god_component": "God component",
    "feature_concentration": "Feature concentration",
    "scattered_functionality": "Scattered functionality",
    "cyclic_dependency:class": "Cyclic dependency between classes",
    "hub_like_dependency:class": "Hub-like class",
}
EXPLAINER = ("You explain refactoring plans to Java developers. Be concrete and brief. Use only the facts given; "
             "do not invent classes, packages or numbers.")
_QUALIFIED = re.compile(r"\b(?:[a-z_][a-z0-9_]*\.){2,}[A-Za-z_][A-Za-z0-9_$]*(?:#[^\s,;)]*\))?")


def _pkg_edges(st: State) -> dict[tuple[str, str], int]:
    g = st.package_graph()
    return {(a, b): d["weight"] for a, b, d in g.edges(data=True)}


def step_effect(rf: RepoFacts, op: dict, base: dict[tuple[str, str], int], focus: set[str]) -> dict:
    """Package dependencies (touching the focus packages) that this step alone removes or adds."""
    after = _pkg_edges(apply(rf, [op]))
    removed = [k for k in base if k not in after]
    added = [k for k in after if k not in base]
    inside = lambda k: k[0] in focus and k[1] in focus  # noqa: E731
    return {"removes": [f"{a} -> {b}" for a, b in sorted(filter(inside, removed))],
            "adds": [f"{a} -> {b}" for a, b in sorted(filter(inside, added))],
            "other_removed": sum(1 for k in removed if not inside(k)),
            "other_added": sum(1 for k in added if not inside(k))}


def evidence(rf: RepoFacts, cls: str, src: str, dst: str) -> str:
    f_new, f_old = rf.features(cls, dst), rf.features(cls, src)
    parts = []
    for name, label in (("structural", "of its dependencies"), ("cochange", "of its co-changes")):
        parts.append(f"{f_new[name]:.0%} {label} are with {dst} (vs {f_old[name]:.0%} with {src})")
    if f_new["semantic"] > f_old["semantic"]:
        parts.append("its name fits the target package better")
    return "; ".join(parts)


def _step_text(rf: RepoFacts, i: int, op: dict, eff: dict) -> list[str]:
    o = op["op"]
    chk = op.get("check", {})
    lines = []
    if o == "move_class" and op.get("new_package"):
        lines.append(f"### Step {i}: Move class `{op['class']}` to the new package `{op['to_package']}`")
        lines.append(f"- **From** `{op['from_package']}` **to** `{op['to_package']}` (new, module "
                     f"`{op.get('module', '')}`)")
        if op.get("reason"):
            lines.append(f"- **Why:** {op['reason']}")
        if op.get("tests"):
            lines.append(f"- **Tests moved with it:** {_names(op['tests'])}")
    elif o == "move_class":
        lines.append(f"### Step {i}: Move class `{op['class']}`")
        lines.append(f"- **From** `{op['from_package']}` **to** `{op['to_package']}` "
                     f"(module `{op.get('module', '')}`, affinity {op.get('affinity') or 0:.2f})")
        if op.get("reason"):
            lines.append(f"- **Why:** {op['reason']}")
        lines.append(f"- **Evidence:** {evidence(rf, op['class'], op['from_package'], op['to_package'])}")
        if op.get("tests"):
            lines.append(f"- **Tests moved with it:** {_names(op['tests'])}")
    elif o == "move_member":
        lines.append(f"### Step {i}: Move static member `{op['member']}`")
        lines.append(f"- **From** class `{op['from_class']}` **to** class `{op['to_class']}`")
        if op.get("reason"):
            lines.append(f"- **Why:** {op['reason']}")
    elif o == "delete_member":
        lines.append(f"### Step {i}: Delete deprecated member `{op['member']}`")
        lines.append("- It is deprecated and has no callers in production code.")
    elif o == "remove_unused_import":
        lines.append(f"### Step {i}: Remove unused import in `{op['class']}`")
        lines.append(f"- `import {op['import']};` is not used in the code.")
    elif o == "extract_interface":
        from .plan.simulate import interface_name
        iface = interface_name(op)
        lines.append(f"### Step {i}: Invert `{op['client']}` -> `{op['provider']}` with an interface")
        lines.append(f"- Create interface `{iface}` in `{op['interface_package']}` with: "
                     + ", ".join(f"`{m.split('#')[-1]}`" for m in op.get("methods", [])))
        lines.append(f"- `{op['provider']}` implements it; `{op['client']}` uses the interface and receives the "
                     "instance from outside (constructor or setter injection).")
    if eff["removes"]:
        lines.append("- **Removes** package dependencies: " + ", ".join(f"`{x}`" for x in eff["removes"]))
    if eff["adds"]:
        lines.append("- **Adds** package dependencies: " + ", ".join(f"`{x}`" for x in eff["adds"]))
    if eff.get("other_removed") or eff.get("other_added"):
        lines.append(f"- Outside the smell: {eff.get('other_removed', 0)} package dependencies disappear, "
                     f"{eff.get('other_added', 0)} new ones appear (other packages now import the new location).")
    edits = chk.get("edits", [])
    if edits:
        lines.append("- **How:**")
        for e in edits:
            lines.append("  - " + _edit_text(e))
    for n in chk.get("notes", []):
        lines.append(f"- **Note ({chk.get('status')}):** {n}")
    return lines


def _names(items, n: int = 8) -> str:
    items = list(items)
    shown = ", ".join(f"`{x}`" for x in items[:n])
    return shown + (f" and {len(items) - n} more" if len(items) > n else "")


def _edit_text(e: dict) -> str:
    k = e["edit"]
    if k == "change_package_declaration":
        return f"change `package` to `{e['package']}` and move the file to `{e['new_file']}`"
    if k == "update_imports":
        return f"update the import in {e['count']} classes: {_names(e['classes'])}"
    if k == "add_import":
        return f"add an import in {e['count']} classes of the old package: {_names(e['classes'])}"
    if k == "update_tests":
        return f"update {e['count']} test classes: {_names(e['classes'])}"
    if k == "make_public":
        return f"make public: {_names(e['elements'])}"
    if k == "update_wiring":
        return "update framework wiring: " + ", ".join(f"`{w['file']}` ({w['kind']})" for w in e["entries"][:5])
    if k == "delete":
        return f"delete `{e['member']}` in `{e['file']}`"
    if k == "remove_import":
        return f"remove `import {e['import']};` from `{e['file']}`"
    if k == "create_interface":
        return f"create `{e['interface']}`"
    if k == "implement_interface":
        return f"`{e['class']}` implements `{e['interface']}` (`{e['file']}`)"
    if k == "replace_type_uses":
        return f"in `{e['class']}` replace uses of `{e['from']}` by `{e['to']}`"
    if k == "create_package":
        return f"create the package `{e['package']}` (module `{e.get('module', '')}`)"
    if k == "move_tests":
        return f"move {e['count']} test classes to `{e['package']}` too: {_names(e['classes'])}"
    if k == "move_member_text":
        return f"cut `{e['member']}` from `{e['from_file']}` and paste it into `{e['to_file']}`"
    if k == "update_references":
        return f"call it as `{e['to']}` in {e['count']} classes: {_names(e['classes'])}"
    return str(e)


def _target_text(t: str, before: dict, after: dict) -> str:
    done = " (**removed**)" if not after.get("present") else ""
    if t == "god_component":
        return f"{before.get('classes')} classes (limit {before.get('limit')}) -> {after.get('classes')}{done}"
    if t == "feature_concentration":
        return (f"{before.get('sets')} unrelated sets in {before.get('classes')} classes, LCC {before.get('lcc')} -> "
                f"{after.get('sets')} sets in {after.get('classes')} classes, LCC {after.get('lcc')} "
                f"(limit 0.2){done}")
    if t == "scattered_functionality":
        return (f"{before.get('methods')} methods use the concern spread over several packages -> "
                f"{after.get('methods')}{done}")
    if t == "cyclic_dependency:class":
        return (f"{before.get('core_classes_on_cycle')} classes on the cycle, {before.get('dependencies_to_cut')} "
                f"dependencies to cut -> {after.get('core_classes_on_cycle')} classes, "
                f"{after.get('dependencies_to_cut')} to cut{done}")
    if t == "cyclic_dependency":
        return (f"{before.get('core_packages_on_cycle')} packages on the cycle, "
                f"{before.get('dependencies_to_cut')} dependencies to cut -> "
                f"{after.get('core_packages_on_cycle')} packages, {after.get('dependencies_to_cut')} to cut"
                + (" (**removed**)" if not after.get("present") else ""))
    if t == "unstable_dependency":
        return (f"depends on {len(before.get('less_stable', []))} less stable packages -> "
                f"{len(after.get('less_stable', []))}" + (" (**removed**)" if not after.get("present") else ""))
    return (f"fan-in {before.get('fan_in')}, fan-out {before.get('fan_out')} -> "
            f"fan-in {after.get('fan_in')}, fan-out {after.get('fan_out')}"
            + (" (**no longer a hub**)" if not after.get("present") else ""))


def allowed_names(group: dict, plan: dict) -> set[str]:
    names = set(group.get("core", [])) | ({group["central"]} if group.get("central") else set())
    for o in plan["operations"]:
        for k in ("class", "from_package", "to_package", "member", "owner", "import", "client", "provider",
                  "interface_package"):
            if o.get(k):
                names.add(o[k])
                names.add(o[k].split("#")[0])
        for e in o.get("check", {}).get("edits", []):
            for k in ("classes", "elements"):
                names |= {x.split("#")[0] for x in e.get(k, [])}
                names |= set(e.get(k, []))
            for k in ("interface", "package", "class"):
                if e.get(k):
                    names.add(e[k])
    return names


def review(text: str, allowed: set[str]) -> list[str]:
    """Qualified names in ``text`` that are not part of the plan (empty list = accepted)."""
    bad = []
    for m in _QUALIFIED.findall(text):
        name = m.rstrip(".")
        if name in allowed or name.split("#")[0] in allowed:
            continue
        if any(a.startswith(name + ".") or a == name for a in allowed):   # a package prefix of a known name
            continue
        bad.append(name)
    return bad


def explain_group(rf: RepoFacts, group: dict, base: dict, llm=None) -> dict:
    out = {"group_id": group["group_id"], "markdown": "", "summary": None, "summary_rejected": []}
    t = group["type"]
    kind = group.get("kind", t)
    lines = [f"## {group['rank']}. {TITLES.get(kind, t)}: {group['group_id']}", ""]
    lines.append(f"- **{'Classes' if kind.endswith(':class') else 'Packages'}:** {_names(group.get('core', []), 12)}")
    if group.get("central"):
        lines.append(f"- **Central {'class' if kind.endswith(':class') else 'package'}:** `{group['central']}`")
    if group.get("skipped"):
        lines += [f"- **Not planned:** {group['skipped']}", ""]
    plan = next((p for p in group.get("plans", []) if p["name"] == group.get("recommended")), None)
    if plan is None:
        if group.get("no_plan_reason"):
            lines.append(f"- **No plan recommended:** {group['no_plan_reason']}")
        elif not group.get("skipped"):
            lines.append("- **No plan recommended:** no candidate removed part of the smell without causing "
                         "more harm (new smells, cost) than benefit. Candidates are listed in `summary.csv`.")
        if group.get("quick_fixes"):
            lines.append(f"- **Quick fixes:** {len(group['quick_fixes'])} (see `plans.json`)")
        out["markdown"] = "\n".join(lines) + "\n"
        return out
    sim = plan["simulation"]
    lines.append(f"- **Recommended plan:** `{plan['name']}` with {len(plan['operations'])} steps, "
                 f"score {plan['score']}, change cost {plan['change_cost']}")
    lines.append(f"- **Effect:** {_target_text(group.get('kind', t), sim['target_before'], sim['target_after'])}")
    if sim["new_smells"]:
        lines.append("- **Side effects (new):** " + "; ".join(
            f"{s['type']} {s.get('central') or ', '.join(s.get('packages', [])[:4])}" for s in sim["new_smells"]))
    if sim["removed_smells"]:
        lines.append(f"- **Other smells removed:** {len(sim['removed_smells'])}")
    lines.append("")
    focus = set(group.get("core", [])) | {o.get("to_package") for o in plan["operations"] if o.get("to_package")}
    if group.get("central"):
        focus.add(group["central"])
    steps = []
    for i, op in enumerate(plan["operations"], 1):
        eff = step_effect(rf, {k: v for k, v in op.items() if k != "check"}, base, focus)
        steps.append(eff)
        lines += _step_text(rf, i, op, eff) + [""]
    others = [p for p in group["plans"] if p["name"] != plan["name"] and p.get("operations")]
    if others:
        lines += ["**Alternatives**", "", "| plan | steps | score | benefit | new smells | cost |", "|---|---|---|---|---|---|"]
        for p in others:
            lines.append(f"| {p['name']} | {len(p['operations'])} | {p.get('score')} | {p.get('benefit')} | "
                         f"{p.get('new_smells')} | {p['change_cost']} |")
        lines.append("")
    if llm is not None and not isinstance(llm, NoLLM):
        facts = "\n".join(lines)
        try:
            text = llm.complete(EXPLAINER, "Write a summary of at most 6 sentences for the developers: why this "
                                           "smell matters here and what the plan does.\n\n" + facts)
            bad = review(text, allowed_names(group, plan))
            if bad:
                out["summary_rejected"] = bad
            else:
                out["summary"] = text.strip()
                lines.insert(2, "> " + text.strip().replace("\n", "\n> ") + "\n")
        except LLMError as e:
            out["summary_rejected"] = [str(e)]
    out["steps"] = steps
    out["markdown"] = "\n".join(lines) + "\n"
    return out


def explain(result: dict, rf: RepoFacts, llm=None) -> dict:
    base = _pkg_edges(initial_state(rf))
    groups = [explain_group(rf, g, base, llm) for g in result["groups"]]
    head = [f"# Refactoring plan: {result.get('repo', '')}", ""]
    rec = sum(1 for g in result["groups"] if g.get("recommended"))
    head.append(f"{len(result['groups'])} smell groups planned, {rec} with a recommended plan. Steps are in the "
                "order they should be applied; each step lists the edits it needs. Build and run the tests "
                "after each step.")
    head.append("")
    return {"groups": groups, "markdown": "\n".join(head) + "\n" + "\n".join(g["markdown"] for g in groups)}


def write(report: dict, out_dir: str | Path) -> Path:
    p = Path(out_dir) / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(report["markdown"])
    return p
