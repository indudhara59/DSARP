"""Baseline plan generators for the evaluation (same candidate operations, same simulation).

* :func:`nsga2`: multi-objective genetic algorithm (Deb et al. 2002), the
  approach used in much of the search-based refactoring literature. Genome: a
  bit per candidate operation; objectives (all minimised): remaining smell
  (1 - benefit), change cost, new smells. From the final Pareto front the
  plan with the best plan score (same score as DSARP) is reported.
* :func:`random_search`: random subsets of candidate operations.
* :func:`llm_only`: one request to the language model, validated, no search.

All baselines draw from the same candidate operations (:class:`ActionSpace`
on the original code, plus operations seeded from the solver's candidate
moves) and are evaluated by the same whole-repository simulation and score,
with the same budget of evaluations.
"""

from __future__ import annotations

import random
import time
from types import SimpleNamespace

from ..plan.facts import GroupFacts, RepoFacts
from ..plan.generator import Plan
from ..plan.run import score as plan_score
from ..plan.simulate import Simulator
from ..search.actions import Action, ActionSpace, apply_action
from ..search.llm import LLMError, NoLLM, extract_json
from ..search.mcts import SYSTEM


class Evaluator:
    """Shared, cached evaluation of operation sets."""

    def __init__(self, rf: RepoFacts, gf: GroupFacts, sim: Simulator):
        self.rf, self.gf, self.sim = rf, gf, sim
        self.space = ActionSpace(rf, gf, max_actions=60)
        self.central = gf.smell.get("central", "") if gf.smell_type != "cyclic_dependency" else ""
        self.cache: dict[frozenset, dict] = {}

    def candidates(self) -> list[Action]:
        acts = [a for a in self.space.actions({}) if a.kind == "add"]
        # alternative targets for every movable class of the group (not only problem dependencies)
        seen = {a.key for a in acts}
        for c, opts in self.gf.movable.items():
            for q, aff in opts[1:]:
                op = {"op": "move_class", "class": c, "from_package": self.rf.package[c], "to_package": q,
                      "affinity": aff, "module": self.rf.module.get(c, "")}
                a = Action(f"move:{c}@{q}", "add", (op,), 0.0, f"move {c} to {q}")
                if a.key not in seen and len(acts) < 120:
                    acts.append(a)
                    seen.add(a.key)
        return acts

    def evaluate(self, ops: dict[str, dict]) -> dict:
        key = frozenset((k, str(v.get("to_package", ""))) for k, v in ops.items())
        if key in self.cache:
            return self.cache[key]
        if not ops:
            r = {"score": 0.0, "benefit": 0.0, "new_smells": 0.0, "cost": 0.0}
        else:
            sim = self.sim.run(list(ops.values()), self.gf.smell_type, self.gf.core, self.central)
            cost = sum(self.space.cost(o) for o in ops.values())
            sc = plan_score(SimpleNamespace(change_cost=cost), sim, {"made_public": 0}, self.gf.smell_type)
            r = {"score": sc["score"], "benefit": sc["benefit"], "new_smells": sc["new_smells"], "cost": cost}
        self.cache[key] = r
        return r

    @staticmethod
    def decode(genome: list[int], acts: list[Action]) -> dict[str, dict]:
        ops: dict[str, dict] = {}
        for bit, a in zip(genome, acts):
            if bit:
                ops = apply_action(ops, a)
        return ops

    def plan(self, name: str, ops: dict[str, dict], t0: float, extra: dict) -> Plan | None:
        if not ops:
            return None
        ev = self.evaluate(ops)
        return Plan(self.gf.group_id, name, "heuristic", None, round(ev["cost"], 3), 0.0, None,
                    round(time.time() - t0, 2), name, {"evaluations": len(self.cache), **extra}, list(ops.values()))


# ---------------------------------------------------------------- NSGA-II

def _dominates(a: tuple, b: tuple) -> bool:
    return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))


def _fronts(objs: list[tuple]) -> list[list[int]]:
    n = len(objs)
    dominated = [[] for _ in range(n)]
    count = [0] * n
    fronts: list[list[int]] = [[]]
    for i in range(n):
        for j in range(n):
            if i != j and _dominates(objs[i], objs[j]):
                dominated[i].append(j)
            elif i != j and _dominates(objs[j], objs[i]):
                count[i] += 1
        if count[i] == 0:
            fronts[0].append(i)
    k = 0
    while fronts[k]:
        nxt = []
        for i in fronts[k]:
            for j in dominated[i]:
                count[j] -= 1
                if count[j] == 0:
                    nxt.append(j)
        k += 1
        fronts.append(nxt)
    return fronts[:-1]


