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

    def stage(self) -> None:
        """Remember the current state (git index), so that a later diff shows only what changed since."""
        _git(self.path, "add", "-A")

    def diff_since_stage(self) -> str:
        _git(self.path, "add", "-A", "-N")
        out = _git(self.path, "diff", "--stat", "--patch").stdout
        return out

    def apply(self, patch: str | Path) -> bool:
        r = subprocess.run(["git", "-C", str(self.path), "apply", str(Path(patch).resolve())],
                           capture_output=True, text=True)
        return r.returncode == 0

    def apply_check(self, patch: str | Path) -> bool:
        """Whether a patch applies to this (clean) working copy, as ``git apply`` on a checkout would."""
        r = subprocess.run(["git", "-C", str(self.path), "apply", "--check", str(Path(patch).resolve())],
                           capture_output=True, text=True)
        return r.returncode == 0

    def head(self) -> str:
        return _git(self.path, "rev-parse", "HEAD").stdout.strip()

    def commit_all(self, message: str) -> str:
        """Commit everything in the working copy (local to this working copy) and return the commit."""
        _git(self.path, "add", "-A")
        _git(self.path, "-c", "user.name=dsarp", "-c", "user.email=dsarp@localhost", "commit", "-q",
             "--allow-empty", "--no-verify", "-m", message)
        return self.head()

    def reset_to(self, commit: str) -> None:
        _git(self.path, "reset", "-q", "--hard", commit)
        _git(self.path, "clean", "-qfd")

    def diff_between(self, a: str, b: str) -> str:
        return _git(self.path, "diff", "-M", "--stat", "--patch", a, b).stdout

    def remove(self) -> None:
        if self.git:
            _git(self.repo, "worktree", "remove", "--force", str(self.path), check=False)
            _git(self.repo, "worktree", "prune", check=False)
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
