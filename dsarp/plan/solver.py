"""A small mixed-integer linear model with two interchangeable solvers.

The plan generator describes its problem once, with :class:`LinearModel`, and
solves it with whichever backend is available:

* ``cpsat``: Google OR-Tools CP-SAT (used when ``ortools`` is installed,
  e.g. in the Apptainer image). Coefficients are scaled to integers.
* ``highs``: the HiGHS MILP solver shipped with SciPy (always available).

Both return the variable values, the objective, whether the solution is
proven optimal, and the relative optimality gap when stopped by the time
limit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

SCALE = 1000  # CP-SAT needs integer coefficients


@dataclass
class Var:
    index: int
    name: str
    lb: int
    ub: int


@dataclass
class Solution:
    status: str                    # optimal | feasible | infeasible | error
    objective: float | None
    values: dict[str, float]
    gap: float | None
    seconds: float
    backend: str
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("optimal", "feasible")

    def value(self, name: str) -> float:
        return self.values.get(name, 0.0)


@dataclass
class LinearModel:
    """Variables are integers in [lb, ub]; binaries have bounds [0, 1]."""

    vars: list[Var] = field(default_factory=list)
    by_name: dict[str, Var] = field(default_factory=dict)
    rows: list[tuple[dict[int, float], float, float, str]] = field(default_factory=list)
    objective: dict[int, float] = field(default_factory=dict)

    def var(self, name: str, lb: int = 0, ub: int = 1) -> str:
        if name in self.by_name:
            return name
        v = Var(len(self.vars), name, lb, ub)
        self.vars.append(v)
        self.by_name[name] = v
        return name

    def binary(self, name: str) -> str:
        return self.var(name, 0, 1)

    def has(self, name: str) -> bool:
        return name in self.by_name

    def _terms(self, terms: dict[str, float]) -> dict[int, float]:
        out: dict[int, float] = {}
        for n, c in terms.items():
            if c:
                i = self.by_name[n].index
                out[i] = out.get(i, 0.0) + c
        return out

    def add(self, terms: dict[str, float], lb: float = -np.inf, ub: float = np.inf, name: str = "") -> None:
        """``lb <= sum(c * var) <= ub``."""
        t = self._terms(terms)
        if not t:
            if lb > 0 + 1e-9 or ub < 0 - 1e-9:
                raise ValueError(f"constant constraint '{name}' cannot hold")
            return
        self.rows.append((t, lb, ub, name))

    def minimize(self, terms: dict[str, float]) -> None:
        self.objective = self._terms(terms)

    def add_objective(self, terms: dict[str, float]) -> None:
        for i, c in self._terms(terms).items():
            self.objective[i] = self.objective.get(i, 0.0) + c

    @property
    def size(self) -> dict:
        return {"variables": len(self.vars), "constraints": len(self.rows)}


def available_backends() -> list[str]:
    out = []
    try:
        import ortools.sat.python.cp_model  # noqa: F401
        out.append("cpsat")
    except ImportError:
        pass
    try:
        from scipy.optimize import milp  # noqa: F401
        out.append("highs")
    except ImportError:
        pass
    return out


def solve(model: LinearModel, backend: str = "auto", time_limit: float = 60.0, workers: int | None = None) -> Solution:
    backends = available_backends()
    if backend == "auto":
        if not backends:
            raise RuntimeError("no solver available: install scipy (HiGHS) or ortools (CP-SAT)")
        backend = backends[0]
    if backend == "cpsat":
        if workers is None:
            import os
            workers = int(os.environ.get("SLURM_CPUS_PER_TASK") or os.cpu_count() or 8)
        return _solve_cpsat(model, time_limit, workers)
    if backend == "highs":
        return _solve_highs(model, time_limit)
    raise ValueError(f"unknown backend '{backend}'")


def _solve_highs(model: LinearModel, time_limit: float) -> Solution:
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import lil_matrix

    n = len(model.vars)
    c = np.zeros(n)
    for i, v in model.objective.items():
        c[i] = v
    lb = np.array([v.lb for v in model.vars], dtype=float)
    ub = np.array([v.ub for v in model.vars], dtype=float)
    constraints = []
    if model.rows:
        a = lil_matrix((len(model.rows), n))
        lo, hi = np.empty(len(model.rows)), np.empty(len(model.rows))
        for r, (terms, l, u, _) in enumerate(model.rows):
            for i, v in terms.items():
                a[r, i] = v
            lo[r], hi[r] = l, u
        constraints.append(LinearConstraint(a.tocsr(), lo, hi))
    start = time.time()
    res = milp(c, constraints=constraints, integrality=np.ones(n), bounds=Bounds(lb, ub),
               options={"time_limit": time_limit, "disp": False, "mip_rel_gap": 1e-6})
    secs = time.time() - start
    if res.x is None:
        status = "infeasible" if res.status == 2 else "error"
        return Solution(status, None, {}, None, secs, "highs", res.message)
    values = {v.name: float(round(res.x[v.index])) for v in model.vars}
    optimal = res.status == 0
    gap = getattr(res, "mip_gap", None)
    return Solution("optimal" if optimal else "feasible", float(res.fun), values,
                    0.0 if optimal else (None if gap is None else float(gap)), secs, "highs", res.message)


def _solve_cpsat(model: LinearModel, time_limit: float, workers: int) -> Solution:
    from ortools.sat.python import cp_model

    m = cp_model.CpModel()
    xs = [m.NewIntVar(v.lb, v.ub, v.name) for v in model.vars]

    def scaled(x: float) -> int:
        return int(round(x * SCALE))

    for terms, l, u, _ in model.rows:
        expr = sum(scaled(c) * xs[i] for i, c in terms.items())
        if np.isfinite(l):
            m.Add(expr >= scaled(l))
        if np.isfinite(u):
            m.Add(expr <= scaled(u))
    m.Minimize(sum(scaled(c) * xs[i] for i, c in model.objective.items()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = workers
    start = time.time()
    st = solver.Solve(m)
    secs = time.time() - start
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Solution("infeasible" if st == cp_model.INFEASIBLE else "error", None, {}, None, secs, "cpsat",
                        solver.StatusName(st))
    obj = solver.ObjectiveValue() / SCALE
    bound = solver.BestObjectiveBound() / SCALE
    gap = 0.0 if st == cp_model.OPTIMAL else (abs(obj - bound) / max(abs(obj), 1e-9))
    values = {v.name: float(solver.Value(xs[v.index])) for v in model.vars}
    return Solution("optimal" if st == cp_model.OPTIMAL else "feasible", obj, values, gap, secs, "cpsat",
                    solver.StatusName(st))
