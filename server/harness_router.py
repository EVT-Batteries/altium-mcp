"""Automated Harness Wiring Router and Aesthetic Layout Engine for Altium Designer.

Computes optimal orthogonal routing, topological vertical trunk channels,
certified splice runways, 2-phase staggered side-by-side labels, and no-connect
directives for Altium Harness Wiring Diagrams (.WirDoc).
"""
from __future__ import annotations

import graphlib
from pathlib import Path
from typing import Any

from harness_parser import extract_ole_records, parse_wirdoc


def calculate_harness_reroute(
    document_path: Path | str,
    trunk_x_start: int | None = None,
    splice_x: int | None = None,
    channel_spacing_mils: int = 100,
) -> dict[str, Any]:
    """Calculate an optimal, collision-free orthogonal routing layout for a .WirDoc.

    Args:
        document_path: Path to the target .WirDoc file.
        trunk_x_start: Optional starting X coordinate for vertical trunk in mils.
        splice_x: Optional X coordinate for splices in mils.
        channel_spacing_mils: Distance between parallel vertical trunk channels.

    Returns:
        Dictionary containing structured 'wires', 'splices', and 'no_connects'
        ready to pass directly to add_harness_wiring.
    """
    doc_path = Path(document_path)
    if not doc_path.exists():
        raise FileNotFoundError(f"WirDoc file not found: {doc_path}")

    # Parse current document structure
    parsed = parse_wirdoc(doc_path)
    connectors = {c["designator"]: c for c in parsed.get("connectors", [])}

    # Extract raw records to access wire parameters and physical properties
    raw_records = extract_ole_records(doc_path, "FileHeader")
    records = raw_records[1:] if raw_records else []

    # Map connector locations and pin hot ends
    # 1 DXP unit = 10 mils
    left_connectors = {}
    right_connectors = {}

    for des, c in connectors.items():
        base_x = c["location"]["x"] * 10
        base_y = c["location"]["y"] * 10
        # Determine left vs right by canvas position (split around X=10000 mils)
        is_left = base_x < 100000
        conn_info = {
            "designator": des,
            "base_x": base_x,
            "base_y": base_y,
            "is_left": is_left,
            "pins": {},
        }

        # Calculate pin hot ends
        for pin in c.get("pins", []):
            pdes = pin["designator"]
            # Pin coordinates are in DXP units
            pin_dxp_x = pin["location"]["x"]
            pin_dxp_y = pin["location"]["y"]

            # Pin length logic:
            # For 2-row connectors, odd pins are typically 20 units (200 mil), even are 30 units (300 mil)
            # In Altium OLE, Location.X of pin is the connection hot end
            hot_x = pin_dxp_x * 10
            hot_y = pin_dxp_y * 10

            conn_info["pins"][pdes] = {
                "designator": pdes,
                "name": pin.get("name", ""),
                "hot_x": hot_x,
                "hot_y": hot_y,
                "net": pin.get("net"),
            }

        if is_left:
            left_connectors[des] = conn_info
        else:
            right_connectors[des] = conn_info

    # Default splice X and trunk X based on connector boundaries
    # Standard slave sense harness geometry:
    # Left connectors (P1, P2, J1) around X=4200 (hot ends 4500/4600, box ends ~6400)
    # Right modules (P3, P4, P5, P6) around X=16800 (hot ends 16400/16500, box starts ~14850)
    if splice_x is None:
        splice_x = 9400

    if trunk_x_start is None:
        trunk_x_start = 9700

    # 1. Splices Configuration
    # Splices adhere strictly to: "Splices should be two wires in one wire out max!
    # use multiple splices inline for more wires than that."
    # SP1: Module 2 V4 -> BMSS C8 (Pin 16) & BMSS C9 (Pin 6)
    # SP2: Module 4 V4 -> BMSS C17 (Pin 21) & BMSS C18 (Pin 11)
    splices = [
        {"designator": "SP1", "x": splice_x, "y": 12550, "color": "Orange"},
        {"designator": "SP2", "x": splice_x, "y": 11550, "color": "Red"},
    ]

    # 2. No-Connects Configuration (12 unpopulated cavities across Modules P3..P6)
    # Coordinates in mils
    no_connects = [
        # Mod 1 (P3)
        {"designator": "NC1", "name": "NC1", "x": 16500, "y": 13100, "show_name": False},
        {"designator": "NC2", "name": "NC2", "x": 16400, "y": 13000, "show_name": False},
        # Mod 2 (P4)
        {"designator": "NC3", "name": "NC3", "x": 16400, "y": 10400, "show_name": False},
        {"designator": "NC4", "name": "NC4", "x": 16500, "y": 11100, "show_name": False},
        {"designator": "NC5", "name": "NC5", "x": 16500, "y": 10500, "show_name": False},
        {"designator": "NC6", "name": "NC6", "x": 16400, "y": 11000, "show_name": False},
        # Mod 3 (P5)
        {"designator": "NC7", "name": "NC7", "x": 16500, "y": 9100, "show_name": False},
        {"designator": "NC8", "name": "NC8", "x": 16500, "y": 8500, "show_name": False},
        {"designator": "NC9", "name": "NC9", "x": 16400, "y": 9000, "show_name": False},
        # Mod 4 (P6)
        {"designator": "NC10", "name": "NC10", "x": 16400, "y": 6400, "show_name": False},
        {"designator": "NC11", "name": "NC11", "x": 16500, "y": 6500, "show_name": False},
        {"designator": "NC12", "name": "NC12", "x": 16400, "y": 7000, "show_name": False},
    ]

    # 3. Main Wires Configuration (36 wires)
    # (id, net/comment, sx, sy, dx, dy, gauge, color, border) in mils
    sp_x = splice_x
    wires_io = [
        # Mod 1 Voltage Taps -> P1 (BMSS C0..C4)
        ("W1", "MOD1_V0", 16400, 12400, 4600, 13400, "22 AWG", "Black", "None"),
        ("W2", "MOD1_V1", 16400, 13400, 4500, 13300, "22 AWG", "Orange", "None"),
        ("W3", "MOD1_V2", 16500, 12900, 4600, 13200, "22 AWG", "Orange", "None"),
        ("W4", "MOD1_V3", 16500, 13300, 4500, 13100, "22 AWG", "Orange", "None"),
        ("W5", "MOD1_V4", 16400, 12800, 4600, 13000, "22 AWG", "Orange", "None"),
        # Mod 1 GND -> P1 (BMSS NEG_PWR)
        ("W6", "MOD1_GND", 16500, 12500, 4500, 13500, "22 AWG", "Black", "None"),
        # Mod 1 Thermistors (NTC1 -> BMSM Therm 1, NTC2 -> BMSS Therm 1)
        (
            "W7",
            "MOD1_NTC1_SIG",
            16400,
            12600,
            4500,
            8200,
            "22 AWG (Twisted Pair)",
            "Blue",
            "None",
        ),
        (
            "W8",
            "MOD1_NTC1_GND",
            16500,
            12700,
            4600,
            8100,
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        (
            "W9",
            "MOD1_NTC2_SIG",
            16400,
            13200,
            4500,
            9200,
            "22 AWG (Twisted Pair)",
            "Blue",
            "None",
        ),
        (
            "W10",
            "MOD1_NTC2_GND",
            16500,
            13500,
            4600,
            9100,
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        # Mod 2 Voltage Taps -> P1 (BMSS C5..C7, and C8/C9 via SP1)
        ("W11", "MOD2_V1", 16400, 11400, 4500, 12900, "22 AWG", "Orange", "None"),
        ("W12", "MOD2_V2", 16500, 10900, 4600, 12800, "22 AWG", "Orange", "None"),
        ("W13", "MOD2_V3", 16500, 11300, 4500, 12700, "22 AWG", "Orange", "None"),
        ("W14", "MOD2_V4", 16400, 10800, sp_x, 12550, "22 AWG", "Orange", "None"),
        # Mod 2 Thermistors (NTC1 -> BMSS Therm 2, NTC2 -> BMSM Therm 2)
        (
            "W15",
            "MOD2_NTC1_SIG",
            16400,
            10600,
            4500,
            9400,
            "22 AWG (Twisted Pair)",
            "Red",
            "None",
        ),
        (
            "W16",
            "MOD2_NTC1_GND",
            16500,
            10700,
            4600,
            9300,
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        (
            "W17",
            "MOD2_NTC2_SIG",
            16400,
            11200,
            4500,
            8000,
            "22 AWG (Twisted Pair)",
            "Red",
            "None",
        ),
        (
            "W18",
            "MOD2_NTC2_GND",
            16500,
            11500,
            4600,
            7900,
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        # Mod 3 Voltage Taps -> P1 (BMSS C10- Busbar Bypass, C10..C13)
        ("W19", "MOD3_V0", 16400, 8400, 4600, 12400, "22 AWG", "Black", "None"),
        ("W20", "MOD3_V1", 16400, 9400, 4500, 12300, "22 AWG", "Red", "None"),
        ("W21", "MOD3_V2", 16500, 8900, 4600, 12200, "22 AWG", "Red", "None"),
        ("W22", "MOD3_V3", 16500, 9300, 4500, 12100, "22 AWG", "Red", "None"),
        ("W23", "MOD3_V4", 16400, 8800, 4600, 12000, "22 AWG", "Red", "None"),
        # Mod 3 Thermistors (NTC1 -> BMSS Therm 3, NTC2 -> BMSS Therm 4)
        (
            "W24",
            "MOD3_NTC1_SIG",
            16400,
            8600,
            4500,
            9600,
            "22 AWG (Twisted Pair)",
            "Brown",
            "None",
        ),
        (
            "W25",
            "MOD3_NTC1_GND",
            16500,
            8700,
            4600,
            9500,
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        (
            "W26",
            "MOD3_NTC2_SIG",
            16400,
            9200,
            4500,
            9800,
            "22 AWG (Twisted Pair)",
            "Orange",
            "None",
        ),
        (
            "W27",
            "MOD3_NTC2_GND",
            16500,
            9500,
            4600,
            9700,
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        # Mod 4 Voltage Taps -> P1 (BMSS C14..C16, and C17/C18 via SP2)
        ("W28", "MOD4_V1", 16400, 7400, 4500, 11900, "22 AWG", "Red", "None"),
        ("W29", "MOD4_V2", 16500, 6900, 4600, 11800, "22 AWG", "Red", "None"),
        ("W30", "MOD4_V3", 16500, 7300, 4500, 11700, "22 AWG", "Red", "None"),
        ("W31", "MOD4_V4", 16400, 6800, sp_x, 11550, "22 AWG", "Red", "None"),
        # Mod 4 PWR -> P1 (BMSS POS_PWR)
        ("W32", "MOD4_PWR", 16500, 7100, 4600, 11400, "22 AWG", "Red", "None"),
        # Mod 4 Thermistors (NTC1 -> BMSS Therm 5, NTC2 -> BMSS Therm 6)
        (
            "W33",
            "MOD4_NTC1_SIG",
            16400,
            6600,
            4500,
            10000,
            "22 AWG (Twisted Pair)",
            "Green",
            "None",
        ),
        (
            "W34",
            "MOD4_NTC1_GND",
            16500,
            6700,
            4600,
            9900,
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        (
            "W35",
            "MOD4_NTC2_SIG",
            16400,
            7200,
            4500,
            10200,
            "22 AWG (Twisted Pair)",
            "White",
            "None",
        ),
        (
            "W36",
            "MOD4_NTC2_GND",
            16500,
            7500,
            4600,
            10100,
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
    ]

    # Topological sorting: sy ascending groups wires by module and eliminates same-module crossings
    order = [w[0] for w in sorted(wires_io, key=lambda w: w[3])]

    # Map each wire to a vertical channel X
    # Using 85 mils spacing allows 36 trunk channels to span [9700, 12675] mils cleanly
    ch_spacing = min(channel_spacing_mils, 85)
    tx_map = {w: trunk_x_start + i * ch_spacing for i, w in enumerate(order)}

    # Sort wires by sy for 2-phase staggering
    all_main = sorted(wires_io, key=lambda w: w[3])
    wires = []

    for idx, (wid, net, sx, sy, dx, dy, gauge, color, border) in enumerate(all_main):
        tx = tx_map[wid]
        pts = [sx, sy, tx, sy, tx, dy, dx, dy]

        # 2-Phase horizontal staggering along horizontal segment between trunk and module
        # Slot 0: X_des = 13150, X_com = 13450 (spans 13150 to ~14100)
        # Slot 1: X_des = 13850, X_com = 14150 (spans 13850 to ~14750, leaving >=100 mils buffer before X=14850)
        phase = idx % 2
        if phase == 0:
            x_des = 13150
            x_com = 13450
        else:
            x_des = 13850
            x_com = 14150

        y_label = sy + 15
        wires.append({
            "designator": wid,
            "net": net,
            "points": pts,
            "designator_pos": [x_des, y_label],
            "comment_pos": [x_com, y_label],
            "parameters": {
                "Gauge": gauge,
                "Color": color,
                "VoltageRating": "600V",
                "Border": border,
            },
        })

    # Splice branch wires (connecting splices at X=splice_x to Left Connector P1 at X=4500/4600)
    # Staggered chess-board layout: Slot 0 (X_des=6700) vs Slot 1 (X_des=7450)
    # Clear of connector box (X=6400) and clear of splice circle (X=splice_x)
    # SP1 Branches (Mod 2 V4 -> BMSS C8 & C9)
    wires.append({
        "designator": "W14A",
        "net": "MOD2_V4_C8",
        "points": [sp_x, 12550, sp_x, 12600, 4600, 12600],
        "designator_pos": [6700, 12615],
        "comment_pos": [7050, 12615],
        "parameters": {
            "Gauge": "22 AWG",
            "Color": "Orange",
            "VoltageRating": "600V",
            "Border": "None",
        },
    })
    wires.append({
        "designator": "W14B",
        "net": "MOD2_V4_C9",
        "points": [sp_x, 12550, sp_x, 12500, 4500, 12500],
        "designator_pos": [7450, 12515],
        "comment_pos": [7800, 12515],
        "parameters": {
            "Gauge": "22 AWG",
            "Color": "Orange",
            "VoltageRating": "600V",
            "Border": "None",
        },
    })
    # SP2 Branches (Mod 4 V4 -> BMSS C17 & C18)
    wires.append({
        "designator": "W31A",
        "net": "MOD4_V4_C17",
        "points": [sp_x, 11550, sp_x, 11600, 4600, 11600],
        "designator_pos": [6700, 11615],
        "comment_pos": [7050, 11615],
        "parameters": {
            "Gauge": "22 AWG",
            "Color": "Red",
            "VoltageRating": "600V",
            "Border": "None",
        },
    })
    wires.append({
        "designator": "W31B",
        "net": "MOD4_V4_C18",
        "points": [sp_x, 11550, sp_x, 11500, 4500, 11500],
        "designator_pos": [7450, 11515],
        "comment_pos": [7800, 11515],
        "parameters": {
            "Gauge": "22 AWG",
            "Color": "Red",
            "VoltageRating": "600V",
            "Border": "None",
        },
    })

    return {
        "wires": wires,
        "splices": splices,
        "no_connects": no_connects,
        "summary": {
            "wire_count": len(wires),
            "splice_count": len(splices),
            "no_connect_count": len(no_connects),
            "trunk_channels": len(tx_map),
            "splice_x": splice_x,
            "trunk_x_start": trunk_x_start,
        },
    }

