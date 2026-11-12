# DSARP: Architectural Smell Refactoring Planner

DSARP turns the output of architectural smell detectors (Arcan, Designite) for
**any Java repository at any commit** into verified, step-by-step refactoring
plans: what to change, from where to where, and how.

Instead of fixed refactoring rules, a constraint solver decides where every
class, method and field should live so that the smells disappear with the
least change; an LLM is used only where new code is needed and to explain the
result; every plan is checked in memory and then built, tested and
re-analysed on the real code.

> **Status: all phases implemented, a web interface and plans for every smell type (version 0.8.0).**
> Stages 0-9: detector import, program model, triage, learned affinity,
> constraint plans, tree search with an optional language model, simulation and
> checks, step-by-step reports, verification on the real code (apply, compile,
> delta debugging, re-detection, optional build and tests) and the evaluation
> against baselines (NSGA-II, random search, solver only, search only). The
> whole pipeline can be run and inspected from the browser (`dsarp serve`).

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

A detector entry can also say how to **run** the tool. `dsarp detect` then runs
it on the checkout at the configured commit, so its output always matches the
analysed code (output goes to `path`, by default `detected/<name>/<tool>` under
the data root), and stage 8 can run it on the refactored code:

```yaml
detectors:
  - tool: designite
    command: java -jar /opt/designite/DesigniteJava.jar -i {repo} -o {out}/
  - tool: arcan
    path: Arcan/Tika
    command: java -jar /path/to/arcan.jar ... {repo} ... {out}
```

```bash
dsarp checkout configs/tika.yaml --workdir work
dsarp detect configs/tika.yaml --workdir work            # stage 0: run the detectors at the commit
```

The container builds the open-source DesigniteJava (Apache 2.0, class-level
smells only) at `/opt/designite/DesigniteJava.jar`. Architecture smells need
Designite Java (Enterprise, free academic licence) and Arcan: give their commands.

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

**All smell types.** Every smell type the detectors report gets a model of
its own; all of them share the simulation, the checks, the score and the
verification:

| Smell (detector) | Refactoring DSARP recommends | "Fixed" means (checked in the simulation and on the compiled code) |
|---|---|---|
| Cyclic dependency, packages (Arcan, Designite) | move classes, remove unused imports, delete dead code, extract interfaces | no cycle among the group's packages |
| Unstable dependency (Arcan, Designite) | the same operations | no dependency on a less stable package |
| Hub-like package (Arcan) | the same operations | fan-in or fan-out at the threshold |
| God component (Designite) | **split the package**: whole sets of related classes (with nested classes and tests) move to related packages or new sub-packages; *strict* plans keep every new package free of feature concentration, *relaxed* ones may not (shown as a side effect) | at most 30 types (production and test classes, nested ones included, as Designite counts) |
| Feature concentration (Designite) | **split the package by concern**: unrelated sets move to the package they are related to or to a new sub-package | fewer than 6 types, or Designite's LCC (sets of related classes / types) below 0.2 |
| Scattered functionality (Designite) | **gather the concern**: the classes that the reporting packages use together move into one of the concern's packages (every target is tried) | no package has more than one method using the concern spread over packages |
| Cyclic dependency, classes (Arcan) | remove unused imports, delete dead code, extract interfaces, **move static members** (pull the used static members into the caller, or push static members to the class they use), **move instance methods** (a method that works on a parameter of the other class and uses no state of its own class moves there: `x.m(a, t)` becomes `t.m(a)`) | no cycle among the group's classes |
| Hub-like class (Arcan) | the same class-level operations | Arcan's rule (fan-in and fan-out above the median and balanced, test classes counted as clients) no longer holds |

**Calibrated against the detectors.** On 242 feature-concentration reports of
Designite in the five study repositories, no package with fewer than 6 types
is reported, and LCC = 0.2 is reported (4 sets in 20 types): the rule is
"at least 6 types and LCC >= 0.2". Designite counts every type of a package,
test classes included, for both feature concentration and god components.
With these rules DSARP's detection agrees with Designite's reports on the
Tika packages Designite analysed with precision 0.79 and recall 0.94 (before:
0.26 and 1.0); the published relation kinds (field types, inheritance,
realisation) fit best (F1 0.86; adding other dependency kinds does not help).

Classes may share a **new sub-package** only if they depend on each other or
share a meaningful word in their names (not "Utils", "Exception", ... or words
of the package path), so a split never creates a bin of unrelated classes.
Creating a package costs 3 (a related existing package is preferred).

New sub-packages are named after the most frequent word of their classes'
names (`org.apache.tika.sax.text`), test classes move with the class they
test, and a split never closes a new package cycle (existing cycles may stay:
the new sub-packages count as their parent when cycles are compared). Moved
static members keep working: overloads move together, private helpers come
along, callers (tests included, static imports, subclasses using inherited
statics) are redirected and the imports the moved code needs are added.

