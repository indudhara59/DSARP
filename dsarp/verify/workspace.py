"""Working copies of a checkout at the analysed commit."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


class Workspace:
    """A clean copy of ``repo`` at ``commit`` in ``path`` that can be reset between attempts."""

    def __init__(self, repo: str | Path, path: str | Path, commit: str | None = None):
        self.repo, self.path = Path(repo).resolve(), Path(path).resolve()
        self.commit = commit
        self.git = (self.repo / ".git").exists()

    def __enter__(self) -> "Workspace":
        self.create()
        return self

    def __exit__(self, *exc) -> None:
        self.remove()

    def create(self) -> None:
        if self.path.exists():
            self.remove()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.git:
            _git(self.repo, "worktree", "add", "--detach", "--force", str(self.path), self.commit or "HEAD")
        else:
            shutil.copytree(self.repo, self.path, symlinks=True)
            subprocess.run(["git", "init", "-q", str(self.path)], check=True)
            for args in (("add", "-A"), ("-c", "user.name=dsarp", "-c", "user.email=dsarp@localhost",
                                         "commit", "-qm", "base", "--allow-empty")):
                _git(self.path, *args)

    def reset(self) -> None:
        _git(self.path, "reset", "-q", "--hard")
        _git(self.path, "clean", "-qfd")

    def diff(self) -> str:
        _git(self.path, "add", "-A")
        out = _git(self.path, "diff", "--cached", "-M", "--stat", "--patch").stdout
        _git(self.path, "reset", "-q")
        return out

    def remove(self) -> None:
        if self.git:
            _git(self.repo, "worktree", "remove", "--force", str(self.path), check=False)
            _git(self.repo, "worktree", "prune", check=False)
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
