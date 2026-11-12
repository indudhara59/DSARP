"""Per-repository configuration.

Every repository is described by one YAML file, so the pipeline contains no
project-specific code. Example::

    name: tika
    repo_url: https://github.com/apache/tika
    commit: 697d7c047daf1f661a4ed067bbc8f9c58bb6faa2
    language: java
    build: maven
    detectors:
      - tool: arcan
        path: Arcan/Tika
      - tool: designite
        path: Designite/Tika
    protected_api: [org.apache.tika.parser.Parser]
    exclude_paths: ["**/src/test/**"]

Relative detector paths are resolved against a data root (``--data-root`` or
the ``DSARP_DATA`` environment variable).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_TOOLS = {"arcan", "designite"}
SUPPORTED_BUILDS = {"auto", "maven", "gradle"}


class ConfigError(ValueError):
    """Raised when a repository configuration is invalid."""


@dataclass
class DetectorInput:
    """Location of one detector's output.

    Attributes:
        tool: ``arcan`` or ``designite``.
        path: Folder with the tool's CSV files.
        module: Build module this output covers, for detectors that were run
            per module (e.g. Designite on ``jaas`` only). ``None`` means the
            whole repository.
        path_root: Absolute path prefix used on the machine where the
            detector ran (Designite writes absolute file paths). Optional; it
            is inferred when missing.
        command: How to run the detector (optional), with ``{repo}`` (source
            folder) and ``{out}`` (output folder) as placeholders, e.g.
            ``java -jar /opt/DesigniteJava.jar -i {repo} -o {out}``. With a
            command, ``dsarp detect`` runs the tool on the checkout at the
            configured commit and writes its output to ``path`` (default
            ``detected/<name>/<tool>`` under the data root), so the input always
            matches the analysed code; stage 8 can run it on the changed code.
    """

    tool: str
    path: Path
    module: str | None = None
    path_root: str | None = None
    command: str | None = None


@dataclass
class RepoConfig:
    """Settings for one repository and commit."""

    name: str
    repo_url: str
    commit: str | None
    detectors: list[DetectorInput]
    language: str = "java"
    build: str = "auto"
    protected_api: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    architecture_rules: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @property
    def detector_tools(self) -> list[str]:
        return [d.tool for d in self.detectors]


def default_data_root() -> Path | None:
    """Data root from the ``DSARP_DATA`` environment variable, if set."""
    value = os.environ.get("DSARP_DATA")
    return Path(value) if value else None


def load_config(path: str | Path, data_root: str | Path | None = None) -> RepoConfig:
    """Read and validate a repository configuration file.

    Args:
        path: YAML file.
        data_root: Folder that relative detector paths are resolved against.
            Defaults to ``DSARP_DATA``, then to the config file's folder.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    root = Path(data_root) if data_root else (default_data_root() or path.parent)
    return parse_config(raw, root, source=str(path))


def parse_config(raw: dict[str, Any], data_root: Path, source: str = "<config>") -> RepoConfig:
    """Build a :class:`RepoConfig` from an already-parsed mapping."""
    for key in ("name", "repo_url", "detectors"):
        if key not in raw:
            raise ConfigError(f"{source}: missing required key '{key}'")

    build = str(raw.get("build", "auto")).lower()
    if build not in SUPPORTED_BUILDS:
        raise ConfigError(f"{source}: build must be one of {sorted(SUPPORTED_BUILDS)}, got '{build}'")
    language = str(raw.get("language", "java")).lower()
    if language != "java":
        raise ConfigError(f"{source}: only 'java' is supported for now, got '{language}'")

    detectors: list[DetectorInput] = []
    if not isinstance(raw["detectors"], list) or not raw["detectors"]:
        raise ConfigError(f"{source}: 'detectors' must be a non-empty list")
    for i, entry in enumerate(raw["detectors"]):
        if not isinstance(entry, dict) or "tool" not in entry or ("path" not in entry and "command" not in entry):
            raise ConfigError(f"{source}: detectors[{i}] needs 'tool' and 'path' (or a 'command' to run it)")
        tool = str(entry["tool"]).lower()
        if tool not in SUPPORTED_TOOLS:
            raise ConfigError(f"{source}: detectors[{i}].tool must be one of {sorted(SUPPORTED_TOOLS)}, got '{tool}'")
        mod = entry.get("module")
        p = Path(entry.get("path") or f"detected/{raw['name']}/{tool}{'_' + str(mod) if mod else ''}")
        detectors.append(DetectorInput(
            tool=tool,
            path=p if p.is_absolute() else data_root / p,
            module=mod,
            path_root=entry.get("path_root"),
            command=str(entry["command"]) if entry.get("command") else None,
        ))

    commit = raw.get("commit")
    return RepoConfig(
        name=str(raw["name"]),
        repo_url=str(raw["repo_url"]),
        commit=str(commit) if commit else None,
        detectors=detectors,
        language=language,
        build=build,
        protected_api=list(raw.get("protected_api") or []),
        exclude_paths=list(raw.get("exclude_paths") or []),
        architecture_rules=dict(raw.get("architecture_rules") or {}),
        notes=str(raw.get("notes") or ""),
    )
