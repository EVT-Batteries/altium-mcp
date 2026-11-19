"""plan_flat_layout: formboard-style layout of a synthetic bundle tree (no Altium, no files)."""
import itertools
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness_flat_layout import FlatLayoutError, plan_flat_layout, segments_cross  # noqa: E402


def _cp(des, x, y):
    return {"designator": des, "location": {"x": x, "y": y}, "show_name": True, "unique_id": "U" + des}


def _conn(des, cp, x, y, ori=0):
    return {"designator": des, "connection_point": cp, "location": {"x": x, "y": y}, "orientation": ori,
            "mirrored": False, "unique_id": "C" + des,
            "texts": {"Designator": {"x": x - 5, "y": y + 10, "hidden": False, "justification": "bottom_left"}}}


def _bundle(des, a, z, mm, wires):
    return {"designator": des, "end1_connection_point": a, "end2_connection_point": z, "length": {"mm": mm},
            "unique_id": "B" + des, "wires": wires}


def _layout():
    # P1 - CPA - CPB - J2 trunk; J1 off CPB, P6 off CPA, P7 off a breakout CPC on P6's branch
    cps = [_cp("CP_P1", 100, 100), _cp("CPA", 200, 100), _cp("CPB", 210, 100), _cp("CP_J1", 300, 130),
           _cp("CP_J2", 320, 100), _cp("CP_P6", 300, 60), _cp("CPC", 260, 70), _cp("CP_P7", 320, 40)]
    conns = [_conn("P1", "CP_P1", 100, 90), _conn("J1", "CP_J1", 309, 130, 90), _conn("J2", "CP_J2", 329, 100),
             _conn("P6", "CP_P6", 300, 51), _conn("P7", "CP_P7", 311, 40)]
    w16 = [f"W{i}" for i in range(16)]
    bundles = [_bundle("B1", "CP_P1", "CPA", 170, w16), _bundle("B2", "CPA", "CPB", 4.4, w16[:12]),
               _bundle("B3", "CPA", "CPC", 60, w16[12:]), _bundle("B6", "CPC", "CP_P6", 20, w16[12:14]),
               _bundle("B7", "CPC", "CP_P7", 25, w16[14:]),
               _bundle("B4", "CP_J1", "CPB", 29, w16[:6]), _bundle("B5", "CP_J2", "CPB", 55, w16[6:12])]
    return {"connection_points": cps, "connectors": conns, "bundles": bundles}


