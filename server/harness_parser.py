"""Parser for Altium Harness documents (.WirDoc, .LdrDoc) and projects (.PrjHar).

Enables reading, inspecting, and rule-checking Altium Harness Wiring Diagrams,
Layout Drawings, and Project files directly without requiring Altium GUI
compilation or licensing.
"""

from __future__ import annotations

import configparser
import struct
from pathlib import Path
from typing import Any

import olefile


def parse_pipe_record(text: str) -> dict[str, str]:
    """Parse an Altium pipe-delimited record into a dictionary.

    Format: |KEY1=VAL1|KEY2=VAL2|...
    """
    record: dict[str, str] = {}
    for part in text.split("|"):
        if "=" in part:
            key, value = part.split("=", 1)
            record[key.strip()] = value
    return record


def extract_ole_records(
    file_path: Path | str, stream_name: str = "FileHeader"
) -> list[dict[str, str]]:
    """Extract and parse length-prefixed pipe records from an Altium OLE stream.

    In Altium compound documents (.WirDoc, .LdrDoc, .SchDoc), streams like
    'FileHeader' contain sequential blocks structured as:
      [uint32 little-endian length][payload string (UTF-8 / Latin-1)][null-byte]
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if not olefile.isOleFile(str(path)):
        raise ValueError(f"File is not a valid Altium OLE compound document: {path}")

    with olefile.OleFileIO(str(path)) as ole:
        if not ole.exists(stream_name):
            raise KeyError(f"Stream '{stream_name}' not found in {path.name}")
        with ole.openstream(stream_name) as stream:
            data = stream.read()

    offset = 0
    records: list[dict[str, str]] = []

    while offset + 4 <= len(data):
        block_len = struct.unpack("<I", data[offset : offset + 4])[0]
        offset += 4
        if offset + block_len > len(data):
            break
        block = data[offset : offset + block_len]
        offset += block_len

        records.append(_decode_record(block.rstrip(b"\x00")))

    return records


def _decode_record(block: bytes) -> dict[str, str]:
    """Fields of one record, each decoded the way Altium wrote it.

    Plain fields are in the ANSI code page (cp1252), with '|' inside a value
    escaped as byte 0x8E. Non-ASCII text also comes as a '%UTF8%<key>' field
    holding the UTF-8 form (there '|' is escaped as U+00A6 '¦'), which wins
    over the plain one. Decoding the whole record at once mangles one or the
    other.
    """
    fields = parse_pipe_record(block.decode("latin-1"))  # byte-preserving
    record: dict[str, str] = {}
    utf8: dict[str, str] = {}
    for key, value in fields.items():
        raw = value.encode("latin-1")
        if key.startswith("%UTF8%"):
            utf8[key[6:]] = raw.decode("utf-8", errors="replace").replace("¦", "|")
        else:
            record[key] = raw.replace(b"\x8e", b"|").decode("cp1252", errors="replace")
    record.update(utf8)
    return record


def _coord(props: dict[str, str], key: str) -> int | float:
    """Coordinate in DXP units (10 mils): integer field plus its 1/100000 _Frac.

    Altium omits either field when it is zero, so both are optional.
    """
    whole = int(props.get(key) or 0)
    frac = int(props.get(f"{key}_Frac") or 0)
    return whole + frac / 100000 if frac else whole


# AssociatedObjectType of a cavity container (RECORD 131); omitted for crimps
CAVITY_PART_TYPES = {0: "crimp", 1: "seal", 2: "plug", 3: "other"}
# Wire-crossing harness objects: RECORD -> output key
RECT_OBJECTS = {"117": "shields", "118": "twists", "123": "cables"}


def _text(props: dict[str, str], key: str) -> str:
    """Field value, with Altium's '*' (not set) as empty."""
    value = props.get(key, "")
    return "" if value == "*" else value


def _uid_list(props: dict[str, str], count_key: str, item_key: str) -> list[str]:
    count = int(props.get(count_key, 0) or 0)
    return [
        props[f"{item_key}{i}"] for i in range(1, count + 1) if f"{item_key}{i}" in props
    ]


def shield_pin(shield: dict[str, Any]) -> dict[str, float] | None:
    """Connection pin of a "shield with connection" (style 1), where a drain
    wire ends: the middle of the bottom edge, turned with the rotation
    (1 right edge, 2 top, 3 left). None for a plain shield."""
    if shield.get("style") != 1:
        return None
    x1, y1, x2, y2 = shield["x1"], shield["y1"], shield["x2"], shield["y2"]
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    return [
        {"x": cx, "y": y1},
        {"x": x2, "y": cy},
        {"x": cx, "y": y2},
        {"x": x1, "y": cy},
    ][shield.get("rotation", 0) % 4]


