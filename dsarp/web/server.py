"""Web interface for DSARP: run the pipeline, follow it on a flowchart, browse results.

Start it in the folder that holds ``configs/`` (the repository root)::

    python3 -m dsarp.web                 # or: dsarp serve
    python3 -m dsarp.web --mode slurm    # on an HPC login node

It listens on 127.0.0.1 only and prints a URL with a one-time token. On a
cluster, forward the port from your laptop: ``ssh -L 8765:localhost:8765 <login node>``.
Only the standard library is used.
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import uuid
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from . import results as R
from .jobs import ACTIVE, MODES, JobStore, Settings, _valid_resources
from .stages import BY_ID, NAME_RE, ORDER, STAGES, Paths, StageError, command, output_state

STATIC = Path(__file__).parent / "static"
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
URL_RE = re.compile(r"^(https://|git@|ssh://|file://|/)[^\s'\"`;|&$<>]{1,300}$")
GID_RE = re.compile(r"^[A-Za-z]-[a-z]{3}-[0-9a-f]{6,16}$")


# -- configs -------------------------------------------------------------------

def read_config(path: Path) -> dict:
    """Parse a repository config; PyYAML if installed, else a reader for the simple config format."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except ImportError:
        return _mini_yaml(text)


def _scalar(v: str):
    v = v.strip()
    if not v:
        return ""
    if v[0] in "\"'":
        try:
            return json.loads(v) if v[0] == '"' else v[1:-1]
        except json.JSONDecodeError:
            return v.strip("\"'")
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    if v.startswith("{") and v.endswith("}"):
        return {}
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v in ("null", "~"):
        return None
    return v


def _mini_yaml(text: str) -> dict:
    """Top-level ``key: value`` lines and lists of flat mappings (enough for repo configs)."""
    out: dict = {}
    key, item = None, None
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if not line.startswith((" ", "-")):
            k, _, v = line.partition(":")
            key = k.strip()
            out[key] = _scalar(v) if v.strip() else []
            item = None
        elif line.strip().startswith("- ") and key is not None:
            body = line.strip()[2:]
            if ":" in body and not body.startswith(("'", '"')):
                item = {}
                k, _, v = body.partition(":")
                item[k.strip()] = _scalar(v)
                out[key].append(item)
            else:
                out[key].append(_scalar(body))
        elif item is not None and ":" in line:
            k, _, v = line.strip().partition(":")
            item[k.strip()] = _scalar(v)
    return out