def _crowding(front: list[int], objs: list[tuple]) -> dict[int, float]:
    d = {i: 0.0 for i in front}
    for m in range(len(objs[0])):
        s = sorted(front, key=lambda i: objs[i][m])
        lo, hi = objs[s[0]][m], objs[s[-1]][m]
        d[s[0]] = d[s[-1]] = float("inf")
        if hi == lo:
            continue
        for k in range(1, len(s) - 1):
            d[s[k]] += (objs[s[k + 1]][m] - objs[s[k - 1]][m]) / (hi - lo)
    return d


def nsga2(ev: Evaluator, evaluations: int = 600, population: int = 40, seed: int = 0,
          time_limit: float = 300.0) -> tuple[Plan | None, dict]:
    rnd = random.Random(seed)
    acts = ev.candidates()
    k = len(acts)
    t0 = time.time()
    if k == 0:
        return None, {"candidates": 0}

    def objectives(g: list[int]) -> tuple:
        r = ev.evaluate(ev.decode(g, acts))
        return (1.0 - r["benefit"], r["cost"], r["new_smells"])

    density = min(0.5, 5.0 / k)
    pop = [[int(rnd.random() < density) for _ in range(k)] for _ in range(population)]
    used = 0
    gen = 0
    while used < evaluations and time.time() - t0 < time_limit:
        objs = [objectives(g) for g in pop]
        fronts = _fronts(objs)
        rank, crowd = {}, {}
        for r, f in enumerate(fronts):
            crowd.update(_crowding(f, objs))
            for i in f:
                rank[i] = r

        def pick() -> list[int]:
            a, b = rnd.randrange(len(pop)), rnd.randrange(len(pop))
            better = a if (rank[a], -crowd[a]) < (rank[b], -crowd[b]) else b
            return pop[better]

        children = []
        while len(children) < population:
            p1, p2 = pick(), pick()
            child = [x if rnd.random() < 0.5 else y for x, y in zip(p1, p2)]
            child = [1 - x if rnd.random() < 1.0 / k else x for x in child]
            children.append(child)
        used += population
        both = pop + children
        objs2 = [objectives(g) for g in both]
        nxt: list[int] = []
        for f in _fronts(objs2):
            if len(nxt) + len(f) <= population:
                nxt += f
            else:
                cd = _crowding(f, objs2)
                nxt += sorted(f, key=lambda i: -cd[i])[: population - len(nxt)]
                break
        pop = [both[i] for i in nxt]
        gen += 1
    objs = [objectives(g) for g in pop]
    front = _fronts(objs)[0]
    best = max(front, key=lambda i: ev.evaluate(ev.decode(pop[i], acts))["score"])
    ops = ev.decode(pop[best], acts)
    info = {"candidates": k, "generations": gen, "pareto_front": len(set(objs[i] for i in front))}
    return ev.plan("nsga2", ops, t0, info), info


# ---------------------------------------------------------------- random search

def random_search(ev: Evaluator, evaluations: int = 600, seed: int = 0, time_limit: float = 300.0):
    rnd = random.Random(seed)
    acts = ev.candidates()
    t0 = time.time()
    best, best_ops = 0.0, {}
    for _ in range(evaluations):
        if not acts or time.time() - t0 > time_limit:
            break
        size = rnd.randint(1, min(len(acts), 15))
        ops: dict[str, dict] = {}
        for a in rnd.sample(acts, size):
            ops = apply_action(ops, a)
        r = ev.evaluate(ops)["score"]
        if r > best:
            best, best_ops = r, ops
    return ev.plan("random", best_ops, t0, {"candidates": len(acts)}), {"candidates": len(acts)}


# ---------------------------------------------------------------- language model only

def llm_only(ev: Evaluator, llm) -> tuple[Plan | None, dict]:
    if llm is None or isinstance(llm, NoLLM):
        return None, {"skipped": "no language model"}
    t0 = time.time()
    acts = ev.candidates()[:60]
    gf = ev.gf
    prompt = "\n".join([f"Smell: {gf.smell_type} ({gf.group_id}), packages: {', '.join(gf.core)}",
                        "Candidate actions (id: action):"] + [f"  {i}: {a.label}" for i, a in enumerate(acts)]) + \
        '\n\nChoose the actions that remove the smell with the fewest changes. ' \
        'Answer with JSON: {"actions": [ids], "reason": "one sentence"}'
    try:
        ans = extract_json(llm.complete(SYSTEM, prompt))
    except LLMError as e:
        return None, {"error": str(e)}
    ids = ans.get("actions", []) if isinstance(ans, dict) else ans
    ops: dict[str, dict] = {}
    valid = 0
    for i in ids:
        try:
            ops = apply_action(ops, acts[int(i)])
            valid += 1
        except (ValueError, IndexError, TypeError):
            continue
    info = {"proposed": len(ids), "valid": valid}
    return ev.plan("llm_only", ops, t0, info), info