def parse_wirdoc(file_path: Path | str) -> dict[str, Any]:
    """Parse an Altium Harness Wiring Diagram (.WirDoc) or Layout Drawing (.LdrDoc).

    Extracts placed connectors (RECORD=106), connector pins (RECORD=113),
    parameters/designators (RECORD=34 and RECORD=41), and net labels (RECORD=4).

    Returns:
        Structured dictionary containing connectors, pins, nets, and summary.
    """
    raw_records = extract_ole_records(file_path, "FileHeader")
    if not raw_records:
        return {"error": "Empty or invalid FileHeader stream", "connectors": []}
    return parse_wirdoc_records(raw_records, file_path)


def parse_wirdoc_records(
    raw_records: list[dict[str, str]], file_path: Path | str
) -> dict[str, Any]:
    """parse_wirdoc on already extracted records (record 0 is the header)."""
    # Record 0 is the stream header (|HEADER=...|Weight=...|...)
    header_rec = raw_records[0] if raw_records else {}
    records = raw_records[1:]  # 0-indexed matches internal Altium OwnerIndex

    connectors: dict[int, dict[str, Any]] = {}
    wires_dict: dict[int, dict[str, Any]] = {}
    splices_dict: dict[int, dict[str, Any]] = {}
    no_connects_list: list[dict[str, Any]] = []
    net_labels: list[dict[str, Any]] = []
    pins_by_index: dict[int, dict[str, Any]] = {}
    # Components nested in pin cavities (RECORD 106 owned by a RECORD 131
    # cavity container owned by the pin) or attached to a connector/splice
    # (106 in a RECORD 125 container), resolved after the first pass
    cavity_parts: list[tuple[int, int, dict[str, str]]] = []
    # Parameters of records not otherwise collected (e.g. cavity parts)
    other_params: dict[int, dict[str, str]] = {}
    # Twists, shields, cables (rectangles across wires) and wire breaks
    rect_objects: dict[int, dict[str, Any]] = {}
    wire_breaks: list[dict[str, Any]] = []

    # First pass: collect connectors, wires, splices, parameters, pins, and net labels
    for idx, props in enumerate(records):
        rtype = props.get("RECORD")

        if rtype == "106" and "OwnerIndex" in props:
            # Crimp/seal/plug in a cavity or an associated part, not a placed connector
            cavity_parts.append((int(props["OwnerIndex"]), idx, props))
        elif rtype in RECT_OBJECTS and "OwnerIndex" not in props:
            xa, xb = _coord(props, "Location.X"), _coord(props, "Corner.X")
            ya, yb = _coord(props, "Location.Y"), _coord(props, "Corner.Y")
            obj: dict[str, Any] = {
                "kind": RECT_OBJECTS[rtype],
                "x1": min(xa, xb),
                "y1": min(ya, yb),
                "x2": max(xa, xb),
                "y2": max(ya, yb),
                "designator": "",
                "rotation": int(props.get("Rotation", 0) or 0),
                "unique_id": props.get("UniqueID", ""),
                "wire_uids": _uid_list(
                    props, "ConnectedWiresUniqueIdsCount", "ConnectedWireUniqueId"
                ),
                "parameters": {},
            }
            if rtype == "117":
                obj["style"] = int(props.get("Style", 0) or 0)
                obj["drain_wire_uids"] = _uid_list(
                    props, "ConnectedPinWiresUniqueIdsCount", "ConnectedPinWireUniqueId"
                )
            if rtype in ("117", "123"):
                obj["comment"] = ""
            if rtype == "123":
                obj["part_number"] = _text(props, "DesignItemId")
                obj["source_library"] = _text(props, "SourceLibraryName")
            rect_objects[idx] = obj
        elif rtype == "130":  # Wire break (cross-sheet connector)
            wire_breaks.append(
                {
                    "designator": props.get("Text", ""),
                    "x": _coord(props, "Location.X"),
                    "y": _coord(props, "Location.Y"),
                    "unique_id": props.get("UniqueID", ""),
                    "wire_uid": props.get("ConnectedObjectUniqueId", ""),
                    "wire": None,
                }
            )
        elif rtype == "106":  # Harness Connector
            connectors[idx] = {
                "index": idx,
                "designator": "?",
                "lib_reference": props.get("LibReference", ""),
                "description": props.get("ComponentDescription", ""),
                "comment": props.get("ComponentDescription", ""),
                "location": {
                    "x": _coord(props, "Location.X"),
                    "y": _coord(props, "Location.Y"),
                },
                "unique_id": props.get("UniqueID", ""),
                "pins": [],
                "parameters": {},
                "associated_parts": [],
            }
        elif rtype == "107":  # Harness Wire
            count = int(props.get("LocationCount", 0))
            pts = []
            for vi in range(1, count + 1):
                pts.extend([_coord(props, f"X{vi}"), _coord(props, f"Y{vi}")])
            wires_dict[idx] = {
                "index": idx,
                "designator": "?",
                "net": "?",
                "points": pts,
                "design_item_id": props.get("DesignItemId", ""),
                "source_library": props.get("SourceLibraryName", ""),
                "unique_id": props.get("UniqueID", ""),
                "end_vertex1_conn_uid": props.get(
                    "EndVertex1ConnectedObjectUniqueID", ""
                ),
                "end_vertex2_conn_uid": props.get(
                    "EndVertex2ConnectedObjectUniqueID", ""
                ),
                "parameters": {},
            }
        elif rtype == "108":  # Harness Splice
            sx = _coord(props, "Location.X")
            sy = _coord(props, "Location.Y")
            conn_count = int(props.get("ConnectedWiresUniqueIdsCount", 0))
            conn_wire_uids = [
                props[f"ConnectedWireUniqueId{ci}"]
                for ci in range(1, conn_count + 1)
                if f"ConnectedWireUniqueId{ci}" in props
            ]
            splices_dict[idx] = {
                "index": idx,
                "designator": "?",
                "location": {
                    "x": sx,
                    "y": sy,
                },
                "unique_id": props.get("UniqueID", ""),
                "connected_wires_count": conn_count,
                "connected_wire_uids": conn_wire_uids,
                "associated_parts": [],
            }
        elif rtype == "119":  # Harness No-Connect
            no_connects_list.append(
                {
                    "x": _coord(props, "Location.X"),
                    "y": _coord(props, "Location.Y"),
                    "name": props.get("Text", ""),
                    "unique_id": props.get("UniqueID", ""),
                }
            )
        elif rtype in ["34", "41"]:  # Component / Wire / Splice Parameter
            owner = int(props.get("OwnerIndex", -1))
            name = props.get("Name", "")
            val = props.get("Text", "")
            if owner in connectors:
                if name == "Designator" and val:
                    connectors[owner]["designator"] = val
                elif name:
                    if name != "PinUniqueId":
                        connectors[owner]["parameters"][name] = val
                    if name == "Comment" and val:
                        connectors[owner]["comment"] = val
            elif owner in wires_dict:
                # Visible label positions (hidden ones need no clearance)
                if (
                    name in ("Designator", "Comment")
                    and "Location.X" in props
                    and props.get("IsHidden") != "T"
                ):
                    wires_dict[owner][f"{name.lower()}_pos"] = {
                        "x": _coord(props, "Location.X"),
                        "y": _coord(props, "Location.Y"),
                    }
                if name == "Designator" and val:
                    wires_dict[owner]["designator"] = val
                elif name == "Comment" and val:
                    wires_dict[owner]["net"] = val
                elif name:
                    wires_dict[owner]["parameters"][name] = val
            elif owner in splices_dict and name == "Designator" and val:
                splices_dict[owner]["designator"] = val
            elif owner in rect_objects and name:
                obj = rect_objects[owner]
                value = "" if val == "*" else val
                if name in ("Designator", "Comment"):
                    obj[name.lower()] = value
                else:
                    obj["parameters"][name] = value
            elif name and rtype == "41":
                other_params.setdefault(owner, {})[name] = val
        elif rtype == "113":  # Harness Connector Pin
            owner = int(props.get("OwnerIndex", -1))
            if owner in connectors:
                x_base = _coord(props, "Location.X")
                y_base = _coord(props, "Location.Y")
                length = _coord(props, "PinLength")
                cong = int(props.get("PinConglomerate", 0))
                orient = cong & 0x03
                if orient == 0:
                    hx, hy = x_base + length, y_base
                elif orient == 1:
                    hx, hy = x_base, y_base + length
                elif orient == 2:
                    hx, hy = x_base - length, y_base
                else:
                    hx, hy = x_base, y_base - length

                conn_count = int(props.get("ConnectedWiresUniqueIdsCount", 0))
                conn_wire_uids = [
                    props[f"ConnectedWireUniqueId{ci}"]
                    for ci in range(1, conn_count + 1)
                    if f"ConnectedWireUniqueId{ci}" in props
                ]

                pins_by_index[idx] = {
                        "designator": props.get("Designator", ""),
                        "name": props.get("Name", ""),
                        "electrical": int(props.get("Electrical", 0)),
                        "location": {
                            "x": x_base,
                            "y": y_base,
                        },
                        "hot_end": {
                            "x": hx,
                            "y": hy,
                        },
                        "length": length,
                        "orientation": orient,
                        "unique_id": props.get("UniqueID", ""),
                        "connected_wire_uids": conn_wire_uids,
                        "connected_wire": None,
                        "net": None,
                        "is_no_connect": False,
                        "no_connect_id": None,
                        "cavity_parts": [],
                    }
                connectors[owner]["pins"].append(pins_by_index[idx])
        elif rtype in [
            "4",
            "25",
        ]:  # Net Label (4: Harness/Text Label, 25: Standard Net Label)
            text = props.get("Text", "").strip()
            # Text owned by a connector (e.g. its title) belongs to it, not the sheet
            if text and "OwnerIndex" not in props:
                net_labels.append(
                    {
                        "text": text,
                        "x": _coord(props, "Location.X"),
                        "y": _coord(props, "Location.Y"),
                        "color": props.get("Color", ""),
                        "unique_id": props.get("UniqueID", ""),
                    }
                )

    # Cavity contents: 106 -> 131 cavity container (AssociatedObjectType =
    # slot) -> 113 pin; associated parts: 106 -> 125 -> connector 106 or
    # splice 108. Altium writes each crimp twice with the same UniqueID;
    # keep one.
    for container_idx, part_idx, props in cavity_parts:
        container = records[container_idx] if container_idx < len(records) else {}
        params = {
            k: v
            for k, v in other_params.get(part_idx, {}).items()
            if k != "Comment" and v != "*"
        }
        owner_idx = int(container.get("OwnerIndex", -1))
        if container.get("RECORD") == "125":
            owner = connectors.get(owner_idx) or splices_dict.get(owner_idx)
            parts = owner["associated_parts"] if owner else None
            part_type = None
        else:
            pin = pins_by_index.get(owner_idx)
            parts = pin["cavity_parts"] if pin else None
            part_type = CAVITY_PART_TYPES.get(
                int(container.get("AssociatedObjectType", 0) or 0), "other"
            )
        if parts is None:
            continue
        uid = props.get("UniqueID", "")
        known = next((p for p in parts if p["unique_id"] == uid), None)
        if known is not None:  # second copy of the part, which holds its parameters
            known["parameters"].update(params)
            continue
        part = {
            "part_number": _text(props, "DesignItemId"),
            "source_library": _text(props, "SourceLibraryName"),
            "source_table": _text(props, "DatabaseTableName"),
            "description": _text(props, "ComponentDescription"),
            "unique_id": uid,
            "parameters": params,
        }
        if part_type is not None:
            part = {"type": part_type, **part}
        parts.append(part)

    # Wires linked to twists/shields/cables and wire breaks, by designator
    wire_des_by_uid = {w["unique_id"]: w["designator"] for w in wires_dict.values()}
    rect_lists: dict[str, list[dict[str, Any]]] = {k: [] for k in RECT_OBJECTS.values()}
    for obj in rect_objects.values():
        obj["wires"] = [wire_des_by_uid.get(u, u) for u in obj.pop("wire_uids")]
        if "drain_wire_uids" in obj:
            obj["drain_wires"] = [
                wire_des_by_uid.get(u, u) for u in obj.pop("drain_wire_uids")
            ]
            obj["pin"] = shield_pin(obj)
        rect_lists[obj.pop("kind")].append(obj)
    for brk in wire_breaks:
        wire = next(
            (
                w
                for w in wires_dict.values()
                if w["unique_id"] == brk["wire_uid"]
                or brk["unique_id"] in (w["end_vertex1_conn_uid"], w["end_vertex2_conn_uid"])
                or (
                    len(w["points"]) >= 4
                    and (brk["x"], brk["y"])
                    in ((w["points"][0], w["points"][1]), (w["points"][-2], w["points"][-1]))
                )
            ),
            None,
        )
        brk["wire"] = wire["designator"] if wire else None
        brk.pop("wire_uid")

    # Resolve comment if "=PartNumber" or empty
    for c in connectors.values():
        if c.get("comment") == "=PartNumber" or not c.get("comment"):
            c["comment"] = c["parameters"].get(
                "PartNumber",
                c["parameters"].get("Manufacturer Part Number 1", c["description"]),
            )

    # Second pass: Associate No-Connect directives to pins
    for c in connectors.values():
        for pin in c["pins"]:
            hx, hy = pin["hot_end"]["x"], pin["hot_end"]["y"]
            for nc in no_connects_list:
                if (nc["x"] == hx and nc["y"] == hy) or (
                    nc["unique_id"] and nc["unique_id"] == pin["unique_id"]
                ):
                    pin["is_no_connect"] = True
                    pin["no_connect_id"] = nc["name"]
                    break

    # Third pass: Associate wires to pins and splices
    pin_by_uid = {}
    pin_by_hotend = {}
    for c in connectors.values():
        for pin in c["pins"]:
            pin_by_uid[pin["unique_id"]] = (c, pin)
            pin_by_hotend[(pin["hot_end"]["x"], pin["hot_end"]["y"])] = (c, pin)

    splice_by_uid = {}
    splice_by_loc = {}
    for s in splices_dict.values():
        splice_by_uid[s["unique_id"]] = s
        splice_by_loc[(s["location"]["x"], s["location"]["y"])] = s

    for w in wires_dict.values():
        pts = w["points"]
        if len(pts) >= 4:
            e1 = (pts[0], pts[1])
            e2 = (pts[-2], pts[-1])
            w_net = w["net"] if w["net"] != "?" else None

            # Endpoint 1
            matched_pin1 = pin_by_uid.get(
                w["end_vertex1_conn_uid"]
            ) or pin_by_hotend.get(e1)
            if matched_pin1:
                _, p1 = matched_pin1
                p1["connected_wire"] = w["designator"]
                if w_net and not p1["net"]:
                    p1["net"] = w_net

            # Endpoint 2
            matched_pin2 = pin_by_uid.get(
                w["end_vertex2_conn_uid"]
            ) or pin_by_hotend.get(e2)
            if matched_pin2:
                _, p2 = matched_pin2
                p2["connected_wire"] = w["designator"]
                if w_net and not p2["net"]:
                    p2["net"] = w_net

    # Fourth pass: Trace and propagate nets across splices
    for s in splices_dict.values():
        s_loc = (s["location"]["x"], s["location"]["y"])
        s_uid = s["unique_id"]
        splice_wires = []
        for w in wires_dict.values():
            pts = w["points"]
            if len(pts) >= 4:
                e1 = (pts[0], pts[1])
                e2 = (pts[-2], pts[-1])
                if (
                    w["end_vertex1_conn_uid"] == s_uid
                    or w["end_vertex2_conn_uid"] == s_uid
                    or e1 == s_loc
                    or e2 == s_loc
                    or w["unique_id"] in s["connected_wire_uids"]
                ):
                    splice_wires.append(w)

        splice_pins = []
        splice_net_names = []
        for w in splice_wires:
            if w["net"] != "?":
                splice_net_names.append(w["net"])
            for pt in [
                (w["points"][0], w["points"][1]),
                (w["points"][-2], w["points"][-1]),
            ]:
                if pt in pin_by_hotend:
                    splice_pins.append(pin_by_hotend[pt][1])

        chosen_net = None
        if splice_net_names:
            chosen_net = splice_net_names[0]
        else:
            for p in splice_pins:
                if p["net"]:
                    chosen_net = p["net"]
                    break

        if chosen_net:
            for p in splice_pins:
                p["net"] = chosen_net

    # Fifth pass: Spatial fallback for net labels touching pins
    for c in connectors.values():
        for pin in c["pins"]:
            if not pin["net"]:
                hx, hy = pin["hot_end"]["x"], pin["hot_end"]["y"]
                px, py = pin["location"]["x"], pin["location"]["y"]
                candidates = [
                    nl["text"]
                    for nl in net_labels
                    if (abs(nl["y"] - hy) <= 8 and abs(nl["x"] - hx) <= 45)
                    or (abs(nl["y"] - py) <= 8 and abs(nl["x"] - px) <= 45)
                ]
                if candidates:
                    pin["net"] = candidates[0]

    connector_list = sorted(connectors.values(), key=lambda c: c["designator"])
    total_pins = sum(len(c["pins"]) for c in connector_list)
    connected_pins = sum(
        sum(
            1
            for p in c["pins"]
            if p["net"] is not None or p.get("connected_wire") is not None
        )
        for c in connector_list
    )

    # Build net-to-pin connectivity mapping
    nets_to_pins: dict[str, list[dict[str, Any]]] = {}
    for c in connector_list:
        for pin in c["pins"]:
            if pin["net"]:
                net_name = pin["net"]
                if net_name not in nets_to_pins:
                    nets_to_pins[net_name] = []
                nets_to_pins[net_name].append(
                    {
                        "connector": c["designator"],
                        "pin": pin["designator"],
                        "pin_name": pin["name"],
                        "electrical": pin["electrical"],
                    }
                )

    unique_nets = sorted({nl["text"] for nl in net_labels}.union(nets_to_pins.keys()))

    wires_list = sorted(wires_dict.values(), key=lambda w: w["designator"])
    splices_list = sorted(splices_dict.values(), key=lambda s: s["designator"])

    return {
        "file_path": str(Path(file_path).resolve()),
        "header": {
            "version": header_rec.get("HEADER", ""),
            "minor_version": header_rec.get("MinorVersion", ""),
            "unique_id": header_rec.get("UniqueID", ""),
        },
        "summary": {
            "connector_count": len(connector_list),
            "wire_count": len(wires_list),
            "splice_count": len(splices_list),
            "no_connect_count": len(no_connects_list),
            "total_pins": total_pins,
            "connected_pins": connected_pins,
            "net_label_count": len(net_labels),
            "unique_net_count": len(unique_nets),
            "twist_count": len(rect_lists["twists"]),
            "shield_count": len(rect_lists["shields"]),
            "cable_count": len(rect_lists["cables"]),
            "wire_break_count": len(wire_breaks),
        },
        "connectors": connector_list,
        "wires": wires_list,
        "splices": splices_list,
        "twists": rect_lists["twists"],
        "shields": rect_lists["shields"],
        "cables": sorted(rect_lists["cables"], key=lambda c: c["designator"]),
        "wire_breaks": wire_breaks,
        "no_connects": no_connects_list,
        "net_labels": net_labels,
        "nets": unique_nets,
        "net_connections": nets_to_pins,
    }


