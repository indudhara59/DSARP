# DSARP: Architectural Smell Refactoring Planner

DSARP turns the output of architectural smell detectors (Arcan, Designite) for
**any Java repository at any commit** into verified, step-by-step refactoring
plans: what to change, from where to where, and how.

Instead of fixed refactoring rules, a constraint solver decides where every
class, method and field should live so that the smells disappear with the
least change; an LLM is used only where new code is needed and to explain the
result; every plan is checked in memory and then built, tested and
re-analysed on the real code.

> **Status: phase 1 of 6.** Detector import and normalization (pipeline
> stages 0 and 1) are implemented and tested. See [Roadmap](#roadmap).

## Pipeline

| Stage | What it does | Status |
|---|---|---|
| 0 Detect | Run Arcan and Designite at the commit, or import their output | import done |
| 1 Normalize | One smell table and one component table for all tools | done |
| 2 Program model | Classes, methods, fields, typed dependencies, modules, API, history | phase 2 |
| 3 Triage | Drop false smells, group into components, prioritize, set scope | phase 3 |
| 4 Learned priors | Affinity of elements to places, learned from refactoring history | phase 5 |
| 5 Generate plans | Constraint solver (CP-SAT); LLM + tree search as fallback | phases 4-5 |
| 6 Filter and rank | Graph simulation, preconditions, smell re-detection, ranking | phase 4 |
| 7 Explain | LLM agents write exact edit steps | phase 5 |
| 8 Verify | Apply, build, test, re-run detectors (once per run) | phase 6 |
| 9 Report | Ranked roadmap for users; evaluation for research | phase 6 |

## Installation

```bash
python3 -m pip install -e ".[dev]"
dsarp --version
```

Python 3.10 or newer. Later phases also need Java 21 and Maven or Gradle; the
Apptainer image in `containers/` has everything.

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

# All repositories as one Slurm job array
ls configs/*.yaml > configs.list
sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/ingest_array.sbatch

# Test suite on a compute node
sbatch -A <project> slurm/run_tests.sbatch
```

## Tests

```bash
python3 -m pytest -q                                   # unit tests (synthetic fixtures)
DSARP_DATA=/path/to/data python3 -m pytest -q          # also checks the five study repositories
```

Detector output of the study repositories is not stored in this repository.

## Study repositories

| Repository | Commit | Notes |
|---|---|---|
| Apache Tika | `697d7c0` | multi-module Maven |
| Apache Struts | `d59aea5` | multi-module Maven |
| Apache Karaf | `5f5677d` | OSGi; Designite run per module |
| Apache Log4j 2 | `4f474b3` | multi-module Maven |
| Apache Cassandra | not recorded | custom layout; Arcan smell files missing; Designite per package group |

## Roadmap

1. **Foundation and data import** (stages 0-1). Done.
2. **Program model** (stage 2): JavaParser-based extraction of elements, typed dependencies, modules, framework wiring, public API and co-change history.
3. **Triage and in-memory smell detection** (stage 3): false-smell removal, grouping, prioritization; detection rules calibrated against Arcan.
4. **Constraint plan generator** (stages 5a, 6): CP-SAT model, several trade-off plans, graph simulation and precondition checks.
5. **Learning and LLM parts** (stages 4, 5b, 7): learned affinity and ranking, LLM with tree search, explanation agents.
6. **Verification, report and evaluation** (stages 8-9): OpenRewrite, builds, delta debugging, reports, evaluation against baselines.

## Project layout

```
dsarp/            Python package
  config.py       repository configuration (YAML)
  model.py        tool-independent smell model
  paths.py        source roots, modules, test and generated code
  detectors/      one adapter per detector (arcan, designite)
  ingest.py       stages 0-1: import, merge, flags, manifest
  cli.py          command-line interface
configs/          one YAML file per study repository
containers/       Apptainer definition
slurm/            Slurm job scripts for Noctua 2
tests/            unit tests, synthetic fixtures, real-data checks
```
