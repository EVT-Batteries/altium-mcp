"""Unit tests for harness_router (orthogonal in-place re-routing).

Run from repo root:  python -m unittest server.tests.test_harness_router -v
or from server/:      python -m unittest tests.test_harness_router -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import harness_router  # noqa: E402
from harness_router import (  # noqa: E402
    LABEL_SPACING,
    _crossings,
    calculate_harness_reroute,
    label_positions,
)

WORKSPACE_DIR = Path(__file__).resolve().parents[3]
SLAVE_WIRDOC = (
    WORKSPACE_DIR / "P42C-1_Slave_Harness" / "P42C-1_Slave_Sense_Harness.WirDoc"
)


def _pin(des, name, x, y, length=20):
    """Left-facing pin of a right-hand connector; hot end at (x, y) in DXP units."""
    return {
        "designator": des, "name": name, "length": length, "orientation": 2,
        "location": {"x": x + length, "y": y}, "hot_end": {"x": x, "y": y},
    }


def _wire(des, net, x1, y1, x2, y2):
    """A rubber-banded (diagonal) wire in DXP units."""
    return {"designator": des, "net": net, "points": [x1, y1, x2, y2], "parameters": {}}


def _sheet(wires, connectors=(), splices=()):
    return {"wires": list(wires), "connectors": list(connectors), "splices": list(splices)}


class TestHarnessRouter(unittest.TestCase):

    def route(self, parsed, **kw):
        with patch.object(harness_router, "parse_wirdoc", return_value=parsed), \
             patch.object(Path, "exists", return_value=True):
            return calculate_harness_reroute("sheet.WirDoc", **kw)

    def assert_orthogonal(self, pts):
        for i in range(0, len(pts) - 2, 2):
            x1, y1, x2, y2 = pts[i:i + 4]
            self.assertTrue(x1 == x2 or y1 == y2, f"diagonal segment in {pts}")

    def test_label_positions_side_by_side(self):
        des, com = label_positions([0, 0, 4000, 0, 4000, 1000, 6000, 1000])
        self.assertEqual(des[1], com[1])
        self.assertEqual(com[0] - des[0], LABEL_SPACING)
        self.assertEqual(des[1], 15)  # just above the longest (first) run

    def test_straight_wire_stays_straight(self):
        res = self.route(_sheet([_wire("W1", "A", 100, 500, 900, 500)]))
        self.assertEqual(res["reroutes"][0]["points"], [1000, 5000, 9000, 5000])

    def test_endpoints_and_direction_preserved(self):
        # Stored right -> left: the route must start where the wire starts,
        # or Altium would re-attach the ends to the wrong objects
        res = self.route(_sheet([_wire("W1", "A", 900, 300, 100, 700)]))
        pts = res["reroutes"][0]["points"]
        self.assertEqual(pts[:2], [9000, 3000])
        self.assertEqual(pts[-2:], [1000, 7000])
        self.assert_orthogonal(pts)

    def test_nested_staircase_has_no_crossings(self):
        # Four UP wires keeping their vertical order: routable without crossings
        wires = [_wire(f"W{i}", f"N{i}", 100, 100 + 10 * i, 1500, 600 + 10 * i) for i in range(4)]
        res = self.route(_sheet(wires))
        self.assertEqual(res["summary"]["crossings"], 0)
        xs = {w["designator"]: w["points"][2] for w in res["reroutes"]}
        # lowest start in the channel closest to the destination
        self.assertGreater(xs["W0"], xs["W1"])
        self.assertGreater(xs["W1"], xs["W2"])
        for x in xs.values():
            self.assertEqual(x % 100, 0)
        gaps = sorted(xs.values())
        self.assertTrue(all(b - a >= 300 for a, b in zip(gaps, gaps[1:])))

    def test_non_overlapping_spans_share_a_channel(self):
        wires = [
            _wire("W1", "A", 100, 100, 1500, 200),
            _wire("W2", "B", 100, 600, 1500, 700),
        ]
        res = self.route(_sheet(wires))
        self.assertEqual(res["summary"]["channels"], 1)

    def test_channels_clear_pin_names(self):
        # Right connector pins named 20 characters: 20 x 65 = 1300 mils from the
        # pin base, i.e. 1100 beyond a 200 mil pin, + 150 clearance
        pins = [_pin("1", "X" * 20, 1500, 600)]
        res = self.route(_sheet([_wire("W1", "A", 100, 100, 1500, 600)], [{"pins": pins}]))
        channel_x = res["reroutes"][0]["points"][2]
        self.assertLessEqual(channel_x, 15000 - 1250)

    def test_corridors_follow_connector_pairs(self):
        wires = [
            _wire("W1", "A", 100, 100, 800, 300),
            _wire("W2", "B", 1200, 100, 2000, 300),
        ]
        res = self.route(_sheet(wires))
        self.assertEqual(res["summary"]["corridors"], 2)
        x1 = res["reroutes"][0]["points"][2]
        x2 = res["reroutes"][1]["points"][2]
        self.assertTrue(1000 < x1 < 8000 and 12000 < x2 < 20000)

    def test_duplicate_designators_are_reported_not_moved(self):
        wires = [_wire("W1", "A", 100, 100, 900, 300), _wire("W1", "B", 100, 400, 900, 600)]
        res = self.route(_sheet(wires))
        self.assertEqual(res["reroutes"], [])
        self.assertTrue(any("not unique" in w for w in res["warnings"]))

    def test_crossings_counts_proper_intersections_only(self):
        self.assertEqual(_crossings([0, 5, 10, 5], [[5, 0, 5, 10]]), 1)
        self.assertEqual(_crossings([0, 5, 10, 5], [[10, 0, 10, 10]]), 0)  # touching end

    def test_reroute_slave_sheet(self):
        if not SLAVE_WIRDOC.exists():
            self.skipTest("Sample Slave WirDoc not found in workspace")
        res = calculate_harness_reroute(SLAVE_WIRDOC)
        self.assertEqual(len(res["reroutes"]), 40)
        for r in res["reroutes"]:
            self.assert_orthogonal(r["points"])
            self.assertEqual(r["designator_pos"][1], r["comment_pos"][1])
            self.assertEqual(r["comment_pos"][0] - r["designator_pos"][0], LABEL_SPACING)


if __name__ == "__main__":
    unittest.main()
