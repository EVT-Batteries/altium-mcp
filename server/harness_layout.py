"""Reader for Altium Harness Layout Drawings (.LdrDoc).

A layout drawing is Altium's synchronised copy of a wiring diagram plus the
physical layout: connection points, bundles between them, coverings on the
bundles and layout labels. Wires have no geometry here; Altium routes each
wire through the bundles between the connection points of its two ends (the
shortest path unless the designer assigned another one) and derives the wire
lengths from the bundle lengths.

Record types (Altium 26.7, FileHeader stream; API ObjectId in brackets):
  106 connector (81), 113 pin (88), 109 layout label (84),
  110 connection point (85), 111 bundle (86), 115 wire, 116 splice,
  120 no-connect, 122 twist, 128 covering (104).
Every object copied from the wiring diagram carries WiringDiagramOriginUniqueId,
the UniqueID of its .WirDoc original. Which components a connection point
holds is stored in the compressed 'HarnessConnectionPointConnector' stream.
"""

from __future__ import annotations

import heapq
import struct
import zlib
from pathlib import Path
from typing import Any

import olefile
from harness_parser import _coord, _text, _uid_list, extract_ole_records

# Internal Altium coordinate units per mil (lengths such as LengthLong)
COORD_PER_MIL = 10000
MM_PER_MIL = 0.0254

CP_STYLES = {0: "circle", 1: "square", 2: "insulator"}
# Copies of wiring-diagram objects: RECORD -> output key
SYNCED_OBJECTS = {
    "116": "splices",
    "120": "no_connects",
    "121": "shields",
    "122": "twists",
    "127": "cables",
}


def coord_to_mm(value: float) -> float:
    return round(value / COORD_PER_MIL * MM_PER_MIL, 3)


def mm_to_coord(value: float) -> int:
    return round(value / MM_PER_MIL * COORD_PER_MIL)


def read_connection_point_streams(
    file_path: Path | str,
) -> dict[str, list[dict[str, Any]]]:
    """Objects assigned to each connection point: {cp uid: [{uid, pin_count}]}.

    The 'HarnessConnectionPointConnector' stream is a record header followed
    by a zlib block holding: int32 version, int32 count, then per connection
    point its UniqueID, int32 object count and per object its UniqueID, int32
    pin count and that many pin ids (Pascal strings: length byte + text).
    """
    with olefile.OleFileIO(str(file_path)) as ole:
        if not ole.exists("HarnessConnectionPointConnector"):
            return {}
        data = ole.openstream("HarnessConnectionPointConnector").read()
    start = data.find(b"x\x9c")
    if start < 0:
        return {}
    try:
        blob = zlib.decompress(data[start:])
    except zlib.error:
        return {}

    pos = 0

    def int32() -> int:
        nonlocal pos
        (value,) = struct.unpack_from("<i", blob, pos)
        pos += 4
        return value

    def pstr() -> str:
        nonlocal pos
        size = blob[pos]
        text = blob[pos + 1 : pos + 1 + size].decode("latin-1")
        pos += 1 + size
        return text

    result: dict[str, list[dict[str, Any]]] = {}
    try:
        int32()  # version
        for _ in range(int32()):
            cp_uid = pstr()
            objects = []
            for _ in range(int32()):
                obj_uid = pstr()
                pins = [pstr() for _ in range(int32())]
                objects.append({"uid": obj_uid, "pin_count": len(pins)})
            result[cp_uid] = objects
    except (struct.error, IndexError):
        pass
    return result


def _points(props: dict[str, str]) -> list[float]:
    pts: list[float] = []
    for i in range(1, int(props.get("LocationCount", 0) or 0) + 1):
        pts.extend([_coord(props, f"X{i}"), _coord(props, f"Y{i}")])
    return pts


def _length(props: dict[str, str], key: str) -> dict[str, float]:
    value = int(props.get(key) or 0)
    return {"mils": value / COORD_PER_MIL, "mm": coord_to_mm(value)}


