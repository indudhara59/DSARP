"""Command-line interface.

Examples::

    dsarp check configs/tika.yaml
    dsarp ingest configs/tika.yaml --data-root /path/to/data --out results/tika
    dsarp ingest configs/*.yaml --data-root /path/to/data --out results
    dsarp summary results/tika
    dsarp checkout configs/tika.yaml --workdir work
    dsarp model configs/tika.yaml --repo work/tika --out results/tika/model
    dsarp triage results/tika
    dsarp calibrate results/tika
    dsarp learn results/tika results/struts --workdir work
    dsarp plan results/tika --groups 10 --time-limit 60
    dsarp explain results/tika
    dsarp verify results/tika --workdir work
    dsarp evaluate results/tika --groups 10
    dsarp report results/tika
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from . import __version__
from .checkout import CheckoutError, checkout
from .config import ConfigError, load_config
from .extractor import ExtractorError
from .ingest import ingest, write
from .program import ProgramModel, build_model


def _cmd_check(args: argparse.Namespace) -> int:
    status = 0
    for path in args.configs:
        try:
            cfg = load_config(path, args.data_root)
        except ConfigError as exc:
            print(f"INVALID  {path}: {exc}")
            status = 1
            continue
        missing = [str(d.path) for d in cfg.detectors if not Path(d.path).is_dir()]
        state = "OK" if not missing else "MISSING DATA"
        print(f"{state:8s} {path}: {cfg.name} @ {cfg.commit or '(commit not set)'} "
              f"detectors={','.join(cfg.detector_tools)}")
        for m in missing:
            print(f"         missing folder: {m}")
            status = 1
    return status


def _cmd_ingest(args: argparse.Namespace) -> int:
    many = len(args.configs) > 1
    status = 0
    for path in args.configs:
        try:
            cfg = load_config(path, args.data_root)
            result = ingest(cfg)
        except (ConfigError, FileNotFoundError) as exc:
            print(f"ERROR {path}: {exc}", file=sys.stderr)
            status = 1
            continue
        base = Path(args.out) if args.out else Path("results")
        out = base / cfg.name if (many or not args.out) else base
        write(result, out)
        _print_summary(result.manifest, out)
    return status


def _print_summary(manifest: dict, out: Path) -> None:
    c = manifest["counts"]
    print(f"\n{manifest['repo']} @ {manifest['commit'] or '?'}  ->  {out}")
    print(f"  components: {c['packages']} packages, {c['classes']} classes")
    print(f"  smells: {c['smells']}  (confirmed by both tools: {c['confirmed_by_both']})")
    print(f"  flags: test-only {c.get('test_only', 0)}, test-support-only {c.get('test_support_only', 0)}, "
          f"generated-only {c.get('generated_only', 0)}, with external members {c.get('with_external_members', 0)}, "
          f"with parent packages {c.get('with_parent_members', 0)}")
    for tool, types in c["by_tool_and_type"].items():
        print(f"    {tool:10s} " + ", ".join(f"{k}={v}" for k, v in sorted(types.items())))
    for w in manifest["warnings"]:
        print(f"  warning: {w}")


def _cmd_summary(args: argparse.Namespace) -> int:
    for d in args.results:
        manifest = json.loads((Path(d) / "manifest.json").read_text(encoding="utf-8"))
        _print_summary(manifest, Path(d))
        if args.detail:
            smells = pd.read_csv(Path(d) / "smells.csv")
            print(smells.groupby(["tool", "type", "level"]).size().to_string())
    return 0


def _cmd_checkout(args: argparse.Namespace) -> int:
    status = 0
    for path in args.configs:
        try:
            cfg = load_config(path, args.data_root)
            dest = checkout(cfg, args.workdir, full_history=not args.shallow)
            print(f"{cfg.name}: {dest} @ {cfg.commit}")
        except (ConfigError, CheckoutError) as exc:
            print(f"ERROR {path}: {exc}", file=sys.stderr)
            status = 1
    return status


def _cmd_detect(args: argparse.Namespace) -> int:
    """Stage 0: run the configured detectors on the checkout at the configured commit."""
    import json
    import subprocess
    import time

    from .verify.detectors import execute
    status = 0
    for path in args.configs:
        try:
            cfg = load_config(path, args.data_root)
        except ConfigError as exc:
            print(f"ERROR {path}: {exc}", file=sys.stderr)
            status = 1
            continue
        repo = Path(args.workdir) / cfg.name
        if not repo.exists():
            print(f"ERROR {cfg.name}: no checkout at {repo} (run dsarp checkout)", file=sys.stderr)
            status = 1
            continue
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        if cfg.commit and not head.startswith(cfg.commit[:12]):
            print(f"ERROR {cfg.name}: checkout is at {head[:12]}, not {cfg.commit[:12]} (run dsarp checkout)",
                  file=sys.stderr)
            status = 1
            continue
        runs = [d for d in cfg.detectors if d.command and (not args.tool or d.tool in args.tool)]
        if not runs:
            print(f"{cfg.name}: no detector has a 'command' in the configuration; nothing to run")
            continue
        for d in runs:
            src = repo
            if d.module:
                cands = [p for p in [repo / d.module] + sorted(repo.glob(f"**/{d.module}"))[:5] if p.is_dir()]
                src = cands[0] if cands else repo
            t0 = time.time()
            ok, tail = execute(d.command, src, d.path, timeout=args.timeout)
            (Path(d.path) / "dsarp-detect.json").write_text(json.dumps(
                {"tool": d.tool, "module": d.module, "commit": head, "command": d.command, "ok": ok,
                 "seconds": round(time.time() - t0, 1)}, indent=1))
            print(f"{cfg.name}: {d.tool}{' (' + d.module + ')' if d.module else ''} at {head[:12]} -> {d.path}: "
                  + ("ok" if ok else "FAILED\n" + tail) + f" [{time.time() - t0:.0f} s]")
            status |= 0 if ok else 1
    return status


def _cmd_model(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config, args.data_root)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    repo = Path(args.repo) if args.repo else Path(args.workdir) / cfg.name
    out = Path(args.out) if args.out else Path("results") / cfg.name / "model"
    try:
        res = build_model(cfg, repo, out, include_tests=not args.no_tests, classpath=args.classpath or "",
                          memory=args.memory, history_commits=args.history, ingest_dir=args.ingest,
                          keep_raw=not args.drop_raw)
    except (ExtractorError, RuntimeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    m = res.manifest
    c = m["counts"]
    print(f"\n{m['repo']} @ {m['commit']}  ->  {res.out}")
    print(f"  files: {c['analysed_files']} analysed of {c['java_files']} (excluded: {c['excluded_files']})")
    print(f"  types: {c['types']}, members: {c['members']}, dependencies: {c['dependencies']}, packages: {c['packages']}")
    print(f"  build: {m['build_system']}, {c['build_modules']} modules, {c['module_dependencies']} module dependencies")
    print(f"  API types: {c['api_types']}, wiring references: {c['wiring_refs_in_repo']} {m['wiring_by_kind']}")
    ex = m["extractor"]
    print(f"  extractor: {ex.get('seconds')} s, compiler errors {ex.get('compiler_errors')}, "
          f"unresolved type uses {ex.get('unresolved_type_uses')}")
    if m.get("history"):
        h = m["history"]
        print(f"  history: {h['commits_used']} commits, {h['cochange_pairs']} co-change pairs")
    for tool, cv in m.get("coverage", {}).items():
        if isinstance(cv, dict) and "fraction" in cv:
            print(f"  {tool} classes found: {cv['found']}/{cv['classes']} ({cv['fraction']:.1%}); "
                  f"in files not at this commit: {cv['file_not_at_commit']}, other: {cv['not_found_other']}")
    for w in m["warnings"]:
        print(f"  warning: {w}")
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    from .triage import triage
    status = 0
    for d in args.results:
        d = Path(d)
        model = Path(args.model) if args.model else d / "model"
        if not (d / "smells.csv").exists() or not (model / "model.json").exists():
            print(f"ERROR {d}: needs smells.csv (dsarp ingest) and {model}/model.json (dsarp model)", file=sys.stderr)
            status = 1
            continue
        res = triage(d, model, Path(args.out) if args.out else d / "triage", repo=d.name)
        m = res.manifest
        print(f"\n{d.name}: {m['smells']} smells -> {m['decisions']}; {m['groups']} groups, {m['quick_fixes']} quick fixes")
        for r in m["by_reason"]:
            if r["decision"] != "keep":
                print(f"  {r['decision']:11s} {r['tool']:9s} {r['n']:4d}  {r['reason']}")
        print("  top groups:")
        for g in m["top_groups"][:args.top]:
            print(f"    {g['rank']:3d}. {g['group_id']}  {g['type']}:{g['level']}  size {g['size']}  "
                  f"priority {g['priority']:.3f}  ({g['tools']})")
    return status


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from .calibrate import calibrate
    for d in args.results:
        d = Path(d)
        model = ProgramModel(Path(args.model) if args.model else d / "model")
        smells = pd.read_csv(d / "smells.csv", low_memory=False)
        report = calibrate(model, smells)
        (d / "calibration.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\n{d.name}:  recall / precision of in-memory detection vs. each detector "
              f"(published rules | pipeline thresholds)")
        for tool, rep in report.items():
            for key, a in rep["agreement"].items():
                b = rep["agreement_pipeline_thresholds"].get(key, {})
                print(f"  {tool:9s} {key:30s} {a.get('recall')} / {a.get('precision')}  |  "
                      f"{b.get('recall')} / {b.get('precision')}")
            print(f"  {tool:9s} best thresholds: {rep['best']}")
    return 0


def _cmd_learn(args: argparse.Namespace) -> int:
    import json

    import pandas as pd

    from .learn import affinity as af
    from .learn import moves as mv
    from .plan.facts import RepoFacts
    from .program import ProgramModel
    frames = []
    for d in map(Path, args.results):
        model_dir = d / "model"
        if not (model_dir / "model.json").exists():
            print(f"ERROR {d}: no program model (run dsarp model)", file=sys.stderr)
            return 1
        meta = json.loads((model_dir / "model.json").read_text())
        out = d / "learn"
        out.mkdir(parents=True, exist_ok=True)
        moves_file = out / "moves.csv"
        repo = Path(args.workdir) / meta["repo"]
        if moves_file.exists() and not args.remine:
            moves = pd.read_csv(moves_file, keep_default_na=False)
        elif (repo / ".git").exists():
            moves = mv.mine(repo, meta.get("commit") or "HEAD", args.max_commits)
            moves.to_csv(moves_file, index=False)
        else:
            print(f"  {d.name}: no checkout at {repo}; learning from the current placement only")
            moves = None
        rf = RepoFacts.build(ProgramModel(model_dir))
        df = af.build_dataset(rf, moves, repo=meta["repo"], max_classes=args.max_classes)
        df.to_csv(out / "dataset.csv", index=False)
        frames.append(df)
        model = af.fit(df)
        model.evaluation = af.evaluate(df)
        model.save(out / "affinity.json")
        ev = model.evaluation
        print(f"{d.name}: {0 if moves is None else len(moves)} moves in history, {df['query'].nunique()} examples; "
              f"top-1 learned {ev['learned']['top1']} vs fixed weights {ev['fixed_weights']['top1']} "
              f"(moves: {ev['learned'].get('move_top1')} vs {ev['fixed_weights'].get('move_top1')})")
        print(f"  written: {out / 'affinity.json'}")
    if len(frames) > 1:
        allf = pd.concat(frames, ignore_index=True)
        pooled = af.fit(allf)
        pooled.evaluation = {"leave_one_repository_out": af.evaluate_leave_one_repo_out(allf)}
        out = Path(args.pooled_out)
        pooled.save(out)
        for repo, ev in pooled.evaluation["leave_one_repository_out"].items():
            print(f"  unseen {repo}: top-1 learned {ev['learned']['top1']} vs fixed {ev['fixed_weights']['top1']}")
        print(f"  pooled model: {out}")
    return 0


def _llm(name: str | None):
    from .search.llm import from_env
    return from_env(name)


def _cmd_explain(args: argparse.Namespace) -> int:
    import json

    from .explain import explain, write
    from .plan.facts import RepoFacts
    from .program import ProgramModel
    for d in map(Path, args.results):
        plans = d / "plans" / "plans.json"
        if not plans.exists():
            print(f"ERROR {d}: no plans (run dsarp plan)", file=sys.stderr)
            return 1
        result = json.loads(plans.read_text())
        rf = RepoFacts.build(ProgramModel(d / "model"))
        rep = explain(result, rf, _llm(args.llm))
        print(f"{d.name}: written {write(rep, d / 'plans')}")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    from .review import review_plans
    from .search.llm import LLMError, from_env
    try:
        llm = from_env("openai", temperature=0.0)
    except LLMError as e:
        print(f"ERROR: {e}. Set DSARP_LLM_URL (e.g. https://ai-gateway.uni-paderborn.de/v1), DSARP_LLM_MODEL "
              "and DSARP_LLM_KEY (export it in the shell; it is never stored).", file=sys.stderr)
        return 1
    for d in map(Path, args.results):
        if not (d / "plans" / "plans.json").exists():
            print(f"ERROR {d}: no plans (run dsarp plan)", file=sys.stderr)
            return 1
        st = review_plans(d, llm, groups=args.groups, apply=not args.dry_run)
        print(f"{d.name}: reviewed {st['reviewed']} plans with {st['model']} ({st['llm_calls']} new calls): "
              f"{st['accept']} accepted, {st['caution']} with caution, {st['reject']} rejected; "
              f"{st['replaced']} replaced by another plan, {st['dropped']} without a plan, {st['failed']} unanswered")
        if not args.dry_run:
            from .explain import explain, write
            from .plan.facts import RepoFacts
            from .program import ProgramModel
            import json
            result = json.loads((d / "plans" / "plans.json").read_text())
            rf = RepoFacts.build(ProgramModel(d / "model"), result.get("api_policy", "balanced"))
            print(f"  report updated: {write(explain(result, rf, None), d / 'plans')}")
    return 0


def _cmd_advise(args: argparse.Namespace) -> int:
    import json

    from .advice import advise_groups
    from .search.llm import LLMError, from_env
    try:
        llm = from_env("openai", temperature=0.2)
    except LLMError as e:
        print(f"ERROR: {e}. Set DSARP_LLM_URL, DSARP_LLM_MODEL and DSARP_LLM_KEY.", file=sys.stderr)
        return 1
    for d in map(Path, args.results):
        st = advise_groups(d, llm, groups=args.groups)
        print(f"{d.name}: {st['groups']} groups without a verified plan; advice for {st['advised']}, "
              f"{st['rejected']} discarded, {st['failed']} unanswered ({st['llm_calls']} new calls)")
        from .explain import explain, write
        from .plan.facts import RepoFacts
        from .program import ProgramModel
        result = json.loads((d / "plans" / "plans.json").read_text())
        rf = RepoFacts.build(ProgramModel(d / "model"), result.get("api_policy", "balanced"))
        print(f"  report updated: {write(explain(result, rf, None), d / 'plans')}")
    return 0


def _cmd_llm_models(args: argparse.Namespace) -> int:
    from .search.llm import LLMError, list_models
    try:
        for m in list_models():
            print(m)
    except LLMError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    import json

    from .verify.run import Verifier
    for d in map(Path, args.results):
        if not (d / "plans" / "plans.json").exists():
            print(f"ERROR {d}: no plans (run dsarp plan)", file=sys.stderr)
            return 1
        meta = json.loads((d / "model" / "model.json").read_text())
        repo = Path(args.repo) if args.repo else Path(args.workdir) / meta["repo"]
        if not repo.exists():
            print(f"ERROR {d}: no checkout at {repo} (run dsarp checkout)", file=sys.stderr)
            return 1
        cmds = _detector_cmds(args)
        print(f"\n{d.name}:")
        v = Verifier(d, repo, memory=args.memory, classpath=args.classpath, build_tool=args.build,
                     run_tests=not args.no_tests, max_compiles=args.max_compiles, detector_cmds=cmds)
        v.run(args.groups, set(args.group) if args.group else None)
        print(f"  written: {d / 'verify'}")
    return 0


def _detector_cmds(args: argparse.Namespace) -> dict[str, str]:
    """Detector commands: --arcan-cmd/--designite-cmd, else $DSARP_ARCAN_CMD/$DSARP_DESIGNITE_CMD, else the
    'command' entries of the configuration given with --config (whole-repository runs only)."""
    import os
    cmds = {}
    for tool in ("arcan", "designite"):
        c = getattr(args, f"{tool}_cmd", None) or os.environ.get(f"DSARP_{tool.upper()}_CMD")
        if c:
            cmds[tool] = c
    if getattr(args, "config", None):
        cfg = load_config(args.config, getattr(args, "data_root", None))
        for d in cfg.detectors:
            if d.command and not d.module and d.tool not in cmds:
                cmds[d.tool] = d.command
    return cmds


def _cmd_combine(args: argparse.Namespace) -> int:
    import json

    from .verify.combine import Combiner
    from .verify.run import Verifier
    for d in map(Path, args.results):
        if not (d / "verify" / "verification.json").exists():
            print(f"ERROR {d}: no verification (run dsarp verify)", file=sys.stderr)
            return 1
        meta = json.loads((d / "model" / "model.json").read_text())
        repo = Path(args.repo) if args.repo else Path(args.workdir) / meta["repo"]
        print(f"\n{d.name}:")
        v = Verifier(d, repo, memory=args.memory, classpath=args.classpath)
        r = Combiner(v).run(args.max_compiles, _detector_cmds(args), args.build)
        if "whole_repository" in r:
            b, a = r["whole_repository"]["before"], r["whole_repository"]["after"]
            print(f"  {len(r['plans_applied'])} plans applied together ({r['operations']} operations), compiles: "
                  f"{'yes' if r['compiles'] else 'no'}; {r['removed']} smells removed on the combined code")
            for k in sorted(set(b) | set(a)):
                print(f"    {k}: {b.get(k, 0)} -> {a.get(k, 0)}")
        print(f"  written: {d / 'combined'}")
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    import json

    from .evaluate.history import compare
    for d in map(Path, args.results):
        meta = json.loads((d / "model" / "model.json").read_text())
        repo = Path(args.repo) if args.repo else Path(args.workdir) / meta["repo"]
        print(f"\n{d.name}:")
        compare(d, repo, args.until, args.refminer)
        print(f"  written: {d / 'evaluation' / 'history.csv'}")
    return 0


def _cmd_rating(args: argparse.Namespace) -> int:
    import json

    from .evaluate.rating import agreement, sheet
    if args.action == "sheet":
        for d in map(Path, args.paths):
            print(f"{d.name}: written {sheet(d, args.groups, args.seed)} (fill in 'verdict': sensible, "
                  "questionable or wrong)")
        return 0
    res = agreement(args.paths, args.llm_review)
    print(json.dumps(res, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1))
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    import json

    from .evaluate.harness import Harness, sensitivity, write
    for d in map(Path, args.results):
        affinity = args.affinity
        if affinity is None and (d / "learn" / "affinity.json").exists():
            affinity = str(d / "learn" / "affinity.json")
        if affinity == "none":
            affinity = None
        print(f"\n{d.name}:")
        h = Harness(d, backend=args.backend, time_limit=args.time_limit, search_time=args.search_time,
                    budget=args.budget, llm=_llm(args.llm), affinity=affinity, ablation=args.ablation, seed=args.seed)
        df = h.run(args.groups)
        extra = {}
        if (d / "plans" / "plans.json").exists():
            extra["weight_sensitivity"] = sensitivity(json.loads((d / "plans" / "plans.json").read_text()))
        paths = write(df, d, extra)
        from .evaluate.harness import summarise
        print(summarise(df).to_string(index=False))
        print("  written: " + ", ".join(str(p) for p in paths.values()))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .web.server import main as serve_main
    return serve_main(args.rest)


def _cmd_report(args: argparse.Namespace) -> int:
    from .report import write
    for d in map(Path, args.results):
        print(f"{d.name}: written {write(d)}")
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    from .plan.run import Planner, write
    status = 0
    for d in args.results:
        d = Path(d)
        model = Path(args.model) if args.model else d / "model"
        triage_dir = d / "triage"
        if not (model / "model.json").exists() or not (triage_dir / "groups.csv").exists():
            print(f"ERROR {d}: needs {model}/model.json (dsarp model) and {triage_dir}/groups.csv (dsarp triage)",
                  file=sys.stderr)
            status = 1
            continue
        print(f"\n{d.name}:")
        affinity = args.affinity
        if affinity is None and (d / "learn" / "affinity.json").exists():
            affinity = d / "learn" / "affinity.json"
        if affinity == "none":
            affinity = None
        llm = _llm(args.llm)
        planner = Planner(model, triage_dir, backend=args.backend, time_limit=args.time_limit,
                          affinity=affinity, search=args.search, search_time=args.search_time, llm=llm,
                          api_policy=args.api_policy)
        if affinity:
            print(f"  learned affinity: {affinity}")
        types = set(args.types.split(",")) if args.types else None
        result = planner.run(args.groups, types)
        result["repo"] = d.name
        paths = write(result, Path(args.out) if args.out else d / "plans")
        print("  written: " + ", ".join(str(p) for p in paths.values()))
        if args.explain:
            from .explain import explain
            from .explain import write as write_report
            print("  written: " + str(write_report(explain(result, planner.rf, llm), paths["plans"].parent)))
    return status


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dsarp", description="Architectural smell refactoring planner")
    p.add_argument("--version", action="version", version=f"dsarp {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="validate repo config files and check that detector data exists")
    c.add_argument("configs", nargs="+")
    c.add_argument("--data-root", help="folder that detector paths are relative to (default: $DSARP_DATA)")
    c.set_defaults(func=_cmd_check)

    i = sub.add_parser("ingest", help="stages 0-1: import detector output and merge it")
    i.add_argument("configs", nargs="+")
    i.add_argument("--data-root", help="folder that detector paths are relative to (default: $DSARP_DATA)")
    i.add_argument("--out", help="output folder (one sub-folder per repo when several configs are given)")
    i.set_defaults(func=_cmd_ingest)

    co = sub.add_parser("checkout", help="clone repositories and check out the configured commits")
    co.add_argument("configs", nargs="+")
    co.add_argument("--workdir", default="work", help="folder for the clones (default: work)")
    co.add_argument("--shallow", action="store_true", help="fetch only the commit (no co-change history)")
    co.add_argument("--data-root", help=argparse.SUPPRESS)
    co.set_defaults(func=_cmd_checkout)

    mo = sub.add_parser("model", help="stage 2: build the program model of a checked-out repository")
    mo.add_argument("config")
    mo.add_argument("--repo", help="checkout folder (default: <workdir>/<name>)")
    mo.add_argument("--workdir", default="work")
    mo.add_argument("--out", help="output folder (default: results/<name>/model)")
    mo.add_argument("--ingest", help="stage 1 output folder, to report coverage of detector classes")
    mo.add_argument("--classpath", help="jars of external libraries (optional, improves type resolution)")
    mo.add_argument("--memory", default="4g", help="Java heap for the extractor (default: 4g)")
    mo.add_argument("--history", type=int, default=3000, help="commits of history for co-change (0 = off)")
    mo.add_argument("--no-tests", action="store_true", help="leave test code out")
    mo.add_argument("--drop-raw", action="store_true", help="delete the extractor's raw TSV output")
    mo.add_argument("--data-root", help=argparse.SUPPRESS)
    mo.set_defaults(func=_cmd_model)

    tr = sub.add_parser("triage", help="stage 3: drop false smells, group, prioritise, set scope")
    tr.add_argument("results", nargs="+", help="result folders with smells.csv and model/")
    tr.add_argument("--model", help="program model folder (default: <results>/model)")
    tr.add_argument("--out", help="output folder (default: <results>/triage)")
    tr.add_argument("--top", type=int, default=10, help="number of top groups to print")
    tr.set_defaults(func=_cmd_triage)

    ca = sub.add_parser("calibrate", help="agreement of in-memory detection with the detectors, threshold search")
    ca.add_argument("results", nargs="+")
    ca.add_argument("--model", help="program model folder (default: <results>/model)")
    ca.set_defaults(func=_cmd_calibrate)

    pl = sub.add_parser("plan", help="stages 5a-6: generate, check, simulate and rank refactoring plans")
    pl.add_argument("results", nargs="+", help="result folders with model/ and triage/")
    pl.add_argument("--model", help="program model folder (default: <results>/model)")
    pl.add_argument("--out", help="output folder (default: <results>/plans)")
    pl.add_argument("--groups", type=int, default=10, help="number of top supported groups to plan (default: 10)")
    pl.add_argument("--types", help="comma-separated smell types (default: all supported): cyclic_dependency, "
                    "unstable_dependency, hub_like_dependency, god_component, feature_concentration, "
                    "scattered_functionality; class-level ones as cyclic_dependency:class, hub_like_dependency:class")
    pl.add_argument("--time-limit", type=float, default=60.0, help="solver seconds per plan (default: 60)")
    pl.add_argument("--backend", default="auto", choices=["auto", "cpsat", "highs"],
                    help="solver (auto: CP-SAT if installed, else HiGHS)")
    pl.add_argument("--affinity", help="learned affinity model (default: <results>/learn/affinity.json if it "
                                       "exists; 'none' for the fixed weights)")
    pl.add_argument("--search", default="auto", choices=["auto", "always", "never"],
                    help="tree search after the solver (auto: when no good plan was found)")
    pl.add_argument("--search-time", type=float, default=120.0, help="seconds of tree search per group")
    pl.add_argument("--llm", choices=["none", "openai"], help="language model client (default: $DSARP_LLM or none)")
    pl.add_argument("--api-policy", default="balanced", choices=["balanced", "strict", "off"],
                    help="public API that plans must not move (balanced: widely used public classes; strict: also "
                         "all public classes used from other build modules; off: only protected_api)")
    pl.add_argument("--explain", action="store_true", help="also write plans/report.md")
    pl.set_defaults(func=_cmd_plan)

    le = sub.add_parser("learn", help="stage 4: learn where classes belong (current code and history moves)")
    le.add_argument("results", nargs="+", help="result folders with model/")
    le.add_argument("--workdir", default="work", help="folder with the checkouts (for the history)")
    le.add_argument("--max-commits", type=int, default=20000)
    le.add_argument("--max-classes", type=int, default=3000, help="classes sampled per repository")
    le.add_argument("--remine", action="store_true", help="mine the history again even if moves.csv exists")
    le.add_argument("--pooled-out", default="results/pooled/affinity.json",
                    help="pooled model when several repositories are given")
    le.set_defaults(func=_cmd_learn)

    ex = sub.add_parser("explain", help="stage 7: step-by-step report of the recommended plans")
    ex.add_argument("results", nargs="+")
    ex.add_argument("--llm", choices=["none", "openai"], help="language model for the summary (default: none)")
    ex.set_defaults(func=_cmd_explain)

    rv = sub.add_parser("review", help="stage 6b (optional): design review of the recommended plans by a "
                                       "language model (OpenAI-compatible endpoint, e.g. the Paderborn AI gateway)")
    rv.add_argument("results", nargs="+")
    rv.add_argument("--groups", type=int, help="only the first N groups with a plan")
    rv.add_argument("--dry-run", action="store_true", help="only record the review, do not change the plans")
    rv.set_defaults(func=_cmd_review)

    ad = sub.add_parser("advise", help="stage 7b (optional): design advice by a language model for groups without "
                                      "a verified plan (marked as not verified; never counted)")
    ad.add_argument("results", nargs="+")
    ad.add_argument("--groups", type=int, help="only the first N such groups")
    ad.set_defaults(func=_cmd_advise)

    lm = sub.add_parser("llm-models", help="list the models of the language-model endpoint ($DSARP_LLM_URL)")
    lm.set_defaults(func=_cmd_llm_models)

    dt = sub.add_parser("detect", help="stage 0: run the detectors that have a 'command' in the configuration on "
                                      "the checkout at the configured commit (their output then matches the code)")
    dt.add_argument("configs", nargs="+")
    dt.add_argument("--workdir", default="work", help="folder with the checkouts")
    dt.add_argument("--data-root", default=None)
    dt.add_argument("--tool", action="append", choices=["arcan", "designite"], help="only this tool (repeatable)")
    dt.add_argument("--timeout", type=int, default=7200, help="seconds per detector run")
    dt.set_defaults(func=_cmd_detect)

    hi = sub.add_parser("history", help="evaluation: compare the recommended steps with what the developers did "
                                       "in the commits after the analysed one")
    hi.add_argument("results", nargs="+")
    hi.add_argument("--workdir", default="work")
    hi.add_argument("--repo", help="checkout folder with the full history (default: <workdir>/<name>)")
    hi.add_argument("--until", default="origin/HEAD", help="later revision to compare with (default: origin/HEAD)")
    hi.add_argument("--refminer", help="RefactoringMiner command with {repo} {start} {end} {json}, e.g. "
                                      "'RefactoringMiner -bc {repo} {start} {end} -json {json}' (method moves, "
                                      "extracted interfaces)")
    hi.set_defaults(func=_cmd_history)

    ra = sub.add_parser("rating", help="evaluation: expert rating sheet of the recommendations, and agreement")
    ra.add_argument("action", choices=["sheet", "agreement"])
    ra.add_argument("paths", nargs="+", help="sheet: result folders; agreement: filled rating sheets (CSV)")
    ra.add_argument("--groups", type=int, default=40, help="sheet: groups in the sample (stratified by smell type)")
    ra.add_argument("--seed", type=int, default=0)
    ra.add_argument("--llm-review", help="agreement: evaluation/rating_llm_review.csv (compare raters with it)")
    ra.add_argument("--out", help="agreement: write the result as JSON")
    ra.set_defaults(func=_cmd_rating)

    co = sub.add_parser("combine", help="stage 8b: one combined plan (non-conflicting verified plans applied "
                                        "together, compiled, whole repository before/after)")
    co.add_argument("results", nargs="+")
    co.add_argument("--workdir", default="work", help="folder with the checkouts")
    co.add_argument("--repo", help="checkout folder (default: <workdir>/<name>)")
    co.add_argument("--max-compiles", type=int, default=12, help="compiler runs for dropping plans that break the build")
    co.add_argument("--memory", default="4g")
    co.add_argument("--classpath", default="")
    co.add_argument("--build", default="none", choices=["none", "auto", "maven", "gradle"],
                    help="also build the affected modules and run the tests that use a changed class")
    co.add_argument("--config", help="repository configuration (its detector 'command' entries are run on the "
                                     "original and the combined code)")
    co.add_argument("--data-root", default=None)
    co.add_argument("--arcan-cmd", help="command to run Arcan ({repo}, {out})")
    co.add_argument("--designite-cmd", help="command to run Designite ({repo}, {out})")
    co.set_defaults(func=_cmd_combine)

    ve = sub.add_parser("verify", help="stage 8: apply the recommended plans to the code, compile, detect again")
    ve.add_argument("results", nargs="+")
    ve.add_argument("--workdir", default="work", help="folder with the checkouts")
    ve.add_argument("--repo", help="checkout folder (default: <workdir>/<name>)")
    ve.add_argument("--groups", type=int, help="verify only the first N groups with a plan")
    ve.add_argument("--group", action="append", help="verify this group id (repeatable)")
    ve.add_argument("--build", default="none", choices=["none", "auto", "maven", "gradle"],
                    help="also build the affected modules and run the selected tests (offline)")
    ve.add_argument("--no-tests", action="store_true", help="with --build: compile only")
    ve.add_argument("--max-compiles", type=int, default=30, help="compiler runs for delta debugging per plan")
    ve.add_argument("--memory", default="4g")
    ve.add_argument("--classpath", default="", help="jars of the external libraries (fewer baseline errors)")
    ve.add_argument("--arcan-cmd", help="command to run Arcan on the result ({repo}, {out})")
    ve.add_argument("--designite-cmd", help="command to run Designite on the result ({repo}, {out})")
    ve.add_argument("--config", help="repository configuration: run its detector 'command' entries on each result")
    ve.add_argument("--data-root", default=None)
    ve.set_defaults(func=_cmd_verify)

    ev = sub.add_parser("evaluate", help="stage 9: compare solver, search, hybrid, NSGA-II, random (and LLM)")
    ev.add_argument("results", nargs="+")
    ev.add_argument("--groups", type=int, default=10)
    ev.add_argument("--time-limit", type=float, default=60.0)
    ev.add_argument("--search-time", type=float, default=120.0)
    ev.add_argument("--budget", type=int, default=600, help="minimum evaluations for NSGA-II and random search")
    ev.add_argument("--backend", default="auto", choices=["auto", "cpsat", "highs"])
    ev.add_argument("--affinity")
    ev.add_argument("--llm", choices=["none", "openai"])
    ev.add_argument("--ablation", action="store_true", help="also run the hybrid with the fixed affinity weights")
    ev.add_argument("--seed", type=int, default=0)
    ev.set_defaults(func=_cmd_evaluate)

    rp = sub.add_parser("report", help="stage 9: self-contained HTML report (triage, plans, verification)")
    rp.add_argument("results", nargs="+")
    rp.set_defaults(func=_cmd_report)

    w = sub.add_parser("serve", help="web interface: run stages, follow them on a flowchart, browse results",
                       add_help=False)
    w.add_argument("rest", nargs=argparse.REMAINDER)
    w.set_defaults(func=_cmd_serve)

    s = sub.add_parser("summary", help="print the summary of ingested results")
    s.add_argument("results", nargs="+")
    s.add_argument("--detail", action="store_true", help="also print counts per tool, type and level")
    s.set_defaults(func=_cmd_summary)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:          # the web server has its own options (and needs only the standard library)
        from .web.server import main as serve_main
        return serve_main(argv[1:])
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
