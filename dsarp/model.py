"""Tool-independent data model shared by every pipeline stage.

Every detector adapter converts its own output into these structures, so the
later stages never depend on a particular tool's file format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SmellType(str, Enum):
    """Canonical architectural smell types (union of Arcan and Designite)."""

    CYCLIC_DEPENDENCY = "cyclic_dependency"
    HUB_LIKE_DEPENDENCY = "hub_like_dependency"
    UNSTABLE_DEPENDENCY = "unstable_dependency"
    GOD_COMPONENT = "god_component"
    FEATURE_CONCENTRATION = "feature_concentration"
    SCATTERED_FUNCTIONALITY = "scattered_functionality"
    DENSE_STRUCTURE = "dense_structure"
    AMBIGUOUS_INTERFACE = "ambiguous_interface"
    UNKNOWN = "unknown"


class Level(str, Enum):
    """Granularity of the elements a smell affects."""

    PACKAGE = "package"
    CLASS = "class"
    SYSTEM = "system"


@dataclass
class Smell:
    """One smell instance reported by one detector.

    Attributes:
        smell_id: Stable id, unique within a repository run (``<tool>:<local id>``).
        tool: Detector that reported it (``arcan``, ``designite``).
        smell_type: Canonical smell type.
        level: Package, class or whole-system smell.
        members: Fully qualified names of the affected packages or classes.
        central: The element the smell is centred on, if the detector names one
            (hub of a hub-like dependency, the depending package of an
            unstable dependency).
        related: Further elements the detector mentions, e.g. the less stable
            packages of an unstable dependency.
        groups: For feature concentration, the independent class groups.
        attributes: Detector-specific numbers (severity, size, PageRank, ...).
        source: Which input file or module run the smell came from.
        description: Raw text from the detector, if any.
    """

    smell_id: str
    tool: str
    smell_type: SmellType
    level: Level
    members: list[str]
    central: str | None = None
    related: list[str] = field(default_factory=list)
    groups: list[list[str]] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    description: str = ""


@dataclass
class DetectorResult:
    """Everything one detector adapter produced for one repository.

    Attributes:
        tool: Detector name.
        smells: Smell instances.
        components: One row per package or class with metrics. Required columns:
            ``name``, ``kind`` (``package`` or ``class``), ``file_path``
            (repository-relative, empty for packages), ``module`` (build module
            path, ``.`` for the root), plus any numeric metric columns.
        version: The commit or version id the detector reports, if any.
        warnings: Problems found while reading the input.
    """

    tool: str
    smells: list[Smell]
    components: Any  # pandas.DataFrame; typed loosely to keep this module light
    version: str | None = None
    warnings: list[str] = field(default_factory=list)
