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


def run_detector(tool: str, command: str, repo: str | Path, out: str | Path, timeout: int = 7200) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cmd = command.format(repo=str(repo), out=str(out))
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=timeout)
    (out / "detector.log").write_text(r.stdout + r.stderr)
    if r.returncode != 0:
        return {"tool": tool, "ok": False, "error": "\n".join((r.stdout + r.stderr).splitlines()[-10:])}
    res = ADAPTERS[tool](DetectorInput(tool=tool, path=str(out)))
    counts = Counter(s.smell_type.value for s in res.smells)
    return {"tool": tool, "ok": True, "smells": dict(counts), "total": len(res.smells), "warnings": res.warnings}