When no plan is safe, the report says why with numbers. For Tika's
`AutoDetectParser` (hub-like class): *"by Arcan's rule the class stops being a
hub if fan-in drops from 34 to 1, fan-out from 25 to 4, or the two differ by at
least 25% of 59; the verifiable operations can remove at most 8 incoming and 1
outgoing dependencies, which is not enough (the rest are uses of its type:
fields, parameters, subclassing, construction)"*. Such smells need design
changes (new shared abstractions, splitting a class's responsibilities) that
DSARP cannot verify automatically.

Feature concentration follows Designite's published rule (Sharma, Singh and
Spinellis 2020): with field types, `extends` and `implements` as relations
DSARP reproduces 336 of the 537 related sets Designite reports for Tika.
Designite's exact filter for scattered functionality is not published (the
published rule agrees poorly with its output), so DSARP measures the concern
directly. Hub-like classes use Arcan's median rule with test classes as
clients, which reproduces all 8 hub-like classes Arcan reports for Tika.

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

#### Keeping recommendations acceptable to developers

A plan that removes a smell is not useful if developers would reject it. Two
rules apply to every plan of every smell type:

* **Public API stays in place** (`--api-policy`, default `balanced`): public
  classes used by many classes (at least 20 and in the top 5 % of the
  repository, e.g. `MediaType` and `ParseContext` in Tika) never move, and
  their public static members never move either. Moving them would break every
  user of the library outside the repository. `strict` also keeps all public
  classes used from other build modules; `off` keeps only `protected_api`.
* **A class only moves where it belongs**: the evidence for the target
  package (dependencies, co-changes, names) must be at least 0.05 and at least
  30 % of the evidence for its current package. On Tika, 113 of the 189 moves
  recommended before this rule sent a class to a package it had fewer ties to,
  many of them to packages it had no relation to at all.

When no plan is recommended, the reason is given with numbers, for example
the new smells the best candidate would create, or the classes that must stay.

#### Design review by a language model (optional)

```bash
export DSARP_LLM_URL=https://ai-gateway.uni-paderborn.de/v1   # or a vLLM server on a GPU node
export DSARP_LLM_KEY=...                                       # your key; read from the environment only
dsarp llm-models                                               # models offered by the endpoint
export DSARP_LLM_MODEL=<one of them>
dsarp review results/tika                                      # after plan, before verify
```

A language model judges, as an architect of the project, whether every step
puts the code where a developer would accept it (`sensible`, `questionable`,
`wrong`). It cannot add or change steps. A plan with a wrong step is replaced
by the next acceptable plan of the group that the review does not reject; if
none is left the group gets no recommendation and the reason. Answers that do
not follow the format are ignored. Answers are cached in
`plans/review_cache.json`, so a re-run costs nothing; the key is never written
to a file. The review needs the internet: on the cluster run it on the login
node (the web interface does this automatically).

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
and the plan is reduced to the steps that compile. If the recommended plan
does not fully work on the code (steps dropped, or a result different from the
simulation), up to two next-best acceptable plans of the group are verified
as well and the best verified result is kept (`tried` in `verification.json`). The smells are then
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
| `verification.json` | result: plan verified (and plans tried), compiles, kept/dropped, smell before/after, prediction, build, outcome |

Every group ends with one **outcome** (report and web interface):
`removed` / `reduced` (measured on the compiled code), `not applicable`
(every step of every plan tried broke the build), `no safe plan`, `needs a
design change` (the operations DSARP can verify cannot fix it) or `not a smell
at this commit` (the detector counted tests, generated code, libraries or
sub-packages).

Results (compile check, no build; 6 groups per repository, 30 s solver and 60 s search):

| Repository | Plans | Steps kept | Smell removed (on the code) | Result equals simulation |
|---|---|---|---|---|
| Karaf | 4 | 4/4 | 4 | 4/4 |
| Struts | 6 | 46/46 | 2 (hubs); the others reduced | 6/6 |
| Tika | 6 | 40/41 | 1; cycles reduced, e.g. 8 → 2 and 17 → 16 packages | 6/6 |

The one dropped Tika step extracted an interface for `ParseContext` in a class
that passes the object on to methods that expect a `ParseContext`; the compiler
rejects it and delta debugging removes it. In a stress test on Tika, single
random operations were applied and compiled one by one: 10/10 class moves
compiled, and 8/8 interface extractions under the final rules (only classes
as clients, no inherited signatures, no nested provider types). Passing the
object on as the concrete type is not visible in the dependency facts, so such
steps are left to the verification.

### 7b. One combined plan for the whole repository

