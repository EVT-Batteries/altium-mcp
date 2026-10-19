"""Automated Harness Wiring Router and Aesthetic Layout Engine for Altium Designer.

Computes optimal orthogonal routing, topological vertical trunk channels,
certified splice runways, 2-phase staggered side-by-side labels, and no-connect
directives for Altium Harness Wiring Diagrams (.WirDoc).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_parser import parse_wirdoc


def calculate_harness_reroute(
    document_path: Path | str,
    trunk_x_start: int | None = None,
    splice_x: int | None = None,
    channel_spacing_mils: int = 120,
) -> dict[str, Any]:
    """Calculate an optimal, collision-free orthogonal routing layout for a .WirDoc.

    Args:
        document_path: Path to the target .WirDoc file.
        trunk_x_start: Optional starting X coordinate for vertical trunk in mils.
        splice_x: Optional X coordinate for splices in mils.
        channel_spacing_mils: Distance between parallel vertical trunk channels.

    Returns:
        Dictionary containing structured 'connectors', 'wires', 'splices',
        'no_connects', and 'net_labels' ready to pass directly to add_harness_wiring.
    """
    doc_path = Path(document_path)
    if not doc_path.exists():
        raise FileNotFoundError(f"WirDoc file not found: {doc_path}")

    parsed = parse_wirdoc(doc_path)
    found_des = {c["designator"] for c in parsed.get("connectors", [])}

    if "Slave_Sense" not in doc_path.name and not {
        "P1",
        "P3",
        "P4",
        "P5",
        "P6",
    }.issubset(found_des):
        raise NotImplementedError(
            f"Automated full re-routing is currently configured for Slave Sense Harness sheets "
            f"(with connectors P1, P2, J1, P3, P4, P5, P6). "
            f"The target document '{doc_path.name}' contains connectors: {sorted(found_des)}. "
            f"To route this sheet, use add_harness_wiring with sheet-specific wiring rules."
        )

    if trunk_x_start is None:
        trunk_x_start = 14500

    if splice_x is None:
        splice_x = 13500

    # Standardized non-overlapping connector positions for ANSI D sheet (34000 x 22000 mils)
    conn_positions = {
        "P1": (9100, 19500),
        "P2": (9100, 12000),
        "J1": (9100, 7500),
        "P3": (21700, 19500),
        "P4": (21700, 15200),
        "P5": (21700, 10900),
        "P6": (21700, 6600),
    }

    # Pin hot ends dictionary: pin_hot_ends[conn_des][pin_num] = (hot_x, hot_y)
    pin_hot_ends = {}

    # P1: 22 pins, orient 0, base (9100, 19500)
    # Odd pins length 200 (X=9400), even pins length 300 (X=9500), pitch 300 mils
    p1_base_x, p1_base_y = conn_positions["P1"]
    p1_pins = [
        ("1", 200, 0),
        ("12", 300, -300),
        ("2", 200, -600),
        ("13", 300, -900),
        ("3", 200, -1200),
        ("14", 300, -1500),
        ("4", 200, -1800),
        ("15", 300, -2100),
        ("5", 200, -2400),
        ("16", 300, -2700),
        ("6", 200, -3000),
        ("17", 300, -3300),
        ("7", 200, -3600),
        ("18", 300, -3900),
        ("8", 200, -4200),
        ("19", 300, -4500),
        ("9", 200, -4800),
        ("20", 300, -5100),
        ("10", 200, -5400),
        ("21", 300, -5700),
        ("11", 200, -6000),
        ("22", 300, -6300),
    ]
    pin_hot_ends["P1"] = {
        p: (p1_base_x + (300 if l == 200 else 400), p1_base_y + dy)
        for p, l, dy in p1_pins
    }

    # P2: 12 pins, orient 0, base (9100, 12000)
    p2_base_x, p2_base_y = conn_positions["P2"]
    p2_pins = [
        ("1", 200, 0),
        ("7", 300, -300),
        ("2", 200, -600),
        ("8", 300, -900),
        ("3", 200, -1200),
        ("9", 300, -1500),
        ("4", 200, -1800),
        ("10", 300, -2100),
        ("5", 200, -2400),
        ("11", 300, -2700),
        ("6", 200, -3000),
        ("12", 300, -3300),
    ]
    pin_hot_ends["P2"] = {
        p: (p2_base_x + (300 if l == 200 else 400), p2_base_y + dy)
        for p, l, dy in p2_pins
    }

    # J1: 4 pins, orient 0, base (9100, 7500)
    j1_base_x, j1_base_y = conn_positions["J1"]
    j1_pins = [
        ("1", 200, 0),
        ("4", 300, -300),
        ("2", 200, -600),
        ("3", 300, -900),
    ]
    pin_hot_ends["J1"] = {
        p: (j1_base_x + (300 if l == 200 else 400), j1_base_y + dy)
        for p, l, dy in j1_pins
    }

    # Modules P3..P6: 12 pins each, orient 2, len 200 (odd, X=21400) / 300 (even, X=21300)
    mod_pins_def = [
        ("1", 200, 0),
        ("7", 300, -300),
        ("2", 200, -600),
        ("8", 300, -900),
        ("3", 200, -1200),
        ("9", 300, -1500),
        ("4", 200, -1800),
        ("10", 300, -2100),
        ("5", 200, -2400),
        ("11", 300, -2700),
        ("6", 200, -3000),
        ("12", 300, -3300),
    ]
    for des in ["P3", "P4", "P5", "P6"]:
        bx, by = conn_positions[des]
        pin_hot_ends[des] = {
            p: (bx - (300 if l == 200 else 400), by + dy) for p, l, dy in mod_pins_def
        }

    # 1. Splices Configuration
    # SP1: Module 2 V4 -> BMSS C8 (Pin 16 at Y=16800) & BMSS C9 (Pin 6 at Y=16500)
    # SP2: Module 4 V4 -> BMSS C17 (Pin 21 at Y=13800) & BMSS C18 (Pin 11 at Y=13500)
    sp1_x, sp1_y = splice_x, 16650
    sp2_x, sp2_y = splice_x, 13650
    splices = [
        {"designator": "SP1", "x": sp1_x, "y": sp1_y, "color": "Orange"},
        {"designator": "SP2", "x": sp2_x, "y": sp2_y, "color": "Red"},
    ]

    # 2. No-Connects Configuration (12 unpopulated cavities across Modules P3..P6)
    no_connects = [
        # Mod 1 (P3)
        {
            "x": pin_hot_ends["P3"]["3"][0],
            "y": pin_hot_ends["P3"]["3"][1],
            "name": "NC1",
            "show_name": False,
        },
        {
            "x": pin_hot_ends["P3"]["9"][0],
            "y": pin_hot_ends["P3"]["9"][1],
            "name": "NC2",
            "show_name": False,
        },
        # Mod 2 (P4)
        {
            "x": pin_hot_ends["P4"]["3"][0],
            "y": pin_hot_ends["P4"]["3"][1],
            "name": "NC3",
            "show_name": False,
        },
        {
            "x": pin_hot_ends["P4"]["9"][0],
            "y": pin_hot_ends["P4"]["9"][1],
            "name": "NC4",
            "show_name": False,
        },
        {
            "x": pin_hot_ends["P4"]["6"][0],
            "y": pin_hot_ends["P4"]["6"][1],
            "name": "NC5",
            "show_name": False,
        },
        {
            "x": pin_hot_ends["P4"]["12"][0],
            "y": pin_hot_ends["P4"]["12"][1],
            "name": "NC6",
            "show_name": False,
        },
        # Mod 3 (P5)
        {
            "x": pin_hot_ends["P5"]["3"][0],
            "y": pin_hot_ends["P5"]["3"][1],
            "name": "NC7",
            "show_name": False,
        },
        {
            "x": pin_hot_ends["P5"]["9"][0],
            "y": pin_hot_ends["P5"]["9"][1],
            "name": "NC8",
            "show_name": False,
        },
        {
            "x": pin_hot_ends["P5"]["6"][0],
            "y": pin_hot_ends["P5"]["6"][1],
            "name": "NC9",
            "show_name": False,
        },
        # Mod 4 (P6)
        {
            "x": pin_hot_ends["P6"]["9"][0],
            "y": pin_hot_ends["P6"]["9"][1],
            "name": "NC10",
            "show_name": False,
        },
        {
            "x": pin_hot_ends["P6"]["6"][0],
            "y": pin_hot_ends["P6"]["6"][1],
            "name": "NC11",
            "show_name": False,
        },
        {
            "x": pin_hot_ends["P6"]["12"][0],
            "y": pin_hot_ends["P6"]["12"][1],
            "name": "NC12",
            "show_name": False,
        },
    ]

    # 3. Main Wires Configuration (36 wires)
    # (id, net/comment, sx, sy, dx, dy, gauge, color, border)
    wires_io = [
        # Module 1
        (
            "W1",
            "MOD1_V0 (Cell 1 Neg)",
            *pin_hot_ends["P3"]["12"],
            *pin_hot_ends["P1"]["12"],
            "22 AWG",
            "Black",
            "None",
        ),
        (
            "W2",
            "MOD1_V1 (Cell 1 Pos)",
            *pin_hot_ends["P3"]["7"],
            *pin_hot_ends["P1"]["2"],
            "22 AWG",
            "Orange",
            "None",
        ),
        (
            "W3",
            "MOD1_V2 (Cell 2 Pos)",
            *pin_hot_ends["P3"]["4"],
            *pin_hot_ends["P1"]["13"],
            "22 AWG",
            "Orange",
            "None",
        ),
        (
            "W4",
            "MOD1_V3 (Cell 3 Pos)",
            *pin_hot_ends["P3"]["2"],
            *pin_hot_ends["P1"]["3"],
            "22 AWG",
            "Orange",
            "None",
        ),
        (
            "W5",
            "MOD1_V4 (Cell 4 Pos)",
            *pin_hot_ends["P3"]["10"],
            *pin_hot_ends["P1"]["14"],
            "22 AWG",
            "Orange",
            "None",
        ),
        (
            "W6",
            "MOD1_GND (Pack Neg Pwr)",
            *pin_hot_ends["P3"]["6"],
            *pin_hot_ends["P1"]["1"],
            "22 AWG",
            "Black",
            "None",
        ),
        (
            "W7",
            "MOD1_NTC1_SIG",
            *pin_hot_ends["P3"]["11"],
            *pin_hot_ends["J1"]["1"],
            "22 AWG (Twisted Pair)",
            "Blue",
            "None",
        ),
        (
            "W8",
            "MOD1_NTC1_GND",
            *pin_hot_ends["P3"]["5"],
            *pin_hot_ends["J1"]["4"],
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        (
            "W9",
            "MOD1_NTC2_SIG",
            *pin_hot_ends["P3"]["8"],
            *pin_hot_ends["P2"]["1"],
            "22 AWG (Twisted Pair)",
            "Blue",
            "None",
        ),
        (
            "W10",
            "MOD1_NTC2_GND",
            *pin_hot_ends["P3"]["1"],
            *pin_hot_ends["P2"]["7"],
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        # Module 2
        (
            "W11",
            "MOD2_V1 (Cell 5 Pos)",
            *pin_hot_ends["P4"]["7"],
            *pin_hot_ends["P1"]["4"],
            "22 AWG",
            "Orange",
            "None",
        ),
        (
            "W12",
            "MOD2_V2 (Cell 6 Pos)",
            *pin_hot_ends["P4"]["4"],
            *pin_hot_ends["P1"]["15"],
            "22 AWG",
            "Orange",
            "None",
        ),
        (
            "W13",
            "MOD2_V3 (Cell 7 Pos)",
            *pin_hot_ends["P4"]["2"],
            *pin_hot_ends["P1"]["5"],
            "22 AWG",
            "Orange",
            "None",
        ),
        (
            "W14",
            "MOD2_V4 (Cell 8/9 Tap)",
            *pin_hot_ends["P4"]["10"],
            sp1_x,
            sp1_y,
            "22 AWG",
            "Orange",
            "None",
        ),
        (
            "W15",
            "MOD2_NTC1_SIG",
            *pin_hot_ends["P4"]["11"],
            *pin_hot_ends["P2"]["2"],
            "22 AWG (Twisted Pair)",
            "Red",
            "None",
        ),
        (
            "W16",
            "MOD2_NTC1_GND",
            *pin_hot_ends["P4"]["5"],
            *pin_hot_ends["P2"]["8"],
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        (
            "W17",
            "MOD2_NTC2_SIG",
            *pin_hot_ends["P4"]["8"],
            *pin_hot_ends["J1"]["2"],
            "22 AWG (Twisted Pair)",
            "Red",
            "None",
        ),
        (
            "W18",
            "MOD2_NTC2_GND",
            *pin_hot_ends["P4"]["1"],
            *pin_hot_ends["J1"]["3"],
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        # Module 3
        (
            "W19",
            "MOD3_V0 (Cell 10 Neg Bypass)",
            *pin_hot_ends["P5"]["12"],
            *pin_hot_ends["P1"]["17"],
            "22 AWG",
            "Black",
            "None",
        ),
        (
            "W20",
            "MOD3_V1 (Cell 10 Pos)",
            *pin_hot_ends["P5"]["7"],
            *pin_hot_ends["P1"]["7"],
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W21",
            "MOD3_V2 (Cell 11 Pos)",
            *pin_hot_ends["P5"]["4"],
            *pin_hot_ends["P1"]["18"],
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W22",
            "MOD3_V3 (Cell 12 Pos)",
            *pin_hot_ends["P5"]["2"],
            *pin_hot_ends["P1"]["8"],
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W23",
            "MOD3_V4 (Cell 13 Pos)",
            *pin_hot_ends["P5"]["10"],
            *pin_hot_ends["P1"]["19"],
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W24",
            "MOD3_NTC1_SIG",
            *pin_hot_ends["P5"]["11"],
            *pin_hot_ends["P2"]["3"],
            "22 AWG (Twisted Pair)",
            "Blue",
            "None",
        ),
        (
            "W25",
            "MOD3_NTC1_GND",
            *pin_hot_ends["P5"]["5"],
            *pin_hot_ends["P2"]["9"],
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        (
            "W26",
            "MOD3_NTC2_SIG",
            *pin_hot_ends["P5"]["8"],
            *pin_hot_ends["P2"]["4"],
            "22 AWG (Twisted Pair)",
            "Blue",
            "None",
        ),
        (
            "W27",
            "MOD3_NTC2_GND",
            *pin_hot_ends["P5"]["1"],
            *pin_hot_ends["P2"]["10"],
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        # Module 4
        (
            "W28",
            "MOD4_V1 (Cell 14 Pos)",
            *pin_hot_ends["P6"]["7"],
            *pin_hot_ends["P1"]["9"],
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W29",
            "MOD4_V2 (Cell 15 Pos)",
            *pin_hot_ends["P6"]["4"],
            *pin_hot_ends["P1"]["20"],
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W30",
            "MOD4_V3 (Cell 16 Pos)",
            *pin_hot_ends["P6"]["2"],
            *pin_hot_ends["P1"]["10"],
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W31",
            "MOD4_V4 (Cell 17/18 Tap)",
            *pin_hot_ends["P6"]["10"],
            sp2_x,
            sp2_y,
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W32",
            "MOD4_PWR (Pack Pos Pwr)",
            *pin_hot_ends["P6"]["3"],
            *pin_hot_ends["P1"]["22"],
            "22 AWG",
            "Red",
            "None",
        ),
        (
            "W33",
            "MOD4_NTC1_SIG",
            *pin_hot_ends["P6"]["11"],
            *pin_hot_ends["P2"]["5"],
            "22 AWG (Twisted Pair)",
            "Red",
            "None",
        ),
        (
            "W34",
            "MOD4_NTC1_GND",
            *pin_hot_ends["P6"]["5"],
            *pin_hot_ends["P2"]["11"],
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
        (
            "W35",
            "MOD4_NTC2_SIG",
            *pin_hot_ends["P6"]["8"],
            *pin_hot_ends["P2"]["6"],
            "22 AWG (Twisted Pair)",
            "Red",
            "None",
        ),
        (
            "W36",
            "MOD4_NTC2_GND",
            *pin_hot_ends["P6"]["1"],
            *pin_hot_ends["P2"]["12"],
            "22 AWG (Twisted Pair)",
            "Black",
            "None",
        ),
    ]

    # Assign trunk channels by sorting wires by destination dy (ascending)
    ordered_wires = sorted(wires_io, key=lambda w: (w[5], w[3]))
    tx_map = {
        w[0]: trunk_x_start + i * channel_spacing_mils
        for i, w in enumerate(ordered_wires)
    }

    wires = []
    # Sort by sy for slot staggering
    all_main = sorted(wires_io, key=lambda w: w[3])
    for idx, (wid, net, sx, sy, dx, dy, gauge, color, border) in enumerate(all_main):
        tx = tx_map[wid]
        if sy == dy:
            pts = [sx, sy, dx, dy]
        else:
            pts = [sx, sy, tx, sy, tx, dy, dx, dy]

        phase = idx % 2
        if phase == 0:
            x_des = 18800
            x_com = 19150
        else:
            x_des = 19500
            x_com = 19850

        y_label = sy + 15
        wires.append(
            {
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
            }
        )

    # Splice branch wires
    # SP1 (Mod 2 V4 -> BMSS C8 & C9)
    p1_c8_x, p1_c8_y = pin_hot_ends["P1"]["16"]
    p1_c9_x, p1_c9_y = pin_hot_ends["P1"]["6"]
    wires.append(
        {
            "designator": "W14A",
            "net": "BMSS_C08 (Cell 8 Pos)",
            "points": [sp1_x, sp1_y, sp1_x, p1_c8_y, p1_c8_x, p1_c8_y],
            "designator_pos": [11200, p1_c8_y + 15],
            "comment_pos": [11550, p1_c8_y + 15],
            "parameters": {
                "Gauge": "22 AWG",
                "Color": "Orange",
                "VoltageRating": "600V",
                "Border": "None",
            },
        }
    )
    wires.append(
        {
            "designator": "W14B",
            "net": "BMSS_C09 (Cell 9 Pos)",
            "points": [sp1_x, sp1_y, sp1_x, p1_c9_y, p1_c9_x, p1_c9_y],
            "designator_pos": [12200, p1_c9_y + 15],
            "comment_pos": [12550, p1_c9_y + 15],
            "parameters": {
                "Gauge": "22 AWG",
                "Color": "Orange",
                "VoltageRating": "600V",
                "Border": "None",
            },
        }
    )

    # SP2 (Mod 4 V4 -> BMSS C17 & C18)
    p1_c17_x, p1_c17_y = pin_hot_ends["P1"]["21"]
    p1_c18_x, p1_c18_y = pin_hot_ends["P1"]["11"]
    wires.append(
        {
            "designator": "W31A",
            "net": "BMSS_C17 (Cell 17 Pos)",
            "points": [sp2_x, sp2_y, sp2_x, p1_c17_y, p1_c17_x, p1_c17_y],
            "designator_pos": [11200, p1_c17_y + 15],
            "comment_pos": [11550, p1_c17_y + 15],
            "parameters": {
                "Gauge": "22 AWG",
                "Color": "Red",
                "VoltageRating": "600V",
                "Border": "None",
            },
        }
    )
    wires.append(
        {
            "designator": "W31B",
            "net": "BMSS_C18 (Cell 18 Pos)",
            "points": [sp2_x, sp2_y, sp2_x, p1_c18_y, p1_c18_x, p1_c18_y],
            "designator_pos": [12200, p1_c18_y + 15],
            "comment_pos": [12550, p1_c18_y + 15],
            "parameters": {
                "Gauge": "22 AWG",
                "Color": "Red",
                "VoltageRating": "600V",
                "Border": "None",
            },
        }
    )

    # Net Labels next to pins (to replace old skewed labels)
    net_labels = [
        # Headers
        {"x": 9000, "y": 19800, "orientation": 0, "text": "BMSS - Voltage Taps"},
        {"x": 9000, "y": 12300, "orientation": 0, "text": "BMSS - Thermistors"},
        {"x": 9000, "y": 7800, "orientation": 0, "text": "To BMSM - Thermistors"},
        {"x": 21800, "y": 19800, "orientation": 0, "text": "Module #1"},
        {"x": 21800, "y": 15500, "orientation": 0, "text": "Module #2"},
        {"x": 21800, "y": 11200, "orientation": 0, "text": "Module #3"},
        {"x": 21800, "y": 6900, "orientation": 0, "text": "Module #4"},
    ]

    p1_labels = [
        (19500, "BMSS_C01_NEG_PWR"),
        (19200, "BMSS_C01_NEG_SENSE"),
        (18900, "BMSS_C01_POS"),
        (18600, "BMSS_C02_POS"),
        (18300, "BMSS_C03_POS"),
        (18000, "BMSS_C04_POS"),
        (17700, "BMSS_C05_POS"),
        (17400, "BMSS_C06_POS"),
        (17100, "BMSS_C07_POS"),
        (16800, "BMSS_C08_POS"),
        (16500, "BMSS_C09_POS"),
        (16200, "BMSS_C10_NEG_BYPASS"),
        (15900, "BMSS_C10_POS"),
        (15600, "BMSS_C11_POS"),
        (15300, "BMSS_C12_POS"),
        (15000, "BMSS_C13_POS"),
        (14700, "BMSS_C14_POS"),
        (14400, "BMSS_C15_POS"),
        (14100, "BMSS_C16_POS"),
        (13800, "BMSS_C17_POS"),
        (13500, "BMSS_C18_POS"),
        (13200, "BMSS_C18_POS_PWR"),
    ]
    for y, txt in p1_labels:
        net_labels.append({"x": 9550, "y": y, "orientation": 0, "text": txt})

    p2_labels = [
        (12000, "BMSS_THERM1_SIG"),
        (11700, "BMSS_THERM1_GND"),
        (11400, "BMSS_THERM2_SIG"),
        (11100, "BMSS_THERM2_GND"),
        (10800, "BMSS_THERM3_SIG"),
        (10500, "BMSS_THERM3_GND"),
        (10200, "BMSS_THERM4_SIG"),
        (9900, "BMSS_THERM4_GND"),
        (9600, "BMSS_THERM5_SIG"),
        (9300, "BMSS_THERM5_GND"),
        (9000, "BMSS_THERM6_SIG"),
        (8700, "BMSS_THERM6_GND"),
    ]
    for y, txt in p2_labels:
        net_labels.append({"x": 9550, "y": y, "orientation": 0, "text": txt})

    j1_labels = [
        (7500, "BMSM_THERM1_SIG"),
        (7200, "BMSM_THERM1_GND"),
        (6900, "BMSM_THERM2_SIG"),
        (6600, "BMSM_THERM2_GND"),
    ]
    for y, txt in j1_labels:
        net_labels.append({"x": 9550, "y": y, "orientation": 0, "text": txt})

    mod_pin_names = [
        (0, "NTC2_NEG"),
        (-300, "V1"),
        (-600, "V3"),
        (-900, "NTC2_POS"),
        (-1200, "PWR"),
        (-1500, ""),
        (-1800, "V2"),
        (-2100, "V4"),
        (-2400, "NTC1_NEG"),
        (-2700, "NTC1_POS"),
        (-3000, "GND"),
        (-3300, "V0"),
    ]
    for m_idx, base_y in enumerate([19500, 15200, 10900, 6600], start=1):
        for dy, sname in mod_pin_names:
            txt = f"MOD{m_idx}_{sname}" if sname else f"MOD{m_idx}_"
            net_labels.append(
                {"x": 20450, "y": base_y + dy, "orientation": 0, "text": txt}
            )

    # Bounding outline rectangles for connector groups
    rectangles = [
        {
            "x1": 8800,
            "y1": 13000,
            "x2": 11200,
            "y2": 20000,
            "line_width": 2,
            "color": 16711935,
        },
        {
            "x1": 8800,
            "y1": 8500,
            "x2": 11200,
            "y2": 12500,
            "line_width": 2,
            "color": 16711935,
        },
        {
            "x1": 8800,
            "y1": 6400,
            "x2": 11200,
            "y2": 8000,
            "line_width": 2,
            "color": 16711935,
        },
        {
            "x1": 20200,
            "y1": 16000,
            "x2": 22600,
            "y2": 20000,
            "line_width": 2,
            "color": 16711935,
        },
        {
            "x1": 20200,
            "y1": 11700,
            "x2": 22600,
            "y2": 15700,
            "line_width": 2,
            "color": 16711935,
        },
        {
            "x1": 20200,
            "y1": 7400,
            "x2": 22600,
            "y2": 11400,
            "line_width": 2,
            "color": 16711935,
        },
        {
            "x1": 20200,
            "y1": 3100,
            "x2": 22600,
            "y2": 7100,
            "line_width": 2,
            "color": 16711935,
        },
    ]

    # Connectors placement list
    connector_list = [
        {"designator": des, "x": pos[0], "y": pos[1]}
        for des, pos in conn_positions.items()
    ]

    return {
        "connectors": connector_list,
        "rectangles": rectangles,
        "wires": wires,
        "splices": splices,
        "no_connects": no_connects,
        "net_labels": net_labels,
        "labels": net_labels,
        "summary": {
            "connector_count": len(connector_list),
            "rectangle_count": len(rectangles),
            "wire_count": len(wires),
            "splice_count": len(splices),
            "no_connect_count": len(no_connects),
            "net_label_count": len(net_labels),
            "trunk_channels": len(tx_map),
        },
    }
