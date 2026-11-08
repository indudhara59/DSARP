"""Stages 0 and 1: import detector output and merge it into one view.

Produces, for one repository and commit:

* a **smell table**: one row per smell from every detector, with the same
  columns whatever the tool, plus triage flags (test-only, generated-only,
  members outside the repository) and cross-tool matches;
* a **component table**: one row per package and class, with module, test and
  generated flags and the metrics of every detector side by side;
* a **manifest**: commit, inputs with checksums, counts and warnings.

Nothing is filtered here; stage 3 decides what to drop, using the flags.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import __version__, detectors, paths
from .config import RepoConfig
from .model import DetectorResult, Level, Smell, SmellType

METRIC_EXCLUDE = {"name", "kind", "construct", "package", "file_path", "file_path_abs", "tool"}


@dataclass
class IngestResult:
    config: RepoConfig
    commit: str | None
    smells: pd.DataFrame
    components: pd.DataFrame
    manifest: dict
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- commit check

def _resolve_commit(cfg: RepoConfig, results: list[DetectorResult], warnings: list[str]) -> str | None:
    reported = {r.tool: r.version for r in results if r.version}
    commit = cfg.commit
    for tool, version in reported.items():
        looks_like_commit = len(version) == 40 and all(c in "0123456789abcdef" for c in version.lower())
        if not looks_like_commit:
            continue
        if commit is None:
            commit = version
            warnings.append(f"commit not set in config; using {version} reported by {tool}")
        elif version.lower() != commit.lower():
            warnings.append(f"MISMATCH: config commit {commit} but {tool} analysed {version}")
    if commit is None:
        warnings.append("no commit known: set 'commit' in the repo config")
    return commit


# ---------------------------------------------------------------- components

def _relativize(results: list[DetectorResult], cfg: RepoConfig, warnings: list[str]) -> dict[str, str]:
    """Make absolute file paths repository-relative; returns the roots used per tool."""
    roots: dict[str, str] = {}
    by_tool: dict[str, list[tuple[DetectorResult, str | None]]] = {}
    for res, inp in zip(results, cfg.detectors):
        by_tool.setdefault(res.tool, []).append((res, inp.path_root))
    for tool, items in by_tool.items():
        files = []
        for res, _ in items:
            comp = res.components
            if len(comp) and "file_path_abs" in comp:
                files += [(f, p) for f, p in zip(comp["file_path_abs"], comp["package"]) if f]
        if not files:
            continue
        explicit = [r for _, r in items if r]
        root = explicit[0] if explicit else paths.infer_repo_root(files)
        roots[tool] = root
        if not explicit:
            warnings.append(f"{tool}: inferred repository root on the analysis machine as '{root}' (set path_root to override)")
        for res, own_root in items:
            comp = res.components
            if len(comp) and "file_path_abs" in comp:
                r = own_root or root
                mask = comp["file_path_abs"] != ""
                comp.loc[mask, "file_path"] = comp.loc[mask, "file_path_abs"].map(lambda f: paths.relative_to(f, r))
    return roots


def _annotate_classes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    has = df["file_path"].fillna("") != ""
    df["module"] = ""
    df["is_test"] = False
    df["is_generated"] = False
    df.loc[has, "module"] = [paths.module_of(f, p) for f, p in zip(df.loc[has, "file_path"], df.loc[has, "package"])]
    df.loc[has, "is_test"] = [paths.is_test_path(f, p) for f, p in zip(df.loc[has, "file_path"], df.loc[has, "package"])]
    df.loc[has, "is_generated"] = [paths.is_generated_path(f) for f in df.loc[has, "file_path"]]
    df["is_test_module"] = df["module"].map(paths.is_test_module)
    df["has_source"] = has
    return df


def _merge_components(results: list[DetectorResult]) -> pd.DataFrame:
    frames = [r.components for r in results if len(r.components)]
    if not frames:
        return pd.DataFrame(columns=["name", "kind", "package", "file_path", "module", "is_test",
                                     "is_test_module", "is_generated", "has_source", "modules_count",
                                     "role", "tools"])
    all_rows = pd.concat(frames, ignore_index=True, sort=False)
    classes = _annotate_classes(all_rows[all_rows["kind"] == "class"])

    base_cols = ["name", "kind", "construct", "package", "file_path", "module", "is_test",
                 "is_test_module", "is_generated", "has_source"]
    # One row per class: prefer a row that has a source file.
    cls_base = (classes.sort_values("has_source", ascending=False)
                .drop_duplicates("name")[base_cols].set_index("name"))
    pkg_names = set(all_rows.loc[all_rows["kind"] == "package", "name"]) | set(classes["package"].dropna()) - {""}
    src_classes = classes[classes["has_source"]].drop_duplicates("name")
    grouped = src_classes.groupby("package")
    pkg_rows = []
    for name in sorted(pkg_names):
        if name in grouped.groups:
            g = grouped.get_group(name)
            pkg_rows.append({
                "name": name, "kind": "package", "construct": "package", "package": name, "file_path": "",
                "module": ";".join(sorted(set(g["module"]))), "is_test": bool(g["is_test"].all()),
                "is_test_module": bool(g["is_test_module"].all()),
                "is_generated": bool(g["is_generated"].all()), "has_source": True,
            })
        else:
            pkg_rows.append({"name": name, "kind": "package", "construct": "package", "package": name,
                             "file_path": "", "module": "", "is_test": False, "is_test_module": False,
                             "is_generated": False, "has_source": False})
    pkg_base = pd.DataFrame(pkg_rows).set_index("name") if pkg_rows else pd.DataFrame(columns=base_cols).set_index("name")
    base = pd.concat([pkg_base, cls_base[~cls_base.index.isin(pkg_base.index)]], sort=False)
    base["modules_count"] = base["module"].map(lambda m: len([x for x in str(m).split(";") if x]))
    # source: has own source files; parent: only sub-packages have source;
    # external: a library or JDK element that is not part of the repository.
    source_pkgs = sorted(base.index[(base["kind"] == "package") & base["has_source"].astype(bool)])

    def role(name: str, kind: str, has_source: bool) -> str:
        if has_source:
            return "source"
        if kind == "package":
            prefix = name + "."
            i = bisect.bisect_left(source_pkgs, prefix)
            if i < len(source_pkgs) and source_pkgs[i].startswith(prefix):
                return "parent"
        return "external"
    base["role"] = [role(n, k, bool(h)) for n, k, h in zip(base.index, base["kind"], base["has_source"])]

    # Metrics of every tool side by side: arcan_fan_in, designite_fan_in, ...
    for r in results:
        comp = r.components
        if not len(comp):
            continue
        metric_cols = [c for c in comp.columns if c not in METRIC_EXCLUDE]
        m = comp.drop_duplicates(["name", "kind"]).set_index("name")[metric_cols]
        m.columns = [f"{r.tool}_{c}" for c in metric_cols]
        m = m[~m.index.duplicated()]
        new = [c for c in m.columns if c not in base.columns]
        base = base.join(m[new], how="left")
    tools_by_name: dict[str, set[str]] = {}
    for r in results:
        for n in r.components["name"] if len(r.components) else []:
            tools_by_name.setdefault(n, set()).add(r.tool)
    base["tools"] = [";".join(sorted(tools_by_name.get(n, set()))) for n in base.index]
    return base.reset_index().rename(columns={"index": "name"})


# ---------------------------------------------------------------- smells

def _member_flags(members: list[str], info: pd.DataFrame) -> dict:
    roles = [info.at[m, "role"] if m in info.index else "external" for m in members]
    known = [m for m, r in zip(members, roles) if r == "source"]
    if known:
        sub = info.loc[known]
        test_only = bool(sub["is_test"].all())
        test_support_only = bool((sub["is_test"] | sub["is_test_module"]).all())
        generated_only = bool(sub["is_generated"].all())
        split = int((sub["modules_count"] > 1).sum())
    else:
        test_only = test_support_only = generated_only = False
        split = 0
    return {"members_in_repo": len(known), "parent_members": roles.count("parent"),
            "external_members": roles.count("external"), "test_only": test_only,
            "test_support_only": test_support_only, "generated_only": generated_only,
            "split_package_members": split}


def _match_key(s: Smell) -> tuple:
    if s.smell_type in (SmellType.HUB_LIKE_DEPENDENCY, SmellType.UNSTABLE_DEPENDENCY,
                        SmellType.GOD_COMPONENT) and s.central:
        return (s.smell_type, s.level, frozenset([s.central]))
    return (s.smell_type, s.level, frozenset(s.members))


def _cross_match(smells: list[Smell]) -> dict[str, list[str]]:
    """Exact matches between tools (same type, level and members / centre)."""
    index: dict[tuple, list[Smell]] = {}
    for s in smells:
        index.setdefault(_match_key(s), []).append(s)
    matches: dict[str, list[str]] = {}
    for group in index.values():
        tools = {s.tool for s in group}
        if len(tools) < 2:
            continue
        for s in group:
            matches[s.smell_id] = [o.smell_id for o in group if o.tool != s.tool]
    return matches


def _best_overlap(smells: list[Smell]) -> dict[str, float]:
    """Best Jaccard overlap of each package cycle with a cycle of another tool."""
    cycles = [s for s in smells if s.smell_type is SmellType.CYCLIC_DEPENDENCY and s.level is Level.PACKAGE]
    by_tool: dict[str, list[frozenset]] = {}
    for s in cycles:
        by_tool.setdefault(s.tool, []).append(frozenset(s.members))
    out = {}
    for s in cycles:
        mine = frozenset(s.members)
        others = [m for t, sets in by_tool.items() if t != s.tool for m in sets]
        out[s.smell_id] = max((len(mine & o) / len(mine | o) for o in others), default=0.0)
    return out


def _smell_table(smells: list[Smell], comp: pd.DataFrame, repo: str, commit: str | None) -> pd.DataFrame:
    matches, overlap = _cross_match(smells), _best_overlap(smells)
    info = comp.drop_duplicates("name").set_index("name")[
        ["role", "is_test", "is_test_module", "is_generated", "modules_count"]].copy()
    for col in ("is_test", "is_test_module", "is_generated"):
        info[col] = info[col].fillna(False).astype(bool)
    rows = []
    for s in smells:
        row = {
            "smell_id": s.smell_id, "repo": repo, "commit": commit, "tool": s.tool,
            "type": s.smell_type.value, "level": s.level.value, "size": len(s.members),
            "central": s.central or "", "members": json.dumps(s.members),
            "related": json.dumps(s.related), "groups": json.dumps(s.groups),
            "severity": s.attributes.get("Severity"),
            "attributes": json.dumps({k: v for k, v in s.attributes.items() if k != "edges"}, default=str),
            "matched_with": ";".join(matches.get(s.smell_id, [])),
            "confirmed_by": ";".join(sorted({s.tool} | ({m.split(":")[0] for m in matches.get(s.smell_id, [])}))),
            "best_cycle_overlap": overlap.get(s.smell_id),
            "source": s.source, "description": s.description,
        }
        row.update(_member_flags(s.members, info))
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- manifest

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inputs(cfg: RepoConfig) -> list[dict]:
    out = []
    for d in cfg.detectors:
        files = sorted(Path(d.path).glob("*.csv"))
        out.append({"tool": d.tool, "path": str(d.path), "module": d.module,
                    "files": {f.name: _sha256(f) for f in files}})
    return out


def ingest(cfg: RepoConfig) -> IngestResult:
    """Run stages 0 and 1 for one repository configuration."""
    results = [detectors.load(d) for d in cfg.detectors]
    warnings = [w for r in results for w in r.warnings]
    commit = _resolve_commit(cfg, results, warnings)
    roots = _relativize(results, cfg, warnings)
    components = _merge_components(results)
    all_smells = [s for r in results for s in r.smells]
    smells = _smell_table(all_smells, components, cfg.name, commit)

    counts = {}
    if len(smells):
        counts = {t: {k: int(v) for k, v in g.groupby("type").size().items()} for t, g in smells.groupby("tool")}
    manifest = {
        "dsarp_version": __version__,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": cfg.name, "repo_url": cfg.repo_url, "commit": commit,
        "config": {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(cfg).items() if k != "detectors"},
        "inputs": _inputs(cfg), "analysis_roots": roots,
        "counts": {
            "smells": int(len(smells)), "by_tool_and_type": counts,
            "packages": int((components["kind"] == "package").sum()) if len(components) else 0,
            "classes": int((components["kind"] == "class").sum()) if len(components) else 0,
            "confirmed_by_both": int((smells["matched_with"] != "").sum()) if len(smells) else 0,
            "test_only": int(smells["test_only"].sum()) if len(smells) else 0,
            "test_support_only": int(smells["test_support_only"].sum()) if len(smells) else 0,
            "generated_only": int(smells["generated_only"].sum()) if len(smells) else 0,
            "with_parent_members": int((smells["parent_members"] > 0).sum()) if len(smells) else 0,
            "with_external_members": int((smells["external_members"] > 0).sum()) if len(smells) else 0,
        },
        "warnings": warnings,
    }
    return IngestResult(cfg, commit, smells, components, manifest, warnings)


def write(result: IngestResult, out_dir: str | Path) -> Path:
    """Write ``smells.csv``, ``components.csv`` and ``manifest.json``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result.smells.to_csv(out / "smells.csv", index=False)
    result.components.to_csv(out / "components.csv", index=False)
    (out / "manifest.json").write_text(json.dumps(result.manifest, indent=2, default=str), encoding="utf-8")
    return out
