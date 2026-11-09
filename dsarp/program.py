"""Stage 2: the program model of a repository at a commit.

Combines the Java extractor's output with the build structure, framework
wiring, public API and change history, and stores it as plain CSV/JSON files:

========================  ==================================================
``files.csv``             every Java file with module, test/generated flags
``types.csv``             classes, interfaces, enums, records, annotations
``members.csv``           methods, constructors, fields, initializers
``deps.csv``              typed, counted dependencies (member or type level)
``packages.csv``          per package: modules, size, test-only, API types
``modules.json``          build modules and allowed module dependencies
``wiring.csv``            non-Java files naming classes (ServiceLoader, XML)
``cochange.csv``          file pairs that changed together, with counts
``model.json``            manifest: commit, counts, warnings, coverage
========================  ==================================================

:class:`ProgramModel` loads these files and builds class and package graphs.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import pandas as pd

from . import __version__, buildinfo, extractor, history, sources, wiring
from .checkout import head
from .config import RepoConfig

USE_KINDS = {"call", "field_access", "method_ref", "import"}
INTERNAL_SEGMENTS = {"internal", "impl", "private", "shaded"}


# ---------------------------------------------------------------- API rules

def _matches_osgi(pkg: str, patterns: list[str]) -> bool:
    """First matching Export-Package pattern decides (``!`` excludes)."""
    for p in patterns:
        neg = p.startswith("!")
        pat = p[1:] if neg else p
        if pat == "*" or pat == pkg or (pat.endswith(".*") and (pkg == pat[:-2] or pkg.startswith(pat[:-1]))):
            return not neg
    return False


def _exported(pkg: str, patterns: list[str] | None) -> bool:
    if patterns:
        return _matches_osgi(pkg, patterns)
    return not any(seg in INTERNAL_SEGMENTS for seg in pkg.split("."))


def _protected(name: str, rules: list[str]) -> bool:
    return any(name == r or name.startswith(r + ".") or name.startswith(r + "#") for r in rules)


# ---------------------------------------------------------------- build

@dataclass
class ModelResult:
    out: Path
    manifest: dict


def build_model(cfg: RepoConfig, repo: str | Path, out: str | Path, *, include_tests: bool = True,
                classpath: str = "", memory: str = "4g", history_commits: int = 3000,
                ingest_dir: str | Path | None = None, keep_raw: bool = True) -> ModelResult:
    """Build the program model of ``repo`` (a checkout of ``cfg.commit``) into ``out``."""
    repo, out = Path(repo).resolve(), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    actual = head(repo) if (repo / ".git").exists() else None
    if cfg.commit and actual and not actual.startswith(cfg.commit):
        warnings.append(f"MISMATCH: checkout is at {actual}, config commit is {cfg.commit}")

    files = sources.discover(repo, cfg.exclude_paths, include_tests=include_tests)
    kept = files[~files["excluded"]]
    if kept.empty:
        raise RuntimeError(f"no Java files to analyse in {repo}")
    build = buildinfo.read_build(repo, cfg.build)

    raw = out / "extractor"
    summary = extractor.run(repo, kept["file"].tolist(), kept["file"].tolist(),
                            sources.source_roots(files), raw, classpath=classpath, memory=memory)
    t = extractor.load(raw)

    types = t["types"].merge(files[["file", "module", "is_test", "is_test_module", "is_generated"]],
                             on="file", how="left")
    for col in ("is_test", "is_test_module", "is_generated"):
        types[col] = types[col].fillna(False).astype(bool)
    build_module = {m: buildinfo.module_for(m, build.modules) for m in types["module"].dropna().unique()}
    types["build_module"] = types["module"].map(build_module).fillna("")
    exported_by_module = {p: m.exported_packages for p, m in build.modules.items()}
    types["exported_package"] = [
        _exported(pkg, exported_by_module.get(bm)) for pkg, bm in zip(types["package"], types["build_module"])]
    vis_public = types["visibility"] == "public"
    outer_public = types["outer"].map(dict(zip(types["type"], vis_public))).fillna(True)
    types["api"] = vis_public & outer_public & types["exported_package"] & ~types["is_test"] & ~types["is_generated"]
    types["protected"] = [_protected(n, cfg.protected_api) for n in types["type"]]

    wires = wiring.scan(repo, build)
    known = set(types["type"])
    wires["in_repo"] = wires["class"].isin(known)
    wired_counts = wires[wires["in_repo"]].groupby("class").size()
    types["wiring_refs"] = types["type"].map(wired_counts).fillna(0).astype(int)

    deps = t["deps"]
    deps["source_type"] = deps["source"].str.split("#").str[0]
    deps["target_type"] = deps["target"].str.split("#").str[0]
    deps["target_is_member"] = deps["target"].str.contains("#", regex=False)

    members = t["members"]
    api_owner = dict(zip(types["type"], types["api"]))
    members["api"] = members["owner"].map(api_owner).fillna(False).astype(bool) & members["visibility"].isin(["public", "protected"])
    test_type = dict(zip(types["type"], types["is_test"]))
    uses = deps[deps["kind"].isin(USE_KINDS) & deps["target_is_member"]]
    uses = uses[uses["source_type"] != uses["target_type"]]
    callers = uses.groupby("target")["source_type"].nunique()
    test_callers = uses[uses["source_type"].map(test_type).fillna(False).astype(bool)].groupby("target")["source_type"].nunique()
    members["callers"] = members["member"].map(callers).fillna(0).astype(int)
    members["test_callers"] = members["member"].map(test_callers).fillna(0).astype(int)
    members["protected"] = [_protected(m, cfg.protected_api) for m in members["member"]]

    known_files = set(kept["file"])
    hist = None
    if (repo / ".git").exists() and history_commits > 0:
        try:
            hist = history.mine(repo, actual or "HEAD", max_commits=history_commits, known_files=known_files)
            warnings += hist.warnings
        except Exception as exc:  # history is optional; never fail the model on it
            warnings.append(f"co-change history unavailable: {exc}")
    changes = dict(zip(hist.changes["file"], hist.changes["changes"])) if hist is not None else {}
    files["changes"] = files["file"].map(changes).fillna(0).astype(int)
    types["changes"] = types["file"].map(changes).fillna(0).astype(int)

    fstats = t["files"].set_index("file")
    files["lines"] = files["file"].map(fstats["lines"]).fillna(0).astype(int)
    files["types"] = files["file"].map(fstats["top_level_types"]).fillna("")

    packages = _packages(types)

    # ---- write
    files.to_csv(out / "files.csv", index=False)
    types.to_csv(out / "types.csv", index=False)
    members.to_csv(out / "members.csv", index=False)
    deps.to_csv(out / "deps.csv", index=False)
    packages.to_csv(out / "packages.csv", index=False)
    wires.to_csv(out / "wiring.csv", index=False)
    (out / "modules.json").write_text(json.dumps(build.to_json(), indent=2), encoding="utf-8")
    if hist is not None:
        hist.cochange.to_csv(out / "cochange.csv", index=False)

    diag = t["diagnostics"]
    errors = diag[diag["kind"] == "compiler"].sort_values("count", ascending=False)
    unresolved = diag[diag["kind"] == "unresolved_type"].head(15)
    manifest = {
        "dsarp_version": __version__,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": cfg.name, "commit": actual or cfg.commit,
        "counts": {
            "java_files": int(len(files)), "analysed_files": int(len(kept)),
            "excluded_files": {str(k): int(v) for k, v in files[files["excluded"]].groupby("reason").size().items()},
            "types": int(len(types)), "members": int(len(members)), "dependencies": int(len(deps)),
            "packages": int(len(packages)), "build_modules": len(build.modules),
            "module_dependencies": len(build.edges()), "api_types": int(types["api"].sum()),
            "wiring_refs_in_repo": int(wires["in_repo"].sum()),
        },
        "build_system": build.system,
        "extractor": summary,
        "compiler_errors_top": {r["key"]: int(r["count"]) for _, r in errors.head(10).iterrows()},
        "unresolved_types_top": {r["key"]: int(r["count"]) for _, r in unresolved.iterrows()},
        "history": None if hist is None else {
            "commits_used": hist.commits_used, "commits_skipped_large": hist.commits_skipped,
            "cochange_pairs": int(len(hist.cochange)), "shallow": hist.shallow},
        "wiring_by_kind": {str(k): int(v) for k, v in wires.groupby("kind").size().items()},
        "warnings": warnings,
    }
    if ingest_dir:
        manifest["coverage"] = _coverage(Path(ingest_dir), types, repo)
        warnings += [v["warning"] for v in manifest["coverage"].values() if isinstance(v, dict) and "warning" in v]
    (out / "model.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not keep_raw:
        shutil.rmtree(raw, ignore_errors=True)
    return ModelResult(out, manifest)


def _packages(types: pd.DataFrame) -> pd.DataFrame:
    g = types.groupby("package")
    return pd.DataFrame({
        "package": list(g.groups),
        "types": g.size().values,
        "modules": g["module"].apply(lambda s: ";".join(sorted(set(s.dropna())))).values,
        "test_only": g["is_test"].all().values,
        "generated_only": g["is_generated"].all().values,
        "api_types": g["api"].sum().values,
        "protected": g["protected"].any().values,
        "changes": g["changes"].sum().values,
    })


def _coverage(ingest_dir: Path, types: pd.DataFrame, repo: Path) -> dict:
    """How many of each detector's classes the program model found.

    Classes whose file does not exist in the checkout point to the detector
    having analysed a different version of the code than the configured
    commit; they are counted separately.
    """
    comp_file = ingest_dir / "components.csv"
    if not comp_file.exists():
        return {"error": f"{comp_file} not found"}
    comp = pd.read_csv(comp_file, low_memory=False, keep_default_na=False)
    cls = comp[(comp["kind"] == "class") & (comp["role"] == "source")
               & ~comp["is_generated"].astype(str).str.lower().isin(["true", "1"])]
    names = set(types["type"])
    out = {}
    for tool in ("arcan", "designite"):
        sub = cls[cls["tools"].str.contains(tool)]
        if sub.empty:
            continue
        found = sub["name"].isin(names)
        file_missing = ~found & ~sub["file_path"].map(lambda f: bool(f) and (repo / f).exists())
        other = ~found & ~file_missing
        out[tool] = {
            "classes": int(len(sub)), "found": int(found.sum()),
            "fraction": round(float(found.mean()), 4),
            "file_not_at_commit": int(file_missing.sum()),
            "not_found_other": int(other.sum()),
            "examples_not_found": sub.loc[other, "name"].head(5).tolist(),
        }
        if len(sub) and file_missing.mean() > 0.05:
            out[tool]["warning"] = (f"{int(file_missing.sum())} {tool} classes are in files that do not exist at this "
                                    f"commit: {tool} probably analysed a different version of the code")
    return out


# ---------------------------------------------------------------- loading

class ProgramModel:
    """Read access to a stored program model, with class and package graphs."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        read = lambda name: pd.read_csv(self.folder / name, low_memory=False, keep_default_na=False)  # noqa: E731
        self.files = read("files.csv")
        self.types = read("types.csv")
        self.members = read("members.csv")
        self.deps = read("deps.csv")
        self.packages = read("packages.csv")
        self.wiring = read("wiring.csv")
        self.modules = json.loads((self.folder / "modules.json").read_text(encoding="utf-8"))
        self.manifest = json.loads((self.folder / "model.json").read_text(encoding="utf-8"))
        cochange = self.folder / "cochange.csv"
        self.cochange = read("cochange.csv") if cochange.exists() else pd.DataFrame(columns=["file_a", "file_b", "count"])
        for col in ("is_test", "is_test_module", "is_generated", "api", "protected", "exported_package"):
            if col in self.types:
                self.types[col] = self.types[col].astype(str).str.lower().isin(["true", "1"])

    def _type_filter(self, include_tests: bool, include_generated: bool) -> set[str]:
        t = self.types
        keep = pd.Series(True, index=t.index)
        if not include_tests:
            keep &= ~t["is_test"]
        if not include_generated:
            keep &= ~t["is_generated"]
        return set(t.loc[keep, "type"])

    def type_graph(self, include_tests: bool = False, include_generated: bool = False,
                   kinds: set[str] | None = None, collapse_nested: bool = False) -> nx.DiGraph:
        """Class-level dependency graph.

        With ``collapse_nested`` nested types are merged into their top-level
        type (as Arcan analyses classes). Edge attributes: ``weight`` (sum of
        reference counts), ``refs`` (number of distinct member/type-level
        dependencies) and ``kinds``.
        """
        allowed = self._type_filter(include_tests, include_generated)
        d = self.deps
        if kinds:
            d = d[d["kind"].isin(kinds)]
        d = d[d["source_type"].isin(allowed) & d["target_type"].isin(allowed)]
        if collapse_nested:
            top = dict(zip(self.types["type"], self.types["top_level"]))
            d = d.assign(source_type=d["source_type"].map(top), target_type=d["target_type"].map(top))
            allowed = {top[t] for t in allowed} & allowed
        d = d[d["source_type"] != d["target_type"]]
        g = nx.DiGraph()
        info = self.types.set_index("type")
        for t in allowed:
            row = info.loc[t]
            row = row.iloc[0] if isinstance(row, pd.DataFrame) else row
            g.add_node(t, package=row["package"], module=row["module"], kind=row["kind"], api=bool(row["api"]))
        agg = d.groupby(["source_type", "target_type"]).agg(
            weight=("count", "sum"), refs=("count", "size"), kinds=("kind", lambda s: ";".join(sorted(set(s)))))
        for (s, tgt), r in agg.iterrows():
            g.add_edge(s, tgt, weight=int(r["weight"]), refs=int(r["refs"]), kinds=r["kinds"])
        return g

    def package_graph(self, include_tests: bool = False, include_generated: bool = False,
                      kinds: set[str] | None = None) -> nx.DiGraph:
        """Package-level graph; ``class_edges`` counts the class dependencies behind each edge."""
        tg = self.type_graph(include_tests, include_generated, kinds)
        g = nx.DiGraph()
        for n, a in tg.nodes(data=True):
            g.add_node(a["package"])
        for s, t, a in tg.edges(data=True):
            ps, pt = tg.nodes[s]["package"], tg.nodes[t]["package"]
            if ps == pt:
                continue
            if g.has_edge(ps, pt):
                e = g[ps][pt]
                e["weight"] += a["weight"]
                e["class_edges"] += 1
            else:
                g.add_edge(ps, pt, weight=a["weight"], class_edges=1)
        return g

    def callers(self, member: str) -> pd.DataFrame:
        """Dependencies that use a method, constructor or field."""
        d = self.deps
        return d[(d["target"] == member) & d["kind"].isin(USE_KINDS)]

    def module_dependencies(self) -> nx.DiGraph:
        g = nx.DiGraph()
        for path, m in self.modules.get("modules", {}).items():
            g.add_node(path, name=m.get("name"))
            for d in m.get("depends_on", []):
                g.add_edge(path, d)
        return g
