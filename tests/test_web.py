"""Web interface: stage commands, configs, jobs (local and Slurm scripts), HTTP API, result readers."""

from __future__ import annotations

import json
import shlex
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from dsarp.web import results as R
from dsarp.web.jobs import JobStore, Settings, _valid_resources
from dsarp.web.server import App, _mini_yaml, read_config, serve, validate_new_config, write_config
from dsarp.web.stages import BY_ID, STAGES, Paths, StageError, command, output_state

ROOT = Path(__file__).resolve().parents[1]


def _paths(tmp: Path, name="karaf") -> Paths:
    return Paths(config=Path("configs") / f"{name}.yaml", name=name, results=tmp / "results" / name,
                 workdir=tmp / "work", data_root=Path("/data"))


# -- stages -----------------------------------------------------------------------

def test_every_stage_builds_a_command(tmp_path):
    p = _paths(tmp_path)
    for st in STAGES:
        args = command(st.id, p)
        assert args[0] == st.id


def test_plan_options_become_flags(tmp_path):
    args = command("plan", _paths(tmp_path), {"groups": "5", "backend": "cpsat", "explain": False, "llm": "none"})
    s = " ".join(args)
    assert "--groups 5" in s and "--backend cpsat" in s
    assert "--explain" not in args and "--llm" not in args
    assert "--time-limit 300" in s                         # default kept, float printed as int


def test_option_validation(tmp_path):
    p = _paths(tmp_path)
    with pytest.raises(StageError):
        command("plan", p, {"backend": "gurobi"})
    with pytest.raises(StageError):
        command("plan", p, {"groups": 0})
    with pytest.raises(StageError):
        command("plan", p, {"nope": 1})
    with pytest.raises(StageError):
        command("plan", p, {"types": "a\nb"})
    with pytest.raises(StageError):
        command("bogus", p)


def test_ingest_and_model_paths(tmp_path):
    p = _paths(tmp_path)
    assert command("ingest", p)[-2:] == ["--out", str(p.results)]
    m = command("model", p)
    assert m[m.index("--out") + 1] == str(p.results / "model")
    assert "--data-root" in m


def test_output_state(tmp_path):
    p = _paths(tmp_path)
    assert not output_state(BY_ID["plan"], p)["exists"]
    (p.results / "plans").mkdir(parents=True)
    (p.results / "plans" / "plans.json").write_text("{}")
    assert not output_state(BY_ID["plan"], p)["exists"]   # summary.csv still missing
    (p.results / "plans" / "summary.csv").write_text("")
    assert output_state(BY_ID["plan"], p)["exists"]
    (p.checkout / ".git").mkdir(parents=True)
    assert output_state(BY_ID["checkout"], p)["exists"]


# -- configs ----------------------------------------------------------------------

@pytest.mark.parametrize("cfg", sorted((ROOT / "configs").glob("*.yaml")), ids=lambda p: p.stem)
def test_mini_yaml_reads_the_repo_configs(cfg):
    want = yaml.safe_load(cfg.read_text())
    got = _mini_yaml(cfg.read_text())
    for k in ("name", "repo_url", "build"):
        assert str(got.get(k)) == str(want.get(k))
    assert str(got.get("commit") or "") == str(want.get("commit") or "")
    assert [(d["tool"], d["path"], d.get("module")) for d in got["detectors"]] == \
           [(d["tool"], d["path"], d.get("module")) for d in want["detectors"]]


def test_new_config_round_trip(tmp_path):
    cfg = validate_new_config({"name": "demo", "repo_url": "https://github.com/x/y", "commit": "abcdef1",
                               "build": "maven", "protected_api": ["org.x.Api"],
                               "detectors": [{"tool": "arcan", "path": "Arcan/Demo"},
                                             {"tool": "designite", "path": "Designite/Demo/core", "module": "core"}]})
    f = tmp_path / "demo.yaml"
    write_config(f, cfg)
    for data in (read_config(f), _mini_yaml(f.read_text())):
        assert data["name"] == "demo" and data["commit"] == "abcdef1" and data["build"] == "maven"
        assert data["detectors"][1]["module"] == "core"
        assert data["protected_api"] == ["org.x.Api"]
    from dsarp.config import load_config
    assert load_config(f, tmp_path).name == "demo"


@pytest.mark.parametrize("bad", [
    {"name": "../x"}, {"repo_url": "https://x/y; rm -rf /"}, {"commit": "xyz"},
    {"detectors": [{"tool": "arcan", "path": "../../etc"}]}, {"detectors": []},
    {"detectors": [{"tool": "pmd", "path": "a"}]}])
def test_new_config_rejects_bad_input(bad):
    ok = {"name": "demo", "repo_url": "https://github.com/x/y", "commit": "abcdef1",
          "detectors": [{"tool": "arcan", "path": "Arcan/Demo"}]}
    with pytest.raises(ValueError):
        validate_new_config({**ok, **bad})


# -- jobs -------------------------------------------------------------------------

