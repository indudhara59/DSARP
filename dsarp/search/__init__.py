"""Stage 5b: tree search over refactoring operations, guided by learned
affinity and (optionally) a language model.

Used when the constraint model is too large or too coarse: the search works
directly on the result of the whole-repository simulation, so it can repair a
solver plan (drop operations that create new smells) or find plans for
groups where the solver found none.

* :mod:`.llm`   pluggable language-model clients (none, OpenAI-compatible HTTP, e.g. vLLM)
* :mod:`.actions` candidate operations for the current state of a group
* :mod:`.mcts`  Monte Carlo tree search with prior probabilities (PUCT)
"""