def write_config(path: Path, cfg: dict) -> None:
    q = json.dumps
    lines = ["# Written by the DSARP web interface.", f"name: {q(cfg['name'])}", f"repo_url: {q(cfg['repo_url'])}",
             f"commit: {q(cfg['commit'])}", "language: java", f"build: {cfg.get('build', 'auto')}", "detectors:"]
    for d in cfg["detectors"]:
        lines.append(f"  - tool: {d['tool']}")
        lines.append(f"    path: {q(d['path'])}")
        if d.get("module"):
            lines.append(f"    module: {q(d['module'])}")
    lines.append("protected_api: [" + ", ".join(q(x) for x in cfg.get("protected_api", [])) + "]")
    if cfg.get("notes"):
        lines.append(f"notes: {q(cfg['notes'])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_new_config(body: dict) -> dict:
    name = str(body.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise ValueError("name: letters, digits, '.', '_' and '-' only")
    url = str(body.get("repo_url", "")).strip()
    if not URL_RE.match(url):
        raise ValueError("repo_url: an https://, git@ or ssh:// URL, or a local path")
    commit = str(body.get("commit", "")).strip()
    if not COMMIT_RE.match(commit):
        raise ValueError("commit: a hash of 7 to 40 hex characters")
    build = str(body.get("build", "auto"))
    if build not in ("auto", "maven", "gradle"):
        raise ValueError("build: auto, maven or gradle")
    dets = []
    for d in body.get("detectors") or []:
        tool, path, module = str(d.get("tool", "")), str(d.get("path", "")).strip(), str(d.get("module") or "").strip()
        if tool not in ("arcan", "designite"):
            raise ValueError("detector tool: arcan or designite")
        if not path or len(path) > 300 or any(c in path for c in "\n\r\0`$;|&<>") or ".." in Path(path).parts:
            raise ValueError(f"detector path: invalid ({path!r})")
        if module and not NAME_RE.match(module):
            raise ValueError("detector module: letters, digits, '.', '_' and '-' only")
        dets.append({"tool": tool, "path": path, "module": module or None})
    if not dets:
        raise ValueError("add at least one detector output folder")
    api = [str(x).strip() for x in body.get("protected_api") or [] if str(x).strip()]
    if any(not re.fullmatch(r"[A-Za-z0-9_.$*]{1,200}", x) for x in api):
        raise ValueError("protected_api: fully qualified names only")
    notes = str(body.get("notes", ""))[:500].replace("\n", " ")
    return {"name": name, "repo_url": url, "commit": commit, "build": build, "detectors": dets,
            "protected_api": api, "notes": notes}


# -- application state -----------------------------------------------------------

class App:
    def __init__(self, root: Path, state: Path, settings: Settings, token: str):
        self.root = root
        self.state = state
        self.settings = settings
        self.token = token
        self.jobs = JobStore(state, settings)
        self.lock = threading.Lock()

    def save_settings(self) -> None:
        (self.state / "settings.json").write_text(json.dumps(asdict(self.settings), indent=1))

    def _dir(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    def configs(self) -> list[Path]:
        d = self._dir(self.settings.configs_dir)
        return sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")) if d.is_dir() else []

    def config_path(self, name: str) -> Path | None:
        if not NAME_RE.match(name):
            return None
        for p in self.configs():
            if p.stem == name:
                return p
        return None

    def paths(self, cfg_path: Path, name: str) -> Paths:
        s = self.settings
        cfg = Path(os.path.relpath(cfg_path, self.root)) if cfg_path.is_relative_to(self.root) else cfg_path
        return Paths(config=cfg, name=name, results=Path(s.results_dir) / name, workdir=Path(s.workdir),
                     data_root=Path(s.data) if s.data else None)

    def abs_paths(self, p: Paths) -> Paths:
        return Paths(config=self._dir(str(p.config)), name=p.name, results=self._dir(str(p.results)),
                     workdir=self._dir(str(p.workdir)), data_root=p.data_root)

    def project(self, name: str) -> dict | None:
        cp = self.config_path(name)
        if cp is None:
            return None
        try:
            cfg = read_config(cp)
        except (OSError, ValueError) as exc:
            cfg = {"error": str(exc)}
        p = self.paths(cp, name)
        ap = self.abs_paths(p)
        stages, done = [], {}
        for st in STAGES:
            outs = output_state(st, ap)
            job = self.jobs.latest(name, st.id)
            status = "done" if outs["exists"] else "todo"
            if job is not None:
                if job.status in ACTIVE:
                    status = job.status
                elif job.status in ("failed", "lost") and (
                        not outs["exists"] or (outs["mtime"] or 0) < (job.started or job.created)):
                    status = job.status
                elif job.status == "cancelled" and not outs["exists"]:   # older outputs stay valid
                    status = job.status
                elif job.status == "done" and not st.outputs:
                    status = "done"
            done[st.id] = status == "done"
            stages.append({**st.spec(), "status": status, "outputs_state": outs,
                           "job": job.public() if job else None, "command": "dsarp " + " ".join(command(st.id, p))})
        for s in stages:
            s["ready"] = all(done.get(r) for r in s["requires"])
            s["missing"] = [r for r in s["requires"] if not done.get(r)]
        return {"name": name, "config_file": str(p.config), "config": cfg, "results": str(p.results),
                "workdir": str(p.workdir), "checkout": str(p.checkout), "stages": stages}

    def run(self, name: str, body: dict) -> list[dict]:
        cp = self.config_path(name)
        if cp is None:
            raise KeyError(name)
        mode = body.get("mode") or self.settings.mode
        if mode not in MODES:
            raise ValueError("mode: local, container or slurm")
        ids = body.get("stages") or [body.get("stage")]
        if any(i not in BY_ID for i in ids):
            raise ValueError("unknown stage")
        ids = sorted(set(ids), key=ORDER.index)
        p = self.paths(cp, name)
        options, resources = body.get("options") or {}, body.get("resources") or {}
        cmds = [(i, command(i, p, options.get(i))) for i in ids]          # validate everything first
        res_of = {i: {**BY_ID[i].spec()["resources"], **(resources.get(i) or {})} for i in ids}
        if mode == "slurm":
            for i, r in res_of.items():
                if not _valid_resources(r):
                    raise ValueError(f"{i}: resources must be CPUs 1-128, memory like 32G, time like 12:00:00")
        net = [i for i in ids if BY_ID[i].network]
        if net and mode == "slurm":
            if len(ids) > 1:
                raise ValueError(f"{BY_ID[net[0]].label} needs the internet and runs on this machine, not as a "
                                 "Slurm job: run it on its own (after Plan, before Verify)")
            mode = "container" if self.settings.sif else "local"
        chain = uuid.uuid4().hex[:8] if len(ids) > 1 else None
        with self.lock:
            busy = [j for j in self.jobs.list(name) if j.status in ACTIVE and j.stage in ids]
            if busy:
                raise ValueError(f"{busy[0].stage} is already {busy[0].status} (job {busy[0].id})")
            prev, out = None, []
            for i, args in cmds:
                job = self.jobs.submit(name, i, args, res_of[i], chain=chain, after=prev, mode=mode)
                out.append(job.public())
                prev = job
        return out


# -- HTTP ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"dsarp-web/{__version__}"
    app: App

    def log_message(self, fmt, *args):  # quieter log
        if os.environ.get("DSARP_WEB_DEBUG"):
            super().log_message(fmt, *args)

    # helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, default=str).encode(), "application/json")

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _authed(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        given = cookie["dsarp_token"].value if "dsarp_token" in cookie else self.headers.get("X-DSARP-Token", "")
        return bool(given) and hmac.compare_digest(given, self.app.token)

    def _host_ok(self) -> bool:
        if self.server.server_address[0] not in ("127.0.0.1", "::1", "localhost"):
            return True       # listening on a public address: the token is the protection
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("localhost", "127.0.0.1", "::1") or host == self.server.server_address[0]

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("request too large")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ValueError("expected JSON")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    # routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "unexpected Host header")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/" and "token" in q:
            if hmac.compare_digest(q["token"][0], self.app.token):
                return self._send(303, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"dsarp_token={self.app.token}; HttpOnly; SameSite=Strict; Path=/"})
            return self._send(403, _LOGIN.replace("{msg}", "Wrong token.").encode(), "text/html; charset=utf-8")
        if not self._authed():
            if u.path.startswith("/api/"):
                return self._err(401, "not logged in")
            return self._send(401, _LOGIN.replace("{msg}", "").encode(), "text/html; charset=utf-8")
        try:
            self._get(u.path, q)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - report to the browser
            self._err(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self):
        if not self._host_ok():
            return self._err(403, "unexpected Host header")
        if not self._authed():
            return self._err(401, "not logged in")
        try:
            self._post(urlparse(self.path).path, self._body())
        except (ValueError, StageError) as exc:
            self._err(400, str(exc))
        except KeyError as exc:
            self._err(404, f"not found: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._err(500, f"{type(exc).__name__}: {exc}")

    def _static(self, name: str) -> None:
        f = (STATIC / name).resolve()
        if not f.is_relative_to(STATIC.resolve()) or not f.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype, {
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                       "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-src 'self'; "
                                       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})

    def _get(self, path: str, q: dict) -> None:
        app = self.app
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        parts = [unquote(x) for x in path.strip("/").split("/")]
        if parts[:1] != ["api"]:
            return self._err(404, "not found")
        rest = parts[1:]
        if rest == ["info"]:
            return self._json({"version": __version__, "root": str(app.root), "state": str(app.state),
                               "settings": asdict(app.settings), "modes": list(MODES),
                               "stages": [s.spec() for s in STAGES], "time": time.time()})
        if rest == ["projects"]:
            out = []
            for cp in app.configs():
                pr = app.project(cp.stem)
                if pr:
                    c = pr["config"]
                    out.append({"name": pr["name"], "repo_url": c.get("repo_url"), "commit": c.get("commit"),
                                "build": c.get("build"), "error": c.get("error"),
                                "stages": {s["id"]: s["status"] for s in pr["stages"]}})
            return self._json(out)
        if len(rest) >= 2 and rest[0] == "projects":
            name = rest[1]
            pr = app.project(name)
            if pr is None:
                return self._err(404, f"no config for {name}")
            res = app._dir(pr["results"])
            if len(rest) == 2:
                return self._json(pr)
            if rest[2:] == ["overview"]:
                return self._json(R.overview(res))
            if len(rest) == 4 and rest[2] == "group":
                if not GID_RE.match(rest[3]):
                    return self._err(400, "bad group id")
                g = R.group(res, rest[3])
                return self._json(g) if g else self._err(404, "no such group")
            if len(rest) >= 4 and rest[2] == "file":
                f = (res / "/".join(rest[3:])).resolve()
                if not f.is_relative_to(res.resolve()) or not f.is_file():
                    return self._err(404, "no such file")
                ctype = mimetypes.guess_type(f.name)[0] or "text/plain"
                if f.suffix in (".csv", ".md", ".patch", ".yml", ".yaml", ".log", ".tsv"):
                    ctype = "text/plain"
                extra = {"Content-Security-Policy": "sandbox allow-scripts; default-src 'self' 'unsafe-inline' data: https:"} \
                    if ctype == "text/html" else {}
                return self._send(200, f.read_bytes(), ctype + "; charset=utf-8", extra)
            if rest[2:] == ["jobs"]:
                return self._json([j.public() for j in app.jobs.list(name)])
            return self._err(404, "not found")
        if rest == ["jobs"]:
            return self._json([j.public() for j in app.jobs.list()[:200]])
        if len(rest) >= 2 and rest[0] == "jobs":
            job = app.jobs.get(rest[1])
            if job is None:
                return self._err(404, "no such job")
            if len(rest) == 2:
                return self._json(job.public())
            if rest[2] == "log":
                off = int((q.get("offset") or ["0"])[0] or 0)
                text, new = app.jobs.read_log(job.id, off)
                return self._json({"text": text, "offset": new, "status": job.status})
            if rest[2] == "script" and job.mode == "slurm":
                f = app.state / "scripts" / f"{job.id}.sbatch"
                return self._send(200, f.read_bytes() if f.exists() else b"", "text/plain; charset=utf-8")
        return self._err(404, "not found")

    def _post(self, path: str, body: dict) -> None:
        app = self.app
        parts = [unquote(x) for x in path.strip("/").split("/")]
        if parts == ["api", "settings"]:
            s = Settings(**{**asdict(app.settings), **{k: v for k, v in body.items()
                                                        if k in asdict(app.settings)}})
            s.validate()
            app.settings.__dict__.update(asdict(s))
            app.save_settings()
            return self._json(asdict(app.settings))
        if parts == ["api", "projects"]:
            cfg = validate_new_config(body)
            d = app._dir(app.settings.configs_dir)
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{cfg['name']}.yaml"
            if f.exists() and not body.get("overwrite"):
                raise ValueError(f"{f.name} already exists")
            write_config(f, cfg)
            return self._json({"ok": True, "file": str(f)})
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "run":
            return self._json({"jobs": app.run(parts[2], body)})
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "preview":
            cp = app.config_path(parts[2])
            if cp is None:
                raise KeyError(parts[2])
            stage = str(body.get("stage"))
            args = command(stage, app.paths(cp, parts[2]), body.get("options"))
            return self._json({"command": "dsarp " + " ".join(args)})
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
            return self._json(app.jobs.cancel(parts[2]).public())
        return self._err(404, "not found")


_LOGIN = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>DSARP</title>
<style>body{font:15px system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#fbfaf7}
form{background:#fff;border:1px solid #e4e1d8;border-radius:12px;padding:28px;max-width:380px}
input{width:100%;padding:8px;margin:12px 0;border:1px solid #ccc;border-radius:6px;font:inherit}
button{padding:8px 14px;border-radius:6px;border:1px solid #333;background:#1f1e1b;color:#fff;font:inherit}
p{color:#6b6962;font-size:13px}</style></head><body><form method="get" action="/">
<b>DSARP web interface</b><p>Paste the token printed in the terminal where you started the server. {msg}</p>
<input name="token" autocomplete="off" autofocus placeholder="token"><button>Open</button></form></body></html>"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dsarp serve", description="DSARP web interface")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--root", default=".", help="folder with configs/ (default: current folder)")
    p.add_argument("--state", default=".dsarp-web", help="folder for jobs, logs and settings")
    p.add_argument("--mode", choices=MODES, help="how stages run: local, container or slurm")
    p.add_argument("--sif", help="container image (container and slurm mode)")
    p.add_argument("--account", help="Slurm account (-A)")
    p.add_argument("--data-root", help="detector data folder (default: $DSARP_DATA)")
    p.add_argument("--token", help="login token (default: random; or $DSARP_WEB_TOKEN)")
    return p


def make_app(args: argparse.Namespace) -> App:
    root = Path(args.root).resolve()
    state = Path(args.state)
    state = (state if state.is_absolute() else root / state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state, 0o700)
    except OSError:
        pass
    sf = state / "settings.json"
    settings = Settings()
    if sf.exists():
        try:
            stored = json.loads(sf.read_text())
            settings = Settings(**{k: v for k, v in stored.items() if k in asdict(settings)})
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    for k, a in (("mode", "mode"), ("sif", "sif"), ("account", "account"), ("data_root", "data_root")):
        if getattr(args, a, None):
            setattr(settings, k, getattr(args, a))
    settings.validate()
    token = args.token or os.environ.get("DSARP_WEB_TOKEN") or secrets.token_urlsafe(18)
    app = App(root, state, settings, token)
    app.save_settings()
    return app


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.chdir(Path(args.root).resolve())
    args.root = "."
    app = make_app(args)
    try:
        httpd = serve(app, args.host, args.port)
    except OSError as exc:
        print(f"ERROR cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    url = f"http://{host}:{httpd.server_address[1]}/?token={app.token}"
    print(f"DSARP web interface {__version__}")
    print(f"  folder:   {app.root}")
    print(f"  mode:     {app.settings.mode}" + (f" (image {app.settings.sif})" if app.settings.mode != "local" else ""))
    print(f"  open:     {url}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  warning:  listening on a non-local address; anyone who has the token can run jobs as you")
    print(f"  remote:   ssh -L {httpd.server_address[1]}:localhost:{httpd.server_address[1]} <this host>, "
          "then open the URL above on your laptop")
    print("  stop:     Ctrl+C (Slurm jobs keep running)")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
