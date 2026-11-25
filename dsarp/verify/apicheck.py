"""Measured API changes of an accepted fix: japicmp on the compiled classes before and after.

The planner and the Verify stage count public API changes from the source model (``plan/api.py``), which is a
prediction. For accepted fixes Verify measures them: the classes the baseline compiled (the original code) and
the classes the project's build compiled after the plan and its compatibility stubs are packed into two jars
and compared with japicmp (https://siom79.github.io/japicmp/). Counted are binary-incompatible changes to
public types and members: a removed public type counts once, otherwise each incompatible method, constructor
or field, and the type itself when its own declaration changed incompatibly (e.g. a removed interface).

The japicmp jar comes from ``DSARP_JAPICMP_JAR`` or the Maven repository (``dsarp prepare-build`` downloads
it); without it nothing is measured and the predicted count is used.
"""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

JAPICMP = os.environ.get("DSARP_JAPICMP_VERSION", "0.26.5")
ARTIFACT = f"com.github.siom79.japicmp:japicmp:{JAPICMP}:jar:jar-with-dependencies"
MEMBERS = ("methods", "constructors", "fields")


def maven_repo() -> Path:
    return Path(os.environ.get("DSARP_MAVEN_REPO") or Path.home() / ".m2" / "repository")


def japicmp_jar() -> Path | None:
    env = os.environ.get("DSARP_JAPICMP_JAR")
    if env:
        return Path(env) if Path(env).exists() else None
    f = (maven_repo() / "com/github/siom79/japicmp/japicmp" / JAPICMP /
         f"japicmp-{JAPICMP}-jar-with-dependencies.jar")
    return f if f.exists() else None


def fetch_command(maven: list[str]) -> list[str]:
    """The Maven call that downloads the japicmp jar (``maven`` is the project's Maven command, online)."""
    return maven + ["org.apache.maven.plugins:maven-dependency-plugin:3.6.1:get", f"-Dartifact={ARTIFACT}",
                    "-Dtransitive=false"]


def pack(dirs: list[Path], jar: Path) -> int:
    """Pack class directories into one jar (the first directory wins for a class found twice)."""
    jar.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as z:
        for d in dirs:
            for f in sorted(Path(d).rglob("*.class")):
                name = f.relative_to(d).as_posix()
                if name in seen or name.startswith("META-INF/versions/"):
                    continue
                seen.add(name)
                z.write(f, name)
    return len(seen)


def count(xml_file: Path, limit: int = 20) -> dict:
    """Binary-incompatible changes to public types and members in a japicmp XML report."""
    root = ET.parse(xml_file).getroot()
    n, removed, examples = 0, 0, []

    def bad(e) -> bool:
        return e.get("binaryCompatible") == "false"

    for c in root.iter("class"):
        name = c.get("fullyQualifiedName", "?")
        if c.get("changeStatus") == "REMOVED":
            n, removed = n + 1, removed + 1
            examples.append(f"{name} removed")
            continue
        own = c.find("compatibilityChanges")
        if own is not None and any(bad(x) for x in own.findall("compatibilityChange")):
            n += 1
            examples.append(f"{name}: " + ", ".join(sorted({x.get("type", "?") for x in own if bad(x)})))
        for wrap in MEMBERS:
            w = c.find(wrap)
            if w is None:
                continue
            for m in w:
                if bad(m):
                    n += 1
                    examples.append(f"{name}#{m.get('name') or wrap[:-1]} {m.get('changeStatus', '').lower()}")
    return {"count": n, "removed_types": removed, "examples": examples[:limit]}


def run(old_dirs: list[Path], new_dirs: list[Path], out_dir: Path, java: str = "java",
        timeout: int = 1800) -> dict:
    """Compare the compiled classes before and after with japicmp."""
    jar = japicmp_jar()
    if jar is None:
        return {"measured": False, "reason": "japicmp not found (set DSARP_JAPICMP_JAR or run dsarp prepare-build)"}
    if not old_dirs or not new_dirs:
        return {"measured": False, "reason": "no compiled classes to compare"}
    out_dir.mkdir(parents=True, exist_ok=True)
    old, new, xml = out_dir / "before.jar", out_dir / "after.jar", out_dir / "japicmp.xml"
    n_old, n_new = pack(old_dirs, old), pack(new_dirs, new)
    cmd = [java, "-jar", str(jar), "-o", str(old), "-n", str(new), "-a", "public", "-b",
           "--ignore-missing-classes", "--no-annotations", "-x", str(xml)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"measured": False, "reason": f"japicmp did not run ({e.__class__.__name__})"}
    if r.returncode != 0 or not xml.exists():
        return {"measured": False, "reason": "japicmp failed: " + (r.stderr or r.stdout)[-300:]}
    res = count(xml)
    old.unlink(missing_ok=True)
    new.unlink(missing_ok=True)
    return {"measured": True, "tool": f"japicmp {JAPICMP}", "classes_before": n_old, "classes_after": n_new, **res}


def after_dirs(repo: Path, ws: Path, old_dirs: list[Path]) -> list[Path]:
    """Class directories after the plan: the ones the build in the working copy wrote, the original ones for
    modules it did not build."""
    out = []
    for d in old_dirs:
        rel = d.relative_to(repo)
        out.append(ws / rel if (ws / rel).is_dir() else d)
    return out
