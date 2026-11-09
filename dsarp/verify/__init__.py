"""Stage 8: verify plans on the real code.

* :mod:`.workspace` working copies of the checkout (git worktree)
* :mod:`.rewrite`   apply a plan to the sources (built-in engine; OpenRewrite recipe for moves)
* :mod:`.compile`   compile before and after with the JDK compiler and compare the errors;
                    detect the smells again on the compiled code
* :mod:`.build`     build the affected modules with Maven or Gradle and run the selected tests
* :mod:`.debug`     delta debugging: find the operations that break the build, keep the rest
* :mod:`.detectors` re-run Arcan / Designite with user-supplied commands
* :mod:`.run`       orchestration, patches and results
"""
