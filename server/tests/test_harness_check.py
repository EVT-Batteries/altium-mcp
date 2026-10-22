"""Unit tests for harness_check (read-only drawing check of a .WirDoc).

Run from repo root:  python -m unittest server.tests.test_harness_check -v
or from server/:      python -m unittest tests.test_harness_check -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import harness_check
from harness_check import LABEL_SPACING, check_harness_wiring, label_positions
from harness_parser import parse_wirdoc_records


def _pin(des, x, y, facing_right, length=20):
    """Pin with its hot end at (x, y), DXP units."""
    base = x - length if facing_right else x + length
    return {
        "designator": des, "name": des, "length": length,
        "orientation": 0 if facing_right else 2,
        "location": {"x": base, "y": y}, "hot_end": {"x": x, "y": y},
    }


# J1 on the left (pins facing right, hot ends x=120), P1 on the right
# (pins facing left, hot ends x=500); pins at y = 500, 470, 300
J1 = {"designator": "J1", "pins": [_pin("1", 120, 500, True), _pin("2", 120, 470, True)]}
P1 = {"designator": "P1", "pins": [
    _pin("1", 500, 300, False), _pin("2", 500, 500, False), _pin("3", 500, 470, False),
]}


def _wire(des, points, net="N", labels=None):
    w = {"designator": des, "net": net, "points": points, "parameters": {}}
    if labels:
        w["designator_pos"] = {"x": labels[0], "y": labels[1]}
        w["comment_pos"] = {"x": labels[0] + 37.5, "y": labels[1]}
    return w


def _sheet(wires, connectors=(J1, P1), splices=(), twists=(), breaks=(), texts=()):
    return {
        "wires": list(wires), "connectors": list(connectors), "splices": list(splices),
        "twists": list(twists), "wire_breaks": list(breaks), "net_labels": list(texts),
    }


class TestHarnessCheck(unittest.TestCase):

    def check(self, parsed):
        with patch.object(harness_check, "parse_wirdoc", return_value=parsed), \
             patch.object(Path, "exists", return_value=True):
            return check_harness_wiring("sheet.WirDoc")

    def types(self, res):
        return [p["type"] for p in res["problems"]]

    def test_label_positions_side_by_side(self):
        des, com = label_positions([0, 0, 4000, 0, 4000, 1000, 6000, 1000])
        self.assertEqual(des[1], com[1])
        self.assertEqual(com[0] - des[0], LABEL_SPACING)
        self.assertEqual(des[1], 15)  # just above the longest (first) run

    def test_clean_staircase(self):
        res = self.check(_sheet([
            _wire("W1", [120, 500, 300, 500, 300, 300, 500, 300], labels=(140, 501.5)),
        ]))
        self.assertTrue(res["clean"], res["problems"])
        self.assertEqual(res["summary"]["crossings"], 0)

    def test_diagonal_and_dangling_end_are_errors(self):
        res = self.check(_sheet([_wire("W1", [120, 500, 300, 400])]))
        self.assertIn("diagonal", self.types(res))
        self.assertIn("dangling_end", self.types(res))
        self.assertEqual(res["summary"]["errors"], 2)

    def test_wire_break_is_a_valid_end(self):
        res = self.check(_sheet([_wire("W1", [120, 500, 300, 500])], breaks=[{"x": 300, "y": 500}]))
        self.assertNotIn("dangling_end", self.types(res))

    def test_overlap_is_an_error(self):
        res = self.check(_sheet([
            _wire("W1", [120, 500, 300, 500, 300, 300, 500, 300]),
            _wire("W2", [120, 470, 200, 470, 200, 450, 300, 450, 300, 400, 500, 400]),
        ], connectors=(J1, P1, {"designator": "P2", "pins": [_pin("1", 500, 400, False)]})))
        overlaps = [p for p in res["problems"] if p["type"] == "overlap"]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["wires"], ["W1", "W2"])

    def test_wire_through_another_connector(self):
        res = self.check(_sheet(
            [_wire("W1", [120, 500, 600, 500, 600, 300, 700, 300])],
            splices=[{"location": {"x": 700, "y": 300}}],
        ))
        hits = [p for p in res["problems"] if p["type"] == "through_connector"]
        self.assertEqual(len(hits), 1)
        self.assertIn("P1", hits[0]["detail"])

    def test_crossing_is_counted_not_reported(self):
        # W2 starts below W1 but ends above it: one crossing no routing removes
        res = self.check(_sheet([
            _wire("W1", [120, 500, 300, 500, 300, 300, 500, 300]),
            _wire("W2", [120, 470, 350, 470, 350, 400, 500, 400]),
        ], connectors=(J1, P1, {"designator": "P2", "pins": [_pin("1", 500, 400, False)]})))
        self.assertEqual(res["summary"]["crossings"], 1)
        self.assertEqual(res["summary"]["errors"], 0)
        self.assertNotIn("crossing_near_bend", self.types(res))

    def test_crossing_next_to_a_bend(self):
        res = self.check(_sheet([
            _wire("W1", [120, 500, 300, 500, 300, 300, 500, 300]),
            _wire("W2", [120, 470, 305, 470, 305, 600]),
        ], breaks=[{"x": 305, "y": 600}]))
        self.assertIn("crossing_near_bend", self.types(res))

    def test_label_on_another_wire(self):
        brk = [{"x": 500, "y": 520}]
        res = self.check(_sheet([
            _wire("W1", [120, 500, 500, 500], labels=(300, 501.5)),
            _wire("W2", [120, 470, 200, 470, 200, 520, 500, 520]),
        ], breaks=brk))
        self.assertNotIn("label_on_wire", self.types(res))
        res = self.check(_sheet([
            _wire("W1", [120, 500, 500, 500], labels=(300, 501.5)),
            _wire("W2", [120, 470, 310, 470, 310, 520, 500, 520]),
        ], breaks=brk))
        self.assertIn("label_on_wire", self.types(res))

    def test_twist_in_sheet_style(self):
        # Wires at 4700 / 5000 mils: rectangle 100 beyond each, 500 along
        wires = [_wire("W1", [120, 500, 500, 500]), _wire("W2", [120, 470, 500, 470])]
        good = {"x1": 250, "y1": 460, "x2": 300, "y2": 510, "designator": "TW1", "wires": ["W1", "W2"]}
        self.assertTrue(self.check(_sheet(wires, twists=[good]))["clean"])
        flush = {"x1": 250, "y1": 470, "x2": 260, "y2": 500, "designator": "TW1", "wires": ["W1", "W2"]}
        details = [p["detail"] for p in self.check(_sheet(wires, twists=[flush]))["problems"]]
        self.assertTrue(any("hook tips land on the wires at [4600, 5100]" in d for d in details))
        self.assertTrue(any("100 mils long" in d for d in details))

    def test_twist_on_a_label(self):
        wires = [_wire("W1", [120, 500, 500, 500], labels=(260, 501.5)), _wire("W2", [120, 470, 500, 470])]
        twist = {"x1": 250, "y1": 460, "x2": 300, "y2": 510, "designator": "TW1", "wires": ["W1", "W2"]}
        self.assertIn("twist_on_label", self.types(self.check(_sheet(wires, twists=[twist]))))

    def test_parser_keeps_visible_wire_label_positions(self):
        records = [
            {"HEADER": "x"},
            {"RECORD": "107", "LocationCount": "2", "X1": "100", "Y1": "500", "X2": "300", "Y2": "500"},
            {"RECORD": "41", "OwnerIndex": "0", "Name": "Designator", "Text": "W1",
             "Location.X": "150", "Location.Y": "501", "Location.Y_Frac": "50000"},
            {"RECORD": "41", "OwnerIndex": "0", "Name": "Comment", "Text": "NET",
             "Location.X": "190", "Location.Y": "501", "IsHidden": "T"},
        ]
        wire = parse_wirdoc_records(records, "sheet.WirDoc")["wires"][0]
        self.assertEqual(wire["designator_pos"], {"x": 150, "y": 501.5})
        self.assertNotIn("comment_pos", wire)


if __name__ == "__main__":
    unittest.main()
