"""Reader and reviewer for Altium multi-board schematics (.MbsDoc).

A multi-board schematic is a ZIP of JSON files (Altium 26.7):
  logical.json   the connectivity: modules (child PCB / harness projects),
                 virtualModules (Custom Parts), connections (harness, cable,
                 direct), each with its components, entries and pin maps
  Pages/*.json   the drawing (rectangles, connection points, lines)
Object references are integer ids unique in the document. Altium omits
default values: an entry without entryType is male (1 = female), a missing
flag is false.

Custom Parts link library components (one entry per linked component). Each
component pin carries its symbol pin name as ExternalNetName. A harness
connection's entries mate with module entries (entriesConnections), or with an
entry of another harness through an inline connector pair
(interConnectedEntriesConnections, with pin-to-pin pairs). Altium only creates
such a pair by placing an entry on a harness line, which splits it in two;
two existing harnesses cannot be joined. A harness connection names its
harness project in "source" once linked.

The review matches each harness connection to a harness project of the
multi-board project (connector part numbers against the library's Mating
Part), each of its entries to a connector of that project, and traces nets
pin by pin from device pin to device pin through the harness wiring and the
inline connectors.
"""

from __future__ import annotations

import configparser
import json
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from dblib import DbLib, find_dblib, load_dblib
from harness_parser import get_harness_wiring_docs, parse_wirdoc

CONNECTION_KINDS = {
    "__logicalHarnessConnection": "harness",
    "__logicalCableConnection": "cable",
    "__logicalDirectConnection": "direct",
}
BLOCK_KEYS = (("module", "modules"), ("custom_part", "virtualModules"))
# Device pin names that should carry no harness wire
UNUSED_PIN_NAMES = re.compile(r"^(NC|N/?C|RSVD|RESERVED|UNUSED)\d*$", re.IGNORECASE)


def read_mbsdoc_parts(path: Path | str) -> dict[str, Any]:
    """The JSON members of a .MbsDoc, keyed by their name with '/' separators."""
    parts: dict[str, Any] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.filename.lower().endswith(".json"):
                text = archive.read(info).decode("utf-8-sig")
                parts[info.filename.replace("\\", "/")] = json.loads(text)
    return parts


