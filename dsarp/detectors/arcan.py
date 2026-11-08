"""Adapter for Arcan output.

Expected files in the folder (Arcan's CSV export):

* ``smell-characteristics.csv``: one row per smell with metrics
  (``vertexId``, ``smellType``, ``AffectedComponentType``, ``Severity``, ...).
* ``smell-affects.csv``: smell → affected component edges (``fromId``, ``to``).
* ``component-metrics.csv``: one row per package or class.

The smell files are optional (a run may have produced only component
metrics); a warning is recorded when they are missing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .. import paths
from ..config import DetectorInput
from ..model import DetectorResult, Level, Smell, SmellType

TOOL = "arcan"

SMELL_TYPES = {
    "cyclicDep": SmellType.CYCLIC_DEPENDENCY,
    "hubLikeDep": SmellType.HUB_LIKE_DEPENDENCY,
    "unstableDep": SmellType.UNSTABLE_DEPENDENCY,
    "godComponent": SmellType.GOD_COMPONENT,
}
LEVELS = {"CONTAINER": Level.PACKAGE, "UNIT": Level.CLASS}

# Metric columns copied into Smell.attributes when present.
SMELL_ATTRIBUTES = [
    "Severity", "Size", "Shape", "Strength", "NumberOfEdges", "ATDI", "ATDI_WEIGHTED",
    "PageRankWeighted", "PageRankWeightedRanking", "AffectedClassesRatio",
    "AfferentAffectedRatio", "EfferentAffectedRatio", "InstabilityGap", "LOCDensity",
    "SmellExtent", "SmellExtentByWeight", "TotalNumberOfChanges", "AffectedConstructType",
]
COMPONENT_METRICS = {
    "FanIn": "fan_in", "FanOut": "fan_out", "InstabilityMetric": "instability",
    "AbstractnessMetric": "abstractness", "LinesOfCode": "loc", "PageRank": "pagerank",
    "NumberOfChildren": "children", "TotalAmountOfChanges": "changes",
}
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    # Some exports carry empty trailing columns ("Unnamed: 10", ...).
    return df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]


def _parse_list(value: object) -> list[str]:
    """Parse Arcan's ``[a, b, c]`` element lists."""
    if not isinstance(value, str):
        return []
    inner = value.strip().strip("[]")
    return [x.strip() for x in inner.split(",") if x.strip()]


def _clean(value: object) -> object:
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _package_of(name: str, packages: set[str]) -> str:
    """Longest known package that prefixes a class name (handles nested classes)."""
    parts = name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in packages:
            return candidate
    return name.rsplit(".", 1)[0] if "." in name else ""


def load_components(folder: Path) -> pd.DataFrame:
    df = _read(folder / "component-metrics.csv")
    ctype = df["ComponentType"].astype(str).str.upper()
    kind = ctype.map(lambda t: "package" if t == "PACKAGE" else "class")
    names = df["name"].astype(str)
    packages = set(names[kind == "package"])
    rel = df.get("filePathRelative", pd.Series([""] * len(df))).fillna("").astype(str)
    out = pd.DataFrame({
        "name": names,
        "kind": kind,
        "construct": ctype.str.lower(),
        "package": [n if k == "package" else _package_of(n, packages) for n, k in zip(names, kind)],
        "file_path": rel.map(lambda p: "" if p in (".", "") else paths.normalize(p)),
        "file_path_abs": "",
    })
    for src, dst in COMPONENT_METRICS.items():
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")
    out["tool"] = TOOL
    return out


def load(inp: DetectorInput) -> DetectorResult:
    folder = Path(inp.path)
    if not folder.is_dir():
        raise FileNotFoundError(f"Arcan output folder not found: {folder}")
    warnings: list[str] = []

    components = load_components(folder)
    versions = pd.read_csv(folder / "component-metrics.csv", usecols=["versionId"])["versionId"].dropna().astype(str).unique()
    version = versions[0] if len(versions) else None
    if len(versions) > 1:
        warnings.append(f"Arcan output contains {len(versions)} versions; only single-version runs are supported, using {version}")
    if version and not _COMMIT.match(version):
        warnings.append(f"Arcan version id '{version}' is not a git commit hash; set 'commit' in the repo config")

    char_file, aff_file = folder / "smell-characteristics.csv", folder / "smell-affects.csv"
    if not char_file.exists() or not aff_file.exists():
        warnings.append("Arcan smell files missing (smell-characteristics.csv / smell-affects.csv): no Arcan smells loaded")
        return DetectorResult(TOOL, [], components, version, warnings)

    chars = _read(char_file)
    affects = _read(aff_file)
    affected = affects.groupby("fromId")["to"].apply(lambda s: [str(x) for x in s]).to_dict()

    smells: list[Smell] = []
    for row in chars.itertuples(index=False):
        r = row._asdict()
        raw_type = str(r.get("smellType"))
        stype = SMELL_TYPES.get(raw_type, SmellType.UNKNOWN)
        if stype is SmellType.UNKNOWN:
            warnings.append(f"Unknown Arcan smell type '{raw_type}' (vertex {r.get('vertexId')})")
        level = LEVELS.get(str(r.get("AffectedComponentType")).upper(), Level.PACKAGE)
        listed = _parse_list(r.get("AffectedElements"))
        via_edges = affected.get(r["vertexId"], [])
        members = list(dict.fromkeys(listed + via_edges))  # keep order, drop duplicates
        central = r.get("CentralComponent")
        central = str(central) if isinstance(central, str) and central else None
        related = [m for m in members if m != central] if central else []
        attrs = {k: _clean(r[k]) for k in SMELL_ATTRIBUTES if k in r}
        smells.append(Smell(
            smell_id=f"{TOOL}:{r['vertexId']}",
            tool=TOOL,
            smell_type=stype,
            level=level,
            members=members,
            central=central,
            related=related,
            attributes=attrs,
            source=str(folder),
        ))
    return DetectorResult(TOOL, smells, components, version, warnings)
