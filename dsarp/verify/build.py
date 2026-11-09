"""Build the affected modules with the project's build tool and run the selected tests.

Regression test selection: test classes that use a changed class directly
or through one other class (from the program model, tests included) are run;
nothing else. Maven runs offline (``-o``) so that it works on compute nodes
after the dependencies were downloaded once on a login node
(``mvn dependency:go-offline``); Gradle uses ``--offline``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..program import ProgramModel


@dataclass
class BuildResult:
    tool: str
    ok: bool
    command: list[str]
    seconds: float
    log_tail: str = ""
    tests: list[str] = field(default_factory=list)


def select_tests(model: ProgramModel, changed: set[str], hops: int = 1) -> list[str]:
    """Test classes that depend on a changed class (directly or through ``hops`` other classes)."""
    t = model.types
    top = dict(zip(t["type"], t["top_level"]))
    is_test = dict(zip(t["type"], t["is_test"].astype(str).str.lower().isin(["true", "1"])))
    d = model.deps
    pairs = pd.DataFrame({"s": d["source_type"].map(top), "t": d["target_type"].map(top)}).dropna().drop_duplicates()
    users: dict[str, set[str]] = {}
    for s, tt in zip(pairs["s"], pairs["t"]):
        users.setdefault(tt, set()).add(s)
    frontier, seen = set(changed), set(changed)
    for _ in range(hops + 1):
        nxt = set()
        for c in frontier:
            nxt |= users.get(c, set())
        nxt -= seen
        seen |= nxt
        frontier = nxt
    return sorted(c for c in seen if is_test.get(c))


def detect_tool(repo: Path, tool: str = "auto") -> str | None:
    if tool != "auto":
        return tool
    if (repo / "pom.xml").exists():
        return "maven"
    if any((repo / f).exists() for f in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
        return "gradle"
    return None


def build(repo: str | Path, modules: list[str], tests: list[str], tool: str = "auto", timeout: int = 3600,
          offline: bool = True) -> BuildResult:
    repo = Path(repo)
    tool = detect_tool(repo, tool)
    if tool is None:
        return BuildResult("none", False, [], 0.0, "no pom.xml or Gradle build found")
    simple = sorted({t.rsplit(".", 1)[-1] for t in tests})
    if tool == "maven":
        exe = "./mvnw" if (repo / "mvnw").exists() else (shutil.which("mvn") or "mvn")
        cmd = [exe, "-B", "-q"] + (["-o"] if offline else [])
        mods = [m for m in modules if m and m != "."]
        if mods:
            cmd += ["-pl", ",".join(sorted(set(mods))), "-am"]
        if simple:
            cmd += ["test", "-Dtest=" + ",".join(simple), "-Dsurefire.failIfNoSpecifiedTests=false",
                    "-DfailIfNoTests=false", "-Djacoco.skip=true", "-Drat.skip=true", "-Dcheckstyle.skip=true",
                    "-Dspotbugs.skip=true", "-Dforbiddenapis.skip=true", "-Denforcer.skip=true"]
        else:
            cmd += ["test-compile", "-Drat.skip=true", "-Dcheckstyle.skip=true", "-Denforcer.skip=true"]
    else:
        exe = "./gradlew" if (repo / "gradlew").exists() else (shutil.which("gradle") or "gradle")
        cmd = [exe, "--console=plain", "-q"] + (["--offline"] if offline else [])
        tasks = [f":{m.replace('/', ':')}:" if m and m != "." else ":" for m in modules] or [":"]
        if simple:
            for tk in tasks:
                cmd += [tk + "test"] + [x for s in simple for x in ("--tests", f"*{s}")]
        else:
            cmd += [tk + "compileTestJava" for tk in tasks]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout)
        ok, log = r.returncode == 0, (r.stdout + r.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        ok, log = False, str(e)
    return BuildResult(tool, ok, cmd, round(time.time() - t0, 1), "\n".join(log.splitlines()[-40:]), tests)