def parse_ldrdoc(file_path: Path | str) -> dict[str, Any]:
    """Layout drawing objects of a saved .LdrDoc.

    Coordinates are DXP units (1 = 10 mils), lengths are given in mils and mm.
    Wire routes are computed the way Altium assigns wires by default: the
    shortest bundle path between the connection points of the wire's two
    ends (a wire routed differently by hand in Altium is not detected).
    """
    raw = extract_ole_records(file_path, "FileHeader")
    records = raw[1:]
    cp_objects = read_connection_point_streams(file_path)

    connectors: dict[int, dict[str, Any]] = {}
    pins: dict[str, dict[str, Any]] = {}  # pin uid -> pin
    cps: dict[int, dict[str, Any]] = {}
    bundles: dict[int, dict[str, Any]] = {}
    labels: dict[int, dict[str, Any]] = {}
    coverings: dict[int, dict[str, Any]] = {}
    wires: dict[int, dict[str, Any]] = {}
    synced: dict[int, dict[str, Any]] = {}

    for idx, props in enumerate(records):
        rtype = props.get("RECORD")
        owned = "OwnerIndex" in props
        if rtype == "106" and not owned:
            connectors[idx] = {
                "designator": "?",
                "part_number": _text(props, "DesignItemId"),
                "lib_reference": _text(props, "LibReference"),
                "location": {
                    "x": _coord(props, "Location.X"),
                    "y": _coord(props, "Location.Y"),
                },
                "orientation": int(props.get("Orientation", 0) or 0) * 90,
                "mirrored": props.get("IsMirrored") == "T",
                "unique_id": props.get("UniqueID", ""),
                "origin_uid": props.get("WiringDiagramOriginUniqueId", ""),
                "views": [],
                "pin_uids": [],
            }
        elif rtype == "113" and owned:
            owner = int(props["OwnerIndex"])
            if owner in connectors:
                uid = props.get("UniqueID", "")
                pins[uid] = {
                    "connector_index": owner,
                    "designator": props.get("Designator", ""),
                }
                connectors[owner]["pin_uids"].append(uid)
        elif rtype == "110" and not owned:
            cps[idx] = {
                "designator": "",
                "location": {
                    "x": _coord(props, "Location.X"),
                    "y": _coord(props, "Location.Y"),
                },
                "orientation": int(props.get("Orientation", 0) or 0) * 90,
                "style": CP_STYLES.get(int(props.get("Style", 0) or 0), "circle"),
                "show_name": props.get("ShowName") == "T",
                "unique_id": props.get("UniqueID", ""),
                "bundle_uids": _uid_list(
                    props, "ConnectedBundlesUniqueIdsCount", "ConnectedBundleUniqueId"
                ),
            }
        elif rtype == "111" and not owned:
            bundles[idx] = {
                "designator": "",
                "comment": "",
                "points": _points(props),
                "length": _length(props, "LengthLong"),
                "drawn_length": _length(props, "DrawnLength"),
                "length_set_manually": props.get("IsLengthSetManually") == "T",
                "show_break_symbol": props.get("ShowBreakSymbol") == "T",
                "line_width": int(props.get("LineWidth", 0) or 0),
                "unique_id": props.get("UniqueID", ""),
                "end1_uid": props.get("EndVertex1ConnectedObjectUniqueID", ""),
                "end2_uid": props.get("EndVertex2ConnectedObjectUniqueID", ""),
            }
        elif rtype == "109" and not owned:
            labels[idx] = {
                "designator": "",
                "text": props.get("Text", ""),
                "location": {
                    "x": _coord(props, "Location.X"),
                    "y": _coord(props, "Location.Y"),
                },
                "orientation": int(props.get("Orientation", 0) or 0) * 90,
                "alignment": int(props.get("Alignment", 0) or 0),
                "show_only_first_line": props.get("ShowOnlyFirstLine") == "T",
                "part_number": _text(props, "DesignItemId"),
                "source_library": _text(props, "SourceLibraryName"),
                "unique_id": props.get("UniqueID", ""),
            }
        elif rtype == "128" and not owned:
            count = int(props.get("CoveredItemsCount", 0) or 0)
            coverings[idx] = {
                "designator": "",
                "part_number": _text(props, "DesignItemId"),
                "source_library": _text(props, "SourceLibraryName"),
                "thickness": int(props.get("Thickness", 0) or 0),
                "start_offset": _length(props, "PhysicalStartDistance"),
                "end_offset": _length(props, "PhysicalEndDistance"),
                "length": _length(props, "PhysicalLength"),
                "unique_id": props.get("UniqueID", ""),
                "item_uids": [
                    props[f"CoveredItemId{i}"]
                    for i in range(count)
                    if f"CoveredItemId{i}" in props
                ],
            }
        elif rtype == "115":
            wires[idx] = {
                "designator": props.get("Name", ""),
                "net": props.get("Comment", ""),
                "description": props.get("Description", ""),
                "part_number": _text(props, "DesignItemId"),
                "unique_id": props.get("UniqueID", ""),
                "origin_uid": props.get("WiringDiagramOriginUniqueId", ""),
                "end1_uid": props.get("EndVertex1ConnectedObjectUniqueID", ""),
                "end2_uid": props.get("EndVertex2ConnectedObjectUniqueID", ""),
                "twist_uids": _uid_list(
                    props, "ConnectedTwistsUniqueIdsCount", "ConnectedTwistUniqueId"
                ),
            }
        elif rtype in SYNCED_OBJECTS and not owned:
            synced[idx] = {
                "kind": SYNCED_OBJECTS[rtype],
                "designator": props.get("Designator", ""),
                "unique_id": props.get("UniqueID", ""),
                "origin_uid": props.get("WiringDiagramOriginUniqueId", ""),
                "wire_uids": _uid_list(
                    props, "ConnectedWiresUniqueIdsCount", "ConnectedWireUniqueId"
                ),
            }
        elif rtype in ("34", "41") and owned:
            owner = int(props["OwnerIndex"])
            name, val = props.get("Name", ""), _text(props, "Text")
            if owner in connectors:
                if name == "Designator":
                    connectors[owner]["designator"] = val
                elif (
                    name.startswith("LayoutDrawingSymbolModel")
                    and props.get("IsImageParameter") == "T"
                ):
                    view = dict(
                        part.split("=", 1) for part in val.split("|") if "=" in part
                    )
                    connectors[owner]["views"].append(
                        {
                            "name": view.get("ViewName", ""),
                            "type": view.get("ViewType", ""),
                            "side": view.get("ViewSide", ""),
                            "rotation": view.get("Rotation", ""),
                            "zoom": view.get("Zoom", ""),
                            "main": view.get("MainModel") == "True",
                            "location": {
                                "x": _coord(props, "Location.X"),
                                "y": _coord(props, "Location.Y"),
                            },
                        }
                    )
            else:
                target = (
                    cps.get(owner)
                    or bundles.get(owner)
                    or labels.get(owner)
                    or coverings.get(owner)
                )
                if target is not None and name in ("Designator", "Comment"):
                    target[name.lower()] = val

    # Which connection point holds each connector / splice / no-connect
    by_uid = {c["unique_id"]: c for c in connectors.values()}
    synced_by_uid = {s["unique_id"]: s for s in synced.values()}
    cp_by_uid = {cp["unique_id"]: cp for cp in cps.values()}
    holder: dict[str, str] = {}  # object uid -> cp uid
    for cp in cps.values():
        cp["objects"] = []
        for obj in cp_objects.get(cp["unique_id"], []):
            holder[obj["uid"]] = cp["unique_id"]
            target = by_uid.get(obj["uid"]) or synced_by_uid.get(obj["uid"])
            cp["objects"].append(target["designator"] if target else obj["uid"])

    bundle_by_uid = {b["unique_id"]: b for b in bundles.values()}
    for b in bundles.values():
        for end in ("end1", "end2"):
            cp = cp_by_uid.get(b[f"{end}_uid"])
            b[f"{end}_connection_point"] = cp["designator"] if cp else None
    for cp in cps.values():
        cp["bundles"] = [
            bundle_by_uid[u]["designator"] or u
            for u in cp.pop("bundle_uids")
            if u in bundle_by_uid
        ]
    for cov in coverings.values():
        items = []
        for uid in cov.pop("item_uids"):
            target = bundle_by_uid.get(uid) or cp_by_uid.get(uid) or by_uid.get(uid)
            items.append(target["designator"] if target else uid)
        cov["items"] = items

    # Wire ends -> "J1.3" / "SP1", and their connection points
    def end_of(uid: str) -> tuple[str, str | None]:
        if uid in pins:
            pin = pins[uid]
            conn = connectors[pin["connector_index"]]
            return f"{conn['designator']}.{pin['designator']}", holder.get(
                conn["unique_id"]
            )
        if uid in synced_by_uid:
            obj = synced_by_uid[uid]
            return obj["designator"], holder.get(uid)
        return uid, None

    graph: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
    for b in bundles.values():
        if b["end1_uid"] in cp_by_uid and b["end2_uid"] in cp_by_uid:
            weight = int(b["length"]["mils"] * COORD_PER_MIL)
            graph.setdefault(b["end1_uid"], []).append((weight, b["end2_uid"], b))
            graph.setdefault(b["end2_uid"], []).append((weight, b["end1_uid"], b))

    for b in bundles.values():
        b["wires"] = []
    unrouted = []
    for w in wires.values():
        (end1, cp1), (end2, cp2) = end_of(w["end1_uid"]), end_of(w["end2_uid"])
        w["end1"], w["end2"] = end1, end2
        route = _shortest_route(graph, cp1, cp2)
        if route is None:
            w["route"] = None
            w["routed_length"] = None
            unrouted.append(w["designator"])
            continue
        w["route"] = [b["designator"] or b["unique_id"] for b in route]
        total = sum(int(b["length"]["mils"] * COORD_PER_MIL) for b in route)
        w["routed_length"] = {"mils": total / COORD_PER_MIL, "mm": coord_to_mm(total)}
        for b in route:
            b["wires"].append(w["designator"])

    twist_by_uid = {
        s["unique_id"]: s["designator"]
        for s in synced.values()
        if s["kind"] == "twists"
    }
    for w in wires.values():
        w["twists"] = [twist_by_uid.get(u, u) for u in w.pop("twist_uids")]
        for key in ("end1_uid", "end2_uid"):
            w.pop(key)

    for c in connectors.values():
        c["connection_point"] = (
            cp_by_uid.get(holder.get(c["unique_id"], "")) or {}
        ).get("designator")
        c.pop("pin_uids")
    synced_lists: dict[str, list[dict[str, Any]]] = {
        k: [] for k in SYNCED_OBJECTS.values()
    }
    for s in synced.values():
        wire_des = {w["unique_id"]: w["designator"] for w in wires.values()}
        s["wires"] = [wire_des.get(u, u) for u in s.pop("wire_uids")]
        s["connection_point"] = (
            cp_by_uid.get(holder.get(s["unique_id"], "")) or {}
        ).get("designator")
        synced_lists[s.pop("kind")].append(s)

    by_des = lambda items: sorted(items, key=lambda o: o.get("designator") or "")
    return {
        "file_path": str(Path(file_path).resolve()),
        "summary": {
            "connector_count": len(connectors),
            "connection_point_count": len(cps),
            "bundle_count": len(bundles),
            "covering_count": len(coverings),
            "layout_label_count": len(labels),
            "wire_count": len(wires),
            "unrouted_wire_count": len(unrouted),
        },
        "connectors": by_des(connectors.values()),
        "connection_points": by_des(cps.values()),
        "bundles": by_des(bundles.values()),
        "coverings": by_des(coverings.values()),
        "layout_labels": by_des(labels.values()),
        "wires": by_des(wires.values()),
        "unrouted_wires": sorted(unrouted),
        **{k: by_des(v) for k, v in synced_lists.items()},
    }


def _shortest_route(
    graph: dict[str, list[tuple[int, str, dict[str, Any]]]],
    start: str | None,
    goal: str | None,
) -> list[dict[str, Any]] | None:
    """Bundles on the shortest path between two connection points ([] when
    both ends share one), or None when no bundle path connects them."""
    if not start or not goal:
        return None
    if start == goal:
        return []
    best = {start: 0}
    queue: list[tuple[int, int, str, list[dict[str, Any]]]] = [(0, 0, start, [])]
    tie = 0
    while queue:
        dist, _, node, path = heapq.heappop(queue)
        if node == goal:
            return path
        if dist > best.get(node, dist):
            continue
        for weight, nxt, bundle in graph.get(node, []):
            nd = dist + weight
            if nd < best.get(nxt, nd + 1):
                best[nxt] = nd
                tie += 1
                heapq.heappush(queue, (nd, tie, nxt, path + [bundle]))
    return None
