# DSARP: Architectural Smell Refactoring Planner

DSARP turns the output of architectural smell detectors (Arcan, Designite) for
**any Java repository at any commit** into verified, step-by-step refactoring
plans: what to change, from where to where, and how.

Instead of fixed refactoring rules, a constraint solver decides where every
class, method and field should live so that the smells disappear with the
least change; an LLM is used only where new code is needed and to explain the
result; every plan is checked in memory and then built, tested and
re-analysed on the real code.

> **Status: all six phases implemented (version 0.6.0).** Stages 0-9:
> detector import, program model, triage, learned affinity, constraint plans,
> tree search with an optional language model, simulation and checks,
> step-by-step reports, verification on the real code (apply, compile, delta
> debugging, re-detection, optional build and tests) and the evaluation
> against baselines (NSGA-II, random search, solver only, search only).

## Pipeline

| Stage | What it does | Status |
|---|---|---|
| 0 Detect | Run Arcan and Designite at the commit, or import their output | import done |
| 1 Normalize | One smell table and one component table for all tools | done |
| 2 Program model | Classes, methods, fields, typed dependencies, modules, API, history | done |
| 3 Triage | Drop false smells, group into components, prioritize, set scope | done |
| 4 Learned priors | Affinity of classes to packages, learned from the code and the history of class moves | done |
| 5 Generate plans | Constraint solver (CP-SAT or HiGHS); tree search with optional language model | done |
| 6 Filter and rank | Graph simulation, preconditions, smell re-detection, ranking | done |
| 7 Explain | Step-by-step report from facts; optional model summary, checked by a reviewer | done |
| 8 Verify | Apply to a copy of the code, compile, delta debugging, detect again; optional build, tests, Arcan/Designite | done |
| 9 Report | HTML report for users; evaluation against baselines for research | done |

## Installation

```bash
python3 -m pip install -e ".[dev]"
dsarp --version
```

Python 3.10 or newer and a JDK 17 or newer (for the program-model
extractor, built automatically on first use with `java-helpers/extractor/build.sh`;
no Maven or downloads needed). The Apptainer image in `containers/` has
everything.

## Usage

### 1. Describe the repository

One YAML file per repository and commit (see `configs/`):

```yaml
name: tika
repo_url: https://github.com/apache/tika
commit: 697d7c047daf1f661a4ed067bbc8f9c58bb6faa2
language: java
build: maven            # maven | gradle | auto
detectors:
  - tool: arcan
    path: Arcan/Tika      # folder with Arcan's CSV export
  - tool: designite
    path: Designite/Tika  # folder with Designite's CSV output
protected_api: []         # packages/classes plans must never move
```

Detector paths are relative to a data folder given with `--data-root` or the
`DSARP_DATA` environment variable. When a detector was run separately per
module (as Designite on Karaf), list each output folder with its `module`.
Designite writes absolute paths from the machine it ran on; the repository
root there is inferred automatically, or set it with `path_root`.

### 2. Import and merge detector output

```bash
dsarp check configs/tika.yaml --data-root /path/to/data
dsarp ingest configs/tika.yaml --data-root /path/to/data --out results/tika
dsarp ingest configs/*.yaml   --data-root /path/to/data --out results   # all repos
dsarp summary results/tika --detail
```

Output per repository:

| File | Content |
|---|---|
| `smells.csv` | One row per smell from every detector: type, level, members, centre, severity, cross-tool match, and triage flags (test-only, test-support-only, generated-only, external and parent-package members, split packages) |
| `components.csv` | One row per package and class: module, file path, role (`source`, `parent`, `external`), test and generated flags, and every detector's metrics side by side (`arcan_fan_in`, `designite_wmc`, ...) |
| `manifest.json` | Commit, inputs with SHA-256 checksums, counts and warnings |

Nothing is removed at this stage; stage 3 decides what to drop, using the flags.

### 3. Build the program model

```bash
dsarp checkout configs/tika.yaml --workdir work          # clone with history, check out the commit
dsarp model configs/tika.yaml --workdir work --out results/tika/model --ingest results/tika
```

