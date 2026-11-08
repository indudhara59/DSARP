"""Detector adapters: one module per smell detection tool.

Each adapter exposes ``load(inp: DetectorInput) -> DetectorResult``. To support
another detector, add a module with the same function and register it in
``ADAPTERS``.
"""

from __future__ import annotations

from typing import Callable

from ..config import DetectorInput
from ..model import DetectorResult
from . import arcan, designite

ADAPTERS: dict[str, Callable[[DetectorInput], DetectorResult]] = {
    "arcan": arcan.load,
    "designite": designite.load,
}


def load(inp: DetectorInput) -> DetectorResult:
    """Run the adapter registered for ``inp.tool``."""
    try:
        adapter = ADAPTERS[inp.tool]
    except KeyError as exc:
        raise ValueError(f"No adapter for detector '{inp.tool}'. Known: {sorted(ADAPTERS)}") from exc
    return adapter(inp)
