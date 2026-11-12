"""Evaluation against history: did the developers later make the changes DSARP recommends?

For a repository analysed at commit ``C``, the commits after ``C`` (up to a
later revision, by default the remote default branch) show what the
developers actually did. For every recommended step:

* **Move class** ``X`` to ``P``: ``same`` if ``X`` was later moved to ``P``;
  ``other`` if it was moved to another package (the target is reported);
  ``new package`` if the step creates a package and ``X`` later moved to a
  package that did not exist at ``C`` (the developers also split);
  ``deleted`` if its file was later removed; ``unchanged`` otherwise.
* **Move method / member**, **extract interface**: matched against the
  refactorings RefactoringMiner (Tsantalis et al., 2018/2020) reports for the
  later commits, if a RefactoringMiner command is given (``--refminer``);
  otherwise ``not checked``.

Class moves are found with git's rename detection (as for the learned
affinity, :mod:`dsarp.learn.moves`). The share of ``same`` steps is a lower
bound of precision: developers may not have got to a refactoring yet, so an
``unchanged`` step is not wrong; ``other`` and the RefactoringMiner matches
show where developers disagreed or agreed.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections import Counter
from pathlib import Path

import pandas as pd

from ..learn.moves import parse


def later_moves(repo: str | Path, commit: str, until: str = "origin/HEAD", max_commits: int = 50000) -> pd.DataFrame:
    """Class moves (package changes) in the commits after ``commit`` up to ``until``, oldest first."""
    out = subprocess.run(["git", "-C", str(repo), "log", f"{commit}..{until}", "--no-merges", "-M",
                          "--diff-filter=R", "--name-status", f"-n{max_commits}", "--format=@@%H|%ct"],
                         check=True, capture_output=True, text=True, errors="replace").stdout
    return parse(out).sort_values("time").reset_index(drop=True)


def later_deleted(repo: str | Path, commit: str, until: str = "origin/HEAD") -> set[str]:
    """Java files of ``commit`` that no longer exist at ``until`` (and were not just renamed)."""
    out = subprocess.run(["git", "-C", str(repo), "diff", "--name-status", "-M", commit, until, "--", "*.java"],
                         capture_output=True, text=True, errors="replace").stdout
    return {ln.split("\t")[1] for ln in out.splitlines() if ln.startswith("D\t")}


def commits_between(repo: str | Path, commit: str, until: str) -> int:
    out = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", f"{commit}..{until}"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def refminer(command: str, repo: str | Path, commit: str, until: str, out: Path) -> list[dict]:
    """Refactorings found by RefactoringMiner between two revisions.

    ``command`` is how RefactoringMiner is started, with ``{repo}``, ``{start}``, ``{end}`` and ``{json}``
    placeholders, e.g. ``RefactoringMiner -bc {repo} {start} {end} -json {json}``."""
    end = subprocess.run(["git", "-C", str(repo), "rev-parse", until], capture_output=True, text=True).stdout.strip()
    jf = out / "refactoringminer.json"
    cmd = command.format(repo=str(repo), start=commit, end=end, json=str(jf))
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    (out / "refactoringminer.log").write_text(r.stdout + r.stderr)
    if r.returncode != 0 or not jf.exists():
        raise RuntimeError("RefactoringMiner failed: " + "\n".join((r.stdout + r.stderr).splitlines()[-5:]))
    data = json.loads(jf.read_text())
    refs = []
    for c in data.get("commits", []):
        for ref in c.get("refactorings", []):
            refs.append({"commit": c.get("sha1"), "type": ref.get("type"), "description": ref.get("description", "")})
    return refs


def _member_name(mid: str) -> str:
    return mid.split("#", 1)[-1].split("(")[0]


def compare(results: str | Path, repo: str | Path, until: str = "origin/HEAD", refminer_cmd: str | None = None,
            log=print) -> dict:
    r = Path(results)
    plans = json.loads((r / "plans" / "plans.json").read_text())
    meta = json.loads((r / "model" / "model.json").read_text())
    commit = meta.get("commit")
    vf = r / "verify" / "verification.json"
    verified = {v["group_id"]: v for v in json.loads(vf.read_text())} if vf.exists() else {}
    types = pd.read_csv(r / "model" / "types.csv", keep_default_na=False, usecols=["type", "file", "package"])
    file_of = dict(zip(types["type"], types["file"]))
    pkgs_then = set(types["package"])
    moves = later_moves(repo, commit, until)
    deleted = later_deleted(repo, commit, until)
    n_commits = commits_between(repo, commit, until)
    by_class = {}
    for row in moves.itertuples(index=False):          # oldest first: follow a class through several moves
        start = next((k for k, v in by_class.items() if v[-1] == row.old_class), row.old_class)
        by_class.setdefault(start, [row.old_class]).append(row.new_class)
    out_dir = r / "evaluation"
    out_dir.mkdir(parents=True, exist_ok=True)
    refs = []
    if refminer_cmd:
        refs = refminer(refminer_cmd, repo, commit, until, out_dir)
    rows = []
    for g in plans["groups"]:
        name = (verified.get(g["group_id"]) or {}).get("plan") or g.get("recommended")
        plan = next((p for p in g["plans"] if p["name"] == name), None)
        if plan is None:
            continue
        kept = None
        ops_file = r / "verify" / g["group_id"] / "operations.json"
        if ops_file.exists():
            kept = {json.dumps(o, sort_keys=True, default=str) for o in json.loads(ops_file.read_text()).get("kept", [])}
        for i, o in enumerate(plan["operations"], 1):
            op = {k: v for k, v in o.items() if k != "check"}
            if kept is not None and json.dumps(op, sort_keys=True, default=str) not in kept:
                continue
            row = {"group_id": g["group_id"], "smell": g.get("kind") or g["type"], "step": i, "op": op["op"],
                   "element": op.get("class") or op.get("member") or op.get("client") or "",
                   "target": op.get("to_package") or op.get("to_class") or op.get("provider") or "",
                   "result": "not checked", "later": ""}
            if op["op"] == "move_class":
                c = op["class"]
                chain = by_class.get(c, [])
                later_pkgs = [x.rsplit(".", 1)[0] for x in chain[1:]]
                if op["to_package"] in later_pkgs:
                    row["result"] = "same"
                elif later_pkgs and op.get("new_package") and any(p not in pkgs_then for p in later_pkgs):
                    row["result"] = "new package"
                elif later_pkgs:
                    row["result"] = "other"
                elif file_of.get(c) in deleted:
                    row["result"] = "deleted"
                else:
                    row["result"] = "unchanged"
                row["later"] = " -> ".join(later_pkgs)
            elif refs:
                name_ = _member_name(row["element"]) if "#" in row["element"] else row["element"].rsplit(".", 1)[-1]
                want = {"move_member": ("Move Method", "Move Attribute", "Move And Rename Method",
                                        "Move And Inline Method", "Move Static Method"),
                        "extract_interface": ("Extract Interface", "Extract Superclass"),
                        "delete_member": ("Remove Method", "Remove Attribute"),
                        "remove_unused_import": ()}.get(op["op"], ())
                hit = [x for x in refs if x["type"] in want and name_ in x["description"]
                       and (not row["target"] or row["target"].rsplit(".", 1)[-1] in x["description"])]
                row["result"] = "same" if hit else "unchanged"
                row["later"] = hit[0]["description"][:200] if hit else ""
            rows.append(row)
    df = pd.DataFrame(rows, columns=["group_id", "smell", "step", "op", "element", "target", "result", "later"])
    df.to_csv(out_dir / "history.csv", index=False)
    counts = Counter(df["result"]) if not df.empty else Counter()
    checked = len(df) - counts.get("not checked", 0)
    summary = {"commit": commit, "until": until, "later_commits": n_commits, "later_class_moves": len(moves),
               "steps": len(df), "checked": checked, "results": dict(counts),
               "same_share": round(counts.get("same", 0) / checked, 4) if checked else None,
               "touched_share": round((counts.get("same", 0) + counts.get("new package", 0) + counts.get("other", 0)
                                       + counts.get("deleted", 0)) / checked, 4) if checked else None,
               "by_op": {op: dict(Counter(d["result"])) for op, d in df.groupby("op")} if not df.empty else {},
               "refactoringminer": bool(refminer_cmd), "refactorings_found": len(refs)}
    (out_dir / "history.json").write_text(json.dumps(summary, indent=1))
    log(f"{n_commits} later commits, {len(moves)} later class moves; {checked} of {len(df)} steps checked: "
        + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    return summary
