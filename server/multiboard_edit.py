"""Edit Altium multi-board schematics (.MbsDoc) on the saved file.

Altium's multi-board editor has no script interface, so the server edits the
document's JSON (see multiboard.py for the format) while Altium does not hold
the document, and Altium loads the result. Every operation keeps the
document's own invariants:

- Ids: logical objects (logical.json) and drawing items (Pages/*.json) are
  two id spaces; Altium takes max + 1 of each when it loads, so new objects
  get ids above the current maxima.
- Drawing: a harness is a connection group (logicalConnectionId) of
  connection points and branches. A branch (__drawingConnection without
  connectionItemType) joins two points; its path is a chain of visual
  segments (connectionItemType 1) through visual points, all children of the
  branch. Lines are orthogonal between the points' connect positions: an
  entry point connects one grid step along its direction, junction and
  visual points at their position.
- Cached texts (entry designators, titles) live in __drawingLinkedProperty
  children and are updated with their logical value.

Operations (all resolve objects by designator):
  swap_entry_parts  swap the linked components of two entries of one block;
                    the harnesses stay where they are drawn
  join_harnesses    make an open harness entry an inline connector mated with
                    a new entry of another harness, drawn two grid steps away
                    and joined to that harness's line by a new branch
  set_title         change a block's title
"""

from __future__ import annotations

import copy
import itertools
import json
import zipfile
from pathlib import Path
from typing import Any

TOL = 1e-6
# Alignment tolerance for lines, in document units (grid 9.6)
ALIGN = 0.5


class MbsEditError(ValueError):
    """An operation that cannot be applied; nothing is written."""


def _max_id(obj: Any) -> int:
    best = 0
    if isinstance(obj, dict):
        if isinstance(obj.get("id"), int):
            best = obj["id"]
        for v in obj.values():
            best = max(best, _max_id(v))
    elif isinstance(obj, list):
        for v in obj:
            best = max(best, _max_id(v))
    return best


def _xy(p: dict[str, Any] | None) -> tuple[float, float]:
    p = p or {}
    return float(p.get("x", 0.0)), float(p.get("y", 0.0))


def _vec(x: float, y: float) -> dict[str, float]:
    """A point / vector as Altium writes it: zero components left out."""
    out: dict[str, float] = {}
    if abs(x) > TOL:
        out["x"] = x
    if abs(y) > TOL:
        out["y"] = y
    return out


def _entry_name(entry: dict[str, Any]) -> str:
    return entry.get("definedDesignator") or entry.get("calculatedDesignator", "")


