"""Check L2: Java preconditions of a plan and the follow-up edits it needs.

The plan model only knows class-level dependencies. Before a plan is shown,
every operation is checked against the member-level facts of the program
model, and the concrete edits a developer (or OpenRewrite in stage 8) has to
make are listed:

* **Move Class**: package-private or protected members (and package-private
  classes) that are used across the new package boundary must become
  ``public``; classes in the old package that used the moved class without an
  import now need one; framework wiring (``META-INF/services``, Spring, XML,
  OSGi activators) that names the class must be updated; public API changes
  need a deprecation path.
* **Delete member**: tests that call it are listed; public API members get a
  warning (a release note or a major version is needed).
* **Extract Interface**: the interface, its methods, the provider that must
  implement it and the client types that change.

Status per operation: ``ok``, ``warning`` (possible, but needs extra edits or
care) or ``blocked`` (the operation would break the build as planned).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from .facts import RepoFacts
from .simulate import interface_name

NON_PUBLIC = {"package", "protected", ""}


@dataclass
class OpCheck:
    index: int
    status: str = "ok"
    edits: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def warn(self, note: str) -> None:
        if self.status == "ok":
            self.status = "warning"
        self.notes.append(note)

    def block(self, note: str) -> None:
        self.status = "blocked"
        self.notes.append(note)


class Checker:
    def __init__(self, rf: RepoFacts, max_listed: int = 25):
        self.rf, self.max_listed = rf, max_listed
        d = rf.model.deps
        top = rf.top
        d = d.assign(st=d["source_type"].map(top), tt=d["target_type"].map(top))
        d = d[d["st"].notna() & d["tt"].notna() & (d["st"] != d["tt"])]
        self.by_target: dict[str, pd.DataFrame] = {k: g for k, g in d.groupby("tt")}
        self.by_source: dict[str, pd.DataFrame] = {k: g for k, g in d.groupby("st")}
        t = rf.model.types
        self.test_types = set(t.loc[t["is_test"].astype(bool), "top_level"])
        self.files = dict(zip(t["type"], t["file"]))
        self.type_pkg = dict(zip(t["type"], t["package"]))
        self.type_vis = dict(zip(t["type"], t["visibility"]))
        w = rf.model.wiring
        self.wiring: dict[str, list[dict]] = defaultdict(list)
        if len(w):
            for r in w.itertuples(index=False):
                owner = top.get(str(r[2]), str(r[2]))
                self.wiring[owner].append({"file": r[0], "kind": r[1], "key": r[3]})

    # --------------------------------------------------------------- helpers

    def _vis(self, element: str) -> str:
        if "#" in element:
            info = self.rf.member_info.get(element)
            return str(info["visibility"]) if info else "public"
        return str(self.type_vis.get(element, "public"))

    def _limit(self, items) -> list:
        items = sorted(set(items))
        return items[: self.max_listed] + ([f"... {len(items) - self.max_listed} more"]
                                           if len(items) > self.max_listed else [])

    # --------------------------------------------------------------- operations

    def check(self, ops: list[dict], package_after: dict[str, str]) -> list[OpCheck]:
        out = []
        for i, o in enumerate(ops):
            fn = getattr(self, "_" + o["op"], None)
            c = OpCheck(i)
            if fn is None:
                c.warn(f"no checks for operation {o['op']}")
            else:
                fn(o, c, package_after)
            out.append(c)
        return out

    def _pkg(self, c: str, after: dict[str, str]) -> str:
        return after.get(c, self.rf.package.get(c, c.rsplit(".", 1)[0]))

    def _move_class(self, o: dict, chk: OpCheck, after: dict[str, str]) -> None:
        rf, c = self.rf, o["class"]
        new_pkg, old_pkg = o["to_package"], o["from_package"]
        new = bool(o.get("new_package"))
        if rf.protected.get(c):
            chk.block("class is protected by the configuration")
        if c[len(old_pkg) + 1:] in rf.simple_names.get(new_pkg, set()):
            chk.block(f"a class with the same name already exists in {new_pkg}")
        if not new and rf.module.get(c) not in rf.pkg_modules.get(new_pkg, set()):
            chk.block(f"{new_pkg} is in another build module")
        if new:
            if new_pkg in rf.pkg_classes:
                chk.block(f"{new_pkg} already exists")
            other_modules = any(rf.module.get(u) != rf.module.get(c) for u in rf.dependents(c))
            exported = rf.is_exported(old_pkg)
            chk.edits.append({"edit": "create_package", "package": new_pkg, "module": rf.module.get(c, "")})
            if other_modules and exported:
                chk.warn(f"other modules use the class: export the new package {new_pkg} "
                         "(OSGi Export-Package or module-info) like its parent package")
        elif not rf.visible_after_move(c, new_pkg):
            chk.block(f"{new_pkg} is not exported, but other modules use the class (OSGi/module visibility)")
        # used by others: package-private elements of c used from another package
        widen = set()
        importers, same_pkg_users, test_users = set(), set(), set()
        g = self.by_target.get(c)
        if g is not None:
            for src_t, target in zip(g["st"], g["target"]):
                src_pkg = self._pkg(src_t, after) if src_t in rf.prod else self.type_pkg.get(src_t, "")
                if src_t in self.test_types:
                    test_users.add(src_t)
                if src_pkg != new_pkg:
                    importers.add(src_t)
                    if self._vis(target) in NON_PUBLIC and target != c:
                        widen.add(target)
                    if self._vis(c) in NON_PUBLIC:
                        widen.add(c)
                if src_pkg == old_pkg:
                    same_pkg_users.add(src_t)
        # uses others: package-private elements in the old package used by c
        h = self.by_source.get(c)
        uses_widen = set()
        if h is not None:
            for tgt_t, target in zip(h["tt"], h["target"]):
                if self._pkg(tgt_t, after) == old_pkg and self._vis(target) in NON_PUBLIC:
                    uses_widen.add(target)
        if widen:
            chk.warn(f"{len(widen)} non-public elements of the class are used from other packages")
            chk.edits.append({"edit": "make_public", "elements": self._limit(widen)})
        if uses_widen:
            chk.warn(f"the class uses {len(uses_widen)} non-public elements of {old_pkg}")
            chk.edits.append({"edit": "make_public", "elements": self._limit(uses_widen)})
        chk.edits.append({"edit": "change_package_declaration", "file": self.files.get(c, ""),
                          "new_file": self.files.get(c, "").replace(old_pkg.replace(".", "/"),
                                                                    new_pkg.replace(".", "/"), 1),
                          "package": new_pkg})
        if importers:
            chk.edits.append({"edit": "update_imports", "count": len(importers), "classes": self._limit(importers)})
        if same_pkg_users:
            chk.edits.append({"edit": "add_import", "count": len(same_pkg_users),
                              "classes": self._limit(same_pkg_users)})
        moved_tests = set(o.get("tests", []))
        test_users -= moved_tests
        if moved_tests:
            chk.edits.append({"edit": "move_tests", "count": len(moved_tests), "classes": self._limit(moved_tests),
                              "package": new_pkg})
            moving = {c} | moved_tests | {x for x, p in after.items() if p == new_pkg}
            tw = set()
            for t in moved_tests:
                h2 = self.by_source.get(t)
                if h2 is None:
                    continue
                for tgt_t, target in zip(h2["tt"], h2["target"]):
                    if tgt_t in moving or self.type_pkg.get(tgt_t) != old_pkg or self._pkg(tgt_t, after) != old_pkg:
                        continue
                    if self._vis(target) in NON_PUBLIC:
                        tw.add(target)
            if tw:
                chk.warn(f"the moved tests use {len(tw)} non-public elements of {old_pkg}")
                chk.edits.append({"edit": "make_public", "elements": self._limit(tw)})
        if test_users:
            chk.edits.append({"edit": "update_tests", "count": len(test_users), "classes": self._limit(test_users)})
        wiring = self.wiring.get(c, [])
        if wiring:
            chk.warn(f"named in {len(wiring)} framework wiring entries")
            chk.edits.append({"edit": "update_wiring", "entries": wiring[: self.max_listed]})
        if rf.api.get(c):
            chk.warn("public API: keep a deprecated subclass or delegate in the old package for one release")

    def _delete_member(self, o: dict, chk: OpCheck, after: dict[str, str]) -> None:
        mid = o["member"]
        info = self.rf.member_info.get(mid)
        if info is None:
            chk.block("member not found in the program model")
            return
        if info["callers"] - info["test_callers"] > 0:
            chk.block("member still has production callers")
        g = self.by_target.get(self.rf.top.get(info["owner"], info["owner"]))
        tests = set()
        if g is not None:
            tests = {s for s, t in zip(g["st"], g["target"]) if t == mid and s in self.test_types}
        if tests:
            chk.warn(f"called by {len(tests)} test classes")
            chk.edits.append({"edit": "update_tests", "count": len(tests), "classes": self._limit(tests)})
        if info["api"]:
            chk.warn("public API member: remove in a major release and mention it in the release notes")
        chk.edits.append({"edit": "delete", "member": mid, "file": self.files.get(info["owner"], "")})

    def _remove_unused_import(self, o: dict, chk: OpCheck, after: dict[str, str]) -> None:
        chk.edits.append({"edit": "remove_import", "file": self.files.get(o["class"], ""), "import": o["import"]})

    def _extract_interface(self, o: dict, chk: OpCheck, after: dict[str, str]) -> None:
        iface = interface_name(o)
        if iface.rsplit(".", 1)[-1] in self.rf.simple_names.get(o["interface_package"], set()):
            chk.block(f"{iface} already exists")
        chk.edits.append({"edit": "create_interface", "interface": iface, "package": o["interface_package"],
                          "methods": o["methods"]})
        chk.edits.append({"edit": "implement_interface", "class": o["provider"], "interface": iface,
                          "file": self.files.get(o["provider"], "")})
        chk.edits.append({"edit": "replace_type_uses", "class": o["client"], "from": o["provider"], "to": iface,
                          "file": self.files.get(o["client"], "")})
        if self.rf.api.get(o["client"]):
            chk.warn("client is public API: changing parameter or return types is an API change")


def summarise(checks: list[OpCheck]) -> dict:
    status = "ok"
    if any(c.status == "blocked" for c in checks):
        status = "blocked"
    elif any(c.status == "warning" for c in checks):
        status = "warning"
    edits = sum(len(c.edits) for c in checks)
    widen = sum(len([x for x in e.get("elements", []) if not str(x).startswith("...")])
                for c in checks for e in c.edits if e["edit"] == "make_public")
    files = sum(e.get("count", 0) for c in checks for e in c.edits if "count" in e)
    return {"status": status, "blocked": sum(c.status == "blocked" for c in checks),
            "warnings": sum(c.status == "warning" for c in checks), "edits": edits,
            "made_public": widen, "files_touched": files}
