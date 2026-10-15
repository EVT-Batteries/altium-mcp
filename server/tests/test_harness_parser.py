"""Unit tests for harness_parser (reading Altium .WirDoc and .PrjHar files).

Run from repo root:  python -m unittest server.tests.test_harness_parser -v
or from server/:      python -m unittest tests.test_harness_parser -v
"""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from harness_parser import (  # noqa: E402
    get_harness_components,
    parse_pipe_record,
    parse_prjhar,
    parse_wirdoc,
)

# Reference sample files from workspace
WORKSPACE_DIR = Path(__file__).resolve().parents[3]
MAIN_WIRDOC = WORKSPACE_DIR / "P42C-1_Main_Harness" / "P42C-1_Main_Harness.WirDoc"
MAIN_PRJHAR = WORKSPACE_DIR / "P42C-1_Main_Harness" / "P42C-1_Main_Harness.PrjHar"
SLAVE_WIRDOC = (
    WORKSPACE_DIR / "P42C-1_Slave_Harness" / "P42C-1_Slave_Sense_Harness.WirDoc"
)


class TestHarnessParser(unittest.TestCase):

    def test_parse_pipe_record(self):
        sample = "|RECORD=106|LibReference=CONN_PLUG|Location.X=70|Location.Y=1480|"
        res = parse_pipe_record(sample)
        self.assertEqual(res.get("RECORD"), "106")
        self.assertEqual(res.get("LibReference"), "CONN_PLUG")
        self.assertEqual(res.get("Location.X"), "70")
        self.assertEqual(res.get("Location.Y"), "1480")

    def test_parse_prjhar(self):
        if not MAIN_PRJHAR.exists():
            self.skipTest("Sample PrjHar not found in workspace")

        res = parse_prjhar(MAIN_PRJHAR)
        self.assertIn("documents", res)
        self.assertGreater(res["document_count"], 0)
        doc_paths = [d["path"] for d in res["documents"]]
        self.assertIn("P42C-1_Main_Harness.WirDoc", doc_paths)
        self.assertEqual(res["parameters"].get("PROJECT_REVISION"), "A")

    def test_parse_main_wirdoc(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        res = parse_wirdoc(MAIN_WIRDOC)
        self.assertIn("connectors", res)
        self.assertIn("summary", res)
        summary = res["summary"]

        self.assertGreaterEqual(summary["connector_count"], 9)
        self.assertGreater(summary["total_pins"], 100)
        self.assertGreater(summary["connected_pins"], 50)

        # Verify specific connectors and designators
        designators = {c["designator"]: c for c in res["connectors"]}
        self.assertIn("P1", designators)
        self.assertIn("P2", designators)
        self.assertIn("P3", designators)
        self.assertIn("P4", designators)
        self.assertIn("P5", designators)
        self.assertIn("P6", designators)

        # Check P1 pins and nets
        p1 = designators["P1"]
        self.assertEqual(len(p1["pins"]), 26)
        p1_nets = {p["designator"]: p["net"] for p in p1["pins"]}
        self.assertEqual(p1_nets.get("1"), "BMSM1_ALWAYS_ON_PWR")
        self.assertEqual(p1_nets.get("2"), "BMSM1_READY_PWR")

        # Check P2 (Current sensor / remote)
        p2 = designators["P2"]
        self.assertEqual(len(p2["pins"]), 28)
        p2_nets = {p["designator"]: p["net"] for p in p2["pins"]}
        self.assertEqual(p2_nets.get("1"), "BMSM1_REMOTE_COMM_A_POS")
        self.assertEqual(p2_nets.get("2"), "BMSM1_REMOTE_COMM_A_NEG")

    def test_parse_slave_wirdoc(self):
        if not SLAVE_WIRDOC.exists():
            self.skipTest("Sample Slave WirDoc not found in workspace")

        res = parse_wirdoc(SLAVE_WIRDOC)
        summary = res["summary"]
        self.assertGreaterEqual(summary["connector_count"], 7)

    def test_get_harness_components(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        components = get_harness_components(MAIN_WIRDOC)
        self.assertIsInstance(components, list)
        self.assertGreaterEqual(len(components), 9)
        des_list = [c["designator"] for c in components]
        self.assertIn("P1", des_list)
        self.assertIn("P2", des_list)

    def test_get_harness_designators(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        from harness_parser import get_harness_designators
        designators = get_harness_designators(MAIN_WIRDOC)
        self.assertIn("P1", designators)
        self.assertIn("P2", designators)
        self.assertIn("P3", designators)

    def test_get_harness_component_data(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        from harness_parser import get_harness_component_data
        res = get_harness_component_data(MAIN_WIRDOC, ["P1", "NON_EXISTENT"])
        self.assertEqual(len(res["components"]), 1)
        self.assertEqual(res["components"][0]["designator"], "P1")
        self.assertIn("parameters", res["components"][0])
        self.assertEqual(res["components"][0]["parameters"].get("PartNumber"), "1376360-1")
        self.assertIn("NON_EXISTENT", res.get("missing_designators", []))

    def test_get_harness_schematic_data(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        from harness_parser import get_harness_schematic_data
        res = get_harness_schematic_data(MAIN_WIRDOC, ["P1"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["designator"], "P1")
        self.assertEqual(res[0]["sheet"], "P42C-1_Main_Harness.WirDoc")
        self.assertGreaterEqual(len(res[0]["pins"]), 26)

    def test_get_harness_component_pins(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        from harness_parser import get_harness_component_pins
        res = get_harness_component_pins(MAIN_WIRDOC, ["P1"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["designator"], "P1")
        self.assertEqual(len(res[0]["pins"]), 26)
        pin1 = [p for p in res[0]["pins"] if p["designator"] == "1"][0]
        self.assertEqual(pin1["net"], "BMSM1_ALWAYS_ON_PWR")

    def test_get_harness_nets_and_connections(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        from harness_parser import get_harness_net_connections, get_harness_nets
        nets = get_harness_nets(MAIN_WIRDOC)
        self.assertIn("BMSM1_CAN1_H", nets)
        self.assertIn("BMSM1_ALWAYS_ON_PWR", nets)

        conns = get_harness_net_connections(MAIN_WIRDOC, ["BMSM1_CAN1_H"])
        self.assertEqual(len(conns), 1)
        self.assertEqual(conns[0]["net"], "BMSM1_CAN1_H")
        self.assertEqual(conns[0]["pins"][0]["designator"], "P1")

    def test_get_harness_property_names_and_values(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        from harness_parser import (
            get_harness_property_names,
            get_harness_property_values,
        )
        prop_names = get_harness_property_names(MAIN_WIRDOC)
        self.assertIn("PartNumber", prop_names)
        self.assertIn("Manufacturer 1", prop_names)
        self.assertIn("designator", prop_names)

        values = get_harness_property_values(MAIN_WIRDOC, "PartNumber")
        p1_val = [v["PartNumber"] for v in values if v["designator"] == "P1"]
        self.assertEqual(p1_val, ["1376360-1"])

    def test_get_harness_erc_violations(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        from harness_parser import get_harness_erc_violations
        erc = get_harness_erc_violations(MAIN_WIRDOC)
        self.assertGreater(erc["total"], 0)
        self.assertGreaterEqual(erc["errors"], 1)  # P? unannotated
        unannotated = [v for v in erc["violations"] if v["type"] == "Unannotated Designator"]
        self.assertTrue(any("P?" in v["detail"] for v in unannotated))

    def test_find_workspace_harness(self):
        from harness_parser import find_workspace_harness
        found = find_workspace_harness()
        self.assertIsNotNone(found)
        self.assertTrue(found.name.endswith((".PrjHar", ".WirDoc")))


if __name__ == "__main__":
    unittest.main()
