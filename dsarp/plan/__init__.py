"""Stage 5a and checks L1-L2: constraint-based refactoring plan generation.

* :mod:`.solver`: linear model with HiGHS and CP-SAT backends
* :mod:`.facts`: what may change for a group, and where things may go
* :mod:`.generator`: the plan model and the trade-off plans
* :mod:`.simulate`: apply a plan in memory and re-detect smells (L1, L3)
* :mod:`.checks`: Java preconditions and follow-up edits (L2)
* :mod:`.run`: plan all groups of a repository, rank, write results
"""
