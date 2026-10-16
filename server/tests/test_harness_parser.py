"""Unit tests for harness_parser (reading Altium .WirDoc and .PrjHar files).

Run from repo root:  python -m unittest server.tests.test_harness_parser -v
or from server/:      python -m unittest tests.test_harness_parser -v
"""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

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


class TestHarnessMCPFallbacks(unittest.IsolatedAsyncioTestCase):
    """Test MCP tool fallbacks when Altium IPC bridge fails and harness is present."""

    async def asyncSetUp(self):
        # Mock altium_bridge.execute_command to simulate Altium failure or unhandled doc
        self.bridge_patcher = patch(
            "main.altium_bridge.execute_command",
            new_callable=AsyncMock,
            return_value={"success": False, "error": "No PCB document open in Altium"},
        )
        self.mock_bridge = self.bridge_patcher.start()

    async def asyncTearDown(self):
        self.bridge_patcher.stop()

    async def test_fallback_get_project_components(self):
        import main
        raw = await main.get_project_components(None, include_parameters=True)
        data = json.loads(raw)
        self.assertIn("components", data)
        self.assertGreaterEqual(data.get("component_count", 0), 9)
        des_list = [c["designator"] for c in data["components"]]
        self.assertIn("P1", des_list)

    async def test_fallback_get_harness_data(self):
        import main
        # Auto-detects workspace project
        raw = await main.get_harness_data(None)
        data = json.loads(raw)
        self.assertIn("documents", data)
        self.assertGreater(data.get("document_count", 0), 0)

        # Explicit WirDoc path
        raw_wir = await main.get_harness_data(None, file_path=str(MAIN_WIRDOC))
        data_wir = json.loads(raw_wir)
        self.assertIn("connectors", data_wir)
        self.assertIn("summary", data_wir)
        self.assertGreaterEqual(data_wir["summary"]["connector_count"], 9)

    async def test_fallback_get_all_nets(self):
        import main
        raw = await main.get_all_nets(None)
        nets = json.loads(raw)
        self.assertIsInstance(nets, list)
        self.assertIn("BMSM1_CAN1_H", nets)

    async def test_fallback_get_all_designators(self):
        import main
        raw = await main.get_all_designators(None)
        des = json.loads(raw)
        self.assertIsInstance(des, list)
        self.assertIn("P1", des)

    async def test_fallback_get_component_pins(self):
        import main
        raw = await main.get_component_pins(None, ["P1"])
        pins_data = json.loads(raw)
        self.assertEqual(len(pins_data), 1)
        self.assertEqual(pins_data[0]["designator"], "P1")
        self.assertEqual(len(pins_data[0]["pins"]), 26)

    async def test_fallback_get_component_data(self):
        import main
        raw = await main.get_component_data(None, ["P1"])
        data = json.loads(raw)
        self.assertIn("components", data)
        self.assertEqual(data["components"][0]["designator"], "P1")

    async def test_fallback_get_schematic_data(self):
        import main
        raw = await main.get_schematic_data(None, ["P1"])
        data = json.loads(raw)
        self.assertIn("components", data)
        self.assertEqual(len(data["components"]), 1)
        self.assertEqual(data["components"][0]["designator"], "P1")

    async def test_fallback_get_property_names_and_values(self):
        import main
        names_raw = await main.get_all_component_property_names(None)
        names = json.loads(names_raw)
        self.assertIn("PartNumber", names)

        vals_raw = await main.get_component_property_values(None, "PartNumber")
        vals = json.loads(vals_raw)
        p1 = [v for v in vals if v.get("designator") == "P1"]
        self.assertTrue(len(p1) > 0)
        self.assertEqual(p1[0]["PartNumber"], "1376360-1")

    async def test_fallback_get_net_connections(self):
        import main
        raw = await main.get_net_connections(None, ["P1"])
        conns = json.loads(raw)
        self.assertIn("nets", conns)
        self.assertIsInstance(conns["nets"], list)
        self.assertGreater(len(conns["nets"]), 0)

    async def test_fallback_get_erc_violations(self):
        import main
        raw = await main.get_erc_violations(None)
        erc = json.loads(raw)
        self.assertIn("violations", erc)
        self.assertGreater(erc.get("total", 0), 0)

    async def test_fallback_get_board_summary(self):
        import main
        raw = await main.get_board_summary(None)
        summary = json.loads(raw)
        self.assertEqual(summary.get("project_type"), "harness")
        self.assertGreaterEqual(summary.get("connector_count", 0), 9)

    async def test_pcb_only_tool_error_interception(self):
        import main
        raw = await main.get_pcb_layers(None)
        err = json.loads(raw)
        self.assertIn("error", err)
        self.assertEqual(err.get("project_type"), "harness")
        self.assertIn("requires an active PCB layout document", err["error"])
        self.assertIn("get_harness_data", err["error"])

        raw_place = await main.place_components(
            None, [{"designator": "R1", "x": 100, "y": 200}]
        )
        err_place = json.loads(raw_place)
        self.assertEqual(err_place.get("project_type"), "harness")

    async def test_add_harness_wiring_validation(self):
        import main
        # Odd number of coordinates should fail validation
        res = await main.add_harness_wiring(
            None,
            wires=[{"designator": "W1", "net": "NET1", "points": [100, 200, 300]}],
        )
        data = json.loads(res)
        self.assertFalse(data.get("success"))
        self.assertIn("error", data)

    async def test_add_harness_wiring_dispatch(self):
        import main
        self.mock_bridge.return_value = {
            "success": True,
            "result": json.dumps({"success": True, "wires": 2, "no_connects": 1}),
        }
        res = await main.add_harness_wiring(
            None,
            wires=[
                {"designator": "W1", "net": "NET1", "points": [100, 200, 300, 400]},
                {"designator": "W2", "net": "NET2", "points": [500, 600, 700, 800]},
            ],
            no_connects=[{"x": 150, "y": 250}],
            net_labels=[{"x": 100, "y": 200, "orientation": 0, "text": "NET1"}],
        )
        data = json.loads(res)
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("wires"), 2)
        self.assertEqual(data.get("no_connects"), 1)

    async def test_add_harness_wiring_with_document_path(self):
        import main
        self.mock_bridge.return_value = {
            "success": True,
            "result": json.dumps({"success": True, "wires": 1, "no_connects": 0, "sheet": "P42C-1_Slave_Sense_Harness.WirDoc"}),
        }
        res = await main.add_harness_wiring(
            None,
            wires=[{"designator": "W1", "net": "NET1", "points": [100, 200, 300, 400]}],
            document_path=r"C:\test\sheet.WirDoc",
        )
        data = json.loads(res)
        self.assertTrue(data.get("success"))
        from altium_files import read_altium_text
        spec = read_altium_text(Path("C:/Users/Public/altium_mcp/harness_wiring_spec.txt"))
        self.assertTrue(spec.startswith("DOCUMENT|C:\\test\\sheet.WirDoc"))


if __name__ == "__main__":
    unittest.main()

