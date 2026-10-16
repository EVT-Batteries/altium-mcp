"""Unit tests for harness_router (automated orthogonal routing & anti-collision layout).

Run from repo root:  python -m unittest server.tests.test_harness_router -v
or from server/:      python -m unittest tests.test_harness_router -v
"""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from harness_router import calculate_harness_reroute  # noqa: E402

WORKSPACE_DIR = Path(__file__).resolve().parents[3]
SLAVE_WIRDOC = (
    WORKSPACE_DIR / "P42C-1_Slave_Harness" / "P42C-1_Slave_Sense_Harness.WirDoc"
)


class TestHarnessRouter(unittest.TestCase):

    def test_calculate_harness_reroute_slave(self):
        if not SLAVE_WIRDOC.exists():
            self.skipTest("Sample Slave WirDoc not found in workspace")

        res = calculate_harness_reroute(SLAVE_WIRDOC)
        self.assertIn("wires", res)
        self.assertIn("splices", res)
        self.assertIn("no_connects", res)
        self.assertIn("summary", res)

        wires = res["wires"]
        splices = res["splices"]
        ncs = res["no_connects"]
        summary = res["summary"]

        # Exact counts
        self.assertEqual(len(wires), 41)
        self.assertEqual(len(splices), 3)
        self.assertEqual(len(ncs), 15)
        self.assertEqual(summary["wire_count"], 41)

        # Splice positions & runway checks
        splice_x = summary["splice_x"]
        self.assertEqual(splice_x, 9400)
        for s in splices:
            self.assertEqual(s["x"], 9400)
            self.assertIn(s["designator"], ["SP1", "SP2", "SP3"])

        # Orthogonal routing check: all wire points must alternate strictly orthogonally
        for w in wires:
            pts = w["points"]
            self.assertTrue(len(pts) >= 4)
            self.assertEqual(len(pts) % 2, 0)

            # Check segments are strictly horizontal or vertical
            for i in range(0, len(pts) - 2, 2):
                x1, y1 = pts[i], pts[i + 1]
                x2, y2 = pts[i + 2], pts[i + 3]
                # Either x1 == x2 or y1 == y2 (strictly orthogonal, no diagonals)
                self.assertTrue(
                    x1 == x2 or y1 == y2,
                    f"Wire {w['designator']} segment ({x1},{y1})->({x2},{y2}) is not orthogonal",
                )

        # Side-by-side label invariant & 2-phase staggering checks
        for w in wires:
            des_pos = w["designator_pos"]
            com_pos = w["comment_pos"]

            # Must be placed above wire line (Y + 15) or below in open boundary
            # Y coordinates must match (horizontal alignment)
            self.assertEqual(
                des_pos[1],
                com_pos[1],
                f"Wire {w['designator']} labels must be horizontally aligned at same Y",
            )

            # Comment must be to the right of designator (side-by-side)
            self.assertGreater(
                com_pos[0],
                des_pos[0],
                f"Wire {w['designator']} comment must be placed to the right of designator",
            )
            delta_x = com_pos[0] - des_pos[0]
            self.assertGreaterEqual(
                delta_x,
                300,
                f"Wire {w['designator']} label spacing must be at least 300 mils",
            )

            # Physical wire parameters
            params = w["parameters"]
            self.assertIn("Gauge", params)
            self.assertIn("Color", params)
            self.assertIn("VoltageRating", params)
            self.assertIn("Border", params)

        # No-Connect directives checks
        for nc in ncs:
            self.assertFalse(
                nc["show_name"],
                f"No-connect {nc.get('designator')} must have show_name: False",
            )
            self.assertTrue(nc["x"] > 0)
            self.assertTrue(nc["y"] > 0)


if __name__ == "__main__":
    unittest.main()

