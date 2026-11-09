"""Run the Java program-model extractor (``java-helpers/extractor``)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pandas as pd

PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_DIR = PACKAGE_DIR.parent / "java-helpers" / "extractor"
DEFAULT_JAR = SOURCE_DIR / "build" / "dsarp-extractor.jar"
CONTAINER_JAR = Path("/opt/dsarp/dsarp-extractor.jar")


class ExtractorError(RuntimeError):
    pass


def find_jar(build: bool = True) -> Path:
    """Locate the extractor jar; build it from source if needed and possible.

    Order: ``DSARP_EXTRACTOR_JAR``, the container location, the source tree.
    """
    env = os.environ.get("DSARP_EXTRACTOR_JAR")
    if env and Path(env).exists():
        return Path(env)
    if CONTAINER_JAR.exists():
        return CONTAINER_JAR
    if DEFAULT_JAR.exists():
        newest_src = max((p.stat().st_mtime for p in (SOURCE_DIR / "src").rglob("*.java")), default=0)
        if DEFAULT_JAR.stat().st_mtime >= newest_src:
            return DEFAULT_JAR
    if build and (SOURCE_DIR / "build.sh").exists() and shutil.which("javac"):
        res = subprocess.run(["sh", str(SOURCE_DIR / "build.sh")], capture_output=True, text=True)
        if res.returncode == 0 and DEFAULT_JAR.exists():
            return DEFAULT_JAR
        raise ExtractorError(f"building the extractor failed:\n{res.stdout}\n{res.stderr}")
    raise ExtractorError("extractor jar not found: run java-helpers/extractor/build.sh (needs JDK 17+)"
                         " or set DSARP_EXTRACTOR_JAR")


def _read_tsv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, quoting=3)


def run(repo: str | Path, compile_files: list[str], scan_files: list[str], source_roots: list[str],
        out_dir: str | Path, classpath: str = "", memory: str = "4g", timeout: int | None = None) -> dict:
    """Run the extractor with ``repo`` as working directory.

    Args:
        compile_files: Repository-relative files given to the compiler.
        scan_files: Subset whose contents are recorded.
        source_roots: Folders for resolving types not in ``compile_files``.
        classpath: Optional jars of external libraries (improves resolution).
        memory: Java heap, e.g. ``4g``; large repositories need more.

    Returns the extractor summary as a dict.
    """
    repo, out = Path(repo).resolve(), Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "compile-files.txt").write_text("\n".join(compile_files) + "\n", encoding="utf-8")
    (out / "scan-files.txt").write_text("\n".join(scan_files) + "\n", encoding="utf-8")
    cmd = ["java", f"-Xmx{memory}", "-jar", str(find_jar()),
           "--files", str(out / "compile-files.txt"), "--scan-list", str(out / "scan-files.txt"),
           "--sourcepath", os.pathsep.join(source_roots), "--out", str(out)]
    if classpath:
        cmd += ["--classpath", classpath]
    res = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout)
    (out / "extractor.log").write_text(res.stdout + res.stderr, encoding="utf-8")
    if res.returncode != 0:
        tail = "\n".join((res.stdout + res.stderr).splitlines()[-20:])
        raise ExtractorError(f"extractor failed (exit {res.returncode}):\n{tail}")
    summary = {}
    for line in (out / "summary.properties").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            summary[k] = v
    return summary


def load(out_dir: str | Path) -> dict[str, pd.DataFrame]:
    """Read the extractor's TSV files."""
    out = Path(out_dir)
    tables = {name: _read_tsv(out / f"{name}.tsv") for name in ("files", "types", "members", "deps", "diagnostics")}
    for name, cols in {"types": ["line", "end_line"], "members": ["params", "line", "end_line"],
                       "deps": ["count", "line"], "files": ["lines", "imports"], "diagnostics": ["count"]}.items():
        for c in cols:
            tables[name][c] = pd.to_numeric(tables[name][c], errors="coerce").fillna(0).astype(int)
    for name, cols in {"types": ["abstract", "final", "static", "deprecated"],
                       "members": ["static", "abstract", "final", "deprecated", "override"]}.items():
        for c in cols:
            tables[name][c] = tables[name][c] == "1"
    for name in ("files", "types"):
        tables[name]["file"] = tables[name]["file"].str.replace(r"^\./", "", regex=True)
    return tables
