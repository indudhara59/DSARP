"""Class moves in the history of a repository.

A *move* is a rename of a Java file, detected by git's rename detection
(``-M``, content similarity of at least 50%), whose file name stays the same
but whose package changes. Moves between build modules that keep the package
are not counted: they do not change the package structure.

Only history up to the analysed commit is used. The moves tell which package
developers chose for a class (the new one) and which one they rejected (the
old one); :mod:`.affinity` uses them as training examples with a hard negative.
"""

from __future__ import annotations

import posixpath
import subprocess
from pathlib import Path

import pandas as pd

COLUMNS = ["commit", "time", "similarity", "old_file", "new_file", "class_name",
           "old_package", "new_package", "old_class", "new_class"]


def package_of(path: str) -> str:
    """Package of a Java file from its path (the part after the source root)."""
    d = posixpath.dirname(path)
    parts = d.split("/")
    for marker in ("java", "src"):
        if marker in parts:
            idx = len(parts) - 1 - parts[::-1].index(marker)
            return ".".join(parts[idx + 1:])
    return ".".join(parts)


def parse(log: str) -> pd.DataFrame:
    rows = []
    commit, when = "", 0
    for line in log.splitlines():
        if line.startswith("@@"):
            commit, _, t = line[2:].partition("|")
            when = int(t or 0)
            continue
        if not line.startswith("R"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        status, old, new = parts
        if not (old.endswith(".java") and new.endswith(".java")):
            continue
        name_old, name_new = posixpath.basename(old)[:-5], posixpath.basename(new)[:-5]
        if name_old != name_new or name_old in ("package-info", "module-info"):
            continue
        po, pn = package_of(old), package_of(new)
        if po == pn:
            continue
        if "/test/" in f"/{old}" or "/test/" in f"/{new}":
            continue
        rows.append([commit, when, int(status[1:] or 0), old, new, name_new, po, pn,
                     f"{po}.{name_old}" if po else name_old, f"{pn}.{name_new}" if pn else name_new])
    return pd.DataFrame(rows, columns=COLUMNS)


def mine(repo: str | Path, commit: str = "HEAD", max_commits: int = 20000) -> pd.DataFrame:
    """Class moves (package changes) in the history of ``repo`` up to ``commit``, newest first."""
    out = subprocess.run(["git", "-C", str(repo), "log", commit, "--no-merges", "-M", "--diff-filter=R",
                          "--name-status", f"-n{max_commits}", "--format=@@%H|%ct"],
                         check=True, capture_output=True, text=True, errors="replace").stdout
    return parse(out)


def latest_per_class(moves: pd.DataFrame) -> pd.DataFrame:
    """The most recent move of every class name chain (a class moved twice counts once)."""
    if moves.empty:
        return moves
    return moves.sort_values("time", ascending=False).drop_duplicates("new_class").reset_index(drop=True)
