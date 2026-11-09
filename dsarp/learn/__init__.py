"""Stage 4: learned priors.

* :mod:`.moves` mines the class moves developers made in the repository's history;
* :mod:`.affinity` learns where a class belongs (its package) from the current
  code and from those moves, and evaluates the model against the fixed weights.

The learned affinity replaces the fixed weights in the plan model
(:meth:`dsarp.plan.facts.RepoFacts.affinity`) and gives the priors of the tree
search (:mod:`dsarp.search`).
"""
