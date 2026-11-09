"""Delta debugging over the operations of a plan.

``ddmin`` (Zeller and Hildebrandt, 2002) finds a minimal subset of operations
that still fails a test (here: new compiler errors or a failing build).
``keep_passing`` builds the largest prefix-compatible subset that passes:
operations are added in plan order and kept only if the result still passes;
it first tries to drop only the operations found by ``ddmin``.
Both take a ``test(subset) -> bool`` (``True`` = passes) and count the tests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence


class Budget(Exception):
    pass


class Tester:
    __test__ = False

    def __init__(self, test: Callable[[list], bool], max_tests: int = 60):
        self.test, self.max_tests, self.calls = test, max_tests, 0
        self.cache: dict[tuple, bool] = {}

    def __call__(self, items: Sequence) -> bool:
        key = tuple(items)
        if key not in self.cache:
            if self.calls >= self.max_tests:
                raise Budget()
            self.calls += 1
            self.cache[key] = self.test(list(items))
        return self.cache[key]


def ddmin(items: list, fails: Callable[[list], bool]) -> list:
    """Minimal subset of ``items`` for which ``fails`` is true (``fails(items)`` must be true)."""
    n = 2
    cur = list(items)
    while len(cur) >= 2:
        size = max(len(cur) // n, 1)
        chunks = [cur[i:i + size] for i in range(0, len(cur), size)]
        reduced = False
        for ch in chunks:
            if fails(ch):
                cur, n, reduced = ch, 2, True
                break
        if not reduced:
            for ch in chunks:
                rest = [x for x in cur if x not in ch]
                if rest and fails(rest):
                    cur, n, reduced = rest, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(cur):
                break
            n = min(n * 2, len(cur))
    return cur


def keep_passing(items: list, passes: Callable[[list], bool]) -> tuple[list, list, str]:
    """Largest subset (in order) that passes; returns (kept, dropped, method)."""
    if passes(items):
        return list(items), [], "all"
    try:
        culprit = ddmin(items, lambda s: not passes(s))
        rest = [x for x in items if x not in culprit]
        if not rest or passes(rest):
            if len(culprit) == 1 or not rest:
                return rest, culprit, "ddmin"
    except Budget:
        pass
    kept, dropped = [], []
    try:
        for x in items:
            if passes(kept + [x]):
                kept.append(x)
            else:
                dropped.append(x)
    except Budget:
        done = set(kept) | set(dropped)
        dropped += [x for x in items if x not in done]
        return kept, dropped, "greedy (test budget used up)"
    return kept, dropped, "greedy"
