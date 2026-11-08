"""Helpers for source paths: source root, build module, test and generated code.

Works for any project layout. The source root of a file is found from its
package name (``org/apache/x/Foo.java`` under package ``org.apache.x``), so
Maven (``src/main/java``), Gradle and custom layouts such as Cassandra's
``src/java`` or ``tools/stress/src`` are handled the same way.
"""

from __future__ import annotations

import posixpath
import re

# Known source-root tails, most specific first. Removing one of them from a
# source root gives the build module folder.
_ROOT_TAILS = [
    re.compile(r"(?:^|/)src/[A-Za-z0-9_.-]+/(?:java|kotlin|groovy|scala)$"),
    re.compile(r"(?:^|/)src/(?:java|main|test)$"),
    re.compile(r"(?:^|/)(?:test|tests)/[A-Za-z0-9_.-]+$"),
    re.compile(r"(?:^|/)(?:src|java|test|tests)$"),
]
_TEST_SEGMENTS = {"test", "tests", "it", "testfixtures", "integrationtest", "integration-test",
                  "jmh", "e2e", "unit", "distributed", "burn", "microbench", "long", "simulator"}
_GENERATED_MARKERS = ("/target/", "/build/generated", "/generated-sources/",
                      "/generated-test-sources/", "/gen-java/", "/generated/")
# Used only when the package is unknown.
_MAVEN_ROOT = re.compile(r"(?:^|/)src/[A-Za-z0-9_.-]+/(?:java|kotlin|groovy|scala)(?=/)")


def normalize(path: str) -> str:
    """Forward slashes, ``.`` and ``..`` segments resolved, no trailing slash."""
    if not path:
        return ""
    p = path.replace("\\", "/").strip()
    absolute = p.startswith("/")
    p = posixpath.normpath(p)
    if p == ".":
        return ""
    return p if absolute else p.lstrip("/")


def source_root(path: str, package: str | None = None) -> str | None:
    """Folder the package folders start from, or ``None`` if it cannot be found.

    ``core/src/main/java/org/a/B.java`` with package ``org.a`` gives
    ``core/src/main/java``. A file in the default package (``package=""``)
    has its own folder as source root. Without a package, a Maven-style
    ``src/<set>/java`` folder is searched for.
    """
    p = normalize(path)
    if not p:
        return None
    folder = posixpath.dirname(p)
    if package is None:
        m = _MAVEN_ROOT.search(p)
        return p[: m.end()] if m else None
    if package == "":
        return folder
    pkg_dir = package.replace(".", "/")
    if folder == pkg_dir:
        return ""
    if folder.endswith("/" + pkg_dir):
        return folder[: -len(pkg_dir) - 1]
    return None


def _strip_tail(root: str) -> str:
    """Remove a known source-root tail; keeps a leading ``/`` of absolute paths."""
    for tail in _ROOT_TAILS:
        m = tail.search(root)
        if m:
            return root[: m.start()]
    return root


def module_from_root(root: str) -> str:
    """Build module folder for a relative source root (``.`` for the repository root)."""
    return _strip_tail(normalize(root)).strip("/") or "."


def is_test_root(root: str) -> bool:
    """True if a source root holds test code (``src/test/java``, ``test/unit``, ...)."""
    r = normalize(root)
    tail = r[len(_strip_tail(r)):]
    return any(s.lower() in _TEST_SEGMENTS for s in tail.split("/") if s)


def module_of(path: str, package: str | None = None) -> str:
    root = source_root(path, package)
    return module_from_root(root) if root is not None else "."


def is_test_path(path: str, package: str | None = None) -> bool:
    root = source_root(path, package)
    if root is None:
        folders = normalize(path).lower().split("/")[:-1]
        return any(s in ("test", "tests") for s in folders)
    return is_test_root(root)


_TEST_MODULE = re.compile(r"(?:^|[-_.])(?:i?tests?|testing|testkit|it|itests|test-?support)(?:$|[-_.])", re.I)


def is_test_module(module: str) -> bool:
    """True for modules that hold test support code under ``src/main``.

    Examples: ``log4j-core-test``, ``itests/test``, ``tika-integration-tests``.
    Only the module's own folder names are checked, not its parents'.
    """
    if not module or module == ".":
        return False
    return any(_TEST_MODULE.search(seg) for seg in module.split("/"))


def is_generated_path(path: str) -> bool:
    p = "/" + normalize(path) + "/"
    return any(marker in p for marker in _GENERATED_MARKERS)


def common_dir_prefix(paths: list[str]) -> str:
    """Longest common directory prefix of paths of the same kind (all absolute or all relative)."""
    items = [normalize(p) for p in paths if p]
    if not items or len({p.startswith("/") for p in items}) > 1:
        return ""
    return posixpath.commonpath(items)


def infer_repo_root(files_with_packages: list[tuple[str, str | None]]) -> str:
    """Guess the repository root from absolute file paths and their packages.

    Takes the module folder of every file and returns their common prefix.
    With a single module this is the module folder itself, which is then
    also the repository root. Pass the files of *all* detector runs of one
    repository together, so per-module runs share the same root.
    """
    modules = []
    for f, pkg in files_with_packages:
        root = source_root(f, pkg)
        if root is not None:
            modules.append(_strip_tail(root) or root)
    if not modules:
        return common_dir_prefix([posixpath.dirname(normalize(f)) for f, _ in files_with_packages])
    return common_dir_prefix(modules)


def relative_to(path: str, root: str) -> str:
    """Strip ``root`` from ``path`` if it is a prefix; otherwise return ``path`` normalized."""
    p, r = normalize(path), normalize(root)
    if r and (p == r or p.startswith(r + "/")):
        return p[len(r):].lstrip("/")
    return p
