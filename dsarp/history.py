"""Change history: how often files changed, and changed together (co-change).

Classes that change together tend to belong together; this is one of the
signals used to decide where an element should live. Only history up to the
analysed commit is used. Very large commits (mass reformatting, licence
headers) carry no information about coupling and are skipped.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import pandas as pd


@dataclass
class History:
    commits_used: int
    commits_skipped: int
    changes: pd.DataFrame          # file, changes
    cochange: pd.DataFrame         # file_a, file_b, count
    shallow: bool = False
    warnings: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, errors="replace").stdout


def mine(repo: str | Path, commit: str = "HEAD", max_commits: int = 3000,
         max_files_per_commit: int = 30, min_pair_count: int = 2,
         known_files: set[str] | None = None) -> History:
    """Mine ``git log`` of ``repo`` up to ``commit``.

    Args:
        max_commits: Newest commits to read (history before is ignored).
        max_files_per_commit: Commits touching more Java files are skipped.
        min_pair_count: Co-change pairs seen fewer times are dropped.
        known_files: If given, only these (current) files are counted.
    """
    repo = Path(repo)
    warnings: list[str] = []
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    if shallow:
        warnings.append("repository clone is shallow: co-change history is incomplete")
    log = _git(repo, "log", commit, "--no-merges", "--no-renames", f"-n{max_commits}",
               "--format=@@%H", "--name-only")
    changes: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    used = skipped = 0
    for block in log.split("@@")[1:]:
        lines = [x.strip() for x in block.splitlines()[1:] if x.strip().endswith(".java")]
        if known_files is not None:
            lines = [x for x in lines if x in known_files]
        files = sorted(set(lines))
        if not files:
            continue
        if len(files) > max_files_per_commit:
            skipped += 1
            continue
        used += 1
        changes.update(files)
        pairs.update(combinations(files, 2))
    ch = pd.DataFrame(sorted(changes.items()), columns=["file", "changes"])
    co = pd.DataFrame([(a, b, n) for (a, b), n in pairs.items() if n >= min_pair_count],
                      columns=["file_a", "file_b", "count"]).sort_values(["count", "file_a"], ascending=[False, True])
    return History(used, skipped, ch, co.reset_index(drop=True), shallow, warnings)
