"""Baseline: the original code at the commit, built and tested once, before any plan is verified.

A fix can only be proven against a baseline that is complete. This stage builds
every module with the project's own build (offline), runs the **full test
suite** once, and re-runs every failing test up to three times: a test that
passes in a re-run is **flaky** and never counts against a plan; a test that
fails every time **failed already** at the baseline. The baseline is
**partial** when the build fails, the test run does not finish, or a module
with tests produced no test report; then no plan can be an accepted fix
(``--allow-partial-baseline`` overrides this for exploration).

The JDK is chosen from the project's Java level (``maven.compiler.release``,
``source``/``target``, Gradle ``sourceCompatibility`` or toolchain): when
``DSARP_JDK_<level>`` (e.g. ``DSARP_JDK_8``, ``DSARP_JDK_17``) points to a JDK,
the build uses it, otherwise the nearest newer one that is set, otherwise the
default JDK.

Result: ``results/<project>/baseline/baseline.json``.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import build as bld

RERUNS = 3
JACOCO = os.environ.get("DSARP_JACOCO_VERSION", "0.8.12")


def jacoco_goals() -> tuple[str, str]:
    g = f"org.jacoco:jacoco-maven-plugin:{JACOCO}"
    return f"{g}:prepare-agent", f"{g}:report"


# ------------------------------------------------------------------ JDK per repository
def java_level(repo: str | Path) -> int | None:
    """The Java version the project is written for, from its build files (None if not stated)."""
    repo = Path(repo)
    texts = []
    for name in ("pom.xml", "build.gradle", "build.gradle.kts", "gradle.properties"):
        p = repo / name
        if p.exists():
            texts.append(p.read_text(errors="replace"))
    text = "\n".join(texts)
    pats = [r"<maven\.compiler\.release>\s*(\d+(?:\.\d+)?)", r"<release>\s*(\d+(?:\.\d+)?)\s*</release>",
            r"<maven\.compiler\.source>\s*(\d+(?:\.\d+)?)", r"<java\.version>\s*(\d+(?:\.\d+)?)",
            r"<source>\s*(\d+(?:\.\d+)?)\s*</source>", r"languageVersion\s*(?:=|\.set\()\s*JavaLanguageVersion\.of\((\d+)",
            r"sourceCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+(?:[._]\d+)?)",
            r"<javaVersion>\s*(\d+)", r"<javac\.target>\s*(\d+(?:\.\d+)?)"]
    for pat in pats:
        m = re.search(pat, text)
        if m:
            v = m.group(1).replace("_", ".")
            return int(v.split(".")[1]) if v.startswith("1.") else int(float(v))
    return None


def java_home(level: int | None) -> tuple[str | None, int | None]:
    """(JAVA_HOME, version) for a Java level from the DSARP_JDK_<n> variables (None: use the default JDK)."""
    avail = {}
    for k, v in os.environ.items():
        m = re.fullmatch(r"DSARP_JDK_(\d+)", k)
        if m and v and Path(v).exists():
            avail[int(m.group(1))] = v
    if not avail or level is None:
        return None, None
    for n in sorted(avail):
        if n >= level:
            return avail[n], n
    return None, None


def build_env(repo: str | Path) -> dict[str, str] | None:
    """The environment for the project's build: JAVA_HOME set for its Java level when a matching JDK is known."""
    home, _ = java_home(java_level(repo))
    if not home:
        return None
    env = dict(os.environ)
    env["JAVA_HOME"] = home
    env["PATH"] = str(Path(home) / "bin") + os.pathsep + env.get("PATH", "")
    return env


# ------------------------------------------------------------------ modules and reports
def modules_with_tests(repo: Path) -> list[str]:
    """Build modules (relative folders) that contain test sources."""
    out = set()
    for d in repo.glob("**/src/test/java"):
        if any(p in bld.SKIP_PARTS for p in d.parts) or not any(d.glob("**/*.java")):
            continue
        out.add(d.parents[2].relative_to(repo).as_posix() or ".")
    return sorted(out)


