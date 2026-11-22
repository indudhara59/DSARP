"""Public API a plan breaks for code outside the repository.

A library's users compile against its public types and members by fully
qualified name and signature. A plan breaks such a user when it

* moves a public type (``move_class``): the type and every public or
  protected member of it (and of its nested types) get a new name;
* moves or deletes a public member (``move_member``, ``delete_member``);
* makes clients use an extracted interface instead of the provider
  (``extract_interface``): public members of the clients whose signature
  names the provider change their signature.

Counting types and members (rather than classes) follows the way API
compatibility checkers such as japicmp report changes, so the numbers can be
compared with theirs. Adding an interface, a class or a member is not a break.

With compatibility stubs (``compat="stubs"``), a moved public class or
interface that a deprecated stub at the old place can stand in for is not
counted: existing code keeps compiling for a deprecation period. Final
classes, enums, records, annotations and classes without a constructor a
subclass can call cannot be stubbed and still count.
"""

from __future__ import annotations

from collections import defaultdict


def _flag(v) -> bool:
    return str(v).strip().lower() in ("true", "1")


class ApiIndex:
    """Public types and members of a program model, for counting API breaks."""

    def __init__(self, model):
        t = model.types
        self.type_api = dict(zip(t["type"], (_flag(x) for x in t["api"])))
        self.nested: dict[str, list[str]] = defaultdict(list)
        for ty, top in zip(t["type"], t["top_level"]):
            self.nested[top].append(ty)
        m = model.members
        self.member_api = dict(zip(m["member"], (_flag(x) for x in m["api"])))
        self.members_of: dict[str, list[str]] = defaultdict(list)
        for mem, owner, api in zip(m["member"], m["owner"], m["api"]):
            if _flag(api):
                self.members_of[owner].append(mem)
        self.kind = dict(zip(t["type"], t["kind"]))
        self.final = dict(zip(t["type"], (_flag(x) for x in t["final"])))
        self.top = dict(zip(t["type"], t["top_level"]))
        self.ctors: dict[str, list[str]] = defaultdict(list)
        c = m[m["kind"] == "constructor"]
        for owner, vis in zip(c["owner"], c["visibility"]):
            self.ctors[owner].append(str(vis))

    def stubbable(self, cls: str) -> bool:
        """A public top-level class or interface that a deprecated subtype at the old place can stand in for."""
        if not self.type_api.get(cls) or self.top.get(cls) != cls or self.final.get(cls, True):
            return False
        if self.kind.get(cls) == "interface":
            return True
        if self.kind.get(cls) != "class":
            return False
        vis = self.ctors.get(cls, [])
        return not vis or any(v in ("public", "protected") for v in vis)

    def breaks(self, ops: list[dict], compat: str = "none", stubbed: set[str] | None = None) -> dict:
        """API breaks of the operations. ``compat="stubs"``: moved types that can be stubbed do not count
        (``stubbed``: the types actually stubbed, when known from the verification)."""
        types, members, covered = set(), set(), set()
        for o in ops:
            k = o.get("op")
            if k == "move_class" and compat == "stubs" and (
                    o["class"] in stubbed if stubbed is not None else self.stubbable(o["class"])):
                covered.add(o["class"])
                continue
            if k == "move_class":
                for ty in self.nested.get(o["class"], [o["class"]]):
                    if self.type_api.get(ty):
                        types.add(ty)
                        members.update(self.members_of.get(ty, []))
            elif k in ("move_member", "delete_member"):
                for mem in o.get("members") or [o.get("member")]:
                    if mem and self.member_api.get(mem):
                        members.add(mem)
            elif k == "extract_interface":
                prov = o.get("provider", "")
                for client in o.get("clients") or [o.get("client")]:
                    for ty in self.nested.get(client, [client]):
                        for mem in self.members_of.get(ty, []):
                            sig = mem.split("#", 1)[-1]
                            if "(" in sig and prov and prov in sig.split("(", 1)[1]:
                                members.add(mem)
        return {"count": len(types) + len(members), "types": len(types), "members": len(members),
                "covered_by_stubs": len(covered), "examples": sorted(types)[:3] + sorted(members)[:3]}


def api_index(model) -> ApiIndex:
    idx = model.__dict__.get("_api_index")
    if idx is None:
        idx = ApiIndex(model)
        try:
            model.__dict__["_api_index"] = idx
        except (AttributeError, TypeError):
            pass
    return idx


def api_breaks(model, ops: list[dict], compat: str = "none", stubbed: set[str] | None = None) -> dict:
    """Public types and members whose name or signature the operations change (see the module docstring)."""
    return api_index(model).breaks(ops, compat, stubbed)