def _segments(plan):
    segs = []
    for b in plan["bundles"]:
        p = b["points"]
        pts = list(zip(p[0::2], p[1::2]))
        segs += [(b["designator"], pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    return segs


def test_trunk_is_horizontal_from_the_root():
    plan = plan_flat_layout(_layout(), {"root": "P1", "width_mils": 6000})
    rep = plan["report"]
    assert rep["trunk"] == ["CP_P1", "CPA", "CPB", "CP_J2"]   # B2 carries 12 wires, B3 4
    b1 = next(b for b in plan["bundles"] if b["designator"] == "B1")
    assert b1["points"][1] == b1["points"][3] == 1000          # root at its place, trunk at y = 1000
    assert b1["points"][0] == 1000 and b1["points"][2] > 1000


def test_no_crossing_and_no_overlap():
    plan = plan_flat_layout(_layout(), {"root": "P1"})
    segs = _segments(plan)
    for (n1, a, b), (n2, c, d) in itertools.combinations(segs, 2):
        assert not segments_cross(a, b, c, d), (n1, n2)
    # end points of different bundles never coincide except at shared connection points
    cps = {(c["x"], c["y"]) for c in plan["connection_points"] if "x" in c}
    cps |= {(c["connection_point"]["x"], c["connection_point"]["y"]) for c in plan["connectors"]}
    ends = {}
    for name, a, b in segs:
        for p in (a, b):
            ends.setdefault(p, set()).add(name)
    for p, names in ends.items():
        assert len(names) == 1 or p in cps, (p, names)


def test_short_bundle_drawn_at_least_the_minimum_with_break_symbol():
    plan = plan_flat_layout(_layout(), {"root": "P1", "min_bundle_mils": 500, "lengths": {"B2": 4.4}})
    b2 = next(b for b in plan["bundles"] if b["designator"] == "B2")
    assert math.dist(b2["points"][:2], b2["points"][2:4]) >= 500
    assert b2["length_mm"] == 4.4 and b2["show_break_symbol"] is True


def test_branches_alternate_and_take_lanes():
    plan = plan_flat_layout(_layout(), {"root": "P1", "lane_spacing": 700, "first_side": "up"})
    pos = {c["designator"]: (c["x"], c["y"]) for c in plan["connection_points"] if "x" in c}
    pos |= {c["designator"]: (c["connection_point"]["x"], c["connection_point"]["y"]) for c in plan["connectors"]}
    y0 = pos["P1"][1]
    above = [d for d in ("J1", "P6") if pos[d][1] > y0]
    below = [d for d in ("J1", "P6") if pos[d][1] < y0]
    assert len(above) == 1 and len(below) == 1                  # one branch on each side
    # on the branch through CPC the run continues to P7 (tie on wires: the longer way) and P6
    # leaves it outward, one lane further out
    assert pos["P7"][1] == pos["CPC"][1]
    assert abs(pos["P6"][1] - y0) > abs(pos["CPC"][1] - y0)


def test_connectors_face_outward():
    plan = plan_flat_layout(_layout(), {"root": "P1"})
    for c in plan["connectors"]:
        bx = c["x"] - c["connection_point"]["x"]
        if c["designator"] == "P1":
            assert bx <= 0
        else:
            assert bx >= 0, c


def test_connector_texts_turn_with_the_connector():
    """a connector turned a quarter turn keeps its texts beside its body, not on the bundle"""
    lay = _layout()
    j2 = next(c for c in lay["connectors"] if c["designator"] == "J2")
    j2["location"] = {"x": 320, "y": 109}                                  # body above its point
    j2["texts"] = {"Comment": {"x": 300, "y": 109, "hidden": False, "justification": "bottom_left"}}
    plan = plan_flat_layout(lay, {"root": "P1", "connector_texts": "keep"})
    c = next(c for c in plan["connectors"] if c["designator"] == "J2")
    cpx, cpy = c["connection_point"]["x"], c["connection_point"]["y"]
    assert (c["x"] - cpx, c["y"] - cpy) == (90, 0)                         # turned to face right
    # the comment sat 200 mils left of the body; turned with it, it sits 200 mils above it
    assert (c["texts"]["comment"]["x"] - c["x"], c["texts"]["comment"]["y"] - c["y"]) == (0, 200)


def test_connector_texts_around_the_picture():
    """default: designator above the connector's picture, comment and parameters below it"""
    lay = _layout()
    j2 = next(c for c in lay["connectors"] if c["designator"] == "J2")
    j2["location"] = {"x": 320, "y": 109}
    j2["views"] = [{"main": True, "hidden": False, "image": {"x1": 316, "y1": 103, "x2": 324, "y2": 117}}]
    j2["texts"] = {"Designator": {"x": 300, "y": 120, "hidden": False}, "Comment": {"x": 300, "y": 109, "hidden": False},
                   "Supplier": {"x": 300, "y": 90, "hidden": False}, "Hidden": {"x": 0, "y": 0, "hidden": True}}
    plan = plan_flat_layout(lay, {"root": "P1"})
    c = next(c for c in plan["connectors"] if c["designator"] == "J2")
    # picture 80 x 140 mils about the body, turned a quarter turn: 140 wide, 80 high
    x1, y1, y2 = c["x"] - 60, c["y"] - 40, c["y"] + 40
    t = c["texts"]
    assert t["designator"] == {"x": x1, "y": y2 + 50, "justification": "bottom_left"}
    assert t["comment"] == {"x": x1, "y": y1 - 50, "justification": "top_left"}
    assert t["supplier"] == {"x": x1, "y": y1 - 180, "justification": "top_left"}
    assert "hidden" not in t


def test_loop_is_refused():
    lay = _layout()
    lay["bundles"].append(_bundle("B9", "CP_J1", "CP_J2", 10, ["W1"]))
    with pytest.raises(FlatLayoutError):
        plan_flat_layout(lay, {"root": "P1"})


def test_unknown_length_is_refused():
    with pytest.raises(FlatLayoutError):
        plan_flat_layout(_layout(), {"root": "P1", "lengths": {"B42": 1}})


def test_notes_and_pictures_follow_their_connector():
    lay = _layout()
    lay["notes"] = [{"unique_id": "N1", "x1": 330, "y1": 95, "x2": 350, "y2": 105}]   # beside J2
    lay["pictures"] = [{"unique_id": "PIC1", "x1": 2000, "y1": 2000, "x2": 2010, "y2": 2010}]   # far from all
    plan = plan_flat_layout(lay, {"root": "P1"})
    j2 = next(c for c in plan["connectors"] if c["designator"] == "J2")
    moved = {m["unique_id"]: m["offset"] for m in plan["move_objects"]}
    assert moved["N1"] == [j2["x"] - 3290, j2["y"] - 1000]
    assert "PIC1" not in moved


def test_graphics_move_as_groups_and_clear_the_bundles():
    """a photo with an inset photo inside it moves as one; a group lying across a bundle after
    the move is pushed off it, away from its connector's point, and the report says so"""
    lay = _layout()
    # a big picture across the trunk end near J2 (connection point CP_J2 at 3200, 1000), with
    # an inset picture inside it
    lay["pictures"] = [{"unique_id": "BIG", "x1": 300, "y1": 80, "x2": 360, "y2": 110},
                       {"unique_id": "INSET", "x1": 310, "y1": 85, "x2": 320, "y2": 95}]
    plan = plan_flat_layout(lay, {"root": "P1"})
    mv = {m["unique_id"]: m["offset"] for m in plan["move_objects"]}
    assert mv["BIG"] == mv["INSET"]
    big = [3000 + mv["BIG"][0], 800 + mv["BIG"][1], 3600 + mv["BIG"][0], 1100 + mv["BIG"][1]]
    for name, a, b in _segments(plan):
        lo, hi = min(a[1], b[1]), max(a[1], b[1])
        if min(a[0], b[0]) <= big[2] and max(a[0], b[0]) >= big[0]:
            assert hi < big[1] or lo > big[3], (name, a, b, big)
    pushed = plan["report"]["graphics_pushed"]
    assert pushed[0]["graphics"] == ["BIG", "INSET"] and pushed[0]["connector"] == "J2"
    assert pushed[0]["pushed_mils"] < 0          # centre below the point: pushed down


def test_length_check_sees_lengths_with_summary_only(tmp_path):
    """get_harness_data(summary_only=True, expected_lengths=...) checks the full wire records."""
    import asyncio
    import json
    from unittest.mock import patch

    import main

    doc = tmp_path / "A.LdrDoc"
    doc.write_bytes(b"")
    layout = {"connectors": [], "wires": [{"designator": "W1", "end1": "P1.1", "end2": "J1.1",
                                           "routed_length": {"mm": 100.0}, "length": {"mm": 120.0},
                                           "length_type": "mcad"}],
              "summary": {}, "unrouted_wires": []}
    with patch("main.parse_ldrdoc", return_value=layout):
        out = json.loads(asyncio.run(main.get_harness_data(None, str(doc), summary_only=True,
                                                           expected_lengths={"W1": 120.0})))
    assert out["length_check"]["matched"] == 1 and out["length_check"]["ok"], out["length_check"]