def modules_reported(repo: Path) -> list[str]:
    out = set()
    for d in bld.REPORT_DIRS:
        for p in repo.glob(f"**/{d}"):
            if any(p.glob("**/TEST-*.xml")):
                rel = p.relative_to(repo).as_posix()
                out.add(rel[: -len(d)].rstrip("/") or ".")
    return sorted(out)


# ------------------------------------------------------------------ the baseline
def run(repo: str | Path, out_dir: str | Path, tool: str = "auto", timeout: int | None = None,
        reruns: int = RERUNS, log=print, coverage: bool = True) -> dict:
    """Build and test the original code once; write ``baseline.json`` in ``out_dir``."""
    repo, out_dir = Path(repo), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timeout = timeout or int(os.environ.get("DSARP_BASELINE_TIMEOUT", "14400"))
    level = java_level(repo)
    home, jdk = java_home(level)
    env = build_env(repo)
    t0 = time.time()
    tool = bld.detect_tool(repo, tool)
    res = {"tool": tool or "none", "java_level": level, "jdk": jdk, "java_home": home, "complete": False,
           "reasons": [], "tests_run": 0, "failing": [], "flaky": [], "modules_with_tests": [], "modules_reported": [],
           "missing_modules": []}
    if tool is None:
        res["reasons"].append("no pom.xml or Gradle build found")
        return _write(res, out_dir, t0)
    bld._clear_reports(repo)
    if tool == "maven":
        base = bld._maven(repo, offline=bld.offline_default())
        compile_cmd = base + ["test-compile"] + bld.SKIP_CHECKS
        agent, report = jacoco_goals()
        test_cmd = base + ["-fae"] + ([agent] if coverage else []) + ["test"] + ([report] if coverage else []) + [
            "-Dsurefire.timeout=900", "-DfailIfNoTests=false", "-Dmaven.test.failure.ignore=true",
            "-Djacoco.skip=false"] + [x for x in bld.SKIP_CHECKS if "jacoco" not in x]
    else:
        g = bld._gradle(repo, offline=bld.offline_default())
        compile_cmd = g + ["testClasses"]
        test_cmd = g + ["--continue", "test"]
    log(f"baseline: compiling with {tool}" + (f" (JDK {jdk} for Java {level})" if jdk else ""))
    ok, out, finished = bld._run(compile_cmd, repo, timeout, env)
    res["compile_log_tail"] = "\n".join(out.splitlines()[-40:])
    if not ok:
        res["reasons"].append("the original code does not build" + ("" if finished else " in time")
                              + " (are the dependencies downloaded? dsarp prepare-build)")
        return _write(res, out_dir, t0)
    log("baseline: running the full test suite")
    ok, out, finished = bld._run(test_cmd, repo, timeout, env)
    res["test_log_tail"] = "\n".join(out.splitlines()[-40:])
    run_n, failing = bld.test_reports(repo)
    res["tests_run"] = run_n
    res["modules_with_tests"] = modules_with_tests(repo)
    res["modules_reported"] = modules_reported(repo)
    res["missing_modules"] = sorted(set(res["modules_with_tests"]) - set(res["modules_reported"]))
    if not finished:
        res["reasons"].append(f"the test suite did not finish within {timeout} s")
    if res["missing_modules"]:
        res["reasons"].append(f"{len(res['missing_modules'])} modules with tests produced no test report "
                              f"(e.g. {res['missing_modules'][0]}): their tests did not run")
    if run_n == 0:
        res["reasons"].append("no test ran")
    if coverage:
        cov = read_coverage(repo)
        if len(cov):
            cov.to_csv(out_dir / "coverage.csv", index=False)
            lc, lm = int(cov["line_covered"].sum()), int(cov["line_missed"].sum())
            res["coverage"] = {"classes": int(len(cov)), "covered_classes": int((cov["line_covered"] > 0).sum()),
                               "line_ratio": round(lc / max(lc + lm, 1), 4),
                               "modules_with_report": modules_with_coverage(repo)}
        else:
            res["coverage"] = {"classes": 0, "note": "no JaCoCo report (Gradle, or a test setup that overrides "
                                                     "argLine without @{argLine}); coverage is not used"}
    # failing tests: re-run them; one pass makes a test flaky
    still, flaky = set(failing), set()
    for i in range(reruns):
        if not still:
            break
        log(f"baseline: re-running {len(still)} failing tests ({i + 1}/{reruns})")
        passed = rerun(repo, sorted(still), tool, env)
        flaky |= passed
        still -= passed
    res["failing"], res["flaky"] = sorted(still), sorted(flaky)
    res["complete"] = not res["reasons"]
    return _write(res, out_dir, t0)


