"""Learned affinity: where does a class belong?

Training examples come from two sources, both available for any repository:

1. **Current placement** (self-supervised): in a maintained code base most
   classes are in a sensible package. For a sample of production classes the
   current package is the positive example and nearby packages of the same
   build module (packages the class depends on or is used by, and sibling
   packages) are negatives.
2. **Developer moves** (:mod:`.moves`): a class that developers moved from
   package P to package Q, and that still exists in Q, gives Q as the positive
   and P as a *hard* negative, weighted higher. These are exactly the
   decisions a move-class recommendation must reproduce.

Features (:meth:`dsarp.plan.facts.RepoFacts.features`) are computed with the
class itself removed from the package, so the model cannot just recognise the
current package. Limitation: features of historical moves are taken from the
code at the analysed commit, not from the commit before the move.

The model is a standardised logistic regression: interpretable (one weight per
feature, written to ``affinity.json``) and cheap enough to train per
repository or pooled over many repositories. :func:`evaluate` measures, with
cross-validation grouped by package (no package in both training and test
data), how often the correct package is ranked first (top-1) and the mean
reciprocal rank (MRR), for the learned model and for the fixed weights of
phase 4.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..plan.facts import AFFINITY_WEIGHTS, RepoFacts
from .moves import latest_per_class

FEATURES = ["structural_out", "structural_in", "cochange", "semantic", "package_name", "log_size"]


@dataclass
class AffinityModel:
    features: list[str]
    coef: list[float]
    intercept: float
    mean: list[float]
    std: list[float]
    trained_on: list[str] = field(default_factory=list)
    examples: int = 0
    evaluation: dict = field(default_factory=dict)

    def raw(self, f: dict[str, float]) -> float:
        z = self.intercept
        for name, c, m, s in zip(self.features, self.coef, self.mean, self.std):
            z += c * (f.get(name, 0.0) - m) / s
        return z

    def score(self, f: dict[str, float]) -> float:
        z = self.raw(f)
        return 1.0 / (1.0 + math.exp(-max(min(z, 30.0), -30.0)))

    def score_matrix(self, x: np.ndarray) -> np.ndarray:
        z = self.intercept + ((x - np.array(self.mean)) / np.array(self.std)) @ np.array(self.coef)
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=1))

    @classmethod
    def load(cls, path: str | Path) -> "AffinityModel":
        return cls(**json.loads(Path(path).read_text()))


def baseline_score(x: pd.DataFrame) -> np.ndarray:
    """The fixed weights of phase 4 (structural, co-change, names)."""
    w = AFFINITY_WEIGHTS
    structural = x["structural"] if "structural" in x else (x["structural_out"] + x["structural_in"]) / 2
    return (w["structural"] * structural + w["cochange"] * x["cochange"] + w["semantic"] * x["semantic"]).to_numpy()


# ---------------------------------------------------------------- dataset

def candidate_packages(rf: RepoFacts, c: str, exclude: set[str], k: int) -> list[str]:
    """Plausible other packages for ``c`` in its build module: connected packages first, then siblings."""
    module = rf.module.get(c)
    same_module = {p for p, mods in rf.pkg_modules.items() if module in mods}
    linked: dict[str, int] = {}
    for key in rf.out_edges.get(c, []) + rf.in_edges.get(c, []):
        other = key[1] if key[0] == c else key[0]
        p = rf.package[other]
        linked[p] = linked.get(p, 0) + rf.edges[key].weight
    out = [p for p, _ in sorted(linked.items(), key=lambda x: (-x[1], x[0])) if p in same_module and p not in exclude]
    parent = rf.package[c].rsplit(".", 1)[0]
    siblings = sorted(p for p in same_module if p.rsplit(".", 1)[0] == parent and p not in exclude and p not in out)
    return (out + siblings)[:k]


def build_dataset(rf: RepoFacts, moves: pd.DataFrame | None = None, repo: str = "", max_classes: int = 3000,
                  negatives: int = 6, move_weight: float = 3.0, seed: int = 0) -> pd.DataFrame:
    """One row per (class, candidate package) with features, label and query id."""
    rows = []
    moved = set()
    if moves is not None and len(moves):
        for r in latest_per_class(moves).itertuples(index=False):
            c, q, p = r.new_class, r.new_package, r.old_package
            if c not in rf.prod or rf.package.get(c) != q or p not in rf.pkg_classes:
                continue
            if rf.module.get(c) not in rf.pkg_modules.get(p, set()):
                continue
            moved.add(c)
            qid = f"{repo}:move:{c}"
            cands = [q, p] + candidate_packages(rf, c, {q, p}, negatives - 1)
            for pkg in cands:
                rows.append({"query": qid, "repo": repo, "source": "move", "class": c, "package": pkg,
                             "group": q, "label": int(pkg == q), "hard_negative": int(pkg == p),
                             "weight": move_weight, **rf.features(c, pkg)})
    pool = sorted(c for c in rf.prod if c not in moved)
    random.Random(seed).shuffle(pool)
    for c in pool[:max_classes]:
        cur = rf.package[c]
        cands = candidate_packages(rf, c, {cur}, negatives)
        if not cands:
            continue
        qid = f"{repo}:current:{c}"
        for pkg in [cur] + cands:
            rows.append({"query": qid, "repo": repo, "source": "current", "class": c, "package": pkg,
                         "group": cur, "label": int(pkg == cur), "hard_negative": 0, "weight": 1.0,
                         **rf.features(c, pkg)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- training and evaluation

def fit(df: pd.DataFrame, trained_on: list[str] | None = None, c: float = 1.0) -> AffinityModel:
    from sklearn.linear_model import LogisticRegression
    x = df[FEATURES].to_numpy(float)
    mean, std = x.mean(axis=0), x.std(axis=0)
    std[std == 0] = 1.0
    clf = LogisticRegression(C=c, max_iter=2000, class_weight="balanced")
    clf.fit((x - mean) / std, df["label"].to_numpy(), sample_weight=df["weight"].to_numpy())
    return AffinityModel(FEATURES, [float(v) for v in clf.coef_[0]], float(clf.intercept_[0]),
                         [float(v) for v in mean], [float(v) for v in std],
                         sorted(set(trained_on or df["repo"].unique())), int(df["query"].nunique()))


def ranking_metrics(df: pd.DataFrame, scores: np.ndarray) -> dict:
    """Top-1 accuracy and mean reciprocal rank of the positive package per query."""
    d = df[["query", "label", "source"]].assign(score=scores)
    top1, rr, n = [], [], 0
    by_source: dict[str, list] = {}
    for (q, src), g in d.groupby(["query", "source"], sort=False):
        if g["label"].sum() != 1 or len(g) < 2:
            continue
        pos = g.loc[g["label"] == 1, "score"].iloc[0]
        rank = 1 + int((g["score"] > pos).sum()) + 0.5 * int(((g["score"] == pos) & (g["label"] == 0)).sum())
        top1.append(rank == 1)
        rr.append(1.0 / rank)
        by_source.setdefault(src, []).append((rank == 1, 1.0 / rank))
        n += 1
    out = {"queries": n, "top1": round(float(np.mean(top1)), 4) if top1 else None,
           "mrr": round(float(np.mean(rr)), 4) if rr else None}
    for src, vals in by_source.items():
        out[f"{src}_queries"] = len(vals)
        out[f"{src}_top1"] = round(float(np.mean([v[0] for v in vals])), 4)
        out[f"{src}_mrr"] = round(float(np.mean([v[1] for v in vals])), 4)
    return out


def evaluate(df: pd.DataFrame, folds: int = 5) -> dict:
    """Cross-validation grouped by package: learned model vs fixed weights vs structure only."""
    from sklearn.model_selection import GroupKFold
    groups = df["group"].to_numpy()
    n_groups = len(set(groups))
    k = min(folds, n_groups)
    learned = np.zeros(len(df))
    if k >= 2:
        for tr, te in GroupKFold(n_splits=k).split(df, groups=groups):
            if df.iloc[tr]["label"].nunique() < 2:
                continue
            m = fit(df.iloc[tr])
            learned[te] = m.score_matrix(df.iloc[te][FEATURES].to_numpy(float))
    x = df[FEATURES]
    structural = ((x["structural_out"] + x["structural_in"]) / 2).to_numpy()
    return {"folds": k, "learned": ranking_metrics(df, learned),
            "fixed_weights": ranking_metrics(df, baseline_score(df.assign(structural=structural))),
            "structural_only": ranking_metrics(df, structural)}


def evaluate_leave_one_repo_out(df: pd.DataFrame) -> dict:
    """Train on all repositories but one, test on that one (transfer to unseen systems)."""
    out = {}
    for repo in sorted(df["repo"].unique()):
        tr, te = df[df["repo"] != repo], df[df["repo"] == repo]
        if tr.empty or tr["label"].nunique() < 2:
            continue
        m = fit(tr)
        x = te[FEATURES]
        structural = ((x["structural_out"] + x["structural_in"]) / 2).to_numpy()
        out[repo] = {"learned": ranking_metrics(te, m.score_matrix(x.to_numpy(float))),
                     "fixed_weights": ranking_metrics(te, baseline_score(te.assign(structural=structural)))}
    return out
