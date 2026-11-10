"""Compile a working copy with the JDK compiler and compare the errors.

The program-model extractor compiles all Java files of the repository
(without the external libraries, which are usually not available). That gives
a stable set of baseline errors (missing libraries). After a plan is applied
the same files are compiled again; an error that was not there before is
caused by the plan: an unresolved moved class, an inaccessible member, a
duplicate class, a missing import. Errors are compared by file (mapped back
through the moves), error code and message (moved class names mapped back),
not by line, because the plan shifts lines.

The compiled code is also used to **detect the smells again** on the real
result of the plan (not the simulation): the dependencies the compiler sees
after the edits give the new package graph.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
import pandas as pd

from .. import detect as det
from .. import extractor


@dataclass
class CompileResult:
    errors: pd.DataFrame
    types: pd.DataFrame
    deps: pd.DataFrame
    seconds: float
    summary: dict = field(default_factory=dict)


def compile_tree(repo: str | Path, files: list[str], source_roots: list[str], out: str | Path,
                 classpath: str = "", memory: str = "4g", timeout: int | None = None) -> CompileResult:
    t0 = time.time()
    repo = Path(repo)
    files = [f for f in files if (repo / f).exists()]
    summary = extractor.run(repo, files, files, source_roots, out, classpath=classpath, memory=memory,
                            timeout=timeout)
    raw = extractor.load(out)
    err_file = Path(out) / "errors.tsv"
    errors = pd.read_csv(err_file, sep="\t", dtype=str, keep_default_na=False, quoting=3) if err_file.exists() \
        else pd.DataFrame(columns=["file", "line", "code", "message"])
    return CompileResult(errors, raw["types"], raw["deps"], round(time.time() - t0, 1), summary)


def _normalise(msg: str, back: dict[str, str]) -> str:
    if back:
        pat = re.compile("|".join(re.escape(k) for k in sorted(back, key=len, reverse=True)))
        msg = pat.sub(lambda m: back[m.group(0)], msg)
    return msg


_SYMBOL = re.compile(r"symbol:\s+(?:class|interface|enum|variable|method|static)\s+([\w$]+)")


def new_errors(before: pd.DataFrame, after: pd.DataFrame, moved_files: dict[str, str] | None = None,
               renamed: dict[str, str] | None = None, repo_symbols: set[str] | None = None,
               plan_types: set[str] | None = None, repo_packages: set[str] | None = None,
               root: str | Path | None = None) -> pd.DataFrame:
    """Errors in ``after`` that are not in ``before`` (as a multiset).

    With ``repo_symbols`` (simple names of the repository's types and members),
    unresolved symbols that the repository does not define are ignored: they
    come from missing libraries, and javac's error recovery may report them a
    different number of times after an edit. Duplicate-class errors already
    reported before are ignored as well; with ``plan_types`` (moved and created
    types, new names) only duplicate-class errors naming one of them count:
    other duplicates come from compiling all modules together (tests copied
    between modules) and depend on the order javac reads the files.

    With ``root`` (the compiled working copy) and ``repo_packages``, an
    unresolved name that the file imports from a library package is ignored
    too: without the library javac may have resolved the name to a class of
    the same package before the move, and cannot after it."""
    lib_imports: dict[str, set[str]] = {}

    def library_import(file: str, name: str) -> bool:
        if root is None or repo_packages is None:
            return False
        if file not in lib_imports:
            names = set()
            try:
                text = (Path(root) / file).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for m in re.finditer(r"^\s*import\s+(?!static\b)([\w.]+)\.(\w+)\s*;", text, re.M):
                if m.group(1) not in repo_packages:
                    names.add(m.group(2))
            lib_imports[file] = names
        return name in lib_imports[file]

    back_file = {v: k for k, v in (moved_files or {}).items()}
    back_name = {v: k for k, v in (renamed or {}).items()}
    key_b = Counter(zip(before["file"], before["code"], before["message"]))
    rows = []
    seen: Counter = Counter()
    dup_before = set(before.loc[before["code"] == "compiler.err.duplicate.class", "message"])
    for r in after.itertuples(index=False):
        if r.code == "compiler.err.duplicate.class":
            if _normalise(r.message, back_name) in dup_before:
                continue
            if plan_types is not None and not any(t in r.message for t in plan_types):
                continue
        if repo_packages is not None and r.code == "compiler.err.doesnt.exist":
            m = re.search(r"package\s+([\w.]+)\s+does not exist", r.message)
            if m and m.group(1) not in repo_packages:
                continue                  # a library package (e.g. an import copied into a new interface)
        if repo_symbols is not None and r.code.startswith("compiler.err.cant.resolve"):
            m = _SYMBOL.search(r.message)
            if m and (m.group(1) not in repo_symbols or library_import(r.file, m.group(1))):
                continue
        k = (back_file.get(r.file, r.file), r.code, _normalise(r.message, back_name))
        seen[k] += 1
        if seen[k] > key_b.get(k, 0):
            rows.append({"file": r.file, "line": r.line, "code": r.code, "message": r.message})
    return pd.DataFrame(rows, columns=["file", "line", "code", "message"])


def package_graph(res: CompileResult, production_files: set[str]) -> tuple[nx.DiGraph, dict[str, int]]:
    """Package graph and classes per package of the production code that was compiled."""
    t = res.types
    t = t[t["file"].isin(production_files)]
    top = dict(zip(t["type"], t["top_level"]))
    pkg = dict(zip(t["type"], t["package"]))
    d = res.deps
    s = d["source"].str.split("#").str[0].map(top)
    g_ = d["target"].str.split("#").str[0].map(top)
    d = d.assign(st=s, tt=g_).dropna(subset=["st", "tt"])
    d = d[d["st"] != d["tt"]]
    g = nx.DiGraph()
    g.add_nodes_from(set(pkg.values()))
    w: Counter = Counter()
    for a, b in zip(d["st"], d["tt"]):
        pa, pb = pkg[a], pkg[b]
        if pa != pb:
            w[(pa, pb)] += 1
    for (a, b), n in w.items():
        g.add_edge(a, b, weight=n, class_edges=n)
    nested = Counter(top.values())
    counts = Counter()
    for x, n in nested.items():
        if x in pkg:
            counts[pkg[x]] += n
    return g, dict(counts)


def detect(res: CompileResult, production_files: set[str], th: det.Thresholds = det.PIPELINE) -> pd.DataFrame:
    g, counts = package_graph(res, production_files)
    return det.detect(g, None, counts, None, th)
