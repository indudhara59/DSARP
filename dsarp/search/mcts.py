"""Monte Carlo tree search over refactoring operations (PUCT, as in AlphaZero).

* **State**: a set of operations; the value of a state is the plan score of
  :mod:`dsarp.plan.run` computed from the whole-repository simulation (target
  smell removed, new smells elsewhere, change cost). No random rollouts are
  needed because every state can be evaluated directly.
* **Priors**: from :class:`.actions.ActionSpace` (dependency weight × learned
  affinity); actions proposed by the language model get a bonus.
* **Selection**: ``Q + c · P · sqrt(N_parent) / (1 + N)``; children are added
  by progressive widening (the k best-prior actions, k growing with visits).
* **Language model** (optional): at the start it proposes a plan from the
  candidate list; after the search it sees the simulation result of the best
  plan (what remains, what is new) and may propose changes, which are
  evaluated. Its suggestions are only accepted if they are in the candidate
  list and score better.

The best state seen during the search is returned as a plan.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from ..plan.facts import GroupFacts, RepoFacts
from ..plan.generator import Plan
from ..plan.simulate import Simulator
from .actions import Action, ActionSpace, apply_action, op_key
from .llm import LLMError, NoLLM, extract_json

SYSTEM = ("You are a software architect. You remove architectural smells from Java code bases with "
          "small, safe refactorings. Answer only with JSON.")


def state_key(ops: dict[str, dict]) -> frozenset:
    """Identity of a state: operation keys and, for moves, the target package."""
    return frozenset((k, str(v.get("to_package", ""))) for k, v in ops.items())


@dataclass
class Node:
    ops: dict[str, dict]
    prior: float = 1.0
    n: int = 0
    w: float = 0.0
    children: dict[str, "Node"] = field(default_factory=dict)
    actions: list[Action] | None = None

    @property
    def q(self) -> float:
        return self.w / self.n if self.n else 0.0


@dataclass
class SearchResult:
    plan: Plan | None
    evaluations: int
    iterations: int
    seconds: float
    best_score: float
    llm_calls: int
    llm_accepted: int
    trace: list[dict]


class Search:
    def __init__(self, rf: RepoFacts, gf: GroupFacts, sim: Simulator, llm=None, c_puct: float = 1.5,
                 max_depth: int = 15, widen: float = 1.0, llm_bonus: float = 3.0, checker=None):
        from ..plan.run import score as plan_score
        self.rf, self.gf, self.sim = rf, gf, sim
        self.space = ActionSpace(rf, gf)
        self.llm = llm or NoLLM()
        self.c, self.max_depth, self.widen, self.llm_bonus = c_puct, max_depth, widen, llm_bonus
        self.cache: dict[frozenset, tuple[float, object]] = {}
        self.suggested: set[str] = set()
        self.best: tuple[float, dict[str, dict]] = (-math.inf, {})
        self._score = plan_score
        self.central = gf.smell.get("central", "") if gf.smell_type != "cyclic_dependency" else ""
        self.llm_calls = self.llm_accepted = 0
        self.checker = checker
        self._public: dict[str, int] = {}

    def made_public(self, ops: dict[str, dict]) -> int:
        """Elements that must become public (from the checks, per operation; cached)."""
        if self.checker is None:
            return 0
        from ..plan.checks import summarise
        total = 0
        for k, o in ops.items():
            ck = k + ">" + str(o.get("to_package", ""))
            if ck not in self._public:
                after = {o["class"]: o["to_package"]} if o["op"] == "move_class" else {}
                self._public[ck] = summarise(self.checker.check([o], after))["made_public"]
            total += self._public[ck]
        return total

    # ------------------------------------------------------------- evaluation

    def evaluate(self, ops: dict[str, dict]) -> float:
        key = state_key(ops)
        if key in self.cache:
            return self.cache[key][0]
        if not ops:
            val, sim = 0.0, None
        else:
            sim = self.sim.run(list(ops.values()), self.gf.smell_type, self.gf.core, self.central)
            cost = sum(self.space.cost(o) for o in ops.values())
            sc = self._score(SimpleNamespace(change_cost=cost), sim, {"made_public": self.made_public(ops)},
                             self.gf.smell_type)
            val = sc["score"]
        self.cache[key] = (val, sim)
        if val > self.best[0]:
            self.best = (val, dict(ops))
        return val

    # ------------------------------------------------------------- tree

    def _expand(self, node: Node) -> None:
        if node.actions is None:
            node.actions = self.space.actions(node.ops)
            for a in node.actions:
                if a.key in self.suggested:
                    a.prior *= self.llm_bonus

    def _select(self, node: Node) -> tuple[Action | None, Node | None]:
        self._expand(node)
        k = max(2, int(2 + self.widen * math.sqrt(node.n + 1)))
        allowed = sorted(node.actions, key=lambda a: -a.prior)[:k]
        best, best_val = None, -math.inf
        for a in allowed:
            ch = node.children.get(a.key)
            n, q = (ch.n, ch.q) if ch else (0, self._prior_value(node, a))
            u = q + self.c * a.prior * math.sqrt(node.n + 1) / (1 + n)
            if u > best_val:
                best, best_val = a, u
        if best is None:
            return None, None
        if best.key not in node.children:
            node.children[best.key] = Node(apply_action(node.ops, best), best.prior)
        return best, node.children[best.key]

    def _prior_value(self, node: Node, a: Action) -> float:
        return node.q   # first-play urgency: unvisited children start at the parent's value

    def iterate(self) -> None:
        node, path = self.root, [self.root]
        for _ in range(self.max_depth):
            a, child = self._select(node)
            if child is None:
                break
            path.append(child)
            node = child
            if child.n == 0:
                break
        v = self.evaluate(node.ops)
        for n in path:
            n.n += 1
            n.w += v

    # ------------------------------------------------------------- language model

    def _describe(self, ops: dict[str, dict], actions: list[Action], limit: int = 40) -> str:
        gf = self.gf
        lines = [f"Smell: {gf.smell_type} ({gf.group_id}), packages: {', '.join(gf.core)}"]
        if self.central:
            lines.append(f"Central package: {self.central}")
        st_ops = list(ops.values())
        if st_ops:
            lines.append("Current plan:")
            lines += [f"  - {op_key(o)}" + (f" -> {o.get('to_package')}" if o["op"] == "move_class" else "")
                      for o in st_ops]
            _, sim = self.cache.get(state_key(ops), (None, None))
            if sim is not None:
                lines.append(f"Result: target before {sim.target_before}, after {sim.target_after}; "
                             f"new smells {sim.new_smells[:5]}")
        lines.append("Candidate actions (id: action):")
        for i, a in enumerate(actions[:limit]):
            lines.append(f"  {i}: {a.label}")
        return "\n".join(lines)

    def ask_llm(self, ops: dict[str, dict], instruction: str) -> list[Action]:
        if isinstance(self.llm, NoLLM):
            return []
        actions = self.space.actions(ops)
        prompt = (self._describe(ops, actions) + "\n\n" + instruction +
                  '\nAnswer with JSON: {"actions": [ids], "reason": "one sentence"}')
        try:
            ans = extract_json(self.llm.complete(SYSTEM, prompt))
            self.llm_calls += 1
        except LLMError:
            self.llm_calls += 1
            return []
        ids = ans.get("actions", []) if isinstance(ans, dict) else ans
        out = []
        for i in ids:
            try:
                out.append(actions[int(i)])
            except (ValueError, IndexError, TypeError):
                continue
        return out

    def _try_sequence(self, ops: dict[str, dict], acts: list[Action]) -> bool:
        before = self.best[0]
        cur = dict(ops)
        for a in acts:
            self.suggested.add(a.key)
            cur = apply_action(cur, a)
            self.evaluate(cur)
        improved = self.best[0] > before
        self.llm_accepted += int(improved)
        return improved

    # ------------------------------------------------------------- run

    def run(self, iterations: int = 2000, time_limit: float = 120.0, seeds: list[list[dict]] | None = None,
            llm_rounds: int = 2) -> SearchResult:
        t0 = time.time()
        trace = []
        self.root = Node({})
        self.evaluate({})
        for seed in seeds or []:
            ops = {op_key(o): o for o in seed}
            v = self.evaluate(ops)
            trace.append({"event": "seed", "operations": len(ops), "score": v})
        seeded = [(self.cache[state_key({op_key(o): o for o in sd})][0], sd) for sd in seeds or [] if sd]
        start_ops = {op_key(o): o for o in max(seeded, key=lambda x: x[0])[1]} if seeded else {}
        if start_ops:
            self.root = Node(dict(start_ops))           # repair the best seed
        proposal = self.ask_llm(self.root.ops, "Choose the actions (in order) that remove the smell with the "
                                               "fewest and most natural changes.")
        if proposal:
            self._try_sequence(self.root.ops, proposal)
            trace.append({"event": "llm_proposal", "actions": [a.label for a in proposal], "best": self.best[0]})
        i = 0
        while i < iterations and time.time() - t0 < time_limit:
            self.iterate()
            i += 1
        trace.append({"event": "search", "iterations": i, "best": self.best[0]})
        for _ in range(llm_rounds):
            if isinstance(self.llm, NoLLM) or not self.best[1]:
                break
            fix = self.ask_llm(self.best[1], "This plan was simulated (result above). Choose actions that remove "
                                             "what remains of the smell or undo operations that cause new smells.")
            if not fix or not self._try_sequence(self.best[1], fix):
                break
            trace.append({"event": "llm_repair", "actions": [a.label for a in fix], "best": self.best[0]})
        plan = None
        if self.best[1]:
            ops = list(self.best[1].values())
            cost = round(sum(self.space.cost(o) for o in ops), 3)
            plan = Plan(self.gf.group_id, "search", "heuristic", None, cost, 0.0, None,
                        round(time.time() - t0, 2), "mcts" + ("+llm" if self.llm_calls else ""),
                        {"evaluations": len(self.cache), "iterations": i}, ops)
        return SearchResult(plan, len(self.cache), i, round(time.time() - t0, 2), self.best[0],
                            self.llm_calls, self.llm_accepted, trace)