def parse_prjhar(file_path: Path | str) -> dict[str, Any]:
    """Parse an Altium Harness Project file (.PrjHar).

    Extracts project settings, parameters, and associated design documents.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Project not found: {path}")

    config = configparser.ConfigParser(interpolation=None, strict=False)
    # Altium project files are Windows UTF-8 with BOM or ANSI/Latin-1
    try:
        config.read(str(path), encoding="utf-8-sig")
    except UnicodeDecodeError:
        config.read(str(path), encoding="latin-1")

    documents: list[dict[str, str]] = []
    parameters: dict[str, str] = {}

    for section in config.sections():
        if section.lower().startswith("document"):
            doc_path = config.get(section, "DocumentPath", fallback="")
            if doc_path:
                ext = Path(doc_path).suffix.lower()
                kind_map = {
                    ".wirdoc": "HarnessWiringDiagram",
                    ".ldrdoc": "HarnessLayoutDrawing",
                    ".bomdoc": "BOM",
                    ".hardwf": "HarnessDrawing",
                }
                documents.append(
                    {
                        "path": doc_path,
                        "kind": kind_map.get(ext, "Unknown"),
                    }
                )
        elif section.lower().startswith("parameter"):
            name = config.get(section, "Name", fallback="")
            val = config.get(section, "Value", fallback="")
            if name:
                parameters[name] = val

    return {
        "project_path": str(path.resolve()),
        "parameters": parameters,
        "documents": documents,
        "document_count": len(documents),
    }


def find_workspace_harness(start_dir: Path | str | None = None) -> Path | None:
    """Find the most relevant .PrjHar or .WirDoc file in the workspace or search paths."""
    start = Path(start_dir) if start_dir else Path.cwd()
    curr = start.resolve()
    for _ in range(5):
        # Look for .PrjHar first
        prjhars = list(curr.glob("*.PrjHar")) + list(curr.glob("*/*.PrjHar"))
        if prjhars:
            return prjhars[0]
        # Look for .WirDoc
        wirdocs = list(curr.glob("*.WirDoc")) + list(curr.glob("*/*.WirDoc"))
        if wirdocs:
            return wirdocs[0]
        if curr.parent == curr:
            break
        curr = curr.parent
    return None


def get_harness_wiring_docs(file_path: Path | str) -> list[Path]:
    """Get all .WirDoc Path objects from a .PrjHar or single .WirDoc."""
    p = Path(file_path)
    if not p.exists():
        return []
    if p.suffix.lower() == ".prjhar":
        prj = parse_prjhar(p)
        docs: list[Path] = []
        for doc in prj["documents"]:
            if doc["kind"] == "HarnessWiringDiagram":
                w_path = p.parent / doc["path"]
                if w_path.exists():
                    docs.append(w_path)
        return docs
    elif p.suffix.lower() in [".wirdoc", ".ldrdoc"]:
        return [p]
    return []


def get_harness_components(
    file_path: Path | str, include_parameters: bool = False
) -> list[dict[str, Any]]:
    """Get standardized component list from a .WirDoc or .PrjHar file.

    Compatible with the output format of `get_project_components`.
    """
    path = Path(file_path)
    wiring_docs = get_harness_wiring_docs(path)
    all_components: list[dict[str, Any]] = []

    for w_path in wiring_docs:
        data = parse_wirdoc(w_path)
        for c in data.get("connectors", []):
            comp: dict[str, Any] = {
                "designator": c["designator"],
                "comment": c["comment"],
                "description": c["description"],
                "lib_reference": c["lib_reference"],
                "sheet": w_path.name,
                "document_path": str(w_path.resolve()),
                "location": c["location"],
                "pin_count": len(c["pins"]),
                "pins": c["pins"],
            }
            if include_parameters:
                comp["parameters"] = c.get("parameters", {})
            all_components.append(comp)

    return all_components


def get_harness_designators(file_path: Path | str) -> list[str]:
    """Get all component designators in a harness document or project."""
    components = get_harness_components(file_path, include_parameters=False)
    return [comp["designator"] for comp in components if comp.get("designator")]


def get_harness_component_data(
    file_path: Path | str, cmp_designators: list[str] | None = None
) -> dict[str, Any]:
    """Get detailed component data for requested designators (or all).

    Compatible with `get_component_data`.
    """
    all_components = get_harness_components(file_path, include_parameters=True)
    if not cmp_designators:
        return {"components": all_components}

    matched: list[dict[str, Any]] = []
    missing: list[str] = []
    comp_map = {c["designator"]: c for c in all_components}

    for des in cmp_designators:
        if des in comp_map:
            matched.append(comp_map[des])
        else:
            missing.append(des)

    res: dict[str, Any] = {"components": matched}
    if missing:
        res["missing_designators"] = missing
    return res


def get_harness_schematic_data(
    file_path: Path | str, cmp_designators: list[str] | None = None
) -> list[dict[str, Any]]:
    """Get schematic component data for requested designators (or all).

    Compatible with `get_schematic_data`.
    """
    all_components = get_harness_components(file_path, include_parameters=True)
    if not cmp_designators:
        return all_components

    des_set = set(cmp_designators)
    return [c for c in all_components if c["designator"] in des_set]


def get_harness_component_pins(
    file_path: Path | str, cmp_designators: list[str] | None = None
) -> list[dict[str, Any]]:
    """Get pin data for requested components (or all).

    Compatible with `get_component_pins`.
    """
    all_components = get_harness_components(file_path, include_parameters=False)
    des_set = set(cmp_designators) if cmp_designators else None

    result: list[dict[str, Any]] = []
    for c in all_components:
        if des_set is not None and c["designator"] not in des_set:
            continue
        result.append(
            {
                "designator": c["designator"],
                "x": c["location"]["x"],
                "y": c["location"]["y"],
                "rotation": 0,
                "layer": "HarnessWiringDiagram",
                "sheet": c["sheet"],
                "pins": c["pins"],
            }
        )
    return result


def get_harness_nets(file_path: Path | str) -> list[str]:
    """Get all unique net names across a harness project or document."""
    wiring_docs = get_harness_wiring_docs(file_path)
    nets: set[str] = set()
    for w_path in wiring_docs:
        data = parse_wirdoc(w_path)
        nets.update(data.get("nets", []))
    return sorted(nets)


def get_harness_net_connections(
    file_path: Path | str, net_names: list[str] | None = None
) -> list[dict[str, Any]]:
    """Get pin connections for specified nets (or all nets).

    Compatible with `get_net_connections`.
    """
    wiring_docs = get_harness_wiring_docs(file_path)
    all_connections: dict[str, list[dict[str, Any]]] = {}

    for w_path in wiring_docs:
        data = parse_wirdoc(w_path)
        for net, pins in data.get("net_connections", {}).items():
            if net not in all_connections:
                all_connections[net] = []
            for p in pins:
                all_connections[net].append(
                    {
                        "component": p["connector"],
                        "designator": p["connector"],
                        "pin": p["pin"],
                        "pad": p["pin"],
                        "pin_name": p["pin_name"],
                        "sheet": w_path.name,
                    }
                )

    net_filter = set(net_names) if net_names else None
    results: list[dict[str, Any]] = []

    for net_name in sorted(all_connections.keys()):
        if net_filter is not None and net_name not in net_filter:
            continue
        results.append(
            {
                "net": net_name,
                "pins": all_connections[net_name],
                "pin_count": len(all_connections[net_name]),
            }
        )

    return results


def get_harness_property_names(file_path: Path | str) -> list[str]:
    """Get all property and parameter names available across harness components."""
    components = get_harness_components(file_path, include_parameters=True)
    prop_names: set[str] = set()
    for comp in components:
        for k in comp:
            if k != "parameters":
                prop_names.add(k)
        for pk in comp.get("parameters", {}):
            prop_names.add(pk)
    return sorted(prop_names)


def get_harness_property_values(
    file_path: Path | str, property_name: str
) -> list[dict[str, Any]]:
    """Get the value of a specific property for all harness components."""
    components = get_harness_components(file_path, include_parameters=True)
    values: list[dict[str, Any]] = []
    for comp in components:
        des = comp["designator"]
        val = None
        if property_name in comp:
            val = comp[property_name]
        elif property_name in comp.get("parameters", {}):
            val = comp["parameters"][property_name]

        if val is not None:
            values.append(
                {
                    "designator": des,
                    property_name: val,
                }
            )
    return values


def get_harness_erc_violations(file_path: Path | str) -> dict[str, Any]:
    """Perform rule check on harness documents (.WirDoc / .PrjHar).

    Detects:
    - Unannotated designators (e.g. 'P?')
    - Duplicate designators
    - Unconnected pins
    - Single-pin nets
    - Missing component references (no LibReference or PartNumber)
    """
    wiring_docs = get_harness_wiring_docs(file_path)
    violations: list[dict[str, str]] = []

    seen_designators: dict[str, str] = {}
    all_net_connections: dict[str, list[str]] = {}

    for doc in wiring_docs:
        data = parse_wirdoc(doc)
        for c in data.get("connectors", []):
            des = c["designator"]
            # 1. Unannotated designator check
            if "?" in des:
                violations.append(
                    {
                        "level": "Error",
                        "type": "Unannotated Designator",
                        "detail": (
                            f"Component has unannotated designator '{des}' in {doc.name}"
                        ),
                    }
                )
            # 2. Duplicate designator check
            elif des in seen_designators:
                violations.append(
                    {
                        "level": "Error",
                        "type": "Duplicate Designator",
                        "detail": (
                            f"Duplicate designator '{des}' found in {doc.name} "
                            f"(also in {seen_designators[des]})"
                        ),
                    }
                )
            else:
                seen_designators[des] = doc.name

            # 3. Missing part number / lib reference check
            if not c.get("lib_reference") and not c.get("parameters", {}).get(
                "PartNumber"
            ):
                violations.append(
                    {
                        "level": "Warning",
                        "type": "Missing Component Reference",
                        "detail": (
                            f"Component '{des}' has no library reference or "
                            f"PartNumber in {doc.name}"
                        ),
                    }
                )

            # Check pins
            for pin in c.get("pins", []):
                p_des = pin["designator"]
                p_net = pin.get("net")
                is_nc = pin.get("is_no_connect", False)
                has_wire = bool(pin.get("connected_wire"))
                if not p_net and not is_nc and not has_wire:
                    violations.append(
                        {
                            "level": "Warning",
                            "type": "Unconnected Pin",
                            "detail": (
                                f"Unconnected pin {des}.{p_des} "
                                f"({pin.get('name', '')}) in {doc.name}"
                            ),
                        }
                    )
                elif p_net:
                    if p_net not in all_net_connections:
                        all_net_connections[p_net] = []
                    all_net_connections[p_net].append(f"{des}.{p_des}")

    # 4. Single pin nets check
    for net, pins in all_net_connections.items():
        if len(pins) == 1:
            violations.append(
                {
                    "level": "Warning",
                    "type": "Single Pin Net",
                    "detail": f"Net '{net}' has only 1 connection ({pins[0]})",
                }
            )

    fatal_count = sum(1 for v in violations if v["level"] == "Fatal")
    error_count = sum(1 for v in violations if v["level"] == "Error")
    warning_count = sum(1 for v in violations if v["level"] == "Warning")

    return {
        "total": len(violations),
        "fatal": fatal_count,
        "errors": error_count,
        "warnings": warning_count,
        "violations": violations,
        "source": "harness_erc",
    }
