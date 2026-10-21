"""Unit tests for harness_layout (layout drawing spec builder and checks) and
the edit_harness_layout tool. Synthetic layouts only (parse_ldrdoc output
shape, coordinates in DXP units = 10 mils); nothing reaches Altium.

Run from server/:  python -m unittest tests.test_harness_layout -v
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from harness_layout import (
    _follow_end,
    build_layout_spec,
    check_layout,
    coord_to_mm,
    mm_to_coord,
)


def _cp(des, uid, x, y, objects=(), bundles=()):
    return {
        "designator": des,
        "location": {"x": x, "y": y},
        "orientation": 0,
        "style": "circle",
        "show_name": True,
        "unique_id": uid,
        "designator_location": {"x": x + 20, "y": y + 40},
        "objects": list(objects),
        "bundles": list(bundles),
    }


def _bundle(des, uid, points, end1, end2, cp1, cp2):
    return {
        "designator": des,
        "comment": "",
        "points": points,
        "length": {"mils": 1000.0, "mm": 25.4},
        "drawn_length": {"mils": 1000.0, "mm": 25.4},
        "length_set_manually": False,
        "show_break_symbol": False,
        "line_width": 1,
        "unique_id": uid,
        "end1_uid": end1,
        "end2_uid": end2,
        "end1_connection_point": cp1,
        "end2_connection_point": cp2,
        "wires": [],
    }


def make_layout():
    """J1 (CP_J1 at 1000,1000) -- B1 -- CP1 (1200,1200, holds SP1) ; P1 and
    its CP_P1 at 2000,1200 with no bundle; COV1 on B1; label L1; SP2 free."""
    return {
        "connectors": [
            {
                "designator": "J1",
                "location": {"x": 950, "y": 1000},
                "orientation": 0,
                "mirrored": False,
                "unique_id": "UJ1",
                "connection_point": "CP_J1",
            },
            {
                "designator": "P1",
                "location": {"x": 2050, "y": 1200},
                "orientation": 0,
                "mirrored": False,
                "unique_id": "UP1",
                "connection_point": "CP_P1",
            },
        ],
        "connection_points": [
            _cp("CP_J1", "UCPJ1", 1000, 1000, ["J1"], ["B1"]),
            _cp("CP1", "UCP1", 1200, 1200, ["SP1"], ["B1"]),
            _cp("CP_P1", "UCPP1", 2000, 1200, ["P1"]),
        ],
        "bundles": [
            _bundle(
                "B1",
                "UB1",
                [1000, 1000, 1200, 1000, 1200, 1200],
                "UCPJ1",
                "UCP1",
                "CP_J1",
                "CP1",
            )
        ],
        "coverings": [
            {
                "designator": "COV1",
                "unique_id": "UCOV1",
                "items": ["B1"],
            }
        ],
        "layout_labels": [
            {
                "designator": "L1",
                "text": "OLD",
                "location": {"x": 1500, "y": 1500},
                "unique_id": "UL1",
            }
        ],
        "splices": [
            {"designator": "SP1", "unique_id": "USP1"},
            {"designator": "SP2", "unique_id": "USP2"},
        ],
        "wires": [],
        "unrouted_wires": [],
    }


def records_of(spec, kind):
    return [r for r in spec["records"] if r[0] == kind]


class TestUnits(unittest.TestCase):
    def test_mm_coord_round_trip(self):
        self.assertEqual(mm_to_coord(254), 100_000_000)  # 10000 mils
        self.assertEqual(coord_to_mm(100_000_000), 254.0)

    def test_follow_end_keeps_end_segment_straight(self):
        # horizontal first segment: neighbour takes the new y
        self.assertEqual(
            _follow_end([0, 0, 100, 0, 100, 100], True, -50, 20),
            [-50, 20, 100, 20, 100, 100],
        )
        # vertical last segment: neighbour takes the new x
        self.assertEqual(
            _follow_end([0, 0, 100, 0, 100, 100], False, 150, 120),
            [0, 0, 150, 0, 150, 120],
        )
        # two-point bundle: only the end moves
        self.assertEqual(_follow_end([0, 0, 100, 0], False, 100, 50), [0, 0, 100, 50])


class TestBuildLayoutSpec(unittest.TestCase):
    def setUp(self):
        self.layout = make_layout()

    def test_new_connection_point_and_bundle_from_connector(self):
        spec = build_layout_spec(
            self.layout,
            connection_points=[
                {
                    "designator": "CP2",
                    "x": 16000,
                    "y": 12000,
                    "style": "square",
                    "objects": ["SP2"],
                }
            ],
            bundles=[
                {"designator": "B2", "from": "CP1", "to": "CP2", "length_mm": 300},
                {"from": "CP2", "to": "P1", "via": [[16000, 12000]]},
            ],
        )
        self.assertEqual(spec["errors"], [])
        cp = records_of(spec, "CPOINT")[0]
        # new: no uid, designator placed at the default offset
        self.assertEqual(
            cp, ["CPOINT", "", "CP2", 16000, 12000, 1, "", "", 16200, 12400]
        )
        self.assertEqual(
            records_of(spec, "CPASSIGN"), [["CPASSIGN", "DES:CP2", "USP2"]]
        )
        self.assertEqual(records_of(spec, "CPUNASSIGN"), [])
        b2, b3 = records_of(spec, "BUNDLE")
        self.assertEqual(b2[:5], ["BUNDLE", "", "B2", "", mm_to_coord(300)])
        self.assertEqual(b2[7:], [12000, 12000, 16000, 12000])
        # unnamed new bundle gets the next free B<n>; a connector means its CP
        self.assertEqual(b3[2], "B3")
        self.assertEqual(b3[7:], [16000, 12000, 16000, 12000, 20000, 12000])
        self.assertEqual(
            spec["expect"]["bundles"], {"B2": ["CP1", "CP2"], "B3": ["CP2", "CP_P1"]}
        )

    def test_moving_a_connection_point_moves_its_bundle_ends(self):
        spec = build_layout_spec(
            self.layout, connection_points=[{"designator": "CP1", "x": 13000}]
        )
        self.assertEqual(spec["errors"], [])
        cp = records_of(spec, "CPOINT")[0]
        # designator keeps its offset from the point (+200, +400 mils)
        self.assertEqual(
            cp, ["CPOINT", "UCP1", "", 13000, 12000, "", "", "", 13200, 12400]
        )
        (b1,) = records_of(spec, "BUNDLE")
        self.assertEqual(b1[1], "UB1")
        self.assertEqual(b1[7:], [10000, 10000, 13000, 10000, 13000, 12000])
        self.assertEqual(spec["expect"]["bundles"], {"B1": ["CP_J1", "CP1"]})

    def test_property_edit_still_follows_moved_ends(self):
        # A bundle named only for a property change must still follow its
        # moved connection point (it lost its connection otherwise)
        spec = build_layout_spec(
            self.layout,
            connection_points=[{"designator": "CP1", "x": 13000}],
            bundles=[{"designator": "B1", "length_mm": None}],
        )
        rec = records_of(spec, "BUNDLE")
        self.assertEqual(rec[0][:5], ["BUNDLE", "UB1", "", "", "AUTO"])
        self.assertEqual(rec[0][7:], [])
        self.assertEqual(rec[1][7:], [10000, 10000, 13000, 10000, 13000, 12000])

    def test_connector_move_takes_its_connection_point_along(self):
        spec = build_layout_spec(
            self.layout,
            connectors=[
                {"designator": "P1", "x": 21500, "y": 12000, "orientation": 90}
            ],
        )
        self.assertEqual(spec["errors"], [])
        self.assertEqual(
            records_of(spec, "CONNECTOR"), [["CONNECTOR", "UP1", 21500, 12000, 1, ""]]
        )
        # CP_P1 was 500 mils left of P1; turned 90 degrees it is 500 below
        cp = records_of(spec, "CPOINT")[0]
        self.assertEqual(cp[:5], ["CPOINT", "UCPP1", "", 21500, 11500])

    def test_explicit_connection_point_position_wins(self):
        spec = build_layout_spec(
            self.layout,
            connectors=[{"designator": "P1", "x": 21500, "y": 12000}],
            connection_points=[{"designator": "CP_P1", "x": 21000, "y": 12500}],
        )
        cps = records_of(spec, "CPOINT")
        self.assertEqual(len(cps), 1)
        self.assertEqual(cps[0][3:5], [21000, 12500])

    def test_reassigning_a_splice_unassigns_it_first(self):
        spec = build_layout_spec(
            self.layout,
            connection_points=[{"designator": "CP_P1", "objects": ["SP1"]}],
        )
        self.assertEqual(
            records_of(spec, "CPUNASSIGN") + records_of(spec, "CPASSIGN"),
            [["CPUNASSIGN", "UCP1", "USP1"], ["CPASSIGN", "UCPP1", "USP1"]],
        )

    def test_covering_on_new_bundle_and_label(self):
        spec = build_layout_spec(
            self.layout,
            bundles=[{"designator": "B2", "from": "CP1", "to": "CP_P1"}],
            coverings=[
                {
                    "designator": "COV2",
                    "bundles": ["B1", "B2"],
                    "part_number": "TUBE",
                    "start_offset_mm": -10,
                }
            ],
            layout_labels=[{"designator": "L1", "y": 16000, "text": "NEW"}],
        )
        self.assertEqual(spec["errors"], [])
        (cov,) = records_of(spec, "COVERING")
        self.assertEqual(
            cov,
            [
                "COVERING",
                "",
                "COV2",
                "TUBE",
                "",
                "",
                mm_to_coord(-10),
                "",
                "UB1",
                "DES:B2",
            ],
        )
        (lbl,) = records_of(spec, "LABEL")
        # the omitted x is filled in from the current location
        self.assertEqual(lbl[:6], ["LABEL", "UL1", "", "NEW", 15000, 16000])

    def test_delete_and_record_order(self):
        spec = build_layout_spec(
            self.layout,
            delete=["COV1", "L1"],
            bundles=[{"designator": "B2", "from": "CP1", "to": "CP_P1"}],
        )
        self.assertEqual(spec["errors"], [])
        self.assertEqual(
            [r[0] for r in spec["records"]], ["DELETE", "DELETE", "BUNDLE"]
        )

    def test_errors(self):
        spec = build_layout_spec(
            self.layout,
            connectors=[{"designator": "P9", "x": 0, "y": 0}],
            connection_points=[
                {"designator": "CP9"},
                {"designator": "CP1", "style": "x"},
            ],
            bundles=[
                {"designator": "B9", "from": "CP1", "to": "NOPE"},
                {"designator": "B8", "points": [12000, 12000, 15000, 15000]},
                {"designator": "B7", "from": "CP1", "to": "CP_P1", "length_mm": 6000},
            ],
            coverings=[{"designator": "COV9"}],
            delete=["CP_J1", "XYZ"],
        )
        text = " ".join(spec["errors"])
        for part in (
            "'P9' not found",
            "CP9",
            "style must be",
            "from/to",
            "exactly on connection points",
            "length_mm",
            "needs bundles",
            "holds",
            "'XYZ'",
        ):
            self.assertIn(part, text)

    def test_orientation_must_be_quarter_turns(self):
        spec = build_layout_spec(
            self.layout, connectors=[{"designator": "P1", "orientation": 45}]
        )
        self.assertIn("multiple of 90", spec["errors"][0])


class TestCheckLayout(unittest.TestCase):
    def test_expected_and_lost_connections(self):
        before = make_layout()
        after = copy.deepcopy(before)
        after["bundles"][0]["end2_connection_point"] = None
        # B1 not expected (not touched): losing an end is still a problem
        problems, found = check_layout(after, {"bundles": {}}, before=before)
        self.assertEqual(found, [])
        self.assertIn("B1: lost its connection", problems[0])
        # expected: reported once, as an unconnected end
        problems, found = check_layout(
            after, {"bundles": {"B1": ["CP_J1", "CP1"]}}, before=before
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("not connected", problems[0])
        self.assertEqual(found[0]["designator"], "B1")

    def test_wrong_ends_and_missing_bundle(self):
        layout = make_layout()
        problems, _ = check_layout(
            layout, {"bundles": {"B1": ["CP_J1", "CP_P1"], "B5": ["CP1", "CP_P1"]}}
        )
        self.assertIn("expected", problems[0])
        self.assertIn("B5: not in the saved drawing", problems[1])


class TestEditHarnessLayoutTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.doc = tmp / "Test.LdrDoc"
        self.doc.write_bytes(b"")
        self.before = make_layout()
        self.after = copy.deepcopy(self.before)
        self.after["bundles"].append(
            _bundle(
                "B2", "UB2", [1200, 1200, 2000, 1200], "UCP1", "UCPP1", "CP1", "CP_P1"
            )
        )
        self.parse = patch("main.parse_ldrdoc", side_effect=[self.before, self.after])
        self.parse.start()
        self.bridge = patch(
            "main.altium_bridge._execute_command_locked",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "result": {"success": True, "saved": True, "not_found": []},
            },
        )
        self.mock_bridge = self.bridge.start()
        self.spec_file = tmp / "harness_layout_spec.txt"
        self.spec_patch = patch("main.HARNESS_LAYOUT_SPEC_FILE", self.spec_file)
        self.spec_patch.start()
        self.lock_patch = patch("main.BRIDGE_LOCK_FILE", tmp / "bridge.lock")
        self.lock_patch.start()

    async def asyncTearDown(self):
        self.lock_patch.stop()
        self.spec_patch.stop()
        self.bridge.stop()
        self.parse.stop()
        self.tmp.cleanup()

    async def test_writes_spec_and_verifies(self):
        import main

        raw = await main.edit_harness_layout(
            None,
            document_path=str(self.doc),
            bundles=[{"designator": "B2", "from": "CP1", "to": "P1"}],
        )
        data = json.loads(raw)
        self.assertTrue(data["success"], data)
        self.assertEqual(data["bundles"][0]["from"], "CP1")
        self.assertEqual(data["newly_unrouted_wires"], [])
        self.mock_bridge.assert_awaited_once_with("edit_harness_layout", {})
        lines = self.spec_file.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lines[0], f"DOCUMENT|{self.doc.resolve()}")
        self.assertEqual(lines[1], "BUNDLE||B2|||||12000|12000|20000|12000")

    async def test_unconnected_bundle_fails(self):
        import main

        self.after["bundles"][1]["end2_connection_point"] = None
        raw = await main.edit_harness_layout(
            None,
            document_path=str(self.doc),
            bundles=[{"designator": "B2", "from": "CP1", "to": "P1"}],
        )
        data = json.loads(raw)
        self.assertFalse(data["success"])
        self.assertIn("not connected", data["problems"][0])

    async def test_dry_run_and_errors_never_reach_altium(self):
        import main

        raw = await main.edit_harness_layout(
            None,
            document_path=str(self.doc),
            bundles=[{"designator": "B2", "from": "CP1", "to": "P1"}],
            dry_run=True,
        )
        self.assertTrue(json.loads(raw)["dry_run"])
        self.parse.stop()
        self.parse = patch("main.parse_ldrdoc", return_value=self.before)
        self.parse.start()
        raw = await main.edit_harness_layout(
            None, document_path=str(self.doc), bundles=[{"from": "CP1", "to": "X"}]
        )
        self.assertFalse(json.loads(raw)["success"])
        raw = await main.edit_harness_layout(None, document_path=str(self.doc) + "x")
        self.assertIn("not an existing .LdrDoc", json.loads(raw)["error"])
        self.mock_bridge.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