def rerun(repo: Path, tests: list[str], tool: str, env=None, timeout: int = 3600) -> set[str]:
    """Run the given tests (``Class#method``) again; the ones that pass."""
    if not tests:
        return set()
    bld._clear_reports(repo, outputs=False)
    if tool == "maven":
        sel = ",".join(sorted({t.replace("#", "#") for t in tests}))
        cmd = bld._maven(repo, offline=bld.offline_default()) + [
            "-fae", "test", f"-Dtest={sel}", "-Dsurefire.failIfNoSpecifiedTests=false", "-DfailIfNoTests=false"] \
            + bld.SKIP_CHECKS
    else:
        cmd = bld._gradle(repo, offline=bld.offline_default()) + ["--continue", "test"] + [
            x for t in tests for x in ("--tests", t.replace("#", "."))]
    bld._run(cmd, repo, timeout, env)
    ran = bld.test_results(repo)
    return {t for t in tests if ran.get(t) is True}


def read_coverage(repo: Path) -> "pd.DataFrame":
    """Line coverage per top-level class from JaCoCo's CSV reports (``target/site/jacoco/jacoco.csv``)."""
    import pandas as pd
    frames = []
    for f in repo.glob("**/target/site/jacoco/jacoco.csv"):
        try:
            frames.append(pd.read_csv(f))
        except (OSError, ValueError):
            continue
    if not frames:
        return pd.DataFrame(columns=["class", "line_covered", "line_missed", "ratio"])
    df = pd.concat(frames, ignore_index=True)
    df["class"] = df["PACKAGE"].astype(str) + "." + df["CLASS"].astype(str).str.split(".").str[0]
    g = df.groupby("class").agg(line_covered=("LINE_COVERED", "sum"), line_missed=("LINE_MISSED", "sum")).reset_index()
    g["ratio"] = (g["line_covered"] / (g["line_covered"] + g["line_missed"]).clip(lower=1)).round(4)
    return g


def modules_with_coverage(repo: Path) -> list[str]:
    return sorted((f.parents[3].relative_to(repo).as_posix() or ".")
                  for f in repo.glob("**/target/site/jacoco/jacoco.csv"))


def load_coverage(results: str | Path) -> dict[str, float]:
    """Line coverage ratio per top-level class from the baseline (empty when not measured)."""
    import pandas as pd
    f = Path(results) / "baseline" / "coverage.csv"
    if not f.exists():
        return {}
    df = pd.read_csv(f)
    return dict(zip(df["class"], df["ratio"].astype(float)))


def _write(res: dict, out_dir: Path, t0: float) -> dict:
    res["seconds"] = round(time.time() - t0, 1)
    (out_dir / "baseline.json").write_text(json.dumps(res, indent=1))
    return res


def load(results: str | Path) -> dict | None:
    p = Path(results) / "baseline" / "baseline.json"
    return json.loads(p.read_text()) if p.exists() else None


def as_build_result(b: dict) -> bld.BuildResult:
    """The baseline as the 'before' side of a plan's build comparison (failing and flaky tests are not blamed)."""
    return bld.BuildResult(b.get("tool", "none"), b.get("complete", False), [], b.get("seconds", 0.0),
                           tests=["(full suite)"], compiled=b.get("tool") != "none" and "the original code does not "
                           "build" not in " ".join(b.get("reasons", [])), tests_finished=True,
                           tests_run=b.get("tests_run", 0), failing=sorted(set(b.get("failing", []))
                                                                           | set(b.get("flaky", []))),
                           error="; ".join(b.get("reasons", [])))