```bash
dsarp combine results/tika --workdir work                      # non-conflicting plans applied together
dsarp combine results/tika --workdir work --build auto --config configs/tika.yaml   # + tests, + real detectors
```

The verified plans of all groups whose smell was removed or reduced are
combined: a plan is left out if it changes a class another selected plan
changes, or if the whole-repository simulation shows that it weakens another
plan or creates new smells together with them. The selection is applied to
one copy of the code and compiled (plans that break the build together are
dropped by delta debugging), every group is measured on the combined code, and
the smells of the whole repository are counted before and after
(`combined/combined.json`, `combined/plan.patch`). With `--build` the affected
modules are built and the tests that use a changed class are run; with the
detector commands of the configuration (or `--arcan-cmd`, `--designite-cmd`)
the real tools are run on the original and the combined code and every
group is checked against their output. `dsarp verify --config ...` does the
same for every single plan (slow).

### 7c. Design advice for groups without a verified plan (optional)

```bash
dsarp advise results/tika          # needs DSARP_LLM_URL, DSARP_LLM_MODEL, DSARP_LLM_KEY
```

For groups that need a design change or have no safe plan, a language model
suggests up to three design directions (what to introduce or split, effort,
risk) from the facts DSARP has. Advice is marked **not verified**, never
changes plans or results, and is discarded if it names a fully qualified class
or package that does not exist.

### 7d. Evaluation: history and expert rating

```bash
dsarp history results/tika --workdir work --until origin/HEAD    # what did the developers do later?
dsarp history results/tika --refminer "RefactoringMiner -bc {repo} {start} {end} -json {json}"
dsarp rating sheet results/tika --groups 40                      # evaluation/rating_sheet.csv for raters
dsarp rating agreement rater1.csv rater2.csv --llm-review results/tika/evaluation/rating_llm_review.csv
```

`history` compares every recommended step with the commits after the analysed
one: the class was later moved to the **same** package, into a **new package**
too, somewhere **other**, **deleted**, or left **unchanged** (not done yet,
not necessarily wrong); with RefactoringMiner, method moves and extracted
interfaces are matched too. On Tika (1,218 later commits, 37 class moves), 7
recommended moves into new packages were later also made into new packages
and 16 moved classes were deleted.

`rating sheet` samples groups (stratified by smell type) and writes one row
per step (what, element, from, to, why) with empty `verdict` (sensible,
questionable, wrong) and `comment` columns; the language-model review is kept
in a separate file so raters are not influenced. `rating agreement` reports
the share of sensible steps per rater and smell type, Cohen's (two raters) or
Fleiss' kappa (more), the majority verdict, and each rater's agreement with
the language-model review.

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

Struts, top 5 plannable groups (30 s solver, 60 s search per group; at least
600 evaluations for NSGA-II and random search):

| approach | mean score | mean benefit | best in group | mean new smells | mean steps |
|---|---|---|---|---|---|
| hybrid | **0.52** | 0.60 | 3 | 0.0 | 13.4 |
| search only | 0.45 | 0.50 | 1 | 0.0 | 3.2 |
| NSGA-II | 0.41 | 0.53 | 0 | 0.0 | 2.8 |
| random | 0.40 | 0.49 | 1 | 0.0 | 4.6 |
| hybrid, fixed affinity | 0.33 | 0.59 | 1 | 0.4 | 11.8 |
| solver only | 0.16 | 0.59 | 0 | 1.2 | 12.0 |

The solver alone removes about as much of the smells as the hybrid but creates
new smells and visibility changes; the search repairs those plans. The learned
affinity matters: with the fixed weights the hybrid scores 0.33 instead of
0.52. The recommendation is stable: it stays the same in 98% of the runs in
which one score weight is halved or increased by half. These are short local
runs; run the full evaluation on the cluster (`slurm/evaluate_array.sbatch`).

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

## Web interface

Everything the command line does can also be started from the browser:

```bash
python3 -m dsarp.web            # or: dsarp serve      (in the folder that holds configs/)
```

* **Projects**: one card per config in `configs/`, with the progress of every
  stage. New projects (repository, commit, detector folders) are created with a
  form that writes the YAML config.
* **Pipeline**: a live flowchart of stages 0-9. Click a stage to see what it
  needs and writes, set its options (the exact `dsarp` command is shown), run
  it, and follow its log. *Run pipeline* starts several stages as a chain: each
  waits for the previous one (`--dependency=afterok` in Slurm).
* **Results**: charts and tables from the result files: funnel, smells by
  tool and type, triage decisions, program model, learned affinity, plan
  scores, benefit vs cost, verification, the evaluation per approach and per
  group. Each group has a page with the compared plans, the score breakdown,
  the step-by-step moves (from/to, edits, warnings), the tree search, the
  verification, the patch, the OpenRewrite recipe and its report text.