def parse_prjmbd(path: Path | str) -> dict[str, Any]:
    """Documents and child projects of a multi-board project (.PrjMbd)."""
    path = Path(path)
    config = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        config.read(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        config.read(path, encoding="latin-1")
    documents: list[str] = []
    for section in config.sections():
        if section.lower().startswith("document"):
            doc = config.get(section, "DocumentPath", fallback="")
            if doc:
                documents.append(doc.replace("\\", "/"))
    children = [
        {
            "path": str((path.parent / d).resolve()),
            "name": Path(d).stem,
            "kind": "harness" if d.lower().endswith(".prjhar") else "pcb",
        }
        for d in documents
        if d.lower().endswith((".prjhar", ".prjpcb"))
    ]
    return {
        "path": str(path.resolve()),
        "documents": documents,
        "child_projects": children,
    }


def find_workspace_prjmbd(start: Path | str | None = None) -> Path | None:
    """The first .PrjMbd in start (default: the working folder), a folder below it
    or a parent."""
    start = Path(start or Path.cwd()).resolve()
    for folder in [start, *start.parents][:4]:
        found = sorted(folder.glob("*.PrjMbd")) + sorted(folder.glob("*/*.PrjMbd"))
        if found:
            return found[0]
    return None


def find_prjmbd(mbsdoc: Path | str) -> Path | None:
    """The .PrjMbd next to (or above) a .MbsDoc that lists it."""
    mbsdoc = Path(mbsdoc).resolve()
    for folder in [mbsdoc.parent, *mbsdoc.parent.parents][:3]:
        for prj in sorted(folder.glob("*.PrjMbd")):
            for doc in parse_prjmbd(prj)["documents"]:
                if (prj.parent / doc).resolve() == mbsdoc:
                    return prj
    return None


def _params(obj: dict[str, Any]) -> dict[str, str]:
    return {p["name"]: p.get("value", "") for p in obj.get("parameters", [])}


def _source(src: dict[str, Any] | None) -> dict[str, str] | None:
    if not src or not src.get("sourceProject"):
        return None
    return {
        k: src[k]
        for k in ("sourceProject", "projectName", "boardFileName", "revisionName")
        if src.get(k)
    }


def _component(comp: dict[str, Any]) -> dict[str, Any]:
    params = _params(comp)
    return {
        "id": comp["id"],
        "designator": comp.get("designator", ""),
        "part_number": params.get("DesignItemId", ""),
        "library": params.get("SourceLibraryName", ""),
        "table": params.get("DatabaseTableName", ""),
        "pins": [
            {
                "id": p["id"],
                "number": p.get("number") or p.get("pinId", ""),
                "name": p.get("name", ""),
                "net": _params(p).get("ExternalNetName") or None,
            }
            for p in comp.get("pins", [])
        ],
    }


def _entry_name(entry: dict[str, Any]) -> str:
    return entry.get("definedDesignator") or entry.get("calculatedDesignator", "")


def parse_mbsdoc(path: Path | str) -> dict[str, Any]:
    """Blocks (modules, Custom Parts), connections and their links, by label.

    Entries are labelled "<block>.<entry>" (MOD_1.J2) and "<connection>.<entry>"
    (H_1.Entry_3). Pins keep their document ids so links can be followed.
    """
    parts = read_mbsdoc_parts(path)
    logical = parts.get("logical.json", {})
    labels: dict[int, str] = {}
    blocks: list[dict[str, Any]] = []
    for kind, key in BLOCK_KEYS:
        for b in logical.get(key, []):
            comps = {c["id"]: _component(c) for c in b.get("components", [])}
            pin_entry = {
                m["key"]: m["value"] for m in b.get("entryPinMap", {}).get("map", [])
            }
            designator = b.get("designator", "")
            entries = []
            for e in b.get("entries", []):
                comp = comps.get(e.get("linkedComponent"))
                pins = [
                    p
                    for c in comps.values()
                    for p in c["pins"]
                    if pin_entry.get(p["id"]) == e["id"]
                ]
                entry = {
                    "id": e["id"],
                    "designator": _entry_name(e),
                    "label": f"{designator}.{_entry_name(e)}",
                    "part_number": comp["part_number"] if comp else None,
                    "library": comp["library"] if comp else None,
                    "connection_id": e.get("logicalConnection"),
                    "pins": pins,
                }
                labels[e["id"]] = entry["label"]
                entries.append(entry)
            blocks.append(
                {
                    "id": b["id"],
                    "kind": kind,
                    "designator": designator,
                    "title": b.get("title", ""),
                    "source": _source(b.get("source")),
                    "entries": entries,
                }
            )

    connections: list[dict[str, Any]] = []
    pin_owner: dict[int, tuple[str, dict[str, Any]]] = {}
    for c in logical.get("connections", []):
        designator = c.get("designator", "")
        comps = [_component(x) for x in c.get("components", [])]
        pin_entry = {
            m["key"]: m["value"] for m in c.get("entryPinMap", {}).get("map", [])
        }
        entries = []
        for e in c.get("entries", []):
            own = [
                comp
                for comp in comps
                if any(pin_entry.get(p["id"]) == e["id"] for p in comp["pins"])
            ]
            entry = {
                "id": e["id"],
                "designator": _entry_name(e),
                "label": f"{designator}.{_entry_name(e)}",
                "gender": "female" if e.get("entryType") == 1 else "male",
                "inline": bool(e.get("isInterConnection")),
                # Components whose pins the entry carries: the inline connector,
                # or the harness connector assigned to a harness end
                "inline_part_numbers": [comp["part_number"] for comp in own],
                "component_designators": [comp["designator"] for comp in own],
                "mates": [],
            }
            labels[e["id"]] = entry["label"]
            entries.append(entry)
        for comp in comps:
            for p in comp["pins"]:
                pin_owner[p["id"]] = (
                    designator,
                    {**p, "part_number": comp["part_number"]},
                )
        connections.append(
            {
                "id": c["id"],
                "kind": CONNECTION_KINDS.get(c.get("$type", ""), c.get("$type", "")),
                "designator": designator,
                "source": _source(c.get("source")),
                "inline_components": [
                    {"part_number": x["part_number"], "pins": len(x["pins"])}
                    for x in comps
                ],
                "entries": entries,
                "_mates": c.get("entriesConnections", []),
                "_inter": c.get("interConnectedEntriesConnections", []),
                "branches": len(c.get("connectionEntriesConnections", [])),
            }
        )

    entry_index = {e["id"]: e for c in connections for e in c["entries"]}
    block_entry_index = {e["id"]: e for b in blocks for e in b["entries"]}
    inline_links: list[dict[str, Any]] = []
    for c in connections:
        for link in c.pop("_mates"):
            for own, other in (
                (link["entry2"], link["entry1"]),
                (link["entry1"], link["entry2"]),
            ):
                if own in entry_index and entry_index[own]["label"].startswith(
                    c["designator"] + "."
                ):
                    entry_index[own]["mates"].append(labels.get(other, f"#{other}"))
                    if other in block_entry_index:
                        block_entry_index[other]["mated_to"] = entry_index[own]["label"]
                    break
        for link in c.pop("_inter"):
            a, b = link["entry1"], link["entry2"]
            for own, other in ((a, b), (b, a)):
                label = labels.get(other, f"#{other}")
                if own in entry_index and label not in entry_index[own]["mates"]:
                    entry_index[own]["mates"].append(label)
            pairs = []
            for pc in link.get("physicalConnections", []):
                pair = pc.get("pinPair", {})
                p1, p2 = (
                    pin_owner.get(pair.get("pin1")),
                    pin_owner.get(pair.get("pin2")),
                )
                if p1 and p2:
                    pairs.append(
                        {
                            "a": f"{p1[0]}:{p1[1]['part_number']}.{p1[1]['number']}",
                            "b": f"{p2[0]}:{p2[1]['part_number']}.{p2[1]['number']}",
                            "_a": p1,
                            "_b": p2,
                        }
                    )
            inline_links.append(
                {
                    "entries": [labels.get(a, f"#{a}"), labels.get(b, f"#{b}")],
                    "pin_pairs": pairs,
                }
            )

    pages = [k for k in parts if k.lower().startswith("pages/")]
    return {
        "file_path": str(Path(path).resolve()),
        "page_count": len(pages),
        "blocks": blocks,
        "connections": connections,
        "inline_links": inline_links,
    }


def drawing_geometry(path: Path | str) -> dict[str, Any]:
    """Drawn shape of every connection: its entry points, junctions and line
    segments, in the document's coordinates (y up, grid in grid_size).

    Segments run between connect positions (an entry point connects one grid
    step along its direction), so they are horizontal or vertical; a new
    branch may land on any of them or on a junction.
    """
    parts = read_mbsdoc_parts(path)
    grid = float(parts.get("options.json", {}).get("gridSize", 9.6))
    logical = parts.get("logical.json", {})
    names = {c["id"]: c.get("designator", "") for c in logical.get("connections", [])}
    entry_names = {
        e["id"]: f"{c.get('designator', '')}.{_entry_name(e)}"
        for c in logical.get("connections", [])
        for e in c.get("entries", [])
    }
    out: dict[str, Any] = {"grid_size": grid, "connections": {}}
    for page_name, page in sorted(parts.items()):
        if not page_name.lower().startswith("pages/"):
            continue
        items = {i["id"]: i for i in page.get("items", [])}
        groups = {
            i["id"]: names.get(i.get("logicalConnectionId"), f"#{i['id']}")
            for i in items.values()
            if i["$type"] == "__drawingConnectionGroup"
        }

        def connect(p: dict[str, Any]) -> list[float]:
            x, y = (
                p.get("position", {}).get("x", 0.0),
                p.get("position", {}).get("y", 0.0),
            )
            d = p.get("direction", {})
            if p.get("connectionItemType") == 1 or not d:
                return [round(x, 4), round(y, 4)]
            return [
                round(x + d.get("x", 0.0) * grid, 4),
                round(y + d.get("y", 0.0) * grid, 4),
            ]

        def group_of(
            item: dict[str, Any], items: dict = items, groups: dict = groups
        ) -> int | None:
            while item and item.get("parentId") not in groups:
                item = items.get(item.get("parentId"))
            return item.get("parentId") if item else None

        for item in items.values():
            gid = group_of(item)
            if gid is None:
                continue
            geo = out["connections"].setdefault(
                groups[gid],
                {"page": page_name, "entries": [], "junctions": [], "segments": []},
            )
            if item["$type"] == "__drawingConnectionPoint" and not item.get(
                "connectionItemType"
            ):
                pos = [
                    round(item.get("position", {}).get(k, 0.0), 4) for k in ("x", "y")
                ]
                if "logicalObjectId" in item:
                    geo["entries"].append(
                        {
                            "entry": entry_names.get(item["logicalObjectId"], ""),
                            "position": pos,
                            "connects_at": connect(item),
                            "direction": [
                                item["direction"].get("x", 0.0),
                                item["direction"].get("y", 0.0),
                            ],
                            "mated": "matedConnectionPointId" in item,
                        }
                    )
                elif "pinId" not in item:
                    geo["junctions"].append(pos)
            elif (
                item["$type"] == "__drawingConnection"
                and item.get("connectionItemType") == 1
            ):
                a, b = items.get(item["endItemId1"]), items.get(item["endItemId2"])
                if a and b:
                    geo["segments"].append([connect(a), connect(b)])
    return out


def _issues(model: dict[str, Any], db: DbLib | None) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def add(severity: str, kind: str, where: str, detail: str) -> None:
        issues.append(
            {"severity": severity, "type": kind, "where": where, "detail": detail}
        )

    titles: dict[str, list[str]] = defaultdict(list)
    for b in model["blocks"]:
        if b["title"]:
            titles[b["title"]].append(b["designator"])
        if b["kind"] == "custom_part" and not b["entries"]:
            add(
                "warning",
                "custom_part_without_entries",
                b["designator"],
                "no linked component",
            )
        for e in b["entries"]:
            if b["kind"] == "custom_part" and not e["part_number"]:
                add(
                    "warning",
                    "entry_without_component",
                    e["label"],
                    "entry links no library component",
                )
            elif e["part_number"] and db and not db.find(e["part_number"]):
                add(
                    "warning",
                    "component_not_in_library",
                    e["label"],
                    f"{e['part_number']} is not in {db.name}",
                )
            if not e.get("mated_to") and e["connection_id"] is None:
                add("warning", "unconnected_entry", e["label"], "no connection")
    for title, designators in titles.items():
        if len(designators) > 1:
            add(
                "info",
                "duplicate_title",
                ", ".join(designators),
                f"same title '{title}'",
            )
    for c in model["connections"]:
        if c["kind"] == "harness" and not c["source"]:
            add(
                "info",
                "harness_not_linked",
                c["designator"],
                "no harness project linked (the harness project takes it with "
                "Design > Import Changes From <multi-board project>)",
            )
        for e in c["entries"]:
            if not e["mates"]:
                add(
                    "error",
                    "dangling_entry",
                    e["label"],
                    "mates no module entry and no other harness; to join two harnesses, "
                    "make them one harness and place an entry on its line (Altium splits it "
                    "with an inline connector pair)",
                )
    for link in model["inline_links"]:
        if not link["pin_pairs"]:
            add(
                "warning",
                "inline_without_pins",
                " / ".join(link["entries"]),
                "inline connector maps no pins",
            )
    return issues


def _mating(db: DbLib | None, part_number: str | None) -> tuple[set[str], str | None]:
    """Mating part numbers of a library part, and its manufacturer part number."""
    if not db or not part_number:
        return set(), None
    found = db.find(part_number)
    if not found:
        return set(), None
    row = found[1]
    mates = {m.strip() for m in row.get("Mating Part", "").split(",") if m.strip()}
    return mates, row.get("Manufacturer Part Number 1") or part_number


def _harness_connectors(project: Path) -> list[dict[str, Any]]:
    out = []
    for doc in get_harness_wiring_docs(project):
        data = parse_wirdoc(doc)
        for c in data["connectors"]:
            params = c.get("parameters", {})
            out.append(
                {
                    "designator": c["designator"],
                    "part_number": params.get("PartNumber") or c.get("comment", ""),
                    "sheet": doc.name,
                    # A pin is in a net only when a wire lands on it: no-connect
                    # pins keep their callout text as net name
                    "pins": {
                        p["designator"]: p.get("net")
                        if p.get("connected_wire")
                        else None
                        for p in c["pins"]
                    },
                }
            )
    return out


def _fits(db: DbLib | None, device_pn: str, harness_pn: str) -> bool:
    """Whether a harness connector mates a device connector (either row's Mating Part)."""
    mates, mpn = _mating(db, device_pn)
    if harness_pn in mates or harness_pn == device_pn:
        return True
    h_mates, _ = _mating(db, harness_pn)
    return bool(mpn and mpn in h_mates) or device_pn in h_mates


def review_multiboard(
    path: Path | str,
    harness_map: dict[str, str] | None = None,
    entry_map: dict[str, dict[str, str]] | None = None,
    dblib_path: Path | str | None = None,
    include_pins: bool = True,
) -> dict[str, Any]:
    """Parse a .MbsDoc (or the .MbsDoc of a .PrjMbd) and review it.

    harness_map: {"H_1": "<harness project name or path>"} overrides the match by
    part numbers; entry_map: {"H_3": {"Entry_5": "P5"}} assigns entries to
    harness connectors where several identical connectors make it ambiguous.
    """
    path = Path(path)
    prj = None
    if path.suffix.lower() == ".prjmbd":
        prj = path
        docs = [
            d for d in parse_prjmbd(path)["documents"] if d.lower().endswith(".mbsdoc")
        ]
        if not docs:
            raise FileNotFoundError(f"{path.name} lists no .MbsDoc")
        path = (prj.parent / docs[0]).resolve()
    else:
        prj = find_prjmbd(path)
    model = parse_mbsdoc(path)
    db = None
    dbp = Path(dblib_path) if dblib_path else find_dblib(path.parent)
    if dbp:
        try:
            db = load_dblib(dbp)
        except (OSError, ValueError, configparser.Error):
            db = None
    project = parse_prjmbd(prj) if prj else {"path": None, "child_projects": []}
    model["project"] = project["path"]
    model["child_projects"] = project["child_projects"]
    model["library"] = str(dbp) if db else None
    issues = _issues(model, db)

    harness_projects = {
        c["name"]: Path(c["path"])
        for c in project["child_projects"]
        if c["kind"] == "harness"
    }
    connectors_cache: dict[str, list[dict[str, Any]]] = {}

    def connectors_of(name: str) -> list[dict[str, Any]]:
        if name not in connectors_cache:
            p = harness_projects.get(name)
            connectors_cache[name] = _harness_connectors(p) if p and p.exists() else []
        return connectors_cache[name]

    block_entries = {e["label"]: e for b in model["blocks"] for e in b["entries"]}
    harness_review: list[dict[str, Any]] = []
    # (connection, entry label) -> (project name, harness connector designator)
    assigned: dict[str, tuple[str, str]] = {}
    for c in model["connections"]:
        if c["kind"] != "harness":
            continue
        # Which part numbers each entry needs on the harness side
        needs: dict[str, tuple[str, str]] = {}
        for e in c["entries"]:
            if e["inline"] and e["inline_part_numbers"]:
                needs[e["designator"]] = ("inline", e["inline_part_numbers"][0])
            else:
                dev = next(
                    (block_entries[m] for m in e["mates"] if m in block_entries), None
                )
                if dev and dev["part_number"]:
                    needs[e["designator"]] = ("device", dev["part_number"])

        def fits(kind: str, pn: str, connector: dict[str, Any]) -> bool:
            return (
                connector["part_number"] == pn
                if kind == "inline"
                else _fits(db, pn, connector["part_number"])
            )

        wanted = (harness_map or {}).get(c["designator"])
        if wanted:
            match = next(
                (
                    n
                    for n, p in harness_projects.items()
                    if wanted in (n, str(p)) or Path(wanted).stem == n
                ),
                None,
            )
            scores = {match: -1} if match else {}
            if not match:
                issues.append(
                    {
                        "severity": "error",
                        "type": "harness_map_unknown",
                        "where": c["designator"],
                        "detail": f"{wanted} is no harness project of the multi-board project",
                    }
                )
        else:
            scores = {
                name: sum(
                    any(fits(kind, pn, k) for k in connectors_of(name))
                    for kind, pn in needs.values()
                )
                for name in harness_projects
            }
            best = max(scores.values(), default=0)
            scores = {n: s for n, s in scores.items() if s == best and best > 0}
            match = next(iter(scores)) if len(scores) == 1 else None
        review: dict[str, Any] = {
            "connection": c["designator"],
            "linked_project": c["source"]["sourceProject"] if c["source"] else None,
            "matched_project": match,
            "candidates": sorted(scores) if not match else [],
            "entries": [],
        }
        if not match:
            harness_review.append(review)
            continue
        harness = connectors_of(match)
        part_of = {k["designator"]: k["part_number"] for k in harness}
        forced = dict((entry_map or {}).get(c["designator"], {}))
        # An entry carrying a component designated after a harness connector
        # (assign_entry_parts) is assigned to that connector
        for e in c["entries"]:
            named = [
                d
                for d in e["component_designators"]
                if d in part_of and not d.endswith("?")
            ]
            if e["designator"] not in forced and named:
                forced[e["designator"]] = named[0]
        taken = set(forced.values())
        pending = []
        for e in c["entries"]:
            need = needs.get(e["designator"])
            row = {
                "entry": e["label"],
                "mates": e["mates"],
                "needs": need[1] if need else None,
            }
            if e["designator"] in forced:
                row["connector"] = forced[e["designator"]]
            elif need:
                row["candidates"] = [
                    k["designator"] for k in harness if fits(need[0], need[1], k)
                ]
                pending.append(row)
            review["entries"].append(row)
        # Assign unique candidates, repeating as assignments free others
        progress = True
        while progress:
            progress = False
            for row in pending:
                if "connector" in row:
                    continue
                free = [d for d in row["candidates"] if d not in taken]
                if len(free) == 1:
                    row["connector"] = free[0]
                    taken.add(free[0])
                    progress = True
        entry_by_label = {e["label"]: e for e in c["entries"]}
        for row in review["entries"]:
            if "connector" in row:
                row.pop("candidates", None)
                row["connector_part"] = part_of.get(row["connector"], "")
                assigned[row["entry"]] = (match, row["connector"])
                carried = entry_by_label[row["entry"]]["inline_part_numbers"]
                if not carried and row["needs"]:
                    issues.append(
                        {
                            "severity": "info",
                            "type": "harness_entry_without_part",
                            "where": row["entry"],
                            "detail": f"no component; the harness has "
                            f"{row['connector']} ({row['connector_part']}) here "
                            "(edit_multiboard_schematic assign_harness_parts)",
                        }
                    )
                elif carried and row["connector_part"] not in carried:
                    issues.append(
                        {
                            "severity": "error",
                            "type": "entry_part_mismatch",
                            "where": row["entry"],
                            "detail": f"entry carries {', '.join(carried)}, harness "
                            f"connector {row['connector']} is {row['connector_part']}",
                        }
                    )
            elif row.get("candidates"):
                issues.append(
                    {
                        "severity": "warning",
                        "type": "ambiguous_harness_connector",
                        "where": row["entry"],
                        "detail": f"{len(row['candidates'])} connectors of {match} fit "
                        f"({', '.join(row['candidates'])}); pass entry_map",
                    }
                )
            elif row["needs"]:
                issues.append(
                    {
                        "severity": "error",
                        "type": "no_harness_connector",
                        "where": row["entry"],
                        "detail": f"no connector of {match} mates {row['needs']}",
                    }
                )
        harness_review.append(review)
    model["harness_review"] = harness_review

    if include_pins:
        matched = {
            r["connection"]: r["matched_project"]
            for r in harness_review
            if r["matched_project"]
        }
        model["pin_review"], model["system_nets"], pin_issues = _trace(
            model, assigned, matched, connectors_of
        )
        issues.extend(pin_issues)
    for c in model["connections"]:
        c.pop("id", None)
    for link in model["inline_links"]:
        for pair in link["pin_pairs"]:
            pair.pop("_a", None)
            pair.pop("_b", None)
    if not include_pins:
        for b in model["blocks"]:
            for e in b["entries"]:
                e["pin_count"] = len(e.pop("pins"))
    model["issues"] = issues
    model["summary"] = {
        "blocks": len(model["blocks"]),
        "connections": len(model["connections"]),
        "errors": sum(i["severity"] == "error" for i in issues),
        "warnings": sum(i["severity"] == "warning" for i in issues),
        "infos": sum(i["severity"] == "info" for i in issues),
    }
    return model


def _trace(
    model: dict[str, Any],
    assigned: dict[str, tuple[str, str]],
    matched: dict[str, str],
    connectors_of: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Per-pin table of each device entry, and nets traced device to device.

    Nodes: ("d", device entry, pin), ("h", connection, harness connector, pin) and
    ("net", connection, harness net). Each harness connection is one instance of
    its project, so two harnesses built from the same project stay apart.
    """
    parent: dict[Any, Any] = {}

    def find(x: Any) -> Any:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: Any, b: Any) -> None:
        parent[find(a)] = find(b)

    issues: list[dict[str, str]] = []
    pin_review: list[dict[str, Any]] = []
    device_name: dict[Any, str] = {}
    block_entries = {e["label"]: e for b in model["blocks"] for e in b["entries"]}

    for c in model["connections"]:
        project = matched.get(c["designator"])
        if not project:
            continue
        harness = {k["designator"]: k for k in connectors_of(project)}
        for k in harness.values():
            for pin, net in k["pins"].items():
                if net:
                    union(
                        ("h", c["designator"], k["designator"], pin),
                        ("net", c["designator"], net),
                    )
        for e in c["entries"]:
            k = harness.get(assigned.get(e["label"], ("", ""))[1])
            dev = next(
                (block_entries[m] for m in e["mates"] if m in block_entries), None
            )
            if not k or not dev:
                continue
            conn = k["designator"]
            rows = []
            dev_pins = {p["number"]: p for p in dev["pins"]}
            for number in sorted(set(dev_pins) | set(k["pins"]), key=_pin_sort):
                dp = dev_pins.get(number)
                net = k["pins"].get(number)
                where = f"{dev['label']}.{number}"
                if dp:
                    device_name[("d", dev["label"], number)] = dp["name"]
                rows.append(
                    {
                        "pin": number,
                        "device": dp["name"] if dp else None,
                        "harness": f"{conn}.{number}" if number in k["pins"] else None,
                        "net": net,
                    }
                )
                if dp is None:
                    issues.append(
                        _issue(
                            "error",
                            "pin_missing_on_device",
                            where,
                            f"{project} {conn}.{number} has no device pin",
                        )
                    )
                elif number not in k["pins"]:
                    issues.append(
                        _issue(
                            "error",
                            "pin_missing_on_harness",
                            where,
                            f"{project} {conn} has no pin {number}",
                        )
                    )
                else:
                    union(
                        ("d", dev["label"], number),
                        ("h", c["designator"], conn, number),
                    )
                    if net and UNUSED_PIN_NAMES.match(dp["name"] or ""):
                        issues.append(
                            _issue(
                                "warning",
                                "wired_to_unused_pin",
                                where,
                                f"device pin '{dp['name']}' carries harness net {net}",
                            )
                        )
            pin_review.append(
                {
                    "entry": dev["label"],
                    "part_number": dev["part_number"],
                    "harness": f"{c['designator']}:{project}.{conn}",
                    "pins": rows,
                }
            )

    # Inline connector pairs join two harness instances pin by pin
    inline_entry = {
        c["designator"]: e["label"]
        for c in model["connections"]
        for e in c["entries"]
        if e["inline"]
    }
    for link in model["inline_links"]:
        for pair in link["pin_pairs"]:
            nodes = []
            for conn_desig, pin in (pair["_a"], pair["_b"]):
                label = next(
                    (
                        lbl
                        for lbl in link["entries"]
                        if lbl.startswith(conn_desig + ".")
                    ),
                    inline_entry.get(conn_desig),
                )
                if label in assigned:
                    nodes.append(("h", conn_desig, assigned[label][1], pin["number"]))
            if len(nodes) == 2:
                union(*nodes)

    mapped = {(lbl.split(".")[0], conn) for lbl, (_, conn) in assigned.items()}
    groups: dict[Any, list[Any]] = defaultdict(list)
    for node in set(parent) | set(device_name):
        groups[find(node)].append(node)
    system_nets = []
    unwired: list[str] = []
    for members in groups.values():
        devices = sorted(
            f"{n[1]}.{n[2]} ({device_name[n]})" for n in members if n[0] == "d"
        )
        if not devices:
            continue
        nets = sorted({f"{n[1]}:{n[2]}" for n in members if n[0] == "net"})
        if not nets and len(devices) == 1:
            unwired.append(devices[0])
            continue
        # Harness pins on connectors no entry is mapped to: the net goes on there
        open_ends = sorted(
            f"{n[1]}:{n[2]}.{n[3]}"
            for n in members
            if n[0] == "h" and (n[1], n[2]) not in mapped
        )
        net = {"harness_nets": nets, "devices": devices}
        if open_ends:
            net["unmapped_harness_pins"] = open_ends
        system_nets.append(net)
        if len(devices) == 1 and nets and not open_ends:
            issues.append(
                _issue(
                    "warning",
                    "single_device_net",
                    devices[0],
                    f"{', '.join(nets)} reaches no other device pin",
                )
            )
    system_nets.sort(key=lambda s: (s["harness_nets"][:1], s["devices"]))
    for entry in pin_review:
        entry["unwired_pins"] = [
            d.split(" ")[0].rsplit(".", 1)[1]
            for d in sorted(unwired)
            if d.startswith(entry["entry"] + ".")
        ]
    return pin_review, system_nets, issues


def _issue(severity: str, kind: str, where: str, detail: str) -> dict[str, str]:
    return {"severity": severity, "type": kind, "where": where, "detail": detail}


def _pin_sort(number: str) -> tuple[int, str]:
    return (int(number), "") if number.isdigit() else (10**6, number)
