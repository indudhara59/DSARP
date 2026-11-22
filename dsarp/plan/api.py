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

    def breaks(self, ops: list[dict]) -> dict:
        types, members = set(), set()
        for o in ops:
            k = o.get("op")
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
                "examples": sorted(types)[:3] + sorted(members)[:3]}


def api_breaks(model, ops: list[dict]) -> dict:
    """Public types and members whose name or signature the operations change (see the module docstring)."""
    idx = model.__dict__.get("_api_index")
    if idx is None:
        idx = ApiIndex(model)
        try:
            model.__dict__["_api_index"] = idx
        except (AttributeError, TypeError):
            pass
    return idx.breaks(ops)
