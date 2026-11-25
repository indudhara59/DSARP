"""Align the program model with the bytecode (what Arcan analyses).

Arcan reads compiled classes; DSARP's program model is extracted from the
source. They mostly agree, but not always: constants are inlined into the
classes that use them, source-only annotations and Javadoc references vanish,
and the compiler adds references (bridge methods, synthetic accessors, string
concatenation helpers) that the source does not show. A plan that is judged
on a different graph than Arcan's can look right to DSARP and wrong to Arcan.

``dsarp align`` runs ``jdeps`` on the compiled main classes of every module
(after ``dsarp baseline`` built them) and writes ``model/bytecode_edges.csv``
(top-level class pairs) and ``model/alignment.json``. The program model then:

* marks each source dependency as seen in the bytecode or not (``in_bytecode``),
* adds the class pairs only the bytecode has (kind ``bytecode``),
* and builds every smell graph (class and package level; the simulation and the
  triage) from what the bytecode shows.

The dependency facts used to rewrite code and to check moves keep every source
dependency, because the code still needs them to compile.

A **partial** graph is refused: when a module with main sources has no compiled
classes, the alignment is not used (``alignment.json``: ``complete: false``).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pandas as pd

LINE = re.compile(r"^\s+([\w$.]+)\s+->\s+([\w$.]+)\s+(\S+)\s*$")


def class_dirs(repo: Path) -> tuple[list[Path], list[str]]:
    """Compiled main-class folders of the modules, and modules with main sources but no compiled classes."""
    found, missing = [], []
    for src in sorted(repo.glob("**/src/main/java")):
        if any(p in (".git", "node_modules") for p in src.parts) or "target" in src.parts[:-3]:
            continue
        if not any(src.glob("**/*.java")):
            continue
        mod = src.parents[2]
        cands = [mod / "target" / "classes", mod / "build" / "classes" / "java" / "main"]
        d = next((c for c in cands if c.is_dir() and any(c.glob("**/*.class"))), None)
        if d:
            found.append(d)
        else:
            missing.append(mod.relative_to(repo).as_posix() or ".")
    return found, missing


def run_jdeps(dirs: list[Path], jdeps: str = "jdeps", multi_release: str | None = None) -> list[tuple[str, str]]:
    """Class-level dependencies (binary names) among the given class folders."""
    cp = ":".join(str(d) for d in dirs)
    pairs = []
    for d in dirs:
        cmd = [jdeps, "-verbose:class", "-filter:none", "-cp", cp]
        if multi_release:
            cmd += ["--multi-release", multi_release]
        cmd.append(str(d))
        r = subprocess.run(cmd, capture_output=True, text=True)
        for line in r.stdout.splitlines():
            m = LINE.match(line)
            if m:
                pairs.append((m.group(1), m.group(2)))
    return pairs


def _top(binary: str, top_of: dict[str, str]) -> str | None:
    name = binary.split("$", 1)[0]
    return top_of.get(name)


def align(results: str | Path, repo: str | Path, jdeps: str = "jdeps", log=print) -> dict:
    """Run jdeps on the compiled classes and write the alignment files into ``results/model``."""
    from .program import ProgramModel
    results, repo = Path(results), Path(repo)
    mdir = results / "model"
    model = ProgramModel(mdir, aligned=False)
    t = model.types
    top_of = dict(zip(t["type"], t["top_level"]))
    prod = set(t.loc[~t["is_test"] & ~t["is_generated"], "top_level"])
    dirs, missing = class_dirs(repo)
    info = {"complete": False, "class_dirs": [d.relative_to(repo).as_posix() for d in dirs],
            "modules_without_classes": missing}
    if not dirs:
        info["reason"] = "no compiled classes found (run dsarp baseline first)"
    elif missing:
        info["reason"] = (f"{len(missing)} modules with sources have no compiled classes (e.g. {missing[0]}): "
                          "a partial bytecode graph is not used")
    if not dirs:
        (mdir / "alignment.json").write_text(json.dumps(info, indent=1))
        return info
    log(f"align: jdeps on {len(dirs)} class folders")
    raw = run_jdeps(dirs, jdeps)
    byte = set()
    for a, b in raw:
        s, d = _top(a, top_of), _top(b, top_of)
        if s and d and s != d and s in prod and d in prod:
            byte.add((s, d))
    pd.DataFrame(sorted(byte), columns=["source_type", "target_type"]).to_csv(mdir / "bytecode_edges.csv",
                                                                             index=False)
    dd = model.deps
    src = set()
    for s, d in zip(dd["source_type"].map(top_of), dd["target_type"].map(top_of)):
        if isinstance(s, str) and isinstance(d, str) and s != d and s in prod and d in prod:
            src.add((s, d))
    pkg = dict(zip(t["type"], t["package"]))
    psrc = {(pkg[a], pkg[b]) for a, b in src if pkg[a] != pkg[b]}
    pbyte = {(pkg[a], pkg[b]) for a, b in byte if pkg[a] != pkg[b]}
    info.update({
        "complete": not missing,
        "class_pairs": {"both": len(src & byte), "source_only": len(src - byte), "bytecode_only": len(byte - src)},
        "package_pairs": {"both": len(psrc & pbyte), "source_only": len(psrc - pbyte),
                          "bytecode_only": len(pbyte - psrc)},
        "examples_source_only": [f"{a} -> {b}" for a, b in sorted(src - byte)[:10]],
        "examples_bytecode_only": [f"{a} -> {b}" for a, b in sorted(byte - src)[:10]],
    })
    (mdir / "alignment.json").write_text(json.dumps(info, indent=1))
    return info


def apply_alignment(model) -> bool:
    """Called by ``ProgramModel``: mark source dependencies seen in the bytecode and add bytecode-only pairs.
    Returns whether the model is aligned (a complete alignment exists)."""
    folder = model.folder
    af, ef = folder / "alignment.json", folder / "bytecode_edges.csv"
    if not af.exists() or not ef.exists():
        return False
    info = json.loads(af.read_text())
    if not info.get("complete"):
        return False
    e = pd.read_csv(ef)
    pairs = set(zip(e["source_type"], e["target_type"]))
    top_of = dict(zip(model.types["type"], model.types["top_level"]))
    d = model.deps
    st, tt = d["source_type"].map(top_of), d["target_type"].map(top_of)
    prod = set(model.types.loc[~model.types["is_test"], "top_level"])
    seen = [(s, t) in pairs or s == t or s not in prod or t not in prod for s, t in zip(st, tt)]
    d = d.assign(in_bytecode=seen)
    have = set(zip(st, tt))
    extra = [(s, t) for s, t in pairs if (s, t) not in have]
    if extra:
        cols = {c: "" for c in d.columns}
        rows = [{**cols, "source": s, "target": t, "source_type": s, "target_type": t, "kind": "bytecode",
                 "count": 1, "in_bytecode": True} for s, t in extra]
        d = pd.concat([d, pd.DataFrame(rows)], ignore_index=True)
    model.deps = d
    return True
