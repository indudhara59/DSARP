"""Build the affected modules with the project's build tool and run the selected tests.

Regression test selection: test classes that use a changed class directly
or through one other class (from the program model, tests included) are run;
nothing else. Maven runs offline (``-o``) so that it works on compute nodes
after the dependencies were downloaded once on a login node
(``mvn dependency:go-offline``); Gradle uses ``--offline``.
"""

from __future__ import annotations

import os
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
    ok: bool                       # compiled, and every selected test that ran passed
    command: list[str]
    seconds: float
    log_tail: str = ""
    tests: list[str] = field(default_factory=list)
    compiled: bool = False         # main and test code compiled with the real build (libraries included)
    tests_finished: bool = True    # False: the test run hit the time limit
    tests_run: int = 0
    failing: list[str] = field(default_factory=list)   # failing tests, as Class#method (or Class)
    error: str = ""                # why the build could not run at all

    def as_dict(self) -> dict:
        return {"tool": self.tool, "ok": self.ok, "compiled": self.compiled, "tests_selected": len(self.tests),
                "tests_run": self.tests_run, "tests_finished": self.tests_finished, "failing": self.failing[:50],
                "seconds": self.seconds, "command": " ".join(self.command), "error": self.error,
                "log_tail": self.log_tail[-3000:]}


MAX_TESTS = 2000


def select_tests(model: ProgramModel, changed: set[str], hops: int = 2) -> list[str]:
    """Test classes that depend on a changed class (directly or through ``hops`` other classes)."""
    return select_tests_scoped(model, changed, hops)[0]


def select_tests_scoped(model: ProgramModel, changed: set[str], hops: int = 2) -> tuple[list[str], str]:
    """Tests to run for a change, and why they were chosen.

    * ``uses the changed classes``: test classes that depend on a changed class, directly or through up to
      ``hops`` other classes;
    * plus, when a changed class is named in configuration (wiring) files, every test of its module, since
      such behaviour is reached through configuration, not through code references;
    * when no test depends on the changed classes: every test of the affected modules
      (``whole module (no test uses the changed classes)``);
    * ``none``: the affected modules have no tests at all."""
    t = model.types
    flag = lambda col: dict(zip(t["type"], t[col].astype(str).str.lower().isin(["true", "1"])))
    top = dict(zip(t["type"], t["top_level"]))
    is_test = flag("is_test")
    module = dict(zip(t["type"], t["build_module"].fillna("").astype(str))) if "build_module" in t else {}
    wired = dict(zip(t["type"], pd.to_numeric(t.get("wiring_refs", 0), errors="coerce").fillna(0)))
    d = model.deps
    pairs = pd.DataFrame({"s": d["source_type"].map(top), "t": d["target_type"].map(top)}).dropna().drop_duplicates()
    users: dict[str, set[str]] = {}
    for s_, tt in zip(pairs["s"], pairs["t"]):
        users.setdefault(tt, set()).add(s_)
    frontier, seen = set(changed), set(changed)
    for _ in range(hops + 1):
        nxt = set()
        for c in frontier:
            nxt |= users.get(c, set())
        nxt -= seen
        seen |= nxt
        frontier = nxt
    direct = {c for c in seen if is_test.get(c) and top.get(c, c) == c}
    mods = {module.get(c, "") for c in changed}
    module_tests = {c for c, m in module.items() if m in mods and is_test.get(c) and top.get(c) == c}
    scope = "uses the changed classes"
    if any(wired.get(c, 0) > 0 for c in changed):
        direct |= module_tests
        scope = "uses the changed classes + module tests (classes named in configuration)"
    if not direct:
        direct, scope = module_tests, "whole module (no test uses the changed classes)"
    if not direct:
        return [], "none"
    return sorted(direct)[:MAX_TESTS], scope