The extractor in `java-helpers/extractor` uses the JDK's own compiler API
(parse and attribute), so names resolve exactly as `javac` resolves them.
External libraries are not needed: references to them are recorded as
unresolved and analysis continues. Passing their jars with `--classpath`
improves resolution further. Large repositories need more memory
(`--memory 16g`).

| File | Content |
|---|---|
| `files.csv` | every Java file: module, test, generated, excluded and why, lines, changes |
| `types.csv` | classes, interfaces, enums, records, annotations: package, module, visibility, deprecated, public API, protected by config, wiring references, changes |
| `members.csv` | methods, constructors, fields, initializers: visibility, static, deprecated, `@Override`, callers (all and from tests) |
| `deps.csv` | dependencies from a member or type to a type or member, with kind (`call`, `field_access`, `new`, `extends`, `implements`, `import`, `param_type`, `return_type`, `field_type`, `local_type`, `type_ref`, `annotation`, ...) and count |
| `packages.csv` | per package: modules, size, test-only, API types, changes |
| `modules.json` | build modules (Maven or Gradle), allowed module dependencies, OSGi exported packages and activators |
| `wiring.csv` | non-Java files that name classes: ServiceLoader, Spring factories, XML (Blueprint, Spring, Struts, web.xml), bundle activators |
| `cochange.csv` | pairs of files that changed together in git history, with counts |
| `model.json` | commit, counts, compiler diagnostics, history statistics, coverage of the detectors' classes, warnings |

`model.json` reports, per detector, how many of its classes the model found.
Classes in files that do not exist at the commit mean the detector analysed
a different version of the code; a warning is raised.

### 4. Triage and in-memory detection

```bash
dsarp triage results/tika          # needs results/tika/smells.csv and results/tika/model/
dsarp calibrate results/tika       # optional: agreement with Arcan and Designite (slower)
```

Every detector smell gets a decision: **keep**, **drop** with a reason
(test code only, generated code only, only external libraries, not in the
analysed code, a reported cycle that does not exist between production
classes, or exists only through test code), or **report only** (dense
structure). Kept smells are **grouped**: all cycles inside one strongly
connected component of the production graph form one group, other smells are
grouped by the element they are centred on. Each group gets a **priority**
(severity × (0.1 + PageRank) × (1 + change activity), ×1.2 when both tools
report it) and a **scope** (core elements, neighbouring packages or classes
that are possible targets, and **quick fixes**: package dependencies inside a
cycle that exist only through unused imports or deprecated code).

| File in `triage/` | Content |
|---|---|
| `smells_triaged.csv` | every smell with decision, reason and group |
| `groups.csv` | groups ranked by priority, with size, edges, PageRank, changes, API members, quick fixes |
| `scopes.json` | per group: core elements, neighbours, core classes, quick fixes |
| `detected_baseline.csv` | in-memory detection on production code (baseline for measuring plans) |
| `triage.json` | counts per decision and reason, top groups, baseline summary |

`dsarp.detect` re-implements the detection rules on the program model, so the
effect of a plan on the smells can be measured in memory. Package-level cycles
agree closely with Arcan (recall 0.91-0.96, precision 0.94-1.0 on Tika, Karaf
and Struts). Hub-like and unstable dependency use thresholds calibrated against
Arcan 2.x (fan-in and fan-out above the 90th percentile; at least half of the
dependencies on less stable packages), because the published rules (median,
30%) flag many more components than Arcan 2.x reports.

```python
from dsarp.program import ProgramModel
m = ProgramModel("results/tika/model")
g = m.package_graph()            # production code; include_tests=True to add tests
m.callers("org.apache.tika.mime.MimeTypes#getMimeType(java.io.File)")
```

### 5. Learned affinity (where does a class belong?)

```bash
dsarp learn results/tika results/struts results/karaf --workdir work   # per repository + pooled model
```