class MbsDocument:
    """A loaded .MbsDoc: logical and page JSON plus the other members as bytes."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.members: list[tuple[zipfile.ZipInfo, bytes]] = []
        self.json: dict[str, Any] = {}
        with zipfile.ZipFile(self.path) as archive:
            for info in archive.infolist():
                data = archive.read(info)
                self.members.append((info, data))
                name = info.filename.replace("\\", "/")
                if name == "logical.json" or name.lower().startswith("pages/"):
                    self.json[name] = json.loads(data.decode("utf-8-sig"))
                if name == "options.json":
                    self.grid = float(
                        json.loads(data.decode("utf-8-sig")).get("gridSize", 9.6)
                    )
        if "logical.json" not in self.json:
            raise MbsEditError(f"{self.path.name} has no logical.json")
        self.grid = getattr(self, "grid", 9.6)
        self.logical = self.json["logical.json"]
        self.pages = {k: v for k, v in self.json.items() if k != "logical.json"}
        self._next_object = _max_id(self.logical) + 1
        self._next_item = max((_max_id(p) for p in self.pages.values()), default=0) + 1

    # --- ids and lookup -------------------------------------------------
    def object_id(self) -> int:
        self._next_object += 1
        return self._next_object - 1

    def item_id(self) -> int:
        self._next_item += 1
        return self._next_item - 1

    def blocks(self) -> list[dict[str, Any]]:
        return self.logical.get("modules", []) + self.logical.get("virtualModules", [])

    def block(self, designator: str) -> dict[str, Any]:
        for b in self.blocks():
            if b.get("designator") == designator:
                return b
        raise MbsEditError(f"no block {designator!r}")

    def connection(self, designator: str) -> dict[str, Any]:
        for c in self.logical.get("connections", []):
            if c.get("designator") == designator:
                return c
        raise MbsEditError(f"no connection {designator!r}")

    def entry(self, owner: dict[str, Any], designator: str) -> dict[str, Any]:
        for e in owner.get("entries", []):
            if designator in (_entry_name(e), e.get("calculatedDesignator")):
                return e
        names = [_entry_name(e) for e in owner.get("entries", [])]
        raise MbsEditError(
            f"{owner.get('designator')} has no entry {designator!r} ({', '.join(names)})"
        )

    def all_items(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """(page, item) for every drawing item."""
        return [
            (page, item)
            for page in self.pages.values()
            for item in page.get("items", [])
        ]

    def item(self, item_id: int) -> dict[str, Any]:
        for _, item in self.all_items():
            if item.get("id") == item_id:
                return item
        raise MbsEditError(f"no drawing item {item_id}")

    def items_of(self, logical_id: int, kind: str) -> list[dict[str, Any]]:
        return [
            i
            for _, i in self.all_items()
            if i.get("logicalObjectId") == logical_id and i["$type"] == kind
        ]

    def children(self, parent_id: int) -> list[dict[str, Any]]:
        return [i for _, i in self.all_items() if i.get("parentId") == parent_id]

    def page_of(self, item: dict[str, Any]) -> dict[str, Any]:
        for page in self.pages.values():
            if any(i is item for i in page.get("items", [])):
                return page
        raise MbsEditError(f"drawing item {item.get('id')} is on no page")

    def connect_position(self, point: dict[str, Any]) -> tuple[float, float]:
        x, y = _xy(point.get("position"))
        dx, dy = _xy(point.get("direction"))
        if point.get("connectionItemType") == 1 or (abs(dx) < TOL and abs(dy) < TOL):
            return x, y
        return x + dx * self.grid, y + dy * self.grid

    # --- save --------------------------------------------------------------
    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path or self.path)
        tmp = target.with_name(target.name + ".tmp")
        with zipfile.ZipFile(tmp, "w") as archive:
            for info, data in self.members:
                name = info.filename.replace("\\", "/")
                if name in self.json:
                    text = json.dumps(self.json[name], indent=2, ensure_ascii=False)
                    data = text.replace("\n", "\r\n").encode("utf-8")
                out = zipfile.ZipInfo(info.orig_filename, date_time=info.date_time)
                # ZipInfo() turns "\" into "/" on Windows; keep Altium's name
                out.filename = info.orig_filename
                out.compress_type = info.compress_type
                out.external_attr = info.external_attr
                archive.writestr(out, data)
        tmp.replace(target)
        return target


# --- operations -------------------------------------------------------------


def swap_entry_parts(
    doc: MbsDocument, block: str, entries: list[str]
) -> dict[str, Any]:
    if not isinstance(entries, list) or len(entries) != 2:
        raise MbsEditError("swap_entry_parts needs entries: [two entry designators]")
    b = doc.block(block)
    e1, e2 = (doc.entry(b, name) for name in entries)
    if e1 is e2:
        raise MbsEditError("swap_entry_parts needs two different entries")
    for c in doc.logical.get("connections", []):
        for link in c.get("entriesConnections", []) + c.get(
            "interConnectedEntriesConnections", []
        ):
            if {link.get("entry1"), link.get("entry2")} & {
                e1["id"],
                e2["id"],
            } and link.get("physicalConnections"):
                raise MbsEditError(
                    f"{block} entries are mated pin by pin to {c['designator']}; not supported"
                )
    c1, c2 = e1.get("linkedComponent"), e2.get("linkedComponent")
    comps = {c["id"]: c for c in b.get("components", [])}
    pins1 = {p["id"] for p in comps.get(c1, {}).get("pins", [])}
    pins2 = {p["id"] for p in comps.get(c2, {}).get("pins", [])}
    for key in ("linkedComponent", "calculatedDesignator", "definedDesignator"):
        v1, v2 = e1.get(key), e2.get(key)
        for e, v in ((e1, v2), (e2, v1)):
            if v is None:
                e.pop(key, None)
            else:
                e[key] = v
    for m in b.get("entryPinMap", {}).get("map", []):
        if m["key"] in pins1:
            m["value"] = e2["id"]
        elif m["key"] in pins2:
            m["value"] = e1["id"]
    labels = {}
    for e in (e1, e2):
        for item in doc.items_of(e["id"], "__drawingModuleEntry"):
            for child in doc.children(item["id"]):
                if (
                    child["$type"] == "__drawingLinkedProperty"
                    and child.get("propertyValuePath") == "Designator"
                ):
                    labels[e["id"]] = child
    for e in (e1, e2):
        if e["id"] in labels:
            labels[e["id"]]["directValue"] = e.get(
                "calculatedDesignator"
            ) or _entry_name(e)
    part = {c["id"]: _param(c, "DesignItemId") for c in b.get("components", [])}
    return {
        "op": "swap_entry_parts",
        "block": block,
        "entries": {
            entries[
                0
            ]: f"now {_entry_name(e1)} ({part.get(e1.get('linkedComponent'))})",
            entries[
                1
            ]: f"now {_entry_name(e2)} ({part.get(e2.get('linkedComponent'))})",
        },
    }


def set_title(doc: MbsDocument, block: str, title: str) -> dict[str, Any]:
    if not title:
        raise MbsEditError("set_title needs a title")
    b = doc.block(block)
    old = b.get("title", "")
    b["title"] = title
    for _, item in doc.all_items():
        if item.get("logicalObjectId") == b["id"] and item["$type"] in (
            "__drawingVirtualModule",
            "__drawingModule",
        ):
            for child in doc.children(item["id"]):
                if child.get("propertyValuePath") == "LogicalObject.Title":
                    child["directValue"] = title
    return {"op": "set_title", "block": block, "old": old, "title": title}


def _param(obj: dict[str, Any], name: str) -> str:
    return next(
        (p.get("value", "") for p in obj.get("parameters", []) if p["name"] == name), ""
    )


def _all_components(doc: MbsDocument) -> list[dict[str, Any]]:
    out = []
    for owner in doc.blocks() + doc.logical.get("connections", []):
        out.extend(owner.get("components", []))
    return out


def _inline_component(
    doc: MbsDocument,
    part: str,
    designator: str,
    pin_sources: dict[str, list[str]],
    library: tuple[str, str] | None,
) -> dict[str, Any]:
    """A new harness-side component of a part: pins copied from a component of
    the same part in the document, or numbered after a harness connector."""
    numbers: list[tuple[str, str]] = []
    template = next(
        (c for c in _all_components(doc) if _param(c, "DesignItemId") == part), None
    )
    if template:
        numbers = [
            (p.get("number") or p.get("pinId", ""), p.get("name", ""))
            for p in template["pins"]
        ]
    elif part in pin_sources:
        numbers = [(n, n) for n in pin_sources[part]]
    if not numbers:
        raise MbsEditError(
            f"no pins known for {part}: no component of it in the document and no harness connector"
        )
    if library:
        lib_name, table = library
    elif template:
        lib_name = _param(template, "SourceLibraryName")
        table = _param(template, "DatabaseTableName")
    else:
        lib_name, table = "", ""
    params = [{"name": "DesignItemId", "value": part, "id": doc.object_id()}]
    if lib_name:
        params.append(
            {"name": "SourceLibraryName", "value": lib_name, "id": doc.object_id()}
        )
    if table:
        params.append(
            {"name": "DatabaseTableName", "value": table, "id": doc.object_id()}
        )
    comp_id = doc.object_id()
    pins = []
    for number, name in numbers:
        pin_id = doc.object_id()
        pins.append(
            {
                "pinId": number,
                "name": name,
                "number": number,
                "parameters": [
                    {"name": "SourceId", "value": "", "id": doc.object_id()},
                    {"name": "ExternalNetName", "id": doc.object_id()},
                ],
                "id": pin_id,
            }
        )
    return {
        "designator": designator,
        "comment": "=PartNumber",
        "pins": pins,
        "parameters": params,
        "id": comp_id,
    }


def _branch_chain(
    doc: MbsDocument, branch: dict[str, Any]
) -> tuple[list[int], list[dict[str, Any]]]:
    """Point ids from the branch's first end to its second, and the visual
    segments between them, in order."""
    segments = [
        i for i in doc.children(branch["id"]) if i["$type"] == "__drawingConnection"
    ]
    chain = [branch["endItemId1"]]
    ordered = []
    left = list(segments)
    while chain[-1] != branch["endItemId2"]:
        seg = next(
            (s for s in left if chain[-1] in (s["endItemId1"], s["endItemId2"])), None
        )
        if seg is None:
            raise MbsEditError(f"branch {branch['id']} has a broken path")
        left.remove(seg)
        ordered.append(seg)
        chain.append(
            seg["endItemId2"] if seg["endItemId1"] == chain[-1] else seg["endItemId1"]
        )
    return chain, ordered


def _on_segment(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> bool:
    """Whether p lies inside a horizontal or vertical segment. Altium's own
    lines are off by fractions of a unit (an entry connects at 355.7 - 9.6 =
    346.1, the corner below it sits at 346.0), so alignment allows ALIGN."""
    (x, y), (ax, ay), (bx, by) = p, a, b
    if abs(ax - bx) < ALIGN and min(ax, bx) - ALIGN < x < max(ax, bx) + ALIGN:
        return min(ay, by) + ALIGN < y < max(ay, by) - ALIGN
    if abs(ay - by) < ALIGN and min(ay, by) - ALIGN < y < max(ay, by) + ALIGN:
        return min(ax, bx) + ALIGN < x < max(ax, bx) - ALIGN
    return False


def _attach_point(
    doc: MbsDocument,
    group: dict[str, Any],
    at: tuple[float, float],
    page: dict[str, Any],
) -> tuple[int, dict[str, Any] | None]:
    """The junction of a harness at a point: an existing junction there, or a
    new one splitting the line through it. Returns (point id, split info)."""
    points = {
        i["id"]: i for i in page["items"] if i["$type"] == "__drawingConnectionPoint"
    }
    for p in points.values():
        if (
            p.get("parentId") == group["id"]
            and not p.get("connectionItemType")
            and "logicalObjectId" not in p
            and "pinId" not in p
            and all(abs(a - b) < ALIGN for a, b in zip(_xy(p.get("position")), at))
        ):
            return p["id"], None
    branches = [
        i
        for i in page["items"]
        if i["$type"] == "__drawingConnection" and i.get("parentId") == group["id"]
    ]
    for branch in branches:
        chain, segments = _branch_chain(doc, branch)
        for k, seg in enumerate(segments):
            a = doc.connect_position(points[chain[k]])
            b = doc.connect_position(points[chain[k + 1]])
            if not _on_segment(at, a, b):
                continue
            junction = {
                "$type": "__drawingConnectionPoint",
                "position": _vec(*at),
                "direction": {},
                "lineStyleId": 3,
                "anchor": 5,
                "parentId": group["id"],
                "selectable": True,
                "id": doc.item_id(),
            }
            page["items"].append(junction)
            # The branch ends at the junction; a new branch takes the rest
            end2 = branch["endItemId2"]
            branch["endItemId2"] = junction["id"]
            far = chain[k + 1]
            if seg["endItemId1"] == far:
                seg["endItemId1"] = junction["id"]
            else:
                seg["endItemId2"] = junction["id"]
            new_branch = {
                "$type": "__drawingConnection",
                "endItemId1": junction["id"],
                "endItemId2": end2,
                "lineStyleId": branch.get("lineStyleId", 5),
                "anchor": branch.get("anchor", 5),
                "parentId": group["id"],
                "selectable": True,
                "id": doc.item_id(),
            }
            page["items"].append(new_branch)
            page["items"].append(
                {
                    "$type": "__drawingConnection",
                    "endItemId1": junction["id"],
                    "endItemId2": far,
                    "lineStyleId": seg.get("lineStyleId", 5),
                    "connectionItemType": 1,
                    "anchor": 5,
                    "parentId": new_branch["id"],
                    "selectable": True,
                    "id": doc.item_id(),
                }
            )
            # The rest of the path now belongs to the new branch
            for later in segments[k + 1 :]:
                later["parentId"] = new_branch["id"]
            for pid in chain[k + 1 : -1]:
                if points[pid].get("parentId") == branch["id"]:
                    points[pid]["parentId"] = new_branch["id"]
            return junction["id"], {
                "split_branch": branch["id"],
                "new_branch": new_branch["id"],
                "ends": [branch["endItemId1"], end2],
            }
    raise MbsEditError(
        f"({at[0]:g}, {at[1]:g}) is on no line or junction of {group.get('id')}; "
        "get_multiboard_data(include_geometry=True) lists the harness lines"
    )


def join_harnesses(
    doc: MbsDocument,
    entry: str,
    to: str,
    part: str,
    mate_part: str,
    attach: list[float] | dict[str, float],
    via: list | None = None,
    entry_gender: str = "male",
    pin_sources: dict[str, list[str]] | None = None,
    libraries: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    if "." not in entry:
        raise MbsEditError("entry must be '<connection>.<entry>', e.g. 'H_1.Entry_4'")
    conn_name, entry_name = entry.split(".", 1)
    c1 = doc.connection(conn_name)
    c2 = doc.connection(to)
    if c1 is c2:
        raise MbsEditError("join_harnesses needs two different harnesses")
    e1 = doc.entry(c1, entry_name)
    linked = [
        link
        for c in doc.logical.get("connections", [])
        for key in ("entriesConnections", "interConnectedEntriesConnections")
        for link in c.get(key, [])
        if e1["id"] in (link.get("entry1"), link.get("entry2"))
    ]
    if linked or e1.get("isInterConnection"):
        raise MbsEditError(
            f"{entry} already mates something; only an open harness end can be joined"
        )
    if any(m["value"] == e1["id"] for m in c1.get("entryPinMap", {}).get("map", [])):
        raise MbsEditError(f"{entry} already has pins")
    points = doc.items_of(e1["id"], "__drawingConnectionPoint")
    if len(points) != 1:
        raise MbsEditError(f"{entry} is drawn {len(points)} times")
    p1 = points[0]
    page = doc.page_of(p1)
    dx, dy = _xy(p1.get("direction"))
    if abs(dx) + abs(dy) < TOL:
        raise MbsEditError(f"{entry} has no direction")
    group = next(
        (
            i
            for i in page["items"]
            if i["$type"] == "__drawingConnectionGroup"
            and i.get("logicalConnectionId") == c2["id"]
        ),
        None,
    )
    if group is None:
        raise MbsEditError(f"{to} is not drawn on the page of {entry}")

    # Geometry: the mate sits two grid steps out, facing away from the entry
    x1, y1 = _xy(p1.get("position"))
    nx, ny = x1 - dx * 2 * doc.grid, y1 - dy * 2 * doc.grid
    start = (nx - dx * doc.grid, ny - dy * doc.grid)
    at = tuple(attach) if isinstance(attach, list) else (attach["x"], attach["y"])
    path = [
        start,
        *[tuple(v) if isinstance(v, list) else (v["x"], v["y"]) for v in via or []],
        at,
    ]
    for a, b in itertools.pairwise(path):
        if (abs(a[0] - b[0]) > ALIGN) == (abs(a[1] - b[1]) > ALIGN):
            raise MbsEditError(
                f"route ({a[0]:g}, {a[1]:g}) -> ({b[0]:g}, {b[1]:g}) is not horizontal or vertical "
                f"(the new branch starts at ({start[0]:g}, {start[1]:g}))"
            )
    junction_id, split = _attach_point(doc, group, at, page)

    # Logical: inline components, entry, mate, pin pairs
    pin_sources = pin_sources or {}
    libraries = libraries or {}
    male = entry_gender.lower() == "male"
    comp1 = _inline_component(
        doc, part, "J?" if male else "P?", pin_sources, libraries.get(part)
    )
    comp2 = _inline_component(
        doc, mate_part, "P?" if male else "J?", pin_sources, libraries.get(mate_part)
    )
    n1 = {p["number"]: p for p in comp1["pins"]}
    n2 = {p["number"]: p for p in comp2["pins"]}
    if set(n1) != set(n2):
        raise MbsEditError(
            f"{part} pins {sorted(n1)} do not match {mate_part} pins {sorted(n2)}"
        )
    taken = {_entry_name(e) for e in c2.get("entries", [])}
    k = 1
    while f"Entry_{k}" in taken:
        k += 1
    e2 = {
        "isInterConnection": True,
        "definedDesignator": f"Entry_{k}",
        "calculatedDesignator": f"Entry_{k}",
    }
    if male:
        e2["entryType"] = 1
    e2["parameters"] = [
        {
            "name": "SystemDesign_ComponentTerminationType",
            "value": "Connector",
            "id": doc.object_id(),
        }
    ]
    e2["id"] = doc.object_id()
    e1["isInterConnection"] = True
    if male:
        e1.pop("entryType", None)
    else:
        e1["entryType"] = 1
    c1.setdefault("components", []).append(comp1)
    c2.setdefault("components", []).append(comp2)
    c1.setdefault("entryPinMap", {}).setdefault("map", []).extend(
        {"key": p["id"], "value": e1["id"]} for p in comp1["pins"]
    )
    c2.setdefault("entryPinMap", {}).setdefault("map", []).extend(
        {"key": p["id"], "value": e2["id"]} for p in comp2["pins"]
    )
    c2.setdefault("entries", []).append(e2)
    pairs = []
    for number in sorted(n1, key=lambda s: (len(s), s)):
        pair_id = doc.object_id()
        pairs.append(
            {
                "$type": "__logicalPinToPinConnection",
                "pinPair": {
                    "pin1": n2[number]["id"],
                    "pin2": n1[number]["id"],
                    "parameters": [],
                    "id": doc.object_id(),
                },
                "parameters": [],
                "id": pair_id,
            }
        )
    c1.setdefault("interConnectedEntriesConnections", []).append(
        {
            "entry1": e1["id"],
            "entry2": e2["id"],
            "physicalConnections": pairs,
            "id": doc.object_id(),
        }
    )
    # Entries of a harness form a chain of entry-to-entry links; hook the new
    # entry to an entry at an end of the line it joins, else to the last one
    ends = split["ends"] if split else []
    neighbour = next(
        (
            doc.item(pid)["logicalObjectId"]
            for pid in ends
            if "logicalObjectId" in doc.item(pid)
        ),
        c2["entries"][-2]["id"] if len(c2["entries"]) > 1 else None,
    )
    if neighbour:
        c2.setdefault("connectionEntriesConnections", []).append(
            {
                "entry1": neighbour,
                "entry2": e2["id"],
                "physicalConnections": [],
                "id": doc.object_id(),
            }
        )

    # Drawing: mate point, its label, the new branch
    mate = {
        "$type": "__drawingConnectionPoint",
        "position": _vec(nx, ny),
        "direction": _vec(-dx, -dy),
        "lineStyleId": p1.get("lineStyleId", 3),
        "logicalObjectId": e2["id"],
        "matedConnectionPointId": p1["id"],
        "anchor": 5,
        "parentId": group["id"],
        "selectable": True,
        "id": doc.item_id(),
    }
    p1["matedConnectionPointId"] = mate["id"]
    page["items"].append(mate)
    label = next(
        (
            c
            for c in doc.children(p1["id"])
            if c.get("propertyValuePath") == "Designator"
        ),
        None,
    )
    if label:
        new_label = copy.deepcopy(label)
        ox, oy = _xy(label.get("offset"))
        new_label["offset"] = _vec(
            -ox if abs(dx) > TOL else ox, -oy if abs(dy) > TOL else oy
        )
        new_label.update(
            {
                "directValue": e2["definedDesignator"],
                "parentId": mate["id"],
                "id": doc.item_id(),
            }
        )
        page["items"].append(new_label)
    branch = {
        "$type": "__drawingConnection",
        "endItemId1": mate["id"],
        "endItemId2": junction_id,
        "lineStyleId": group.get("lineStyleId", 5),
        "anchor": 5,
        "parentId": group["id"],
        "selectable": True,
        "id": doc.item_id(),
    }
    page["items"].append(branch)
    chain = [mate["id"]]
    for corner in path[1:-1]:
        vp = {
            "$type": "__drawingConnectionPoint",
            "position": _vec(*corner),
            "direction": {},
            "connectionItemType": 1,
            "lineStyleId": 6,
            "anchor": 5,
            "parentId": branch["id"],
            "selectable": False,
            "id": doc.item_id(),
        }
        page["items"].append(vp)
        chain.append(vp["id"])
    chain.append(junction_id)
    for a, b in itertools.pairwise(chain):
        page["items"].append(
            {
                "$type": "__drawingConnection",
                "endItemId1": a,
                "endItemId2": b,
                "lineStyleId": group.get("lineStyleId", 5),
                "connectionItemType": 1,
                "anchor": 5,
                "parentId": branch["id"],
                "selectable": True,
                "id": doc.item_id(),
            }
        )
    return {
        "op": "join_harnesses",
        "entry": entry,
        "mate": f"{to}.{e2['definedDesignator']}",
        "parts": {entry: part, f"{to}.{e2['definedDesignator']}": mate_part},
        "mate_position": [nx, ny],
        "route": [list(p) for p in path],
        "split": bool(split),
        "pin_pairs": len(pairs),
    }


OPERATIONS = {
    "swap_entry_parts": swap_entry_parts,
    "join_harnesses": join_harnesses,
    "set_title": set_title,
}


def apply_operations(
    doc: MbsDocument,
    operations: list[dict[str, Any]],
    pin_sources: dict[str, list[str]] | None = None,
    libraries: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Apply operations in order; the first failure raises MbsEditError (the
    caller then writes nothing)."""
    results = []
    for n, op in enumerate(operations, 1):
        if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
            raise MbsEditError(
                f"operation {n}: op must be one of {', '.join(OPERATIONS)}"
            )
        args = {k: v for k, v in op.items() if k != "op"}
        if op["op"] == "join_harnesses":
            args.update(pin_sources=pin_sources, libraries=libraries)
        try:
            results.append(OPERATIONS[op["op"]](doc, **args))
        except TypeError as e:
            raise MbsEditError(f"operation {n} ({op['op']}): {e}") from e
        except MbsEditError as e:
            raise MbsEditError(f"operation {n} ({op['op']}): {e}") from e
    return results
