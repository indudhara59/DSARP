"""Pipeline stages as the web interface sees them.

Each stage knows the ``dsarp`` command it runs, the options it offers in the
form, the files that show it has finished, the stages it needs first and the
Slurm resources it asks for. Only the standard library is used, so the web
server runs with a plain ``python3`` (for example on an HPC login node) while
the stages themselves run in the container or in a Slurm job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Option:
    """One form field, turned into one command-line flag."""

    name: str
    flag: str
    kind: str                      # int, float, choice, bool, text
    default: object = None
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    def spec(self) -> dict:
        return {"name": self.name, "flag": self.flag, "kind": self.kind, "default": self.default,
                "help": self.help, "choices": list(self.choices), "min": self.minimum, "max": self.maximum}


@dataclass(frozen=True)
class Resources:
    """Slurm request of a stage (can be changed in the form)."""

    cpus: int = 2
    mem: str = "8G"
    time: str = "01:00:00"


@dataclass(frozen=True)
class Stage:
    id: str
    label: str
    number: str
    summary: str
    outputs: tuple[str, ...]           # relative to the results folder ({work} = checkout)
    requires: tuple[str, ...] = ()
    optional_requires: tuple[str, ...] = ()
    options: tuple[Option, ...] = ()
    resources: Resources = field(default_factory=Resources)
    inner: tuple[str, ...] = ()        # sub-steps shown inside the flowchart node

    def spec(self) -> dict:
        return {"id": self.id, "label": self.label, "number": self.number, "summary": self.summary,
                "outputs": list(self.outputs), "requires": list(self.requires),
                "optional_requires": list(self.optional_requires), "inner": list(self.inner),
                "options": [o.spec() for o in self.options],
                "resources": {"cpus": self.resources.cpus, "mem": self.resources.mem, "time": self.resources.time}}


STAGES: tuple[Stage, ...] = (
    Stage("check", "Check inputs", "0", "Validate the config and find the Arcan and Designite files",
          outputs=(), resources=Resources(1, "2G", "00:10:00")),
    Stage("checkout", "Checkout", "0", "Clone the repository and check out the commit",
          outputs=("{work}/.git",),
          options=(Option("shallow", "--shallow", "bool", False, "fetch only the commit (no history for co-change "
                                                                   "and learned moves)"),),
          resources=Resources(2, "8G", "02:00:00")),
    Stage("ingest", "Ingest", "1", "Import detector output into one smell table",
          outputs=("smells.csv", "manifest.json"), resources=Resources(2, "8G", "01:00:00")),
    Stage("model", "Program model", "2", "javac extracts classes, members, dependencies; git history",
          outputs=("model/model.json",), requires=("checkout", "ingest"),
          options=(Option("memory", "--memory", "text", "8g", "Java heap for the extractor"),
                   Option("history", "--history", "int", 3000, "commits of history for co-change (0 = off)", minimum=0),
                   Option("no_tests", "--no-tests", "bool", False, "leave test code out")),
          resources=Resources(8, "32G", "04:00:00")),
    Stage("triage", "Triage", "3", "Confirm smells on the model, group, prioritise, scope",
          outputs=("triage/triage.json", "triage/groups.csv"), requires=("ingest", "model"),
          options=(Option("top", "--top", "int", 10, "groups printed in the log", minimum=1),),
          resources=Resources(4, "16G", "01:00:00")),
    Stage("learn", "Learn affinity", "4", "Learn where classes belong from current code and history moves",
          outputs=("learn/affinity.json",), requires=("model",), optional_requires=("checkout",),
          options=(Option("max_commits", "--max-commits", "int", 20000, "history commits to mine", minimum=0),
                   Option("max_classes", "--max-classes", "int", 3000, "classes sampled", minimum=50),
                   Option("remine", "--remine", "bool", False, "mine the history again")),
          resources=Resources(4, "16G", "04:00:00")),
    Stage("plan", "Plan", "5-6", "Solver plans, whole-repo simulation, tree search, score and checks",
          outputs=("plans/plans.json", "plans/summary.csv"), requires=("triage",), optional_requires=("learn",),
          inner=("5a solver", "5b simulate", "5c search", "6 score + checks"),
          options=(Option("groups", "--groups", "int", 20, "top groups to plan", minimum=1),
                   Option("time_limit", "--time-limit", "float", 300.0, "solver seconds per plan", minimum=1),
                   Option("backend", "--backend", "choice", "auto", "solver", ("auto", "cpsat", "highs")),
                   Option("search", "--search", "choice", "auto", "tree search after the solver",
                          ("auto", "always", "never")),
                   Option("search_time", "--search-time", "float", 120.0, "seconds of tree search per group", minimum=1),
                   Option("types", "--types", "text", "", "only these smell types (comma-separated)"),
                   Option("explain", "--explain", "bool", True, "also write plans/report.md"),
                   Option("llm", "--llm", "choice", "none", "language model for suggestions", ("none", "openai"))),
          resources=Resources(16, "32G", "12:00:00")),
    Stage("explain", "Explain", "7", "Step-by-step report of the recommended plans",
          outputs=("plans/report.md",), requires=("plan",),
          options=(Option("llm", "--llm", "choice", "none", "language model for the summary", ("none", "openai")),),
          resources=Resources(1, "4G", "00:30:00")),
    Stage("verify", "Verify", "8", "Apply plans to the code, compile, delta-debug, re-detect, write patches",
          outputs=("verify/verification.json", "verify/summary.csv"), requires=("plan", "checkout"),
          options=(Option("groups", "--groups", "int", None, "only the first N groups (empty = all)", minimum=1),
                   Option("build", "--build", "choice", "none", "also build and run selected tests",
                          ("none", "auto", "maven", "gradle")),
                   Option("max_compiles", "--max-compiles", "int", 30, "compiler runs for delta debugging", minimum=1),
                   Option("memory", "--memory", "text", "4g", "Java heap")),
          resources=Resources(8, "32G", "12:00:00")),
    Stage("evaluate", "Evaluate", "9", "Compare hybrid, solver, search, NSGA-II, random (+ ablation)",
          outputs=("evaluation/summary.csv",), requires=("triage",), optional_requires=("learn",),
          options=(Option("groups", "--groups", "int", 20, "groups to compare", minimum=1),
                   Option("time_limit", "--time-limit", "float", 300.0, "solver seconds", minimum=1),
                   Option("search_time", "--search-time", "float", 120.0, "search seconds", minimum=1),
                   Option("budget", "--budget", "int", 600, "evaluations for NSGA-II and random", minimum=10),
                   Option("ablation", "--ablation", "bool", True, "hybrid with fixed affinity weights too"),
                   Option("seed", "--seed", "int", 0, "random seed", minimum=0)),
          resources=Resources(16, "64G", "24:00:00")),
    Stage("report", "HTML report", "9", "Self-contained report with triage, plans and verification",
          outputs=("report.html",), requires=("plan",), optional_requires=("verify",),
          resources=Resources(1, "4G", "00:20:00")),
)

BY_ID = {s.id: s for s in STAGES}
ORDER = [s.id for s in STAGES]
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class StageError(ValueError):
    """Bad stage id or option value."""


def _value(opt: Option, raw) -> object:
    if raw is None or raw == "":
        return None
    if opt.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).lower() in ("1", "true", "yes", "on")
    if opt.kind in ("int", "float"):
        try:
            v = int(raw) if opt.kind == "int" else float(raw)
        except (TypeError, ValueError) as exc:
            raise StageError(f"{opt.name}: not a number: {raw!r}") from exc
        if opt.minimum is not None and v < opt.minimum:
            raise StageError(f"{opt.name}: must be at least {opt.minimum}")
        if opt.maximum is not None and v > opt.maximum:
            raise StageError(f"{opt.name}: must be at most {opt.maximum}")
        return v
    if opt.kind == "choice":
        if str(raw) not in opt.choices:
            raise StageError(f"{opt.name}: must be one of {', '.join(opt.choices)}")
        return str(raw)
    text = str(raw).strip()
    if len(text) > 200 or any(c in text for c in "\n\r\0"):
        raise StageError(f"{opt.name}: invalid text")
    return text


def option_args(stage: Stage, values: dict | None) -> list[str]:
    """Command-line flags for the chosen option values (defaults filled in)."""
    values = values or {}
    unknown = set(values) - {o.name for o in stage.options}
    if unknown:
        raise StageError(f"unknown options for {stage.id}: {', '.join(sorted(unknown))}")
    args: list[str] = []
    for opt in stage.options:
        v = _value(opt, values.get(opt.name, opt.default))
        if v is None:
            continue
        if opt.kind == "bool":
            if v:
                args.append(opt.flag)
        elif opt.kind == "choice" and opt.name == "llm" and v == "none":
            continue
        else:
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            args += [opt.flag, str(v)]
    return args


@dataclass
class Paths:
    """Where one project's files are (all may be relative to the server's folder)."""

    config: Path
    name: str
    results: Path
    workdir: Path
    data_root: Path | None

    @property
    def checkout(self) -> Path:
        return self.workdir / self.name


def command(stage_id: str, p: Paths, values: dict | None = None) -> list[str]:
    """Arguments after ``dsarp`` for one stage of one project."""
    if stage_id not in BY_ID:
        raise StageError(f"unknown stage: {stage_id}")
    stage = BY_ID[stage_id]
    if not NAME_RE.match(p.name):
        raise StageError(f"invalid project name: {p.name}")
    cfg, res, work = str(p.config), str(p.results), str(p.workdir)
    data = ["--data-root", str(p.data_root)] if p.data_root else []
    base: dict[str, list[str]] = {
        "check": ["check", cfg, *data],
        "checkout": ["checkout", cfg, "--workdir", work, *data],
        "ingest": ["ingest", cfg, *data, "--out", res],
        "model": ["model", cfg, "--workdir", work, "--out", str(p.results / "model"), "--ingest", res, *data],
        "triage": ["triage", res],
        "learn": ["learn", res, "--workdir", work],
        "plan": ["plan", res],
        "explain": ["explain", res],
        "verify": ["verify", res, "--workdir", work],
        "evaluate": ["evaluate", res],
        "report": ["report", res],
    }
    return base[stage_id] + option_args(stage, values)


def output_state(stage: Stage, p: Paths) -> dict:
    """Whether the stage's outputs exist, and when they were last written."""
    if not stage.outputs:
        return {"exists": False, "mtime": None, "files": []}
    files, mtimes = [], []
    for o in stage.outputs:
        f = p.checkout / o[len("{work}/"):] if o.startswith("{work}/") else p.results / o
        if f.exists():
            mtimes.append(f.stat().st_mtime)
            files.append(o)
    ok = len(files) == len(stage.outputs)
    return {"exists": ok, "mtime": max(mtimes) if ok and mtimes else None, "files": files}