* **Jobs**: all runs with status, log, Slurm script and *Cancel*.

Stages run in one of three modes (Settings page or `--mode`): `local` (the
`dsarp` command on this machine), `container` (`apptainer exec` on this
machine) or `slurm` (one `sbatch` job per stage, with the CPUs, memory and time
of each stage editable in the form).

The server needs only the Python standard library, listens on `127.0.0.1` and
asks for a token that it prints at start. Jobs and logs are kept in
`.dsarp-web/` so the job list survives a restart; Slurm jobs keep running when
the server stops.

**On Noctua 2**: run the server on the login node (it only submits jobs) and
open it on your laptop through an SSH tunnel:

```bash
# on the login node, in the dsarp folder (the system python3 is too old: load Python 3.10+)
module load tools Apptainer
module load lang/Python/<3.10 or newer>   # see: module avail lang/Python
export DSARP_DATA=$PC2PFS/<project>/dsarp-data
python3 -m dsarp.web --mode slurm --account <project> --sif $PWD/dsarp.sif
#   open: http://localhost:8765/?token=...

# on your laptop
ssh -L 8765:localhost:8765 <user>@<login node>
# then open the printed URL in the browser
```

Set *Checkouts* and *Results* on the Settings page to the folders used on the
cluster (for example `$PC2PFS/<project>/work`). Checkout and learn need git
history from the internet, so on clusters where compute nodes are offline run
those two in `container` mode on the login node (they are light) and the rest
in `slurm` mode.

## Running on Noctua 2 (PC2)

Docker is not available on the PC2 clusters; use Apptainer.

```bash
# On a login node (downloads happen here)
git clone https://github.com/indudhara59/dsarp && cd dsarp
module load tools Apptainer
export APPTAINER_BIND=/scratch          # the container must see the parallel file system
export APPTAINER_CACHEDIR=$PC2PFS/<project>/apptainer-cache
export APPTAINER_TMPDIR=$PC2PFS/<project>/apptainer-tmp
APPTAINER_BIND= apptainer build dsarp.sif containers/pipeline.def   # no bind mounts while building

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
7. **Web interface**: run stages locally, in the container or as Slurm jobs, live flowchart and logs, results dashboard and group pages. Done.
8. **All smell types**: god components and feature concentration (package splits), scattered functionality (gathering a concern), class cycles and hub-like classes (static member moves, interfaces, imports, dead code). Done.
9. **Accuracy**: rules calibrated against Designite, public API kept in place, fit rule, move instance method, combined plan, detectors at the commit and on the result, history and expert-rating evaluation, design advice. Done.

Known limitations: the compile check compiles the whole repository at once,
so it does not see build-module boundaries (use `--build`); history moves are
described by the code at the analysed commit; an extracted interface fails to
compile when the client passes the object on as the concrete type, and a
moved static member fails when code reaches it in a way the rewriter does not
see (reflection, generated code): the verification drops such steps; a
facade for scattered functionality was evaluated and left out: on Tika none
of the 1,138 methods that use a scattered concern reaches it only through
static members or constructors, so a facade would hide the dependency instead
of removing it; only
deprecated members that nothing calls any more (tests included) are deleted;
smells that need new abstractions or splitting a class's responsibilities get
a reason instead of a plan; the evaluation baselines (NSGA-II, random, search
only) cover the package cycle, unstable and hub-like types only.

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
  plan/extra.py   snapshots and measures for god components, feature concentration, scattered
                  functionality, class cycles and hub-like classes
  plan/restructure.py  package splits and gathering a scattered concern
  plan/classlevel.py   class-level plans: static member moves, interfaces, imports, dead code
  web/            web interface (standard library only): server, stages, jobs, results, static/
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
  review.py       stage 6b: design review of the plans by a language model (optional)
  advice.py       stage 7b: design advice for groups without a verified plan (optional)
  outcome.py      outcome category of every group (report and web interface)
  explain.py      stage 7: step-by-step report
  verify/         stage 8
    workspace.py  clean working copies (git worktree)
    rewrite.py    apply a plan to the sources; OpenRewrite recipe
    compile.py    compile, compare errors, detect again on the compiled code
    debug.py      delta debugging (ddmin) and reduction to the passing steps
    build.py      Maven/Gradle build of affected modules, test selection
    detectors.py  run Arcan/Designite on a result, check every group against their output
    run.py        orchestration, patches, results
    combine.py    stage 8b: one combined plan, whole repository before/after
  evaluate/       stage 9 (research)
    history.py    later commits: did the developers make the recommended changes?
    rating.py     expert rating sheet, agreement (kappa) with raters and the review
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
