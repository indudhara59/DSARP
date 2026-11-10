"""Running stages: on this machine, in the Apptainer container, or as Slurm jobs.

Jobs are kept in ``<state>/jobs.json`` and their output in
``<state>/logs/<id>.log``, so the job list survives a restart of the server.
Commands are always passed as argument lists (no shell), and Slurm scripts
quote every argument, so values from the browser can't inject commands.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

MODES = ("local", "container", "slurm")
ACTIVE = ("queued", "pending", "running")
_SLURM_STATE = {"PENDING": "pending", "CONFIGURING": "pending", "RUNNING": "running", "COMPLETING": "running",
                "COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled", "TIMEOUT": "failed",
                "OUT_OF_MEMORY": "failed", "NODE_FAIL": "failed", "PREEMPTED": "failed", "BOOT_FAIL": "failed",
                "DEADLINE": "failed"}


@dataclass
class Settings:
    """Server settings, editable on the settings page (stored in ``<state>/settings.json``)."""

    mode: str = "local"
    dsarp_cmd: str = "dsarp"              # command for local mode
    sif: str = "dsarp.sif"                # container image for container and slurm mode
    bind: str = "/scratch"                # extra Apptainer bind path
    account: str = ""                     # Slurm -A
    partition: str = "normal"
    modules: str = "tools Apptainer"      # module load ... in Slurm scripts (empty = none)
    configs_dir: str = "configs"
    results_dir: str = "results"
    workdir: str = "work"
    data_root: str = ""                   # empty = $DSARP_DATA

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        for k in ("dsarp_cmd", "sif", "bind", "account", "partition", "modules", "configs_dir", "results_dir",
                  "workdir", "data_root"):
            v = getattr(self, k)
            if not isinstance(v, str) or len(v) > 400 or any(c in v for c in "\n\r\0;&|`$<>"):
                raise ValueError(f"{k}: invalid value")
        if self.account and not all(c.isalnum() or c in "-_" for c in self.account):
            raise ValueError("account: letters, digits, - and _ only")
        if not self.partition or not all(c.isalnum() or c in "-_," for c in self.partition):
            raise ValueError("partition: letters, digits, -, _ and , only")
        if not shlex.split(self.dsarp_cmd):
            raise ValueError("dsarp_cmd must not be empty")

    @property
    def data(self) -> str:
        return self.data_root or os.environ.get("DSARP_DATA", "")


@dataclass
class Job:
    id: str
    project: str
    stage: str
    args: list[str]                       # arguments after "dsarp"
    mode: str
    status: str = "queued"
    created: float = field(default_factory=time.time)
    started: float | None = None
    ended: float | None = None
    returncode: int | None = None
    slurm_id: str | None = None
    chain: str | None = None              # id of the "run all" chain it belongs to
    after: str | None = None              # job this one waits for
    resources: dict = field(default_factory=dict)
    error: str = ""
    pid: int | None = None

    def public(self) -> dict:
        d = asdict(self)
        d["command"] = "dsarp " + " ".join(shlex.quote(a) for a in self.args)
        return d


class JobStore:
    """All jobs of this server, with the processes of the running local ones."""

    def __init__(self, state_dir: Path, settings: Settings):
        self.dir = Path(state_dir)
        (self.dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.dir / "scripts").mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self._last_poll = 0.0
        self._load()

    # -- persistence -------------------------------------------------------
    @property
    def file(self) -> Path:
        return self.dir / "jobs.json"

    def _load(self) -> None:
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for d in raw:
            try:
                job = Job(**d)
            except TypeError:
                continue
            # a local job from an earlier server run can't be followed any more
            if job.mode != "slurm" and job.status in ACTIVE:
                alive = job.pid is not None and _pid_alive(job.pid)
                job.status = "lost" if alive or job.status == "running" else "cancelled"
                job.error = "the server was restarted while this job was waiting or running"
                job.ended = job.ended or time.time()
            self.jobs[job.id] = job

    def save(self) -> None:
        with self.lock:
            data = [asdict(j) for j in sorted(self.jobs.values(), key=lambda j: j.created)][-500:]
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.file)

    def log_path(self, job_id: str) -> Path:
        return self.dir / "logs" / f"{job_id}.log"

    # -- queries -----------------------------------------------------------
    def list(self, project: str | None = None) -> list[Job]:
        self.poll()
        with self.lock:
            js = [j for j in self.jobs.values() if project is None or j.project == project]
        return sorted(js, key=lambda j: j.created, reverse=True)

    def get(self, job_id: str) -> Job | None:
        self.poll()
        return self.jobs.get(job_id)

    def latest(self, project: str, stage: str) -> Job | None:
        js = [j for j in self.list(project) if j.stage == stage]
        return js[0] if js else None

    def read_log(self, job_id: str, offset: int = 0, limit: int = 200_000) -> tuple[str, int]:
        f = self.log_path(job_id)
        if not f.exists():
            return "", offset
        size = f.stat().st_size
        if offset > size:
            offset = 0
        start = max(offset, size - limit) if offset == 0 else offset
        with f.open("rb") as fh:
            fh.seek(start)
            data = fh.read(limit)
        return data.decode("utf-8", "replace"), start + len(data)

    # -- submitting --------------------------------------------------------
    def submit(self, project: str, stage: str, args: list[str], resources: dict | None = None,
               chain: str | None = None, after: Job | None = None, mode: str | None = None) -> Job:
        mode = mode or self.settings.mode
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        job = Job(id=time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], project=project, stage=stage,
                  args=list(args), mode=mode, chain=chain, after=after.id if after else None,
                  resources=dict(resources or {}))
        with self.lock:
            self.jobs[job.id] = job
            self.save()
        if mode == "slurm":
            self._submit_slurm(job, after)
        elif after is None:
            self._start_local(job)
        # local jobs that wait for another one are started by _advance()
        return job

    def _argv(self, job: Job) -> list[str]:
        s = self.settings
        if job.mode == "local":
            return shlex.split(s.dsarp_cmd) + job.args
        return ["apptainer", "exec", s.sif, "dsarp"] + job.args

    def _env(self) -> dict:
        env = dict(os.environ)
        s = self.settings
        if s.data:
            env["DSARP_DATA"] = s.data
        if s.bind:
            parts = [p for p in env.get("APPTAINER_BIND", "").split(",") if p]
            if s.bind not in parts:
                env["APPTAINER_BIND"] = ",".join(parts + [s.bind])
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _start_local(self, job: Job) -> None:
        argv = self._argv(job)
        log = self.log_path(job.id).open("ab")
        log.write(f"$ {' '.join(shlex.quote(a) for a in argv)}\n".encode())
        log.flush()
        try:
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=self._env(), start_new_session=True)
        except OSError as exc:
            log.write(f"could not start: {exc}\n".encode())
            log.close()
            with self.lock:
                job.status, job.error, job.ended, job.returncode = "failed", str(exc), time.time(), -1
                self.save()
            self._advance(job)
            return
        log.close()
        with self.lock:
            job.status, job.started, job.pid = "running", time.time(), proc.pid
            self.procs[job.id] = proc
            self.save()
        threading.Thread(target=self._wait_local, args=(job, proc), daemon=True).start()

    def _wait_local(self, job: Job, proc: subprocess.Popen) -> None:
        rc = proc.wait()
        with self.lock:
            self.procs.pop(job.id, None)
            if job.status != "cancelled":
                job.status = "done" if rc == 0 else "failed"
            job.returncode, job.ended = rc, time.time()
            self.save()
        self._advance(job)

    def _advance(self, finished: Job) -> None:
        """Start the local jobs that waited for ``finished``, or cancel them if it failed."""
        with self.lock:
            waiting = [j for j in self.jobs.values() if j.after == finished.id and j.status == "queued"]
        for j in waiting:
            if finished.status == "done":
                self._start_local(j)
            else:
                with self.lock:
                    j.status, j.ended = "cancelled", time.time()
                    j.error = f"not started: {finished.stage} ended with status {finished.status}"
                    self.save()
                self._advance(j)

    def script(self, job: Job, after: Job | None = None) -> str:
        """The sbatch script of a Slurm job."""
        s, r = self.settings, job.resources
        log = self.log_path(job.id).resolve()
        lines = ["#!/bin/bash",
                 f"#SBATCH -J dsarp-{job.stage}-{job.project}"[:80],
                 f"#SBATCH -p {s.partition}",
                 f"#SBATCH -t {r.get('time', '01:00:00')}",
                 "#SBATCH -N 1", "#SBATCH -n 1",
                 f"#SBATCH --cpus-per-task={int(r.get('cpus', 2))}",
                 f"#SBATCH --mem={r.get('mem', '8G')}",
                 f"#SBATCH -o {log}"]
        if s.account:
            lines.append(f"#SBATCH -A {s.account}")
        lines += ["set -euo pipefail", f"cd {shlex.quote(str(Path.cwd()))}"]
        if s.modules:
            lines += ["module reset >/dev/null 2>&1 || true", f"module load {s.modules}"]
        if s.data:
            lines.append(f"export DSARP_DATA={shlex.quote(s.data)}")
        if s.bind:
            b = shlex.quote(s.bind)
            lines.append(f'case ",${{APPTAINER_BIND:-}}," in *,{b},*) ;; '
                         f'*) export APPTAINER_BIND="${{APPTAINER_BIND:+$APPTAINER_BIND,}}"{b} ;; esac')
        argv = (["apptainer", "exec", s.sif, "dsarp"] if s.sif else shlex.split(s.dsarp_cmd)) + job.args
        lines += ['echo "[$(date -Is)] start on $(hostname) with ${SLURM_CPUS_PER_TASK:-1} CPUs"',
                  " ".join(shlex.quote(a) for a in argv),
                  'echo "[$(date -Is)] done"']
        return "\n".join(lines) + "\n"

    def _submit_slurm(self, job: Job, after: Job | None) -> None:
        if not _valid_resources(job.resources):
            self._fail(job, "invalid resources (cpus 1-128, mem like 32G, time like 12:00:00)")
            return
        script = self.dir / "scripts" / f"{job.id}.sbatch"
        script.write_text(self.script(job, after))
        cmd = ["sbatch", "--parsable"]
        if after is not None:
            if not after.slurm_id:
                self._fail(job, "the job it depends on was not submitted")
                return
            cmd.append(f"--dependency=afterok:{after.slurm_id}")
            cmd.append("--kill-on-invalid-dep=yes")
        cmd.append(str(script))
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._fail(job, f"sbatch failed: {exc}")
            return
        if out.returncode != 0:
            self._fail(job, f"sbatch failed: {out.stderr.strip() or out.stdout.strip()}")
            return
        with self.lock:
            job.slurm_id = out.stdout.strip().split(";")[0]
            job.status = "pending"
            self.save()

    def _fail(self, job: Job, msg: str) -> None:
        with self.lock:
            job.status, job.error, job.ended = "failed", msg, time.time()
            self.save()
        self.log_path(job.id).write_text(msg + "\n")

    # -- status of Slurm jobs ----------------------------------------------
    def poll(self, force: bool = False) -> None:
        if not force and time.time() - self._last_poll < 5:
            return
        self._last_poll = time.time()
        with self.lock:
            active = [j for j in self.jobs.values() if j.mode == "slurm" and j.status in ACTIVE and j.slurm_id]
        if not active or not shutil.which("squeue"):
            return
        states = _slurm_states([j.slurm_id for j in active])
        changed = False
        with self.lock:
            for j in active:
                st = states.get(j.slurm_id)
                if st is None:
                    continue
                new = _SLURM_STATE.get(st.split()[0].rstrip("+"), j.status)
                if new != j.status:
                    if new == "running" and not j.started:
                        j.started = time.time()
                    if new not in ACTIVE:
                        j.ended = time.time()
                        if new == "failed":
                            j.error = f"Slurm state {st}"
                    j.status, changed = new, True
            if changed:
                self.save()

    # -- cancelling --------------------------------------------------------
    def cancel(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status not in ACTIVE:
            return job
        if job.mode == "slurm" and job.slurm_id:
            subprocess.run(["scancel", job.slurm_id], capture_output=True, timeout=30)
        proc = self.procs.get(job.id)
        with self.lock:
            job.status, job.ended = "cancelled", time.time()
            self.save()
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            self._advance(job)
        return job


def _valid_resources(r: dict) -> bool:
    try:
        cpus = int(r.get("cpus", 2))
    except (TypeError, ValueError):
        return False
    return (1 <= cpus <= 128 and re.fullmatch(r"\d{1,4}[MGT]?", str(r.get("mem", "8G"))) is not None
            and re.fullmatch(r"(\d{1,2}-)?\d{1,3}:\d{2}(:\d{2})?", str(r.get("time", "01:00:00"))) is not None)


def _slurm_states(ids: list[str]) -> dict[str, str]:
    states: dict[str, str] = {}
    try:
        out = subprocess.run(["squeue", "-h", "-j", ",".join(ids), "-o", "%i %T"], capture_output=True,
                             text=True, timeout=30)
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    missing = [i for i in ids if i not in states]
    if missing and shutil.which("sacct"):
        try:
            out = subprocess.run(["sacct", "-n", "-X", "-P", "-j", ",".join(missing), "-o", "JobID,State"],
                                 capture_output=True, text=True, timeout=30)
            for line in out.stdout.splitlines():
                parts = line.split("|")
                if len(parts) == 2:
                    states[parts[0]] = parts[1]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return states


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
