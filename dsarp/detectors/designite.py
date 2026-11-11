"""Adapter for Designite (DesigniteJava) output.

Expected files in the folder:

* ``ArchitectureSmells.csv``: ``Project, Package, Smell, Description``.
* ``DesignSmells.csv`` (optional): class-level smells; only the two that are
  architectural (``Cyclically-dependent Modularization`` and
  ``Hub-like Modularization``) are imported.
* ``TypeMetrics.csv`` (optional): class metrics and absolute file paths.

Designite puts the smell details (cycle members, less stable packages, class
groups) into the free-text description, so this module parses those
sentences. Designite writes absolute file paths from the machine it ran on;
they are made repository-relative later, in :mod:`dsarp.ingest`.

Designite reports one cyclic-dependency row per participating package, so
the same cycle appears several times; rows with the same member set are
merged into one smell.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from ..config import DetectorInput
from ..model import DetectorResult, Level, Smell, SmellType

TOOL = "designite"

ARCH_TYPES = {
    "Cyclic Dependency": SmellType.CYCLIC_DEPENDENCY,
    "Unstable Dependency": SmellType.UNSTABLE_DEPENDENCY,
    "God Component": SmellType.GOD_COMPONENT,
    "Feature Concentration": SmellType.FEATURE_CONCENTRATION,
    "Scattered Functionality": SmellType.SCATTERED_FUNCTIONALITY,
    "Dense Structure": SmellType.DENSE_STRUCTURE,
    "Ambiguous Interface": SmellType.AMBIGUOUS_INTERFACE,
}
DESIGN_TYPES = {
    "Cyclically-dependent Modularization": SmellType.CYCLIC_DEPENDENCY,
    "Hub-like Modularization": SmellType.HUB_LIKE_DEPENDENCY,
}
TYPE_METRICS = {
    "NOF": "fields", "NOPF": "public_fields", "NOM": "methods", "NOPM": "public_methods",
    "LOC": "loc", "WMC": "wmc", "NC": "children", "DIT": "dit", "LCOM": "lcom",
    "Fan-In": "fan_in", "Fan-Out": "fan_out",
}

_AFTER = {
    "cycle": re.compile(r"participating components in the cycle are:\s*(?P<list>.+)$", re.S),
    "class_cycle": re.compile(r"participating classes in the cycle are:\s*(?P<list>.+)$", re.S),
    "unstable": re.compile(r"less stable component\(s\):\s*(?P<list>.+)$", re.S),
    "scattered": re.compile(r"realize the same concern:\s*(?P<list>.+)$", re.S),
    "incoming": re.compile(r"Incoming depend[a-z]*:\s*(?P<list>.*?)(?:Outgoing depend[a-z]*:|$)", re.S),
    "outgoing": re.compile(r"Outgoing depend[a-z]*:\s*(?P<list>.+)$", re.S),
}
_NUMBER = re.compile(r"(?P<name>[A-Za-z][A-Za-z ()]*?)\s*=\s*(?P<value>-?\d+(?:\.\d+)?)")
_CLASS_COUNT = re.compile(r"Number of classes in the component are:\s*(?P<n>\d+)")
_GROUP = re.compile(r"\[([^\]]*)\]")
_EDGE = re.compile(r"from:\s*(?P<src>[\w.$]+)\s+to:\s*(?P<dst>[\w.$]+)")


def _split(text: str) -> list[str]:
    """Split a Designite ``a; b; c.`` list, keeping order and dropping repeats."""
    items = [x.strip().rstrip(".").strip() for x in text.split(";")]
    return list(dict.fromkeys(x for x in items if x))


def _list_after(key: str, description: str) -> list[str]:
    m = _AFTER[key].search(description or "")
    if not m:
        return []
    text = m.group("list")
    # A trailing sentence with metrics ("... . LCC = 0.5") is not part of the list.
    text = re.split(r"\.\s+[A-Z][A-Za-z ()]*=", text)[0]
    return _split(text)


def _numbers(description: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in _NUMBER.finditer(description or ""):
        name = m.group("name").strip()
        key = re.sub(r"\W+", "_", name).strip("_").lower()
        if key:
            out[key] = float(m.group("value"))
    m = _CLASS_COUNT.search(description or "")
    if m:
        out["classes"] = float(m.group("n"))
    return out


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    return df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]


def load_components(folder: Path) -> pd.DataFrame:
    """Classes from ``TypeMetrics.csv`` (empty frame if the file is missing)."""
    f = folder / "TypeMetrics.csv"
    cols = ["name", "kind", "construct", "package", "file_path", "file_path_abs"]
    if not f.exists():
        return pd.DataFrame(columns=cols)
    df = _read(f)
    package = df["Package"].fillna("").astype(str).replace({"<All packages>": "", "(default package)": ""})
    cls = df["Class"].astype(str)
    out = pd.DataFrame({
        "name": [f"{p}.{c}" if p else c for p, c in zip(package, cls)],
        "kind": "class",
        "construct": "class",
        "package": package,
        "file_path": "",
        "file_path_abs": df.get("File", pd.Series([""] * len(df))).fillna("").astype(str),
    })
    for src, dst in TYPE_METRICS.items():
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")
    out["tool"] = TOOL
    return out.drop_duplicates(subset=["name"], keep="first")


def _resolver(components: pd.DataFrame):
    """Map simple class names (as Designite writes them) to qualified names."""
    by_simple: dict[str, list[str]] = {}
    for name, pkg in zip(components["name"], components["package"]):
        simple = name[len(pkg) + 1:] if pkg else name
        by_simple.setdefault(simple, []).append(name)

    def resolve(simple: str, prefer_package: str) -> str:
        candidates = by_simple.get(simple, [])
        if len(candidates) == 1:
            return candidates[0]
        same = [c for c in candidates if c == f"{prefer_package}.{simple}"]
        return same[0] if same else simple  # unresolved names stay simple
    return resolve


def load(inp: DetectorInput) -> DetectorResult:
    folder = Path(inp.path)
    if not folder.is_dir():
        raise FileNotFoundError(f"Designite output folder not found: {folder}")
    warnings: list[str] = []
    components = load_components(folder)
    if components.empty:
        warnings.append(f"{folder}: TypeMetrics.csv missing; class metrics and file paths unavailable")
    run = inp.module or folder.name
    smells: list[Smell] = []
    counter = 0

    def new_id() -> str:
        nonlocal counter
        counter += 1
        return f"{TOOL}:{run}:{counter}"

    arch_file = folder / "ArchitectureSmells.csv"
    if arch_file.exists():
        arch = _read(arch_file)
        cycles: dict[frozenset, Smell] = {}
        for row in arch.itertuples(index=False):
            pkg, kind, desc = str(row.Package), str(row.Smell).strip(), str(row.Description or "")
            stype = ARCH_TYPES.get(kind)
            if stype is None:
                warnings.append(f"{folder}: unknown Designite architecture smell '{kind}'")
                continue
            attrs = _numbers(desc)
            if stype is SmellType.CYCLIC_DEPENDENCY:
                members = _list_after("cycle", desc) or [pkg]
                key = frozenset(members)
                if key in cycles:
                    cycles[key].attributes.setdefault("reported_by", []).append(pkg)
                    continue
                attrs["reported_by"] = [pkg]
                s = Smell(new_id(), TOOL, stype, Level.PACKAGE, members, attributes=attrs,
                          source=str(arch_file), description=desc)
                cycles[key] = s
                smells.append(s)
            elif stype is SmellType.UNSTABLE_DEPENDENCY:
                related = _list_after("unstable", desc)
                smells.append(Smell(new_id(), TOOL, stype, Level.PACKAGE, [pkg] + related, central=pkg,
                                    related=related, attributes=attrs, source=str(arch_file), description=desc))
            elif stype is SmellType.SCATTERED_FUNCTIONALITY:
                members = _list_after("scattered", desc) or [pkg]
                attrs["reported_by"] = [pkg]          # the package whose methods use the members together
                smells.append(Smell(new_id(), TOOL, stype, Level.PACKAGE, members, attributes=attrs,
                                    source=str(arch_file), description=desc))
            elif stype is SmellType.FEATURE_CONCENTRATION:
                text = desc.split("are:", 1)[1] if "are:" in desc else ""
                groups = [_split(g) for g in _GROUP.findall(text)]
                smells.append(Smell(new_id(), TOOL, stype, Level.PACKAGE, [pkg], central=pkg,
                                    groups=groups, attributes=attrs, source=str(arch_file), description=desc))
            elif stype is SmellType.DENSE_STRUCTURE:
                edges = [(m.group("src"), m.group("dst")) for m in _EDGE.finditer(desc)]
                members = list(dict.fromkeys([e[0] for e in edges] + [e[1] for e in edges]))
                attrs["dependencies"] = len(edges)
                attrs["edges"] = [list(e) for e in edges]
                smells.append(Smell(new_id(), TOOL, stype, Level.SYSTEM, members, attributes=attrs,
                                    source=str(arch_file), description=desc[:500]))
            else:  # god component, ambiguous interface
                smells.append(Smell(new_id(), TOOL, stype, Level.PACKAGE, [pkg], central=pkg,
                                    attributes=attrs, source=str(arch_file), description=desc))
    else:
        warnings.append(f"{folder}: ArchitectureSmells.csv missing")

    design_file = folder / "DesignSmells.csv"
    if design_file.exists():
        design = _read(design_file)
        design = design[design["Smell"].isin(DESIGN_TYPES)]
        resolve = _resolver(components) if not components.empty else (lambda s, p: s)
        class_cycles: dict[frozenset, Smell] = {}
        for row in design.itertuples(index=False):
            pkg, cls, kind, desc = str(row.Package), str(row.Class), str(row.Smell), str(row.Description or "")
            fq = f"{pkg}.{cls}" if pkg else cls
            stype = DESIGN_TYPES[kind]
            if stype is SmellType.CYCLIC_DEPENDENCY:
                members = [resolve(c, pkg) for c in _list_after("class_cycle", desc)] or [fq]
                key = frozenset(members)
                if key in class_cycles:
                    class_cycles[key].attributes.setdefault("reported_by", []).append(fq)
                    continue
                s = Smell(new_id(), TOOL, stype, Level.CLASS, members,
                          attributes={"reported_by": [fq]}, source=str(design_file), description=desc)
                class_cycles[key] = s
                smells.append(s)
            else:  # hub-like modularization
                incoming = [resolve(c, pkg) for c in _list_after("incoming", desc)]
                outgoing = [resolve(c, pkg) for c in _list_after("outgoing", desc)]
                related = list(dict.fromkeys(incoming + outgoing))
                smells.append(Smell(new_id(), TOOL, stype, Level.CLASS, [fq] + related, central=fq,
                                    related=related, attributes={"incoming": len(incoming), "outgoing": len(outgoing)},
                                    source=str(design_file), description=desc[:500]))

    return DetectorResult(TOOL, smells, components, None, warnings)
