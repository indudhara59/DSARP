"""Framework wiring: places outside Java code that name Java classes.

Moving or renaming such a class silently breaks the program unless the file
is updated too, so later stages either update these files with the change or
avoid moving the class. Found here:

* ``META-INF/services/<interface>``: ServiceLoader providers.
* ``META-INF/spring.factories`` and ``META-INF/spring/*.imports``: Spring Boot.
* XML files (OSGi Blueprint / Declarative Services, Spring, Struts, web.xml,
  ...): attributes whose value is a fully qualified class name.
* OSGi bundle activators (from the build information).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd

from .buildinfo import BuildInfo

SKIP_DIRS = {".git", "target", "build", "node_modules", ".gradle", ".idea", ".mvn"}
_FQCN = re.compile(r"^[a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)+$")
_XML_ATTR = re.compile(r"""\b(class|type|interface|implementation|activator|component|value|ref-class|class-name)\s*=\s*["']([\w$.]+)["']""")
_XML_TEXT = re.compile(r"<(servlet-class|filter-class|listener-class|class|bean-class|action-class)>\s*([\w$.]+)\s*</")
MAX_XML_BYTES = 2_000_000


def _walk(repo: Path):
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            yield Path(dirpath) / f


def scan(repo: str | Path, build: BuildInfo | None = None) -> pd.DataFrame:
    """All wiring references in a repository.

    Columns: ``file`` (repository-relative), ``kind``, ``class`` (the class
    named), ``key`` (interface or XML attribute). Values that are not
    fully qualified names are skipped; checking that the class exists in the
    repository happens when the program model is assembled.
    """
    repo = Path(repo)
    rows = []
    for path in _walk(repo):
        rel = path.relative_to(repo).as_posix()
        parts = rel.split("/")
        if "META-INF" in parts and parts[-2] == "services" and parts[-3] == "META-INF":
            iface = parts[-1]
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.split("#", 1)[0].strip()
                if _FQCN.match(line):
                    rows.append((rel, "service_provider", line, iface))
            if _FQCN.match(iface):
                rows.append((rel, "service_interface", iface, iface))
        elif rel.endswith("META-INF/spring.factories"):
            text = path.read_text(encoding="utf-8", errors="replace").replace("\\\n", "")
            for line in text.splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, values = line.split("=", 1)
                    for v in values.split(","):
                        if _FQCN.match(v.strip()):
                            rows.append((rel, "spring_factories", v.strip(), key.strip()))
        elif "/META-INF/spring/" in "/" + rel and rel.endswith(".imports"):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.split("#", 1)[0].strip()
                if _FQCN.match(line):
                    rows.append((rel, "spring_imports", line, parts[-1]))
        elif rel.endswith(".xml") and "/src/test/" not in "/" + rel:
            try:
                if path.stat().st_size > MAX_XML_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _XML_ATTR.finditer(text):
                if _FQCN.match(m.group(2)):
                    rows.append((rel, "xml_reference", m.group(2), m.group(1)))
            for m in _XML_TEXT.finditer(text):
                if _FQCN.match(m.group(2)):
                    rows.append((rel, "xml_reference", m.group(2), m.group(1)))
    if build is not None:
        for mod in build.modules.values():
            if mod.activator and _FQCN.match(mod.activator):
                pom = "pom.xml" if mod.path == "." else f"{mod.path}/pom.xml"
                rows.append((pom, "bundle_activator", mod.activator, "Bundle-Activator"))
    df = pd.DataFrame(rows, columns=["file", "kind", "class", "key"]).drop_duplicates()
    return df.reset_index(drop=True)