Training examples come from the repository itself: the **current package**
of a sample of classes (positive) against nearby packages of the same module
(negatives), and the **class moves in the git history** (a class moved from P
to Q: Q positive, P a hard negative, weighted 3×). Features are computed with
the class itself removed: share of dependencies (outgoing, incoming) and
co-changes with the package, name similarity, package size. The model is a
standardised logistic regression (one weight per feature, in
`learn/affinity.json`). `dsarp plan` uses it automatically when it exists.
Mine the history on a login node: the clones download file contents on demand.

Cross-validation grouped by package, top-1 = correct package ranked first
among the candidates (fixed weights of phase 4 in brackets):

| | Tika | Struts | Karaf |
|---|---|---|---|
| all examples | **0.75** (0.68) | **0.63** (0.51) | **0.58** (0.44) |
| developer moves only | **0.64** (0.55) | **0.75** (0.47) | **0.46** (0.32) |
| trained on the other two repositories | **0.76** (0.68) | **0.60** (0.51) | **0.57** (0.44) |

| File in `learn/` | Content |
|---|---|
| `moves.csv` | class moves (package changes) in the history up to the commit |
| `dataset.csv` | training examples with features |
| `affinity.json` | model weights and evaluation |

### 6. Refactoring plans

```bash
dsarp plan results/tika --groups 10 --time-limit 60          # needs model/ and triage/
dsarp plan results/tika --types cyclic_dependency --backend cpsat
```

For each of the top groups (package cycles, unstable dependencies, hub-like
packages) a **mixed-integer model** decides where the classes of the group
should live and which dependencies to remove:

