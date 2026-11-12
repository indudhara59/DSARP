"""Re-run the architectural smell detectors on the refactored code.

Arcan and Designite are not shipped with DSARP (licences, versions). Give the
command that runs a detector, with ``{repo}`` (source folder) and ``{out}``
(output folder) as placeholders, e.g.::

    --arcan-cmd "java -jar /opt/arcan/arcan.jar -i {repo} -o {out} ..."
    --designite-cmd "java -jar DesigniteJava.jar -i {repo} -o {out}"

The output is read with the same adapters as in stage 0, so the smells
before (the original detector output) and after can be compared.
"""

from __future__ import annotations

import shlex
import subprocess
from collections import Counter
from pathlib import Path

from ..config import DetectorInput
from ..detectors import ADAPTERS


def execute(command: str, repo: str | Path, out: str | Path, timeout: int = 7200) -> tuple[bool, str]:
    """Run a detector command with ``{repo}`` and ``{out}`` filled in; returns (ok, log tail)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cmd = command.format(repo=str(repo), out=str(out))
    try:
        r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        (out / "detector.log").write_text(str(e))
        return False, str(e)
    (out / "detector.log").write_text(r.stdout + r.stderr)
    return r.returncode == 0, "\n".join((r.stdout + r.stderr).splitlines()[-10:])


def run_detector(tool: str, command: str, repo: str | Path, out: str | Path, timeout: int = 7200,
                 groups: list[dict] | None = None) -> dict:
    """Run a detector on ``repo``; smell counts and, for each of ``groups``, whether its smell is still reported."""
    ok, tail = execute(command, repo, out, timeout)
    if not ok:
        return {"tool": tool, "ok": False, "error": tail}
    res = ADAPTERS[tool](DetectorInput(tool=tool, path=Path(out)))
    counts = Counter(s.smell_type.value for s in res.smells)
    out_d = {"tool": tool, "ok": True, "smells": dict(counts), "total": len(res.smells), "warnings": res.warnings[:5]}
    if groups:
        out_d["groups"] = {g["group_id"]: still_reported(g, res.smells) for g in groups}
    return out_d


def still_reported(group: dict, smells: list, renamed: dict[str, str] | None = None) -> bool | None:
    """Whether a detector's output still contains the group's smell (``None``: the tool does not detect this type).

    Cycles: some cycle of that level still contains two of the group's elements (or, for tools that only name
    the class, one of its classes). Scattered functionality: a smell over at least two of its packages. Other
    types: a smell of that type centred on (or naming) the group's central element."""
    renamed = renamed or {}
    kind = group.get("kind") or group["type"]
    level = "class" if kind.endswith(":class") else "package"
    stype = kind.split(":")[0]
    core = {renamed.get(c, c) for c in group.get("core", [])}
    central = renamed.get(group.get("central", ""), group.get("central", ""))
    same = [s for s in smells if s.smell_type.value == stype and
            (s.level.value == level or (level == "package" and s.level.value == "system"))]
    if not same:
        return None if not any(s.smell_type.value == stype for s in smells) else False
    if stype == "cyclic_dependency":
        need = 1 if level == "class" and all(len(s.members) <= 1 for s in same) else 2
        return any(len(core & set(s.members)) >= need for s in same)
    if stype == "scattered_functionality":
        return any(len(core & set(s.members)) >= 2 for s in same)
    return any(s.central == central or central in s.members for s in same if central)
