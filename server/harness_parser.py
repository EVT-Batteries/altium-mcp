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

        # Altium strings are UTF-8 with fallback to Latin-1
        try:
            text = block.decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            text = block.decode("latin-1", errors="replace").rstrip("\x00")

        records.append(parse_pipe_record(text))

    return records


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

    # Record 0 is the stream header (|HEADER=...|Weight=...|...)
    header_rec = raw_records[0] if raw_records else {}
    records = raw_records[1:]  # 0-indexed matches internal Altium OwnerIndex

    connectors: dict[int, dict[str, Any]] = {}
    wires_dict: dict[int, dict[str, Any]] = {}
    splices_dict: dict[int, dict[str, Any]] = {}
    no_connects_list: list[dict[str, Any]] = []
    net_labels: list[dict[str, Any]] = []

    # First pass: collect connectors, wires, splices, parameters, pins, and net labels
    for idx, props in enumerate(records):
        rtype = props.get("RECORD")

        if rtype == "106":  # Harness Connector
            connectors[idx] = {
                "index": idx,
                "designator": "?",
                "lib_reference": props.get("LibReference", ""),
                "description": props.get("ComponentDescription", ""),
                "comment": props.get("ComponentDescription", ""),
                "location": {
                    "x": int(props.get("Location.X", 0)),
                    "y": int(props.get("Location.Y", 0)),
                },
                "unique_id": props.get("UniqueID", ""),
                "pins": [],
                "parameters": {},
            }
        elif rtype == "107":  # Harness Wire
            count = int(props.get("LocationCount", 0))
            pts = []
            for vi in range(1, count + 1):
                x_val = props.get(f"X{vi}") or props.get(f"X{vi}_Frac")
                y_val = props.get(f"Y{vi}") or props.get(f"Y{vi}_Frac")
                if x_val is not None and y_val is not None:
                    pts.extend([int(x_val), int(y_val)])
            wires_dict[idx] = {
                "index": idx,
                "designator": "?",
                "net": "?",
                "points": pts,
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
            sx = props.get("Location.X") or props.get("Location.X_Frac", 0)
            sy = props.get("Location.Y") or props.get("Location.Y_Frac", 0)
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
                    "x": int(sx),
                    "y": int(sy),
                },
                "unique_id": props.get("UniqueID", ""),
                "connected_wires_count": conn_count,
                "connected_wire_uids": conn_wire_uids,
            }
        elif rtype == "119":  # Harness No-Connect
            ncx = props.get("Location.X") or props.get("Location.X_Frac", 0)
            ncy = props.get("Location.Y") or props.get("Location.Y_Frac", 0)
            no_connects_list.append(
                {
                    "x": int(ncx),
                    "y": int(ncy),
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
                if name == "Designator" and val:
                    wires_dict[owner]["designator"] = val
                elif name == "Comment" and val:
                    wires_dict[owner]["net"] = val
                elif name:
                    wires_dict[owner]["parameters"][name] = val
            elif owner in splices_dict and name == "Designator" and val:
                splices_dict[owner]["designator"] = val
        elif rtype == "113":  # Harness Connector Pin
            owner = int(props.get("OwnerIndex", -1))
            if owner in connectors:
                x_base = int(props.get("Location.X", 0))
                y_base = int(props.get("Location.Y", 0))
                length = int(props.get("PinLength", 0))
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

                connectors[owner]["pins"].append(
                    {
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
                    }
                )
        elif rtype in [
            "4",
            "25",
        ]:  # Net Label (4: Harness/Text Label, 25: Standard Net Label)
            text = props.get("Text", "").strip()
            if text:
                net_labels.append(
                    {
                        "text": text,
                        "x": int(props.get("Location.X", 0)),
                        "y": int(props.get("Location.Y", 0)),
                        "color": props.get("Color", ""),
                        "unique_id": props.get("UniqueID", ""),
                    }
                )

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
        },
        "connectors": connector_list,
        "wires": wires_list,
        "splices": splices_list,
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