| Operation | When the model may use it |
|---|---|
| `move_class` | to a neighbouring package of the same build module, without a name clash, unless the class is protected API |
| `delete_member` | deprecated method/field/constructor without production callers |
| `remove_unused_import` | the dependency exists only through an import |
| `extract_interface` | the client only calls instance methods of the provider (Dependency Inversion: the interface lives in the client's package) |

Constraints keep every class in one package, turn class dependencies into
package dependencies, require the target smell to disappear (cycles: a
topological order of the packages, cycles are added lazily; unstable: the
dependencies on less stable packages go; hub: fan-in or fan-out drops to the
threshold) and stop packages from growing into god components. The cost of a
change grows with poor fit (affinity from structure, co-change and names),
the number of classes that use the moved class, public API and framework
wiring; new package dependencies cost extra. Several **trade-off plans** are
solved: `most_removed`, `least_change` (if the smell can be removed
completely), an `alternative` (diversity cut) and plans for 50% and 25% of the
cost (epsilon constraint).

Every plan is then **applied in memory to the whole repository** and the
smells are detected again (L1, L3: target smell removed? new cycles, hubs,
unstable dependencies or god components elsewhere?), its operations are
**checked** against the member-level facts (L2: members that must become
public, imports and tests to update, wiring files, API warnings, blocked
operations) and the plans are **ranked** (benefit − new smells − cost; the
weights are in `dsarp/plan/run.py`). A plan is recommended only if it is not
blocked and has a positive score.

**Tree search (stage 5b).** When the solver finds no good plan (no positive
score, new smells, or a large optimality gap), a Monte Carlo tree search
(PUCT) works directly on the whole-repository simulation. It starts from the
best solver plan and may add operations suggested by the problem
dependencies that are left (priors: dependency weight × learned affinity) or
drop operations that cause new smells. With `--llm openai` a language model
(any OpenAI-compatible server, e.g. vLLM with an open model on a Noctua GPU
node; `DSARP_LLM_URL`, `DSARP_LLM_MODEL`) proposes actions from the candidate
list at the start and repairs the best plan after seeing its simulation
result; its suggestions are only kept if they are valid candidates and score
better. On the 41-package Struts cycle the search turns solver plans with
negative scores (new smells, many visibility changes) into a plan with 19
steps and no new smells.

**Report (stage 7).** `--explain` (or `dsarp explain results/<repo>`) writes
`plans/report.md`: for each group the effect of the recommended plan, then
every step with what (element, from, to), why (package dependencies it
removes or adds, evidence for the target package) and how (package
declaration and file, imports, members to make public, tests, wiring), and
the alternatives. With a language model, a short summary is added if the
reviewer finds no class or package name in it that is not part of the plan.

| File in `plans/` | Content |
|---|---|
| `plans.json` | per group: plans, operations with their checks and follow-up edits, simulation before/after, score, recommended plan |
| `operations.csv` | one row per operation: group, plan, step, operation, element, from, to, check status |
| `summary.csv` | one row per plan: cost, benefit, new smells, checks, solver status and gap |

Example (Tika, Karaf, Struts, 30 s per plan with HiGHS): small and medium
cycles are removed completely or mostly with 1-12 operations (Karaf: one move
each); the 17-package tika-core cycle is reduced (benefit 0.86, 37
operations); for the 41-package Struts cycle no plan reaches a positive score
with HiGHS in this time, so none is recommended and the candidates are listed.
Use CP-SAT (installed in the container) and longer time limits on the cluster
for large groups.

### 7. Verification on the code

```bash
dsarp verify results/tika --workdir work                  # compile check, delta debugging, re-detection, patches
dsarp verify results/tika --workdir work --build auto     # also Maven/Gradle build + selected tests (offline)
dsarp verify results/tika --arcan-cmd "java -jar arcan.jar -i {repo} -o {out}"   # also re-run a detector
```

Every recommended plan is applied to a clean copy of the checkout (git
worktree): files moved and their `package` line changed, imports and fully
qualified names rewritten in all Java and wiring files, imports added where
classes of the old package used the moved class (and the other way round),
members widened as listed by the checks (overriding methods too), deprecated
members deleted with their Javadoc and annotations, unused imports removed,
interfaces created and implemented. Then **all Java files are compiled** with
the JDK compiler; errors that were not there before are caused by the plan
(errors about library symbols the repository does not define are ignored).
If there are new errors, **delta debugging** finds the steps that cause them
and the plan is reduced to the steps that compile. The smells are then
**detected again on the compiled code** and compared with the original code
and with the prediction of the simulation. With `--build`, the affected
modules are built and the tests that use a changed class are run (download
the dependencies once on a login node; the build runs offline).

| File in `verify/<group>/` | Content |
|---|---|
| `plan.patch` | the change as a git patch (apply with `git apply`) |
| `rewrite.yml` | OpenRewrite recipe for the moves (`org.openrewrite.java.ChangeType`) |
| `operations.json` | steps kept and dropped |
| `new_errors.csv`, `new_errors_full_plan.csv` | compiler errors caused by the plan |
| `verification.json` | result: compiles, kept/dropped, smell before/after, prediction, build |

Results (compile check, no build):

| Repository, group | Steps | Compile | Smell after (verified) | Simulation predicted |
|---|---|---|---|---|
| Karaf, 4 cycles | 1 each | all | removed (4/4) | same |
| Tika, unstable `metadata` | 2 | all | removed | same |
| Tika, OneNote cycle | 9 | all | 4 packages, 1 dependency to cut (from 8, 7) | same |
| Tika, tika-core cycle | 25 | 22 (3 dropped) | 13 packages, 10 to cut (from 17, 22) | 12, 8 |

The three dropped Tika steps moved `AutoDetectParser` and `ServiceLoader` and
deleted a deprecated method that tests still call; the patch touches 623 files
(most for the `ParseContext` move).

### 8. Report and evaluation

```bash
dsarp report results/tika                       # results/tika/report.html (self-contained)
dsarp evaluate results/struts --groups 5 --ablation
```

`report.html` combines triage, the step-by-step plans, the verification (with
the patch) and the evaluation. `dsarp evaluate` runs all generators on the
same groups with the same candidate operations, simulation and score:
`solver` (constraint plans only), `search_only` (tree search from an empty
plan), `hybrid` (DSARP), `nsga2` (multi-objective genetic algorithm with at
least the same number of evaluations), `random`, `llm_only` (with `--llm`) and
`hybrid_fixed_affinity` (ablation). It also reports how stable the
recommendation is when each score weight is halved or increased by half.

Struts, top 5 plannable groups (30 s solver, 60 s search per group):

| approach | mean score | mean benefit | best in group | mean steps |
|---|---|---|---|---|
| hybrid | **0.52** | 0.60 | 2 | 11.6 |
| hybrid, fixed affinity | 0.49 | 0.54 | 1 | 7.6 |
| search only | 0.46 | 0.50 | 2 | 3.2 |
| NSGA-II | 0.44 | 0.55 | 0 | 4.0 |
| random | 0.30 | 0.43 | 0 | 5.0 |
| solver only | 0.19 | 0.60 | 0 | 9.8 |

The solver alone removes as much of the smells as the hybrid but creates new
smells and visibility changes; the search repairs those plans. Run the full
evaluation on the cluster (`slurm/evaluate_array.sbatch`).

### Supported detector output

* **Arcan**: `smell-characteristics.csv`, `smell-affects.csv`, `component-metrics.csv`.
  Smell types: cyclic, hub-like and unstable dependency, god component.
* **Designite (Java)**: `ArchitectureSmells.csv`, `DesignSmells.csv`, `TypeMetrics.csv`.
  Smell types: cyclic and unstable dependency, god component, feature
  concentration, scattered functionality, dense structure, ambiguous interface,
  plus the class-level cyclically-dependent and hub-like modularization.

To support another detector, add a module in `dsarp/detectors/` with a
`load(DetectorInput) -> DetectorResult` function and register it in
`dsarp/detectors/__init__.py`.

Any Java project layout works: the source root of each file is derived from
its package, so Maven, Gradle and custom layouts (e.g. Cassandra's `src/java`)
are handled the same way.

## Running on Noctua 2 (PC2)

Docker is not available on the PC2 clusters; use Apptainer.

```bash
# On a login node (downloads happen here)
git clone https://github.com/indudhara59/dsarp && cd dsarp
module load tools Apptainer
export APPTAINER_CACHEDIR=$PC2PFS/<project>/apptainer-cache
export APPTAINER_TMPDIR=$PC2PFS/<project>/apptainer-tmp
apptainer build dsarp.sif containers/pipeline.def

# Detector output on the parallel file system, not in $HOME
export DSARP_DATA=$PC2PFS/<project>/dsarp-data

# Checkouts on the login node (compute nodes may have no internet access)
export DSARP_WORK=$PC2PFS/<project>/work
apptainer exec dsarp.sif dsarp checkout configs/*.yaml --workdir $DSARP_WORK

# All repositories as one Slurm job array: stages 0-1 only, or stages 0-2
ls configs/*.yaml > configs.list
sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/ingest_array.sbatch
sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/model_array.sbatch   # stages 0-3
apptainer exec dsarp.sif dsarp learn results/* --workdir $DSARP_WORK                 # login node (history)
sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/plan_array.sbatch    # plans (CP-SAT) + report
sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/verify_array.sbatch  # verify + report.html
sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/evaluate_array.sbatch  # evaluation
DSARP_REPO=tika DSARP_LLM_MODEL=<model> VLLM_SIF=<vllm.sif> \
  sbatch -A <project> slurm/plan_llm.sbatch                                      # with a language model (A100)

# Test suite on a compute node
sbatch -A <project> slurm/run_tests.sbatch
```

## Tests

```bash
python3 -m pytest -q                                   # unit tests (synthetic fixtures, small Java project)
DSARP_DATA=/path/to/data python3 -m pytest -q          # also checks the detector data of the study repositories
DSARP_DATA=... DSARP_WORK=/path/to/checkouts python3 -m pytest -q   # also builds real program models
```

Detector output of the study repositories is not stored in this repository.

## Study repositories

| Repository | Commit | Notes |
|---|---|---|
| Apache Tika | `697d7c0` | multi-module Maven; the Designite output comes from a later version (413 classes in files that do not exist at this commit) |
| Apache Struts | `d59aea5` | multi-module Maven |
| Apache Karaf | `5f5677d` | OSGi; Designite run per module |
| Apache Log4j 2 | `4f474b3` | multi-module Maven |
| Apache Cassandra | not recorded | custom layout; Arcan smell files missing; Designite per package group |

## Roadmap

1. **Foundation and data import** (stages 0-1). Done.
2. **Program model** (stage 2): extraction with the JDK compiler API of elements, typed dependencies, modules, framework wiring, public API and co-change history. Done.
3. **Triage and in-memory smell detection** (stage 3): false-smell removal, grouping, prioritization, scope and quick fixes; detection rules calibrated against Arcan and Designite. Done.
4. **Constraint plan generator** (stages 5a, 6): mixed-integer model (CP-SAT or HiGHS), trade-off plans, whole-repository simulation and re-detection, precondition checks, ranking. Done.
5. **Learning, search and explanations** (stages 4, 5b, 7): learned affinity from code and history moves, tree search with optional language model, step-by-step reports with a reviewed summary. Done.
6. **Verification, report and evaluation** (stages 8-9): apply plans to the code, compile, delta debugging, re-detection on the compiled code, optional build/tests/detectors, patches and OpenRewrite recipes, HTML report, evaluation against NSGA-II, random, solver-only and search-only with ablation and weight sensitivity. Done.

Known limitations: the compile check compiles the whole repository at once,
so it does not see build-module boundaries (use `--build`); history moves are
described by the code at the analysed commit; class-level smells (class
cycles, god classes) are triaged but not yet planned.

## Project layout

```
dsarp/            Python package
  config.py       repository configuration (YAML)
  model.py        tool-independent smell model
  paths.py        source roots, modules, test and generated code
  detectors/      one adapter per detector (arcan, designite)
  ingest.py       stages 0-1: import, merge, flags, manifest
  checkout.py     clone and check out the configured commit
  sources.py      find and classify Java files
  buildinfo.py    Maven/Gradle modules, OSGi headers
  wiring.py       ServiceLoader, Spring, XML and activator references
  history.py      change and co-change counts from git
  extractor.py    runs the Java extractor
  program.py      stage 2: program model, class and package graphs
  detect.py       in-memory smell detection (cycles, hubs, unstable, god components)
  calibrate.py    agreement with Arcan/Designite, threshold search
  triage.py       stage 3: decisions, groups, priority, scope, quick fixes
  plan/           stages 5a and 6
    solver.py     linear model, HiGHS and CP-SAT backends
    facts.py      candidates, affinity and costs per group
    generator.py  plan model and trade-off plans
    simulate.py   apply a plan in memory and detect again (L1, L3)
    checks.py     Java preconditions and follow-up edits (L2)
    run.py        plan the top groups, score, rank, write results
  learn/          stage 4
    moves.py      class moves in the git history
    affinity.py   learned affinity: dataset, model, evaluation
  search/         stage 5b
    actions.py    candidate operations for a state
    mcts.py       Monte Carlo tree search (PUCT), language-model proposals and repair
    llm.py        language-model clients (none, OpenAI-compatible)
  explain.py      stage 7: step-by-step report
  verify/         stage 8
    workspace.py  clean working copies (git worktree)
    rewrite.py    apply a plan to the sources; OpenRewrite recipe
    compile.py    compile, compare errors, detect again on the compiled code
    debug.py      delta debugging (ddmin) and reduction to the passing steps
    build.py      Maven/Gradle build of affected modules, test selection
    detectors.py  re-run Arcan/Designite
    run.py        orchestration, patches, results
  evaluate/       stage 9 (research)
    baselines.py  NSGA-II, random search, language model only
    harness.py    all approaches on the same groups, summary, weight sensitivity
  report.py       stage 9 (users): HTML report
  cli.py          command-line interface
java-helpers/
  extractor/      program-model extractor (JDK compiler API, no dependencies)
configs/          one YAML file per study repository
containers/       Apptainer definition
slurm/            Slurm job scripts for Noctua 2
tests/            unit tests, synthetic fixtures, real-data checks
```
