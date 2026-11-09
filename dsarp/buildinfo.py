"""Build structure: modules and the dependencies allowed between them.

Supports Maven (every ``pom.xml``, including modules only listed in profiles)
and Gradle (``settings.gradle[.kts]`` and ``project(':x')`` dependencies).
Other build systems give an empty module graph; later stages then apply no
module constraints.

For OSGi bundles built with the maven-bundle-plugin or bnd, the exported
packages and the bundle activator are read too: exported packages define the
public API, and the activator is wiring that ties a class to its name.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", "target", "build", "node_modules", "src", ".gradle", ".mvn", ".idea"}


@dataclass
class Module:
    path: str                      # repository-relative folder, "." for the root
    name: str                      # artifactId or Gradle project path
    group: str = ""
    depends_on: list[str] = field(default_factory=list)   # module paths
    exported_packages: list[str] | None = None            # OSGi Export-Package patterns, if any
    activator: str | None = None


@dataclass
class BuildInfo:
    system: str                    # maven | gradle | none
    modules: dict[str, Module]

    def edges(self) -> list[tuple[str, str]]:
        return [(m.path, d) for m in self.modules.values() for d in m.depends_on]

    def to_json(self) -> dict:
        return {"system": self.system, "modules": {
            p: {"name": m.name, "group": m.group, "depends_on": m.depends_on,
                "exported_packages": m.exported_packages, "activator": m.activator}
            for p, m in sorted(self.modules.items())}}


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _find_files(repo: Path, names: set[str]) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        out += [Path(dirpath) / f for f in filenames if f in names]
    return sorted(out)


def _split_osgi_list(value: str) -> list[str]:
    """Split an OSGi header (``a.b;version=1,c.*``) into package patterns."""
    value = re.sub(r"\s+", "", value)
    items, depth, cur = [], 0, ""
    for ch in value:
        if ch == '"':
            depth ^= 1
        if ch == "," and not depth:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    return [i.split(";", 1)[0] for i in items if i and not i.startswith("${")]


def read_maven(repo: Path) -> BuildInfo:
    poms = _find_files(repo, {"pom.xml"})
    modules: dict[str, Module] = {}
    raw_deps: dict[str, list[tuple[str, str]]] = {}
    for pom in poms:
        try:
            root = _strip_ns(ET.parse(pom).getroot())
        except ET.ParseError:
            continue
        rel = pom.parent.relative_to(repo).as_posix() or "."
        group = _text(root, "groupId") or _text(root, "parent/groupId")
        artifact = _text(root, "artifactId")
        m = Module(path=rel, name=artifact, group=group)
        deps = []
        for d in root.findall("dependencies/dependency"):
            g = _text(d, "groupId").replace("${project.groupId}", group).replace("${groupId}", group)
            deps.append((g, _text(d, "artifactId")))
        raw_deps[rel] = deps
        for instr in root.iter("instructions"):
            exp = _text(instr, "Export-Package")
            if exp:
                m.exported_packages = _split_osgi_list(exp)
            act = _text(instr, "Bundle-Activator")
            if act:
                m.activator = act
        _read_bnd(pom.parent, m)
        modules[rel] = m
    by_coord = {(m.group, m.name): p for p, m in modules.items()}
    by_artifact: dict[str, list[str]] = {}
    for p, m in modules.items():
        by_artifact.setdefault(m.name, []).append(p)
    for p, deps in raw_deps.items():
        targets = []
        for g, a in deps:
            t = by_coord.get((g, a))
            if t is None and len(by_artifact.get(a, [])) == 1 and (not g or g.startswith("${")):
                t = by_artifact[a][0]
            if t and t != p:
                targets.append(t)
        modules[p].depends_on = sorted(set(targets))
    return BuildInfo("maven", modules)


def _read_bnd(folder: Path, m: Module) -> None:
    for name in ("bnd.bnd", "osgi.bnd"):
        f = folder / name
        if f.exists():
            text = f.read_text(encoding="utf-8", errors="replace").replace("\\\n", "")
            for line in text.splitlines():
                if line.startswith("Export-Package:"):
                    m.exported_packages = _split_osgi_list(line.split(":", 1)[1])
                elif line.startswith("Bundle-Activator:"):
                    m.activator = line.split(":", 1)[1].strip()


_GRADLE_INCLUDE = re.compile(r"""include\s*\(?\s*((?:["'][^"']+["']\s*,?\s*)+)\)?""")
_GRADLE_PROJECT_DIR = re.compile(r"""project\(\s*["']([^"']+)["']\s*\)\.projectDir\s*=\s*(?:file|new File)\(\s*(?:rootDir\s*,\s*)?["']([^"']+)["']""")
_GRADLE_PROJECT_DEP = re.compile(r"""project\(\s*(?:path\s*:\s*)?["']([^"']+)["']""")
_GRADLE_ACCESSOR = re.compile(r"\bprojects\.([\w.]+)")


def read_gradle(repo: Path) -> BuildInfo:
    settings = [repo / "settings.gradle", repo / "settings.gradle.kts"]
    text = "\n".join(s.read_text(encoding="utf-8", errors="replace") for s in settings if s.exists())
    projects = [":"]
    for m in _GRADLE_INCLUDE.finditer(text):
        for name in re.findall(r"""["']([^"']+)["']""", m.group(1)):
            projects.append(name if name.startswith(":") else ":" + name)
    dirs = {p: (p.strip(":").replace(":", "/") or ".") for p in projects}
    for m in _GRADLE_PROJECT_DIR.finditer(text):
        name = m.group(1) if m.group(1).startswith(":") else ":" + m.group(1)
        dirs[name] = m.group(2).strip("/")
    modules = {d: Module(path=d, name=p) for p, d in dirs.items()}
    accessor = {p.strip(":").replace(":", ".").replace("-", "").lower(): d for p, d in dirs.items()}
    for p, d in dirs.items():
        deps = []
        for build in ("build.gradle", "build.gradle.kts"):
            f = repo / d / build
            if f.exists():
                body = f.read_text(encoding="utf-8", errors="replace")
                for dep in _GRADLE_PROJECT_DEP.findall(body):
                    deps.append(dirs.get(dep if dep.startswith(":") else ":" + dep))
                for acc in _GRADLE_ACCESSOR.findall(body):
                    deps.append(accessor.get(acc.replace("-", "").lower()))
        modules[d].depends_on = sorted({x for x in deps if x and x != d})
        _read_bnd(repo / d, modules[d])
    return BuildInfo("gradle", modules)


def read_build(repo: str | Path, system: str = "auto") -> BuildInfo:
    """Read the module structure; ``system`` is ``auto``, ``maven`` or ``gradle``."""
    repo = Path(repo)
    if system == "maven" or (system == "auto" and (repo / "pom.xml").exists()):
        return read_maven(repo)
    if system == "gradle" or (system == "auto" and any((repo / f).exists() for f in
                                                       ("settings.gradle", "settings.gradle.kts", "build.gradle"))):
        return read_gradle(repo)
    return BuildInfo("none", {})


def module_for(folder: str, modules: dict[str, Module]) -> str | None:
    """Build module that contains a repository-relative folder (longest prefix)."""
    best = None
    for p in modules:
        if p == "." or folder == p or folder.startswith(p + "/"):
            if best is None or (p != "." and (best == "." or len(p) > len(best))):
                best = p
    return best
