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
# THarnessWireLengthType: calculated = routed length + LengthOffset, user =
# UserLength, mcad = LengthLong (what a script's SetState_Length sets)
WIRE_LENGTH_TYPES = {0: "calculated", 1: "user", 2: "mcad"}
# Copies of wiring-diagram objects: RECORD -> output key
SYNCED_OBJECTS = {
    "116": "splices",
    "120": "no_connects",
    "121": "shields",
    "122": "twists",
    "127": "cables",
}
# Text names stored differently on disk: file name -> script name
TEXT_NAMES_ON_DISK = {"LengthParameter": "Length"}
# TTextJustification by index: the corner or edge a text's location names.
# Mirroring an object flips its texts' justification (left <-> right).
TEXT_JUSTIFICATIONS = (
    "bottom_left",
    "bottom_center",
    "bottom_right",
    "center_left",
    "center",
    "center_right",
    "top_left",
    "top_center",
    "top_right",
)


def coord_to_mm(value: float) -> float:
    return round(value / COORD_PER_MIL * MM_PER_MIL, 3)


def mm_to_coord(value: float) -> int:
    return round(value / MM_PER_MIL * COORD_PER_MIL)


def read_connection_point_streams(
    file_path: Path | str,
) -> dict[str, list[dict[str, Any]]]:
    """Objects assigned to each connection point: {cp uid: [{uid, pin_count, pins}]}.

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
                objects.append({"uid": obj_uid, "pin_count": len(pins), "pins": pins})
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


def _wire_length(wire: dict[str, Any], routed: int | None) -> dict[str, float] | None:
    """A wire's total length as Altium computes it for its length type
    (routed in internal coords, None when the wire has no route)."""
    kind = wire["length_type"]
    if kind == "user":
        return wire["user_length"]
    if kind == "mcad":
        return wire["fixed_length"]
    if routed is None:
        return None
    value = routed + round(wire["length_offset"]["mils"] * COORD_PER_MIL)
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
    image_views: dict[int, dict[str, Any]] = {}  # image parameter index -> view

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
                "texts": {},
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
                "texts": {},
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
                "texts": {},
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
                "texts": {},
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
                "texts": {},
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
                "length_type": WIRE_LENGTH_TYPES.get(
                    int(props.get("LengthType", 0) or 0), "calculated"
                ),
                "fixed_length": _length(props, "LengthLong"),
                "user_length": _length(props, "UserLength"),
                "length_offset": _length(props, "LengthOffset"),
                "via_bundle_uids": _uid_list(
                    props,
                    "BundlesToGoThroughUniqueIdsCount",
                    "BundleToGoThroughUniqueId",
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
        elif rtype == "30" and owned and int(props["OwnerIndex"]) in image_views:
            image_views[int(props["OwnerIndex"])]["image"] = {
                "x1": _coord(props, "Location.X"),
                "y1": _coord(props, "Location.Y"),
                "x2": _coord(props, "Corner.X"),
                "y2": _coord(props, "Corner.Y"),
            }
        elif rtype in ("34", "41") and owned:
            owner = int(props["OwnerIndex"])
            name, val = props.get("Name", ""), _text(props, "Text")
            is_view = (
                name.startswith("LayoutDrawingSymbolModel")
                and props.get("IsImageParameter") == "T"
            )
            just = int(props.get("Justification", 0) or 0)
            text = {
                "x": _coord(props, "Location.X"),
                "y": _coord(props, "Location.Y"),
                "hidden": props.get("IsHidden") == "T",
                "justification": TEXT_JUSTIFICATIONS[just]
                if 0 <= just < len(TEXT_JUSTIFICATIONS)
                else str(just),
            }
            # Texts as the script names them (a bundle's length is stored as
            # LengthParameter); a connector's hidden parameters are left out
            text_name = TEXT_NAMES_ON_DISK.get(name, name)
            if owner in connectors:
                if not is_view and (
                    not text["hidden"] or name in ("Designator", "Comment")
                ):
                    connectors[owner]["texts"][text_name] = text
                if name == "Designator":
                    connectors[owner]["designator"] = val
                elif is_view:
                    view = dict(
                        part.split("=", 1) for part in val.split("|") if "=" in part
                    )
                    image_views[idx] = {
                        "name": view.get("ViewName", ""),
                        "type": view.get("ViewType", ""),
                        "side": view.get("ViewSide", ""),
                        "rotation": view.get("Rotation", ""),
                        "zoom": view.get("Zoom", ""),
                        "main": view.get("MainModel") == "True",
                        # hidden views = the connector shows its symbol
                        "hidden": props.get("IsHidden") == "T",
                        # the rendered picture (child image), None until
                        # Altium has rendered the view
                        "image": None,
                    }
                    connectors[owner]["views"].append(image_views[idx])
            else:
                target = (
                    cps.get(owner)
                    or bundles.get(owner)
                    or labels.get(owner)
                    or coverings.get(owner)
                )
                if target is not None:
                    target["texts"][text_name] = text
                if target is not None and name in ("Designator", "Comment"):
                    target[name.lower()] = val
                if owner in cps and name == "Designator":
                    cps[owner]["designator_location"] = {
                        "x": _coord(props, "Location.X"),
                        "y": _coord(props, "Location.Y"),
                    }

    # Altium draws a connection point's name by its text's IsHidden; ShowName
    # is only read when it imports an old drawing
    for cp in cps.values():
        if "Designator" in cp["texts"]:
            cp["show_name"] = not cp["texts"]["Designator"]["hidden"]

    # Which connection point holds each connector / splice / no-connect
    by_uid = {c["unique_id"]: c for c in connectors.values()}
    synced_by_uid = {s["unique_id"]: s for s in synced.values()}
    cp_by_uid = {cp["unique_id"]: cp for cp in cps.values()}
    holder: dict[str, str] = {}  # object uid -> cp uid
    # Altium finds a connector wire's connection point by the pin ids the
    # point holds for that connector, not by the connector: pin uid -> cp uid
    pin_holder: dict[str, str] = {}
    for cp in cps.values():
        cp["objects"] = []
        for obj in cp_objects.get(cp["unique_id"], []):
            holder[obj["uid"]] = cp["unique_id"]
            for pin_uid in obj.get("pins", []):
                pin_holder[pin_uid] = cp["unique_id"]
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
            items.append((target["designator"] or uid) if target else uid)
        cov["items"] = items

    # Wire ends -> "J1.3" / "SP1", and their connection points
    def end_of(uid: str) -> tuple[str, str | None]:
        if uid in pins:
            pin = pins[uid]
            conn = connectors[pin["connector_index"]]
            return f"{conn['designator']}.{pin['designator']}", pin_holder.get(uid)
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
        w["via_bundles"] = [
            (bundle_by_uid[u]["designator"] or u) if u in bundle_by_uid else u
            for u in w.pop("via_bundle_uids")
        ]
        route = _shortest_route(graph, cp1, cp2)
        if route is None:
            w["route"] = None
            w["routed_length"] = None
            w["length"] = _wire_length(w, None)
            unrouted.append(w["designator"])
            continue
        w["route"] = [b["designator"] or b["unique_id"] for b in route]
        total = sum(int(b["length"]["mils"] * COORD_PER_MIL) for b in route)
        w["routed_length"] = {"mils": total / COORD_PER_MIL, "mm": coord_to_mm(total)}
        w["length"] = _wire_length(w, total)
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
        # Pins no connection point holds: their wires cannot be routed (a
        # connection point keeping the ids of pins replaced by an update
        # from the library holds none)
        c["unassigned_pins"] = sorted(
            (pins[u]["designator"] for u in c.pop("pin_uids") if u not in pin_holder),
            key=lambda d: (len(d), d),
        )
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


# --- Edit spec ---------------------------------------------------------------
# edit_harness_layout sends one record per line to ExecuteEditHarnessLayout
# (schematic_utils.pas). Coordinates are mils, lengths internal coords; an
# empty field keeps the current value. References to objects created in the
# same run are 'DES:<designator>', all others UniqueIDs.

CP_STYLE_IDS = {name: idx for idx, name in CP_STYLES.items()}
# Connector views: "physical" = picture of the footprint's 3D model (Altium
# renders it), "symbol" = the schematic symbol
CONNECTOR_VIEWS = ("physical", "symbol")
# Designator of a new connection point, from the point (mils); Altium puts
# it near the sheet origin. About where imported connection points have it.
CP_DESIGNATOR_OFFSET = (200, 400)
# A designator further than this from its connection point (mils) was left
# behind by an earlier move and is put back at CP_DESIGNATOR_OFFSET
CP_DESIGNATOR_MAX_OFFSET = 1500


def _mils(point: dict[str, float]) -> tuple[int, int]:
    return round(point["x"] * 10), round(point["y"] * 10)


def _orientation_index(value: Any) -> int:
    """0-3 from an orientation in degrees (0, 90, 180, 270)."""
    deg = int(value)
    if deg % 90:
        raise ValueError(f"orientation must be a multiple of 90 degrees, got {value}")
    return deg // 90 % 4


def _rotate(dx: int, dy: int, quarter_turns: int) -> tuple[int, int]:
    """(dx, dy) turned counter-clockwise by quarter_turns x 90 degrees."""
    for _ in range(quarter_turns % 4):
        dx, dy = -dy, dx
    return dx, dy


def _rendered_main_view(views: list[dict[str, Any]]) -> bool:
    """True when a connector has a rendered main physical view (shown or not)."""
    return any(
        v["type"] == "ePhysicalView" and v["main"] and v.get("image") for v in views
    )


def _shows_physical(views: list[dict[str, Any]]) -> bool:
    """True when a connector is drawn as its physical view, not its symbol."""
    return any(
        v["type"] == "ePhysicalView"
        and v["main"]
        and v.get("image")
        and not v.get("hidden")
        for v in views
    )


def _flag(value: Any) -> str:
    if value is None:
        return ""
    return "1" if bool(value) else "0"


def _text_records(
    ref: str,
    texts: Any,
    owner: str,
    errors: list[str],
    known: dict[str, Any] | None = None,
) -> list[list[Any]]:
    """TEXT records placing or hiding texts of one object. texts is {name:
    [x, y] or {"x", "y", "hidden", "justification"}} in mils; names as Altium's
    script names them (Designator, Comment, Length, or a parameter name), any
    case. A placed text is justified bottom-left (x, y is its bottom-left
    corner) unless a justification (TEXT_JUSTIFICATIONS) is given: a text
    mirrored with its owner is otherwise justified right. known: the object's
    texts from parse_ldrdoc, to check the names against."""
    if not texts:
        return []
    if not isinstance(texts, dict):
        errors.append(f"{owner}: texts must be {{name: [x, y] or {{x, y, hidden}}}}")
        return []
    names = {n.lower(): n for n in known or {}}
    records: list[list[Any]] = []
    for name, pos in texts.items():
        name = str(name).strip()
        just = None
        if isinstance(pos, dict):
            x, y, hidden = pos.get("x"), pos.get("y"), pos.get("hidden")
            just = pos.get("justification")
        elif isinstance(pos, (list, tuple)) and len(pos) == 2:
            (x, y), hidden = pos, None
        else:
            x = y = hidden = None
        if (
            not name
            or (x is None) != (y is None)
            or (x is None and hidden is None and just is None)
        ):
            errors.append(f"{owner}: text '{name}' needs [x, y] or {{x, y, hidden}}")
            continue
        if known is not None and name.lower() not in names:
            errors.append(f"{owner}: no text '{name}' (has {sorted(known)})")
            continue
        if just is None:
            just_id = "" if x is None else 0
        elif str(just).lower() in TEXT_JUSTIFICATIONS:
            just_id = TEXT_JUSTIFICATIONS.index(str(just).lower())
        else:
            errors.append(
                f"{owner}: text '{name}': justification must be one of "
                f"{list(TEXT_JUSTIFICATIONS)}"
            )
            continue
        records.append(
            [
                "TEXT",
                ref,
                names.get(name.lower(), name),
                "" if x is None else int(x),
                "" if y is None else int(y),
                _flag(hidden),
                just_id,
            ]
        )
    return records


def _follow_end(points: list[int], at_start: bool, x: int, y: int) -> list[int]:
    """Bundle points (flat, mils) with one end moved to (x, y). The vertex next
    to it moves along the segment's axis, so a straight end segment stays
    straight (horizontal or vertical) when the bundle has a bend."""
    pts = list(points)
    n = len(pts) // 2
    end = 0 if at_start else n - 1
    nxt = 1 if at_start else n - 2
    old_x, old_y = pts[2 * end], pts[2 * end + 1]
    if n >= 3:
        if pts[2 * nxt + 1] == old_y:  # horizontal end segment
            pts[2 * nxt + 1] = y
        elif pts[2 * nxt] == old_x:  # vertical end segment
            pts[2 * nxt] = x
    pts[2 * end], pts[2 * end + 1] = x, y
    return pts


def build_layout_spec(
    layout: dict[str, Any],
    connectors: list[dict[str, Any]] | None = None,
    connection_points: list[dict[str, Any]] | None = None,
    bundles: list[dict[str, Any]] | None = None,
    coverings: list[dict[str, Any]] | None = None,
    layout_labels: list[dict[str, Any]] | None = None,
    delete: list[str] | None = None,
    wires: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Spec records for edit_harness_layout from the parse_ldrdoc() output of
    the saved drawing and the requested changes.

    Returns {"records": [[field, ...], ...], "errors": [...], "warnings": [...],
    "expect": {"bundles": {bundle uid (new: designator): [from_cp, to_cp]},
    "views": {connector: view}, "wire_lengths": {wire: mm or None}}}, connection
    points by UniqueID (new: DES:<designator>). Records are only usable when
    errors is empty. Objects are named by designator, or by unique_id when they
    have none or share it with others (unnamed 'B?' bundles): a shared designator
    is an error; coordinates are mils, lengths mm.
    """
    errors: list[str] = []
    warnings: list[str] = []
    view_records: list[list[Any]] = []
    expect_views: dict[str, str] = {}

    conns = {c["designator"]: c for c in layout["connectors"]}

    def shared(items: list[dict[str, Any]]) -> set[str]:
        """Designators several objects carry (unnamed ones, e.g. 'B?')."""
        seen: set[str] = set()
        dup: set[str] = set()
        for o in items:
            if o.get("designator"):
                (dup if o["designator"] in seen else seen).add(o["designator"])
        return dup

    def keyed(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Objects by UniqueID and by designator, except shared designators."""
        dup = shared(items)
        table = {o["unique_id"]: o for o in items}
        table.update(
            {
                o["designator"]: o
                for o in items
                if o.get("designator") and o["designator"] not in dup
            }
        )
        return table

    shared_names = set().union(
        *(
            shared(layout[kind])
            for kind in ("connection_points", "bundles", "coverings", "layout_labels")
        )
    )

    def ambiguous(name: Any) -> bool:
        """True, with an error, when name is a designator several objects share."""
        name = str(name or "").strip()
        if name in shared_names:
            errors.append(
                f"'{name}' names several objects; give the one meant by its unique_id"
            )
            return True
        return False

    cps = keyed(layout["connection_points"])
    # A no-connect at a wire end (a free end, e.g. a loose contact) is held like a splice
    held = {
        o["designator"]: o
        for o in [
            *layout["connectors"],
            *layout.get("splices", []),
            *layout.get("no_connects", []),
        ]
    }
    holder = {
        obj: cp for cp in layout["connection_points"] for obj in cp.get("objects", [])
    }

    existing_bundles = keyed(layout["bundles"])
    existing_coverings = keyed(layout["coverings"])
    existing_labels = keyed(layout["layout_labels"])

    def item_key(item: dict[str, Any]) -> str:
        """The designator, or the unique_id when there is none or the
        designator is shared."""
        des = str(item.get("designator") or "").strip()
        uid = str(item.get("unique_id") or "").strip()
        return uid if uid and (not des or des in shared_names) else des or uid

    # Deletions first: what they remove cannot be edited or followed
    deleted: set[str] = set()  # unique ids
    delete_records: list[list[Any]] = []
    for ref in delete or []:
        ref = str(ref).strip()
        if ambiguous(ref):
            continue
        obj = (
            existing_bundles.get(ref)
            or existing_coverings.get(ref)
            or existing_labels.get(ref)
            or cps.get(ref)
        )
        if obj is None:
            errors.append(
                f"delete: no bundle, covering, label or connection point '{ref}'"
            )
            continue
        if ref in cps and cps[ref].get("objects"):
            errors.append(
                f"delete: connection point '{ref}' holds {cps[ref]['objects']}; "
                "assign them to another connection point first"
            )
            continue
        deleted.add(obj["unique_id"])
        delete_records.append(["DELETE", obj["unique_id"]])

    # Final position (mils) of every connection point, by UniqueID (new ones:
    # DES:<designator>, as the script finds them)
    cp_pos: dict[str, tuple[int, int]] = {}
    for cp in layout["connection_points"]:
        if cp["unique_id"] not in deleted:
            cp_pos[cp["unique_id"]] = _mils(cp["location"])
    cp_moves: dict[str, dict[str, Any]] = {}  # cp uid -> {x, y, des_x, des_y}

    def move_cp(cp: dict[str, Any], x: int, y: int, des_pos: Any = None) -> None:
        old_x, old_y = _mils(cp["location"])
        des = cp.get("designator_location")
        if des_pos:
            dx, dy = int(des_pos[0]), int(des_pos[1])
        elif des:
            dx, dy = _mils(des)
            dx, dy = x + dx - old_x, y + dy - old_y
            if max(abs(dx - x), abs(dy - y)) > CP_DESIGNATOR_MAX_OFFSET:
                dx, dy = x + CP_DESIGNATOR_OFFSET[0], y + CP_DESIGNATOR_OFFSET[1]
        else:
            dx, dy = x + CP_DESIGNATOR_OFFSET[0], y + CP_DESIGNATOR_OFFSET[1]
        cp_moves[cp["unique_id"]] = {"x": x, "y": y, "des_x": dx, "des_y": dy}
        cp_pos[cp["unique_id"]] = (x, y)

    connector_records: list[list[Any]] = []
    text_records: list[list[Any]] = []
    cp_records: list[list[Any]] = []
    assign_records: list[list[Any]] = []
    # Connection points given a position explicitly
    placed_cps = set()
    for c in connection_points or []:
        if "x" in c or "y" in c:
            cp = cps.get(item_key(c))
            placed_cps.add(cp["unique_id"] if cp else item_key(c))

    for item in connectors or []:
        des = item_key(item)
        conn = conns.get(des)
        if conn is None:
            errors.append(f"connector '{des}' not found in the layout drawing")
            continue
        old_x, old_y = conn["location"]["x"] * 10, conn["location"]["y"] * 10
        x = int(item.get("x", round(old_x)))
        y = int(item.get("y", round(old_y)))
        try:
            ori = (
                _orientation_index(item["orientation"])
                if item.get("orientation") is not None
                else conn["orientation"] // 90
            )
        except ValueError as e:
            errors.append(f"connector '{des}': {e}")
            continue
        mirror = item.get("mirrored")
        view = item.get("view")
        if view is not None:
            view = str(view).lower()
            if view not in CONNECTOR_VIEWS:
                errors.append(
                    f"connector '{des}': view must be one of {sorted(CONNECTOR_VIEWS)}"
                )
                continue
            if (view == "physical") != _shows_physical(conn["views"]):
                # render: no view to show yet, Altium must render one
                render = view == "physical" and not _rendered_main_view(conn["views"])
                view_records.append(["VIEW", conn["unique_id"], view, _flag(render)])
            expect_views[des] = view
        # A connector's hidden parameters can be shown too: names unchecked
        text_records += _text_records(
            conn["unique_id"], item.get("texts"), f"connector '{des}'", errors
        )
        if not {"x", "y", "orientation", "mirrored"} & item.keys():
            continue
        connector_records.append(
            ["CONNECTOR", conn["unique_id"], x, y, ori, _flag(mirror)]
        )
        # Its own connection point travels with it (Altium leaves it
        # elsewhere): same offset, turned with the connector
        own = cps.get(conn.get("connection_point") or "")
        target = item.get("connection_point")
        if own is None or own["unique_id"] in placed_cps:
            continue
        if len(own.get("objects", [])) > 1 and not target:
            warnings.append(
                f"connector '{des}': connection point {own['designator']} also holds "
                f"{[o for o in own['objects'] if o != des]}; not moved"
            )
            continue
        if target:
            cx, cy = int(target["x"]), int(target["y"])
        else:
            cpx, cpy = _mils(own["location"])
            dx, dy = cpx - round(old_x), cpy - round(old_y)
            if mirror is not None and bool(mirror) != conn["mirrored"]:
                dx = -dx
            dx, dy = _rotate(dx, dy, ori - conn["orientation"] // 90)
            cx, cy = x + dx, y + dy
        move_cp(own, cx, cy)

    for item in connection_points or []:
        des = item_key(item)
        if ambiguous(des):
            continue
        cp = cps.get(des)
        if cp is not None and cp["unique_id"] in deleted:
            errors.append(f"connection point '{des}' is both edited and deleted")
            continue
        style = item.get("style")
        if style is not None and str(style).lower() not in CP_STYLE_IDS:
            errors.append(
                f"connection point '{des}': style must be one of {sorted(CP_STYLE_IDS)}"
            )
            continue
        style_id = "" if style is None else CP_STYLE_IDS[str(style).lower()]
        try:
            ori = (
                ""
                if item.get("orientation") is None
                else _orientation_index(item["orientation"])
            )
        except ValueError as e:
            errors.append(f"connection point '{des}': {e}")
            continue
        des_pos = item.get("designator_pos")
        if cp is None:
            if not des or "x" not in item or "y" not in item:
                errors.append(
                    f"new connection point '{des}' needs a designator, x and y"
                )
                continue
            x, y = int(item["x"]), int(item["y"])
            dx, dy = (
                (int(des_pos[0]), int(des_pos[1]))
                if des_pos
                else (x + CP_DESIGNATOR_OFFSET[0], y + CP_DESIGNATOR_OFFSET[1])
            )
            show = _flag(item.get("show_name"))
            cp_records.append(["CPOINT", "", des, x, y, style_id, show, ori, dx, dy])
            ref = f"DES:{des}"
            cp_pos[ref] = (x, y)
        else:
            if "x" in item or "y" in item or des_pos:
                cur_x, cur_y = cp_pos[cp["unique_id"]]
                x, y = int(item.get("x", cur_x)), int(item.get("y", cur_y))
                move_cp(cp, x, y, des_pos)
            mv = cp_moves.get(cp["unique_id"])
            new_des = str(item.get("new_designator") or "").strip()
            cp_records.append(
                [
                    "CPOINT",
                    cp["unique_id"],
                    new_des,
                    *((mv["x"], mv["y"]) if mv else ("", "")),
                    style_id,
                    _flag(item.get("show_name")),
                    ori,
                    *((mv["des_x"], mv["des_y"]) if mv else ("", "")),
                ]
            )
            ref = cp["unique_id"]
        text_records += _text_records(
            ref,
            item.get("texts"),
            f"connection point '{des}'",
            errors,
            None if cp is None else cp.get("texts"),
        )
        for obj_des in item.get("objects") or []:
            obj = held.get(obj_des)
            if obj is None:
                errors.append(
                    f"connection point '{des}': no connector, splice or no-connect "
                    f"'{obj_des}'"
                )
                continue
            old = holder.get(obj_des)
            if (
                old is not None
                and cp is not None
                and old["unique_id"] == cp["unique_id"]
            ):
                continue
            if old is not None:
                assign_records.append(
                    ["CPUNASSIGN", old["unique_id"], obj["unique_id"]]
                )
            assign_records.append(["CPASSIGN", ref, obj["unique_id"]])
            holder[obj_des] = cp or {"unique_id": ref, "designator": des}
        for obj_des in item.get("release") or []:
            obj = held.get(obj_des)
            old = holder.get(obj_des)
            if (
                obj is None
                or old is None
                or old["designator"] != (cp or {}).get("designator")
            ):
                errors.append(f"connection point '{des}': does not hold '{obj_des}'")
                continue
            assign_records.append(["CPUNASSIGN", old["unique_id"], obj["unique_id"]])
            holder.pop(obj_des)

    # Connector connection points moved above but not listed explicitly
    listed = {r[1] for r in cp_records if r[1]}
    for uid, mv in cp_moves.items():
        if uid not in listed:
            cp_records.append(
                [
                    "CPOINT",
                    uid,
                    "",
                    mv["x"],
                    mv["y"],
                    "",
                    "",
                    "",
                    mv["des_x"],
                    mv["des_y"],
                ]
            )

    def cp_ref(name: Any) -> str | None:
        """Key in cp_pos of the connection point named by a CP designator or
        UniqueID, or of a connector's own connection point."""
        name = str(name or "").strip()
        if ambiguous(name):
            return None
        cp = cps.get(name)
        if cp is None and name in conns:
            cp = cps.get(conns[name].get("connection_point") or "")
        key = cp["unique_id"] if cp is not None else f"DES:{name}"
        return key if key in cp_pos else None

    bundle_records: list[list[Any]] = []
    # Ends (connection point keys as in cp_pos) of the bundles the call draws
    # or moves, by bundle UniqueID (new bundles: designator)
    expect: dict[str, list[str]] = {}
    redrawn: set[str] = set()
    used_names = {b["designator"] for b in layout["bundles"] if b["designator"]}
    new_bundles: set[str] = set()

    for item in bundles or []:
        key = item_key(item)
        if ambiguous(key):
            continue
        bundle = existing_bundles.get(key) if key else None
        if bundle is not None and bundle["unique_id"] in deleted:
            errors.append(f"bundle '{key}' is both edited and deleted")
            continue
        des = key
        if bundle is None and not des:
            n = 1
            while f"B{n}" in used_names:
                n += 1
            des = f"B{n}"
        used_names.add(des)

        points: list[int] | None = None
        ends: list[str] | None = None
        if item.get("from") or item.get("to"):
            a, b = cp_ref(item.get("from")), cp_ref(item.get("to"))
            if a is None or b is None:
                errors.append(
                    f"bundle '{des}': from/to must name connection points or "
                    f"connectors with one (got {item.get('from')!r}, {item.get('to')!r})"
                )
                continue
            points = [*cp_pos[a]]
            for v in item.get("via") or []:
                points.extend([int(v[0]), int(v[1])])
            points.extend(cp_pos[b])
            ends = [a, b]
        elif item.get("points"):
            points = [int(v) for v in item["points"]]
            if len(points) < 4 or len(points) % 2:
                errors.append(f"bundle '{des}': points must be x1, y1, x2, y2, ...")
                continue
            by_pos = {pos: name for name, pos in cp_pos.items()}
            a = by_pos.get((points[0], points[1]))
            b = by_pos.get((points[-2], points[-1]))
            if a is None or b is None:
                errors.append(
                    f"bundle '{des}': both ends must lie exactly on connection points"
                )
                continue
            ends = [a, b]
        elif bundle is None:
            errors.append(f"new bundle '{des}' needs from/to or points")
            continue

        if "length_mm" in item:
            length = (
                "AUTO"
                if item["length_mm"] is None
                else mm_to_coord(float(item["length_mm"]))
            )
            # The script reads lengths as 32-bit integers (internal coords)
            if length != "AUTO" and not 0 < length < 2**31:
                errors.append(
                    f"bundle '{des}': length_mm must be between 0 and "
                    f"{coord_to_mm(2**31 - 1):.0f}"
                )
                continue
        else:
            length = ""
        new_des = des if bundle is None else str(item.get("new_designator") or "")
        record = [
            "BUNDLE",
            "" if bundle is None else bundle["unique_id"],
            new_des,
            str(item.get("comment", "")) if "comment" in item else "",
            length,
            _flag(item.get("show_break_symbol")),
            "" if item.get("line_width") is None else int(item["line_width"]),
        ]
        if points is not None:
            record.extend(points)
        bundle_records.append(record)
        text_records += _text_records(
            f"DES:{des}" if bundle is None else bundle["unique_id"],
            item.get("texts"),
            f"bundle '{des}'",
            errors,
            None if bundle is None else bundle.get("texts"),
        )
        if bundle is None:
            new_bundles.add(des)
        elif points is not None:
            redrawn.add(bundle["unique_id"])
        if ends:
            expect[des if bundle is None else bundle["unique_id"]] = ends
        if (
            points is not None
            and len(points) == 4
            and points[0] != points[2]
            and points[1] != points[3]
        ):
            warnings.append(f"bundle '{des}' is drawn diagonally (no via points)")

    # Bundles on moved connection points follow them: Altium connects bundle
    # ends to connection points by position when it loads the drawing
    for bundle in layout["bundles"]:
        uid = bundle["unique_id"]
        if uid in deleted or uid in redrawn:
            continue
        pts = [round(v * 10) for v in bundle["points"]]
        changed = False
        for end, at_start in (("end1_uid", True), ("end2_uid", False)):
            mv = cp_moves.get(bundle[end])
            if mv:
                pts = _follow_end(pts, at_start, mv["x"], mv["y"])
                changed = True
        if changed:
            bundle_records.append(["BUNDLE", uid, "", "", "", "", "", *pts])
            expect[uid] = [bundle.get("end1_uid") or "", bundle.get("end2_uid") or ""]

    covering_records: list[list[Any]] = []
    for item in coverings or []:
        key = item_key(item)
        if ambiguous(key):
            continue
        cov = existing_coverings.get(key) if key else None
        if cov is not None and cov["unique_id"] in deleted:
            errors.append(f"covering '{key}' is both edited and deleted")
            continue
        refs: list[str] | None = None
        if item.get("bundles") is not None:
            refs = []
            for name in item["bundles"]:
                if ambiguous(name):
                    continue
                b = existing_bundles.get(name)
                if b is not None and b["unique_id"] not in deleted:
                    refs.append(b["unique_id"])
                elif name in new_bundles:
                    refs.append(f"DES:{name}")
                else:
                    errors.append(f"covering '{key}': no bundle '{name}'")
            if not refs:
                errors.append(f"covering '{key}': no bundle to cover")
                continue
        elif cov is None:
            errors.append(f"new covering '{key}' needs bundles")
            continue

        def offset(name: str, item: dict[str, Any] = item) -> Any:
            return "" if item.get(name) is None else mm_to_coord(float(item[name]))

        covering_records.append(
            [
                "COVERING",
                "" if cov is None else cov["unique_id"],
                key if cov is None else str(item.get("new_designator") or ""),
                str(item.get("part_number") or ""),
                str(item.get("source_library") or ""),
                "" if item.get("thickness") is None else int(item["thickness"]),
                offset("start_offset_mm"),
                offset("end_offset_mm"),
                *(refs if refs is not None else []),
            ]
        )
        text_records += _text_records(
            f"DES:{key}" if cov is None else cov["unique_id"],
            item.get("texts"),
            f"covering '{key}'",
            errors,
            None if cov is None else cov.get("texts"),
        )

    label_records: list[list[Any]] = []
    for item in layout_labels or []:
        key = item_key(item)
        if ambiguous(key):
            continue
        lbl = existing_labels.get(key) if key else None
        if lbl is not None and lbl["unique_id"] in deleted:
            errors.append(f"layout label '{key}' is both edited and deleted")
            continue
        if lbl is None and ("x" not in item or "y" not in item or not item.get("text")):
            errors.append(f"new layout label '{key}' needs text, x and y")
            continue
        try:
            ori = (
                ""
                if item.get("orientation") is None
                else _orientation_index(item["orientation"])
            )
        except ValueError as e:
            errors.append(f"layout label '{key}': {e}")
            continue
        pos: tuple[Any, Any] = ("", "")
        if item.get("x") is not None or item.get("y") is not None:
            cur_x, cur_y = _mils(lbl["location"]) if lbl else (0, 0)
            pos = (int(item.get("x", cur_x)), int(item.get("y", cur_y)))
        label_records.append(
            [
                "LABEL",
                "" if lbl is None else lbl["unique_id"],
                key if lbl is None else str(item.get("new_designator") or ""),
                str(item.get("text") or ""),
                *pos,
                ori,
                "" if item.get("alignment") is None else int(item["alignment"]),
                _flag(item.get("show_only_first_line")),
                str(item.get("part_number") or ""),
                str(item.get("source_library") or ""),
            ]
        )
        text_records += _text_records(
            f"DES:{key}" if lbl is None else lbl["unique_id"],
            item.get("texts"),
            f"layout label '{key}'",
            errors,
            None if lbl is None else lbl.get("texts"),
        )

    wire_records: list[list[Any]] = []
    expect_lengths: dict[str, float | None] = {}
    wires_by_des = {w["designator"]: w for w in layout.get("wires", [])}
    for item in wires or []:
        des = item_key(item)
        wire = wires_by_des.get(des)
        if wire is None:
            errors.append(f"wire '{des}' not found in the layout drawing")
            continue
        if "length_mm" not in item:
            errors.append(f"wire '{des}': give length_mm (a number, or None)")
            continue
        if item["length_mm"] is None:
            coord = 0
        else:
            coord = mm_to_coord(float(item["length_mm"]))
            if not 0 < coord < 2**31:
                errors.append(
                    f"wire '{des}': length_mm must be between 0 and "
                    f"{coord_to_mm(2**31 - 1):.0f}"
                )
                continue
        wire_records.append(["WIRELEN", wire["unique_id"], coord])
        expect_lengths[des] = None if coord == 0 else coord_to_mm(coord)

    return {
        "records": [
            *delete_records,
            *connector_records,
            *view_records,
            *cp_records,
            *assign_records,
            *bundle_records,
            *covering_records,
            *label_records,
            *wire_records,
            # Last: texts of objects created above, and after connector moves
            *text_records,
        ],
        "errors": errors,
        "warnings": warnings,
        "expect": {
            "bundles": expect,
            "views": expect_views,
            "wire_lengths": expect_lengths,
        },
    }


def check_layout(
    layout: dict[str, Any],
    expect: dict[str, Any],
    before: dict[str, Any] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Problems and the state of the expected bundles in a re-read drawing:
    every expected bundle must exist with both ends on the expected connection
    points, and no bundle connected at both ends before the edit (before =
    parse_ldrdoc output of the drawing then) may have lost an end.

    expect["bundles"] maps a bundle's UniqueID (or designator) to its ends:
    connection point UniqueIDs, or DES:<designator> / a designator."""
    problems: list[str] = []
    found: list[dict[str, Any]] = []
    expected = expect.get("bundles", {})
    names = [b["designator"] for b in layout["bundles"] if b["designator"]]
    # Unnamed bundles share a designator ('B?'): name them by UniqueID too
    dup = {n for n in names if names.count(n) > 1}
    bundles = {b["unique_id"]: b for b in layout["bundles"]}
    bundles.update(
        {b["designator"]: b for b in layout["bundles"] if b["designator"] not in dup}
    )
    cp_names = {
        cp["unique_id"]: cp["designator"] for cp in layout.get("connection_points", [])
    }

    def label(b: dict[str, Any]) -> str:
        des = b["designator"]
        return f"{des} ({b['unique_id']})" if not des or des in dup else des

    def end_name(ref: str) -> str:
        return cp_names.get(ref) or ref.removeprefix("DES:")

    def same_end(ref: str, uid: Any, name: Any) -> bool:
        return ref == uid or ref.removeprefix("DES:") == name

    for old in (before or {}).get("bundles", []):
        b = bundles.get(old["unique_id"])
        if (
            b is not None
            and old["unique_id"] not in expected
            and (old["designator"] in dup or old["designator"] not in expected)
            and old.get("end1_connection_point")
            and old.get("end2_connection_point")
            and not (b.get("end1_connection_point") and b.get("end2_connection_point"))
        ):
            problems.append(
                f"bundle {label(b)}: lost its connection to a connection point"
            )
    wired = {w.get(end) for w in layout.get("wires", []) for end in ("end1", "end2")}
    for c in layout["connectors"]:
        loose = [
            p for p in c.get("unassigned_pins", []) if f"{c['designator']}.{p}" in wired
        ]
        if loose:
            problems.append(
                f"connector {c['designator']}: wired pins {', '.join(loose)} are held "
                "by no connection point (their wires cannot be routed)"
            )
    for key, ends in expected.items():
        b = bundles.get(key)
        if b is None:
            problems.append(f"bundle {key}: not in the saved drawing")
            continue
        got = [b.get("end1_connection_point"), b.get("end2_connection_point")]
        uids = [b.get("end1_uid"), b.get("end2_uid")]
        if None in got:
            problems.append(
                f"bundle {label(b)}: end not connected to a connection point ({got})"
            )
        elif all(ends) and not any(
            same_end(ends[0], uids[i], got[i])
            and same_end(ends[1], uids[1 - i], got[1 - i])
            for i in (0, 1)
        ):
            problems.append(
                f"bundle {label(b)}: connects {got}, "
                f"expected {[end_name(e) for e in ends]}"
            )
        found.append(
            {
                "designator": label(b),
                "from": got[0],
                "to": got[1],
                "length_mm": b["length"]["mm"],
                "drawn_length_mm": b["drawn_length"]["mm"],
                "length_set_manually": b["length_set_manually"],
                "wires": len(b["wires"]),
            }
        )
    conns = {c["designator"]: c for c in layout["connectors"]}
    for des, view in expect.get("views", {}).items():
        views = conns[des]["views"] if des in conns else []
        if (view == "physical") != _shows_physical(views):
            problems.append(f"connector {des}: view is not {view} in the saved drawing")
    wires = {w["designator"]: w for w in layout.get("wires", [])}
    for des, mm in expect.get("wire_lengths", {}).items():
        w = wires.get(des)
        if w is None:
            problems.append(f"wire {des}: not in the saved drawing")
        elif mm is None and w["length_type"] != "calculated":
            problems.append(f"wire {des}: length is still {w['length_type']}")
        elif mm is not None and (
            w["length_type"] != "mcad" or abs(w["fixed_length"]["mm"] - mm) > 0.01
        ):
            problems.append(f"wire {des}: length not set to {mm} mm")
    return problems, found


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