def _wait(store, ids, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if all(store.jobs[i].status not in ("queued", "pending", "running") for i in ids):
            return
        time.sleep(0.1)
    raise AssertionError("jobs did not finish")


def test_local_chain_stops_after_a_failure(tmp_path):
    script = "import sys; print('args', sys.argv[1:]); sys.exit(3 if 'fail' in sys.argv else 0)"
    s = Settings(mode="local", dsarp_cmd=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}")
    store = JobStore(tmp_path / "state", s)
    a = store.submit("demo", "triage", ["triage", "ok"])
    b = store.submit("demo", "plan", ["plan", "fail"], after=a)
    c = store.submit("demo", "report", ["report"], after=b)
    _wait(store, [a.id, b.id, c.id])
    assert (a.status, b.status, c.status) == ("done", "failed", "cancelled")
    assert b.returncode == 3 and "not started" in c.error
    text, off = store.read_log(a.id)
    assert "args ['triage', 'ok']" in text and off > 0
    more, off2 = store.read_log(a.id, off)
    assert more == "" and off2 == off
    # a new store reads the saved jobs
    again = JobStore(tmp_path / "state", s)
    assert again.jobs[b.id].status == "failed"


def test_local_job_can_be_cancelled(tmp_path):
    s = Settings(mode="local", dsarp_cmd=f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'")
    store = JobStore(tmp_path / "state", s)
    j = store.submit("demo", "plan", ["plan"])
    time.sleep(0.3)
    store.cancel(j.id)
    _wait(store, [j.id])
    assert j.status == "cancelled"


def test_missing_command_fails_cleanly(tmp_path):
    store = JobStore(tmp_path / "state", Settings(mode="local", dsarp_cmd="/nonexistent/dsarp"))
    j = store.submit("demo", "plan", ["plan"])
    assert j.status == "failed" and j.returncode == -1


def test_slurm_script_quotes_arguments(tmp_path):
    s = Settings(mode="slurm", sif="/scratch/p/dsarp.sif", account="hpc-prf-x", data_root="/scratch/data")
    store = JobStore(tmp_path / "state", s)
    from dsarp.web.jobs import Job
    job = Job(id="j1", project="demo", stage="plan", args=["plan", "results/demo", "--types", "a; rm -rf /"],
              mode="slurm", resources={"cpus": 16, "mem": "32G", "time": "12:00:00"})
    text = store.script(job)
    assert "#SBATCH -A hpc-prf-x" in text and "#SBATCH --cpus-per-task=16" in text
    assert "apptainer exec /scratch/p/dsarp.sif dsarp plan results/demo --types 'a; rm -rf /'" in text
    assert "export DSARP_DATA=/scratch/data" in text


def test_resource_validation():
    assert _valid_resources({"cpus": 16, "mem": "32G", "time": "12:00:00"})
    assert _valid_resources({"cpus": "4", "mem": "4000", "time": "1-00:00:00"})
    assert not _valid_resources({"cpus": 0})
    assert not _valid_resources({"mem": "32G; echo"})
    assert not _valid_resources({"time": "soon"})


def test_settings_validation():
    with pytest.raises(ValueError):
        Settings(mode="docker").validate()
    with pytest.raises(ValueError):
        Settings(account="x; rm").validate()
    with pytest.raises(ValueError):
        Settings(sif="a.sif && rm").validate()
    Settings(mode="slurm", account="hpc-prf-dssecs").validate()


# -- results ------------------------------------------------------------------------

def _results(d: Path) -> None:
    d.mkdir(parents=True)
    (d / "smells.csv").write_text("smell_id,tool,type,level\na:1,arcan,cyclic_dependency,package\n"
                                  "d:1,designite,cyclic_dependency,package\n")
    (d / "manifest.json").write_text(json.dumps({"repo": "demo", "commit": "abcdef1"}))
    (d / "triage").mkdir()
    (d / "triage" / "triage.json").write_text(json.dumps({"smells": 2, "decisions": {"keep": 1, "drop": 1}, "groups": 1,
        "groups_by_type": {"cyclic_dependency:package": 1},
        "by_reason": [{"tool": "arcan", "decision": "drop", "reason": "test only", "n": 1}]}))
    (d / "triage" / "groups.csv").write_text("group_id,type,packages,priority\nC-pac-0123abcd,cyclic_dependency,"
                                             "\"[\"\"a\"\", \"\"b\"\"]\",0.5\n")
    (d / "plans").mkdir()
    (d / "plans" / "plans.json").write_text(json.dumps({"groups": [{"group_id": "C-pac-0123abcd", "type": "cyclic_dependency",
        "rank": 1, "priority": 0.5, "recommended": "most_removed",
        "plans": [{"name": "most_removed", "score": 0.9, "benefit": 1.0, "change_cost": 5.0, "new_smells": 0,
                   "operations": [{"op": "move_class", "class": "a.X", "from_package": "a", "to_package": "b"}]}]}]}))
    (d / "plans" / "report.md").write_text("# Report\n\n## 1. Cyclic dependency: C-pac-0123abcd\n\n- step\n\n## 2. Other: X\n")
    (d / "verify" / "C-pac-0123abcd").mkdir(parents=True)
    (d / "verify" / "summary.csv").write_text("group_id,plan,operations,compiles,kept,target_removed,prediction_matches,"
                                              "files_changed,patch_lines,seconds\nC-pac-0123abcd,most_removed,1,True,1,"
                                              "True,True,2,10,3.0\n")
    (d / "verify" / "C-pac-0123abcd" / "plan.patch").write_text("diff --git a/x b/x\n+new\n")


def test_overview_and_group(tmp_path):
    assert R.overview(tmp_path / "missing") == {"available": {"report": False, "report_md": False}}
    d = tmp_path / "res"
    _results(d)
    ov = R.overview(d)
    assert ov["smells"]["total"] == 2 and ov["triage"]["groups"] == 1
    assert ov["plans"]["planned"] == 1 and ov["plans"]["groups"][0]["score"] == 0.9
    assert ov["verify"][0]["compiles"] is True
    g = R.group(d, "C-pac-0123abcd")
    assert g["plan"]["recommended"] == "most_removed" and g["patch"].startswith("diff")
    assert g["report_md"].startswith("## 1.") and "Other" not in g["report_md"]
    assert R.group(d, "C-pac-ffffffff") is None


# -- HTTP API -------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "demo.yaml").write_text(
        "name: demo\nrepo_url: https://github.com/x/y\ncommit: abcdef1\ndetectors:\n  - tool: arcan\n    path: A\n")
    _results(tmp_path / "results" / "demo")
    (tmp_path / "secret.txt").write_text("secret")
    app = App(tmp_path, tmp_path / ".state", Settings(mode="local", dsarp_cmd=f"{sys.executable} -c pass"), "tok")
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()
    httpd.server_close()


def _req(url, token="tok", data=None, headers=None):
    h = {"X-DSARP-Token": token} if token else {}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h)) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw.decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_api_needs_the_token(server):
    url, _ = server
    assert _req(url + "/api/projects", token=None)[0] == 401
    assert _req(url + "/api/projects", token="wrong")[0] == 401
    assert _req(url + "/api/projects")[0] == 200
    assert _req(url + "/api/info", headers={"Host": "evil.example"})[0] == 403
    code, page = _req(url + "/", token=None)
    assert code == 401 and "token" in page


