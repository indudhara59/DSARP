"""Clone a repository and check out the configured commit."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import RepoConfig


class CheckoutError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        raise CheckoutError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).strip()


def checkout(cfg: RepoConfig, workdir: str | Path, full_history: bool = True) -> Path:
    """Clone ``cfg.repo_url`` into ``workdir/<name>`` (or reuse it) at ``cfg.commit``.

    The clone keeps the commit history (needed for co-change) but downloads
    file contents only when needed (``--filter=blob:none``). With
    ``full_history=False`` only the commit itself is fetched.
    """
    if not cfg.commit:
        raise CheckoutError(f"{cfg.name}: no commit configured; set 'commit' in the repo config")
    dest = Path(workdir) / cfg.name
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        if full_history:
            _git("clone", "--no-checkout", "--filter=blob:none", cfg.repo_url, str(dest))
        else:
            _git("init", "-q", str(dest))
            _git("remote", "add", "origin", cfg.repo_url, cwd=dest)
            _git("fetch", "--depth", "1", "origin", cfg.commit, cwd=dest)
    try:
        _git("cat-file", "-e", f"{cfg.commit}^{{commit}}", cwd=dest)
    except CheckoutError:
        _git("fetch", "origin", cfg.commit, cwd=dest)
    _git("-c", "advice.detachedHead=false", "checkout", "--force", cfg.commit, cwd=dest)
    actual = head(dest)
    if not actual.startswith(cfg.commit) and not cfg.commit.startswith(actual):
        raise CheckoutError(f"{cfg.name}: expected commit {cfg.commit}, got {actual}")
    return dest
