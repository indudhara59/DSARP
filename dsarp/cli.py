"""Command-line interface.

Examples::

    dsarp check configs/tika.yaml
    dsarp ingest configs/tika.yaml --data-root /path/to/data --out results/tika
    dsarp ingest configs/*.yaml --data-root /path/to/data --out results
    dsarp summary results/tika
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from . import __version__
from .config import ConfigError, load_config
from .ingest import ingest, write


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

    s = sub.add_parser("summary", help="print the summary of ingested results")
    s.add_argument("results", nargs="+")
    s.add_argument("--detail", action="store_true", help="also print counts per tool, type and level")
    s.set_defaults(func=_cmd_summary)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