def test_api_projects_results_and_files(server):
    url, _ = server
    code, ps = _req(url + "/api/projects")
    assert [p["name"] for p in ps] == ["demo"] and ps[0]["stages"]["plan"] == "todo"   # summary.csv missing
    code, pr = _req(url + "/api/projects/demo")
    plan = next(s for s in pr["stages"] if s["id"] == "plan")
    assert plan["command"].startswith("dsarp plan results/demo")
    assert _req(url + "/api/projects/demo/overview")[1]["plans"]["planned"] == 1
    assert _req(url + "/api/projects/demo/group/C-pac-0123abcd")[1]["patch"].startswith("diff")
    assert _req(url + "/api/projects/demo/group/..%2F..")[0] in (400, 404)
    assert _req(url + "/api/projects/demo/file/plans/report.md")[0] == 200
    assert _req(url + "/api/projects/demo/file/../../secret.txt")[0] == 404
    assert _req(url + "/api/projects/demo/file/%2E%2E/%2E%2E/secret.txt")[0] == 404
    assert _req(url + "/api/projects/nope")[0] == 404


def test_api_preview_run_and_new_project(server):
    url, app = server
    code, r = _req(url + "/api/projects/demo/preview", data={"stage": "plan", "options": {"groups": 3}})
    assert code == 200 and "--groups 3" in r["command"]
    assert _req(url + "/api/projects/demo/preview", data={"stage": "plan", "options": {"backend": "x"}})[0] == 400
    code, r = _req(url + "/api/projects/demo/run", data={"stages": ["triage", "plan"]})
    assert code == 200 and [j["stage"] for j in r["jobs"]] == ["triage", "plan"]
    assert r["jobs"][1]["after"] == r["jobs"][0]["id"]
    _wait(app.jobs, [j["id"] for j in r["jobs"]])
    code, r = _req(url + "/api/projects", data={"name": "new1", "repo_url": "https://github.com/a/b", "commit": "1234567",
                                                "detectors": [{"tool": "arcan", "path": "Arcan/New"}]})
    assert code == 200 and (app.root / "configs" / "new1.yaml").exists()
    assert _req(url + "/api/projects", data={"name": "new1", "repo_url": "https://github.com/a/b", "commit": "1234567",
                                             "detectors": [{"tool": "arcan", "path": "x"}]})[0] == 400
    code, s = _req(url + "/api/settings", data={"mode": "slurm", "account": "hpc-prf-x"})
    assert code == 200 and s["mode"] == "slurm"
    assert _req(url + "/api/settings", data={"account": "a b"})[0] == 400
    assert _req(url + "/api/projects/demo/run", data={"stage": "verify", "resources": {"verify": {"cpus": 999}}})[0] == 400
