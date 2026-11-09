"""Apply a plan to a working copy of the source code.

The built-in engine edits the Java sources directly, using the positions
recorded in the program model:

* **move_class**: move the file to the new package folder, change the
  ``package`` line, rewrite imports and fully qualified names in all Java files
  and in framework wiring files (``META-INF/services``, Spring, XML), add
  imports where classes of the old package used the class without one, and
  add imports to the moved class for the classes of its old package it uses;
* **make public** (from the checks): widen the visibility of the members and
  classes listed by the checks;
* **delete_member**: remove the declaration with its annotations and Javadoc;
* **remove_unused_import**: remove the import line;
* **extract_interface**: create the interface with the signatures of the
  used methods, let the provider implement it and change the client to use it.

Edits are made in an order that keeps the recorded line numbers valid:
line-based edits first (bottom-up per file), then moves, then name rewrites.
The result is checked by compiling (:mod:`.compile`); operations that break
the build are found and dropped by :mod:`.debug`.

An OpenRewrite recipe (``org.openrewrite.java.ChangeType``) for the moves can
be written as well, for teams that prefer to apply the moves with their build
tool (:func:`openrewrite_recipe`).
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..plan.simulate import interface_name
from ..program import ProgramModel

TEXT_SUFFIXES = {".xml", ".properties", ".factories", ".imports", ".yaml", ".yml", ".json", ""}


@dataclass
class ApplyResult:
    changed: set[str] = field(default_factory=set)          # repository-relative paths (after the plan)
    moved: dict[str, str] = field(default_factory=dict)     # old file -> new file
    created: list[str] = field(default_factory=list)
    created_types: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Rewriter:
    def __init__(self, model: ProgramModel):
        self.model = model
        t = model.types
        self.type_file = dict(zip(t["type"], t["file"]))
        self.type_line = dict(zip(t["type"], pd.to_numeric(t["line"], errors="coerce")))
        self.type_pkg = dict(zip(t["type"], t["package"]))
        self.top = dict(zip(t["type"], t["top_level"]))
        m = model.members
        self.member_file = {mid: self.type_file.get(o, "") for mid, o in zip(m["member"], m["owner"])}
        self.member_lines = dict(zip(m["member"], zip(pd.to_numeric(m["line"], errors="coerce"),
                                                       pd.to_numeric(m["end_line"], errors="coerce"))))
        self.member_name = dict(zip(m["member"], m["name"]))
        self.member_kind = dict(zip(m["member"], m["kind"]))
        f = model.files
        f = f[~f["excluded"].astype(str).str.lower().isin(["true", "1"])]
        self.java_files = list(f["file"])
        self.file_root = dict(zip(f["file"], f["source_root"]))
        self.file_pkg = dict(zip(f["file"], f["package"]))
        w = model.wiring
        self.wiring_files = sorted(set(w["file"])) if len(w) else []
        d = model.deps
        ext = d[d["kind"].isin(["extends", "implements"])]
        self.subtypes: dict[str, set[str]] = defaultdict(set)
        for a, b in zip(ext["source_type"], ext["target_type"]):
            if a != b:
                self.subtypes[b].add(a)

    def overrides(self, member: str) -> list[str]:
        """Members that override ``member`` in subtypes (they must be widened together)."""
        owner, sig = member.split("#", 1)
        if sig.startswith("<init>") or "(" not in sig:
            return []
        out, todo, seen = [], [owner], {owner}
        while todo:
            t = todo.pop()
            for sub in self.subtypes.get(t, ()):
                if sub in seen:
                    continue
                seen.add(sub)
                todo.append(sub)
                mid = f"{sub}#{sig}"
                if mid in self.member_lines:
                    out.append(mid)
        return out

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _read(path: Path) -> list[str]:
        return path.read_text(encoding="utf-8", errors="replace").split("\n")

    @staticmethod
    def _write(path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")

    def new_file(self, cls: str, new_pkg: str) -> str:
        old = self.type_file[cls]
        root = self.file_root.get(old) or posixpath.dirname(old)[: -len(self.type_pkg[cls].replace(".", "/"))].rstrip("/")
        return posixpath.join(root, new_pkg.replace(".", "/"), posixpath.basename(old)) if root else \
            posixpath.join(new_pkg.replace(".", "/"), posixpath.basename(old))

    # ------------------------------------------------------------------ line edits

    def _decl_line(self, lines: list[str], start: int, name: str, kind: str) -> int | None:
        """Index of the line that declares ``name`` at or after ``start`` (0-based)."""
        if kind in ("class", "interface", "enum", "record", "annotation", "type"):
            pat = re.compile(r"\b(class|interface|enum|record|@interface)\s+" + re.escape(name) + r"\b")
        elif kind == "constructor":
            pat = re.compile(r"\b" + re.escape(name) + r"\s*\(")
        elif kind == "field":
            pat = re.compile(r"\b" + re.escape(name) + r"\s*(=|;|,)")
        else:
            pat = re.compile(r"\b" + re.escape(name) + r"\s*\(")
        for i in range(max(start, 0), min(start + 8, len(lines))):
            if pat.search(lines[i]):
                return i
        return None

    @staticmethod
    def _make_public(line: str) -> str:
        if re.search(r"\bpublic\b", line):
            return line
        if re.search(r"\b(private|protected)\b", line):
            return re.sub(r"\b(private|protected)\b", "public", line, count=1)
        m = re.match(r"^(\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*)", line)
        head = m.group(1) if m else ""
        return head + "public " + line[len(head):]

    def widen(self, element: str, edits: dict[str, list]) -> None:
        if "#" in element:
            owner = element.split("#")[0]
            name = self.member_name.get(element, "")
            kind = self.member_kind.get(element, "method")
            if name == "<init>":
                name, kind = owner.rsplit(".", 1)[-1].split("$")[-1], "constructor"
            line = self.member_lines.get(element, (None, None))[0]
            file = self.member_file.get(element)
        else:
            owner, name, kind = element, element.rsplit(".", 1)[-1].split("$")[-1], "type"
            line, file = self.type_line.get(element), self.type_file.get(element)
        if file and line == line and line is not None and name and name != "<clinit>":
            if ("public", int(line) - 1, name, kind) not in edits[file]:
                edits[file].append(("public", int(line) - 1, name, kind))
        if "#" in element and not getattr(self, "_widening", False):
            self._widening = True
            try:
                for o in self.overrides(element):
                    self.widen(o, edits)
            finally:
                self._widening = False

    def delete(self, member: str, edits: dict[str, list]) -> None:
        file = self.member_file.get(member)
        start, end = self.member_lines.get(member, (None, None))
        if file and start == start and end == end and start is not None:
            edits[file].append(("delete", int(start) - 1, int(end) - 1, ""))

    def _apply_line_edits(self, repo: Path, file: str, ops: list[tuple], res: ApplyResult) -> None:
        path = repo / file
        if not path.exists():
            res.notes.append(f"file not found: {file}")
            return
        lines = self._read(path)
        for op in sorted(ops, key=lambda o: -o[1]):          # bottom-up keeps earlier line numbers valid
            if op[0] == "delete":
                s, e = op[1], op[2]
                while s > 0 and (lines[s - 1].strip().startswith(("*", "/**", "@")) or lines[s - 1].strip() == "*/"):
                    s -= 1
                del lines[s: e + 1]
            elif op[0] == "public":
                i = self._decl_line(lines, op[1], op[2], op[3])
                if i is None:
                    res.notes.append(f"declaration of {op[2]} not found in {file}")
                    continue
                lines[i] = self._make_public(lines[i])
            elif op[0] == "remove_import":
                lines = [ln for ln in lines if not re.match(r"\s*import\s+" + re.escape(op[2]) + r"\s*;", ln)]
        if any(op[0] == "delete" for op in ops):
            lines = _drop_unused_imports(lines)       # the deleted code may have been the only user
        self._write(path, lines)
        res.changed.add(file)

    # ------------------------------------------------------------------ interface

    def _signature(self, member: str, repo: Path) -> str | None:
        file = self.member_file.get(member)
        start = self.member_lines.get(member, (None, None))[0]
        if not file or start is None or start != start:
            return None
        lines = self._read(repo / file)
        name = self.member_name.get(member, "")
        i = self._decl_line(lines, int(start) - 1, name, "method")
        if i is None:
            return None
        text = ""
        for ln in lines[i: i + 10]:
            text += " " + ln.strip()
            if "{" in ln or ";" in ln:
                break
        text = text.split("{")[0].split(";")[0]
        text = re.sub(r"@\w+(\([^)]*\))?", "", text)
        text = re.sub(r"\b(public|protected|private|final|synchronized|native|abstract|default|static)\b", "", text)
        return re.sub(r"\s+", " ", text).strip() + ";"

    def _extract_interface(self, op: dict, repo: Path, res: ApplyResult, clients: list[str] | None = None) -> None:
        iface = interface_name(op)
        pkg, simple = iface.rsplit(".", 1)
        client_file = self.type_file[op["client"]]
        provider_file = self.type_file[op["provider"]]
        sigs = [s for s in (self._signature(m, repo) for m in op.get("methods", [])) if s]
        root = self.file_root.get(client_file, "")
        path = posixpath.join(root, pkg.replace(".", "/"), simple + ".java")
        # imports: those of the provider (unused ones are harmless) and classes of the provider's
        # package named in the signatures (these keep a dependency; re-detection will show it)
        ptext = (repo / provider_file).read_text(encoding="utf-8", errors="replace")
        ppkg = op["provider"].rsplit(".", 1)[0]
        same_pkg = {t.rsplit(".", 1)[-1] for t, q in self.type_pkg.items() if q == ppkg and self.top.get(t) == t}
        used = set(re.findall(r"\b[A-Z]\w*", " ".join(sigs)))
        imports = []
        for ln in ptext.split("\n"):
            m = re.match(r"\s*import\s+(?!static)([\w.]+?)(\.\*)?\s*;", ln)
            if not m:
                continue
            if m.group(2):                                  # wildcard: keep, it may provide a used name
                imports.append(ln.strip())
            elif m.group(1).rsplit(".", 1)[-1] in used:     # only types the signatures name
                imports.append(ln.strip())
        imports += [f"import {ppkg}.{n};" for n in sorted(used & same_pkg) if ppkg != pkg]
        imports = [i for i in dict.fromkeys(imports) if not i.startswith(f"import {pkg}.{simple};")]
        body = [f"package {pkg};", ""] + imports + ([""] if imports else []) + [
                f"/** Operations of {op['provider'].rsplit('.', 1)[-1]} used by "
                f"{op['client'].rsplit('.', 1)[-1]} (extracted to invert the dependency). */",
                f"public interface {simple} {{"] + [f"    {s}" for s in sigs] + ["}", ""]
        self._write(repo / path, body)
        res.created.append(path)
        res.created_types.append(iface)
        # provider implements the interface
        p = repo / provider_file
        lines = self._read(p)
        pname = op["provider"].rsplit(".", 1)[-1]
        i = self._decl_line(lines, int(self.type_line.get(op["provider"], 1)) - 1, pname, "type")
        if i is not None:
            j = i
            while j < len(lines) and "{" not in lines[j]:
                j += 1
            if j < len(lines):
                head = " ".join(lines[i: j + 1])
                if re.search(r"\bimplements\b", head):
                    k = next(x for x in range(i, j + 1) if "implements" in lines[x])
                    lines[k] = re.sub(r"\bimplements\b", f"implements {iface},", lines[k], count=1)
                else:
                    lines[j] = lines[j].replace("{", f"implements {iface} {{", 1)
            self._write(p, lines)
            res.changed.add(provider_file)
        # clients use the interface instead of the provider
        for client in clients or [op["client"]]:
            self._use_interface(repo, self.type_file[client], client, op, pname, simple, res)

    def _use_interface(self, repo: Path, client_file: str, client: str, op: dict, pname: str, simple: str,
                       res: ApplyResult) -> None:
        c = repo / client_file
        text = c.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"(?<![\w.])(?<!new )" + re.escape(pname) + r"\b(?!\s*\.)", simple, text)
        code = _strip_comments_and_strings(text)
        if not re.search(r"(?<![\w.])" + re.escape(pname) + r"\b", code):      # no static uses left
            text = re.sub(r"^[ \t]*import\s+" + re.escape(op["provider"]) + r"[ \t]*;[ \t]*\n", "", text, flags=re.M)
        else:
            res.notes.append(f"{client} still uses {op['provider']} statically; import kept")
        c.write_text(text, encoding="utf-8")
        res.changed.add(client_file)

    # ------------------------------------------------------------------ apply

    def apply(self, repo: str | Path, ops: list[dict], checks: list[dict] | None = None) -> ApplyResult:
        repo = Path(repo)
        res = ApplyResult()
        edits: dict[str, list] = defaultdict(list)
        checks = checks or [o.get("check", {}) for o in ops]
        for op, chk in zip(ops, checks):
            for e in (chk or {}).get("edits", []):
                if e["edit"] == "make_public":
                    for el in e.get("elements", []):
                        if not str(el).startswith("..."):
                            self.widen(el, edits)
            if op["op"] == "delete_member":
                self.delete(op["member"], edits)
            elif op["op"] == "remove_unused_import":
                f = self.type_file.get(op["class"])
                if f:
                    edits[f].append(("remove_import", 10 ** 9, op["import"], ""))
        # moved classes used from other packages must be public
        for op in ops:
            if op["op"] == "move_class" and self._type_visibility(op["class"]) != "public":
                self.widen(op["class"], edits)
        for file, fe in edits.items():
            self._apply_line_edits(repo, file, fe, res)
        # one interface per provider and client package: merge the methods of all its clients
        merged: dict[str, dict] = {}
        clients: dict[str, list[str]] = defaultdict(list)
        for op in ops:
            if op["op"] == "extract_interface":
                k = interface_name(op)
                if k in merged:
                    merged[k]["methods"] = sorted(set(merged[k]["methods"]) | set(op.get("methods", [])))
                else:
                    merged[k] = {**op, "methods": list(op.get("methods", []))}
                clients[k].append(op["client"])
        for k, op in merged.items():
            self._extract_interface(op, repo, res, clients[k])
        moves = [o for o in ops if o["op"] == "move_class"]
        if moves:
            self._apply_moves(repo, moves, res)
        return res

    def _type_visibility(self, cls: str) -> str:
        t = self.model.types
        v = t.loc[t["type"] == cls, "visibility"]
        return str(v.iloc[0]) if len(v) else "public"

    def _apply_moves(self, repo: Path, moves: list[dict], res: ApplyResult) -> None:
        rename = {o["class"]: f"{o['to_package']}.{o['class'].rsplit('.', 1)[-1]}" for o in moves}
        old_pkg = {o["class"]: o["from_package"] for o in moves}
        # 1. move files and change the package line
        current = {f: f for f in self.java_files}
        for o in moves:
            src = self.type_file[o["class"]]
            dst = self.new_file(o["class"], o["to_package"])
            sp, dp = repo / src, repo / dst
            if not sp.exists():
                res.notes.append(f"file not found: {src}")
                continue
            text = sp.read_text(encoding="utf-8", errors="replace")
            text = re.sub(r"^(\s*package\s+)" + re.escape(o["from_package"]) + r"(\s*;)",
                          r"\g<1>" + o["to_package"] + r"\g<2>", text, count=1, flags=re.M)
            dp.parent.mkdir(parents=True, exist_ok=True)
            dp.write_text(text, encoding="utf-8")
            sp.unlink()
            res.moved[src] = dst
            current[src] = dst
            res.changed.add(dst)
        file_pkg = {current[f]: p for f, p in self.file_pkg.items() if f in current}
        back = {v: k for k, v in current.items()}
        for o in moves:
            file_pkg[current.get(self.type_file[o["class"]], "")] = o["to_package"]
        moved_simple = {c: c.rsplit(".", 1)[-1] for c in rename}
        remaining = defaultdict(set)          # old package -> simple names of classes that stay
        for t, p in self.type_pkg.items():
            if self.top.get(t) == t and t not in rename:
                remaining[p].add(t.rsplit(".", 1)[-1])
        # 2. rewrite every Java file
        fq = sorted(rename, key=len, reverse=True)
        fq_re = re.compile(r"(?<![\w.])(" + "|".join(re.escape(c) for c in fq) + r")(?![\w])")
        for f in set(current.values()) | set(res.created):
            path = repo / f
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            new = fq_re.sub(lambda m: rename[m.group(1)], text)
            pkg = file_pkg.get(f, "")
            orig_pkg = self.file_pkg.get(back.get(f, f), pkg)
            body = _strip_comments_and_strings(new)
            add = []
            for c, simple in moved_simple.items():
                wildcard = re.search(r"^\s*import\s+" + re.escape(old_pkg[c]) + r"\.\*\s*;", new, re.M)
                if (old_pkg[c] in (pkg, orig_pkg) or wildcard) and rename[c].rsplit(".", 1)[0] != pkg and \
                        re.search(r"(?<![\w.$])" + re.escape(simple) + r"\b", body) and not _imports(new, rename[c]) \
                        and not _imports_simple(new, simple):
                    add.append(rename[c])
            for c, simple in moved_simple.items():
                if file_pkg.get(current.get(self.type_file[c], ""), "") == pkg and f == current.get(self.type_file[c]):
                    for other in sorted(remaining.get(old_pkg[c], ())):
                        if re.search(r"(?<![\w.$])" + re.escape(other) + r"\b", body) and pkg != old_pkg[c] \
                                and not _imports_simple(new, other):
                            name = f"{old_pkg[c]}.{other}"
                            if not _imports(new, name):
                                add.append(name)
            if add:
                new = _add_imports(new, sorted(set(add)))
            if new != text:
                path.write_text(new, encoding="utf-8")
                res.changed.add(f)
        # 3. wiring files (service registrations, Spring, XML)
        for wf in self.wiring_files:
            p = repo / wf
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            new = fq_re.sub(lambda m: rename[m.group(1)], text)
            if new != text:
                p.write_text(new, encoding="utf-8")
                res.changed.add(wf)
            base = p.name
            if "/META-INF/services/" in "/" + wf and base in rename:     # service interface file name
                q = p.with_name(rename[base])
                p.rename(q)
                res.moved[wf] = str(Path(wf).with_name(rename[base]))
        res.notes += [f"moved {c} -> {n}" for c, n in rename.items()]

    def files_after(self, res: ApplyResult) -> list[str]:
        out = [res.moved.get(f, f) for f in self.java_files]
        return out + [c for c in res.created if c.endswith(".java")]


def _imports(text: str, fqn: str) -> bool:
    return re.search(r"^\s*import\s+" + re.escape(fqn) + r"\s*;", text, re.M) is not None


def _drop_unused_imports(lines: list[str]) -> list[str]:
    """Remove single-type imports whose simple name is no longer used in the code."""
    code = _strip_comments_and_strings("\n".join(lines))
    out = []
    for ln in lines:
        m = re.match(r"\s*import\s+(?!static\b)[\w.]+\.(\w+)\s*;\s*$", ln)
        if m and not re.search(r"(?<![\w$])" + re.escape(m.group(1)) + r"\b", code):
            continue
        out.append(ln)
    return out


def _imports_simple(text: str, simple: str) -> bool:
    """The file already imports a type with this simple name (that import shadows same-package types)."""
    return re.search(r"^\s*import\s+(?!static\b)[\w.]+\." + re.escape(simple) + r"\s*;", text, re.M) is not None


def _add_imports(text: str, names: list[str]) -> str:
    lines = text.split("\n")
    idx = None
    for i, ln in enumerate(lines):
        if re.match(r"\s*import\s", ln):
            idx = i
    if idx is None:
        for i, ln in enumerate(lines):
            if re.match(r"\s*package\s", ln):
                idx = i
                lines.insert(i + 1, "")
                idx = i + 1
                break
    ins = [f"import {n};" for n in names]
    if idx is None:
        return "\n".join(ins + lines)
    lines[idx + 1: idx + 1] = ins
    return "\n".join(lines)


def _strip_comments_and_strings(text: str) -> str:
    """Code without comments, string/char literals, and package/import lines (one pass, text blocks too)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            i = n if j == -1 else j + 3
            out.append('""')
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            out.append(c + c)
        else:
            out.append(c)
            i += 1
    return re.sub(r"^\s*(package|import)\s[^;]*;", " ", "".join(out), flags=re.M)


def openrewrite_recipe(ops: list[dict], name: str = "dsarp.Plan") -> str:
    """OpenRewrite recipe (rewrite.yml) that applies the class moves of a plan."""
    lines = ["---", "type: specs.openrewrite.org/v1beta/recipe", f"name: {name}",
             "displayName: DSARP refactoring plan (class moves)", "recipeList:"]
    for o in ops:
        if o["op"] == "move_class":
            new = f"{o['to_package']}.{o['class'].rsplit('.', 1)[-1]}"
            lines += ["  - org.openrewrite.java.ChangeType:",
                      f"      oldFullyQualifiedTypeName: {o['class']}",
                      f"      newFullyQualifiedTypeName: {new}"]
    return "\n".join(lines) + "\n"
