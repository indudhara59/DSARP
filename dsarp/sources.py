"""Find the Java source files of a checked-out repository.

Every ``.java`` file is classified by its source root (derived from its
``package`` line), build module, test or production code, generated code,
and whether it is excluded. Files under ``resources`` folders (templates such
as Maven archetypes) and generated code are excluded from analysis by
default, because they are not part of the code that is compiled as written.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

import pandas as pd

from . import paths

_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", ".idea", ".gradle", ".mvn"}


def read_package(path: Path) -> str:
    """Package declared in a Java file (``""`` for the default package)."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError:
        return ""
    head = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", head))
    m = _PACKAGE.search(head)
    return m.group(1) if m else ""


def _excluded(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def discover(repo: str | Path, exclude: list[str] | None = None, include_tests: bool = True) -> pd.DataFrame:
    """List the Java files of a repository with their classification.

    Columns: ``file`` (repository-relative), ``package``, ``source_root``,
    ``module``, ``is_test``, ``is_test_module``, ``is_generated``,
    ``in_resources``, ``excluded`` and ``reason``.
    """
    repo = Path(repo)
    exclude = list(exclude or [])
    rows = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".java"):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(repo).as_posix()
            pkg = read_package(full)
            root = paths.source_root(rel, pkg)
            module = paths.module_from_root(root) if root is not None else paths.module_of(rel)
            is_test = paths.is_test_root(root) if root is not None else paths.is_test_path(rel)
            generated = paths.is_generated_path(rel)
            in_resources = "/resources/" in ("/" + rel)
            reason = ""
            if name in ("module-info.java",):
                reason = "module descriptor"
            elif generated:
                reason = "generated"
            elif in_resources:
                reason = "resource template"
            elif root is None:
                reason = "package does not match folder"
            elif not include_tests and is_test:
                reason = "test code"
            elif _excluded(rel, exclude):
                reason = "excluded by config"
            rows.append({
                "file": rel, "package": pkg, "source_root": root if root is not None else "",
                "module": module, "is_test": bool(is_test), "is_test_module": paths.is_test_module(module),
                "is_generated": generated, "in_resources": in_resources,
                "excluded": bool(reason), "reason": reason,
            })
    df = pd.DataFrame(rows, columns=["file", "package", "source_root", "module", "is_test", "is_test_module",
                                     "is_generated", "in_resources", "excluded", "reason"])
    return df.sort_values("file").reset_index(drop=True)


def source_roots(files: pd.DataFrame) -> list[str]:
    """Distinct source roots of the included files (repository-relative)."""
    kept = files[~files["excluded"]]
    return sorted({r if r else "." for r in kept["source_root"]})
