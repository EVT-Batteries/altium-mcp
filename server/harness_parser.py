"""Parser for Altium Harness documents (.WirDoc, .LdrDoc) and projects (.PrjHar).

Enables reading and inspecting Altium Harness Wiring Diagrams, Layout Drawings,
and Project files directly without requiring Altium GUI compilation or licensing.
"""
from __future__ import annotations

import configparser
from pathlib import Path
import struct
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


def extract_ole_records(file_path: Path | str, stream_name: str = "FileHeader") -> list[dict[str, str]]:
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
    """Parse an Altium Harness Wiring Diagram (.WirDoc).

    Extracts placed connectors (RECORD=106), connector pins (RECORD=113),
    parameters/designators (RECORD=34), and net labels (RECORD=4).

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
    net_labels: list[dict[str, Any]] = []

    # First pass: collect connectors, parameters, pins, and net labels
    for idx, props in enumerate(records):
        rtype = props.get("RECORD")

        if rtype == "106":  # Harness Connector
            connectors[idx] = {
                "index": idx,
                "designator": "?",
                "lib_reference": props.get("LibReference", ""),
                "description": props.get("ComponentDescription", ""),
                "location": {
                    "x": int(props.get("Location.X", 0)),
                    "y": int(props.get("Location.Y", 0)),
                },
                "unique_id": props.get("UniqueID", ""),
                "pins": [],
            }
        elif rtype == "34":  # Component Parameter (Designator, Comment, etc.)
            owner = int(props.get("OwnerIndex", -1))
            name = props.get("Name", "")
            val = props.get("Text", "")
            if owner in connectors and name == "Designator":
                connectors[owner]["designator"] = val
        elif rtype == "113":  # Harness Connector Pin
            owner = int(props.get("OwnerIndex", -1))
            if owner in connectors:
                connectors[owner]["pins"].append({
                    "designator": props.get("Designator", ""),
                    "name": props.get("Name", ""),
                    "electrical": int(props.get("Electrical", 0)),
                    "location": {
                        "x": int(props.get("Location.X", 0)),
                        "y": int(props.get("Location.Y", 0)),
                    },
                    "unique_id": props.get("UniqueID", ""),
                    "net": None,
                })
        elif rtype == "4":  # Net Label
            text = props.get("Text", "").strip()
            if text:
                net_labels.append({
                    "text": text,
                    "x": int(props.get("Location.X", 0)),
                    "y": int(props.get("Location.Y", 0)),
                    "color": props.get("Color", ""),
                    "unique_id": props.get("UniqueID", ""),
                })

    # Second pass: Associate net labels to pins by spatial proximity
    # In Altium wiring diagrams, net labels are attached directly at pin wire endpoints
    for c in connectors.values():
        for pin in c["pins"]:
            px, py = pin["location"]["x"], pin["location"]["y"]
            candidates = [
                nl["text"]
                for nl in net_labels
                if abs(nl["y"] - py) <= 8 and abs(nl["x"] - px) <= 45
            ]
            if candidates:
                pin["net"] = candidates[0]

    connector_list = sorted(connectors.values(), key=lambda c: c["designator"])
    total_pins = sum(len(c["pins"]) for c in connector_list)
    connected_pins = sum(
        sum(1 for p in c["pins"] if p["net"] is not None)
        for c in connector_list
    )

    return {
        "file_path": str(Path(file_path).resolve()),
        "header": {
            "version": header_rec.get("HEADER", ""),
            "minor_version": header_rec.get("MinorVersion", ""),
            "unique_id": header_rec.get("UniqueID", ""),
        },
        "summary": {
            "connector_count": len(connector_list),
            "total_pins": total_pins,
            "connected_pins": connected_pins,
            "net_label_count": len(net_labels),
        },
        "connectors": connector_list,
        "net_labels": net_labels,
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
                documents.append({
                    "path": doc_path,
                    "kind": kind_map.get(ext, "Unknown"),
                })
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


def get_harness_components(file_path: Path | str) -> list[dict[str, Any]]:
    """Get standardized component list from a .WirDoc or .PrjHar file.

    Compatible with the output format of `get_project_components`.
    """
    path = Path(file_path)
    if path.suffix.lower() == ".prjhar":
        prj = parse_prjhar(path)
        all_components: list[dict[str, Any]] = []
        for doc in prj["documents"]:
            if doc["kind"] == "HarnessWiringDiagram":
                wirdoc_path = path.parent / doc["path"]
                if wirdoc_path.exists():
                    all_components.extend(get_harness_components(wirdoc_path))
        return all_components

    # Assume .WirDoc
    data = parse_wirdoc(path)
    components: list[dict[str, Any]] = []

    for c in data.get("connectors", []):
        components.append({
            "designator": c["designator"],
            "comment": c["description"],
            "description": c["description"],
            "lib_reference": c["lib_reference"],
            "sheet": path.name,
            "document_path": str(path.resolve()),
            "location": c["location"],
            "pin_count": len(c["pins"]),
            "pins": c["pins"],
        })

    return components