def detect_tool(repo: Path, tool: str = "auto") -> str | None:
    if tool != "auto":
        return tool
    if (repo / "pom.xml").exists():
        return "maven"
    if any((repo / f).exists() for f in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
        return "gradle"
    return None


REPORT_DIRS = ("target/surefire-reports", "target/failsafe-reports", "build/test-results")
SKIP_CHECKS = ["-Drat.skip=true", "-Dcheckstyle.skip=true", "-Denforcer.skip=true", "-Djacoco.skip=true",
               "-Dspotbugs.skip=true", "-Dforbiddenapis.skip=true", "-Dmaven.javadoc.skip=true",
               "-Dlicense.skip=true", "-Danimal.sniffer.skip=true", "-Dspotless.check.skip=true"]


OUTPUT_DIRS = ("target/classes", "target/test-classes", "build/classes")
SKIP_PARTS = {".git", "target", "build", "node_modules", ".gradle"}


def offline_default() -> bool:
    return os.environ.get("DSARP_BUILD_OFFLINE", "1").lower() not in ("0", "false", "no")


def _clear_reports(repo: Path, outputs: bool = True) -> None:
    """Remove test reports, and compiled classes: a class file of a moved or deleted source would otherwise
    still be on the class path and hide a broken reference."""
    for d in REPORT_DIRS + (OUTPUT_DIRS if outputs else ()):
        for p in repo.glob(f"**/{d}"):
            if ".git" not in p.parts:
                shutil.rmtree(p, ignore_errors=True)


def test_results(repo: str | Path) -> dict[str, bool]:
    """Every test case in the JUnit XML reports of Maven and Gradle: ``Class#method`` -> passed."""
    import xml.etree.ElementTree as ET
    repo = Path(repo)
    out: dict[str, bool] = {}
    for d in REPORT_DIRS:
        for f in repo.glob(f"**/{d}/**/TEST-*.xml"):
            try:
                root = ET.parse(f).getroot()
            except ET.ParseError:
                out[f"{f.stem[5:]} (unreadable report)"] = False
                continue
            suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
            for su in suites:
                for tc in su.findall("testcase"):
                    if tc.find("skipped") is not None:
                        continue
                    key = f"{tc.get('classname') or su.get('name')}#{tc.get('name')}"
                    ok = tc.find("failure") is None and tc.find("error") is None
                    out[key] = out.get(key, True) and ok          # a re-run of the same test: both must pass
    return out


def test_reports(repo: str | Path) -> tuple[int, list[str]]:
    """Tests run and failing tests (``Class#method``) from the JUnit XML reports of Maven and Gradle."""
    r = test_results(repo)
    return len(r), sorted(k for k, ok in r.items() if not ok)


def _run(cmd: list[str], repo: Path, timeout: int, env: dict | None = None) -> tuple[bool, str, bool]:
    """(success, output, finished in time)."""
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode == 0, r.stdout + r.stderr, True
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return False, out + f"\n[dsarp] stopped after {timeout} s", False
    except FileNotFoundError as e:
        return False, str(e), True


def _maven(repo: Path, offline: bool) -> list[str]:
    exe = "./mvnw" if (repo / "mvnw").exists() else (shutil.which("mvn") or "mvn")
    cmd = [exe, "-B", "-q"] + (["-o"] if offline else [])
    if os.environ.get("DSARP_MAVEN_REPO"):
        cmd.append(f"-Dmaven.repo.local={os.environ['DSARP_MAVEN_REPO']}")
    return cmd


def _gradle(repo: Path, offline: bool) -> list[str]:
    exe = "./gradlew" if (repo / "gradlew").exists() else (shutil.which("gradle") or "gradle")
    return [exe, "--console=plain", "-q"] + (["--offline"] if offline else [])


def build(repo: str | Path, modules: list[str], tests: list[str], tool: str = "auto", timeout: int | None = None,
          offline: bool | None = None, test_timeout: int | None = None) -> BuildResult:
    """Compile the affected modules (main and test code) with the project's own build, then run the selected tests.

    The real build compiles against the libraries, which the compiler check of the verification leaves out. The
    test run has its own time limit; a run that hits it is reported as not finished, not as passed.
    ``offline`` (default ``DSARP_BUILD_OFFLINE``, else yes) uses only downloaded dependencies (compute nodes have
    no internet; see ``dsarp prepare-build``); ``DSARP_MAVEN_REPO`` sets Maven's local repository."""
    repo = Path(repo)
    timeout = timeout or int(os.environ.get("DSARP_BUILD_TIMEOUT", "3600"))
    test_timeout = test_timeout or int(os.environ.get("DSARP_TEST_TIMEOUT", "1800"))
    if offline is None:
        offline = offline_default()
    from .baseline import build_env
    env = build_env(repo)
    tool = detect_tool(repo, tool)
    if tool is None:
        return BuildResult("none", False, [], 0.0, error="no pom.xml or Gradle build found", tests=tests)
    simple = sorted({t.rsplit(".", 1)[-1] for t in tests})
    mods = sorted({m for m in modules if m and m != "."})
    if tool == "maven":
        base = _maven(repo, offline) + (["-pl", ",".join(mods), "-am"] if mods else [])
        compile_cmd = base + ["test-compile"] + SKIP_CHECKS
        test_cmd = base + ["test", "-Dtest=" + ",".join(simple), "-Dsurefire.failIfNoSpecifiedTests=false",
                           "-DfailIfNoTests=false", "-Dsurefire.timeout=600"] + SKIP_CHECKS
    else:
        g = _gradle(repo, offline)
        tasks = [f":{m.replace('/', ':')}:" for m in mods] or [":"]
        compile_cmd = g + [tk + "testClasses" for tk in tasks]
        test_cmd = g + ["--continue"] + [x for tk in tasks for x in [tk + "test"] + [y for s in simple
                                                                                       for y in ("--tests", f"*{s}")]]
    t0 = time.time()
    _clear_reports(repo)
    ok, log, finished = _run(compile_cmd, repo, timeout, env)
    res = BuildResult(tool, False, compile_cmd, 0.0, "\n".join(log.splitlines()[-40:]), tests, compiled=ok)
    if not ok:
        res.error = "the build did not finish in time" if not finished else "compilation failed"
    elif simple:
        ok_t, log_t, finished = _run(test_cmd, repo, test_timeout, env)
        res.command = test_cmd
        res.log_tail = "\n".join(log_t.splitlines()[-40:])
        res.tests_finished = finished
        res.tests_run, res.failing = test_reports(repo)
        if not finished:
            res.error = f"the tests did not finish within {test_timeout} s"
        elif not ok_t and not res.failing:
            res.error = "the test run failed without a test report (see the log)"
        res.ok = finished and ok_t and not res.failing
    else:
        res.ok = True
    res.seconds = round(time.time() - t0, 1)
    return res


def prepare(repo: str | Path, tool: str = "auto", timeout: int = 7200) -> BuildResult:
    """Download everything the build and the test runner need (online, e.g. on a login node), so that the
    verification can build offline on compute nodes: compiles main and test code of all modules and starts the
    test runner without running a test. ``DSARP_MAVEN_REPO`` sets Maven's local repository."""
    repo = Path(repo)
    tool = detect_tool(repo, tool)
    if tool is None:
        return BuildResult("none", False, [], 0.0, error="no pom.xml or Gradle build found")
    if tool == "maven":
        base = _maven(repo, offline=False)
        steps = [base + ["test-compile"] + SKIP_CHECKS,
                 base + ["test", "-Dtest=DsarpNoSuchTest", "-Dsurefire.failIfNoSpecifiedTests=false",
                         "-DfailIfNoTests=false"] + SKIP_CHECKS]
    else:
        exe = "./gradlew" if (repo / "gradlew").exists() else (shutil.which("gradle") or "gradle")
        steps = [[exe, "--console=plain", "testClasses"], [exe, "--console=plain", "test", "--tests", "DsarpNoSuchTest",
                                                           "--continue"]]
    from .baseline import build_env
    env = build_env(repo)
    t0, ok, log = time.time(), True, ""
    for i, cmd in enumerate(steps):
        ok_i, log, finished = _run(cmd, repo, timeout, env)
        if i == 0 and not ok_i:
            ok = False
            break
    _clear_reports(repo)
    return BuildResult(tool, ok, steps[0], round(time.time() - t0, 1), "\n".join(log.splitlines()[-40:]),
                       compiled=ok, error="" if ok else "the build failed (see the log)")


def compare(base: BuildResult, after: BuildResult) -> dict:
    """What a plan did to the build: ``status`` is one of not built, breaks the build, tests did not finish,
    breaks tests, builds (no tests selected) or builds and tests pass."""
    out = {"baseline": base.as_dict(), "after": after.as_dict(), "new_failures": [], "check": "compiler only"}
    if base.tool == "none" or not base.compiled:
        if base.tool == "none":
            why = base.error or "no Maven or Gradle build"
        else:
            why = (f"the original code does not build here with {base.tool} ({base.error or 'build failed'}; "
                   "are the dependencies downloaded? run dsarp prepare-build on a login node)")
        out.update(status="not built", reason=f"{why}: only the compiler check of the sources was done")
        return out
    out["check"] = "build" + (f" and {after.tests_run} tests" if after.tests else "")
    if not after.compiled:
        out.update(status="breaks the build", reason=after.error or "compilation failed")
        return out
    if after.tests and not base.tests_finished:
        out.update(status="builds", check="build",
                   reason=f"the selected tests did not finish on the original code either ({base.error}); "
                          "only the build was compared")
        return out
    if after.tests and not after.tests_finished:
        out.update(status="tests did not finish", reason=after.error)
        return out
    new = sorted(set(after.failing) - set(base.failing))
    out["new_failures"] = new[:50]
    if new:
        out.update(status="breaks tests", reason=f"{len(new)} tests fail that passed before (e.g. {new[0]})")
    elif after.tests and after.tests_run == 0 and base.tests_run > 0:
        out.update(status="breaks tests", reason="the selected tests no longer run")
    elif after.error and not after.failing and after.tests:
        out.update(status="breaks tests", reason=after.error)
    elif after.tests and after.tests_run == 0:
        out.update(status="builds", reason="the selected tests did not run any test case")
    elif after.tests:
        out.update(status="builds and tests pass",
                   reason=f"{after.tests_run} tests ran" + (f"; {len(base.failing)} failed already before"
                                                           if base.failing else ""))
    else:
        out.update(status="builds", reason="no tests use the changed classes")
    return out
