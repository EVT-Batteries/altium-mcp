"""Unit tests for harness_parser (reading Altium .WirDoc and .PrjHar files).

Run from repo root:  python -m unittest server.tests.test_harness_parser -v
or from server/:      python -m unittest tests.test_harness_parser -v
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from harness_parser import (
    get_harness_components,
    parse_pipe_record,
    parse_prjhar,
    parse_wirdoc,
)

# Reference sample files from workspace
WORKSPACE_DIR = Path(__file__).resolve().parents[3]
MAIN_WIRDOC = WORKSPACE_DIR / "P42C-1_Main_Harness" / "P42C-1_Main_Harness_Master_1.WirDoc"
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
        self.assertIn("P42C-1_Main_Harness_Master_1.WirDoc", doc_paths)
        self.assertEqual(res["parameters"].get("PROJECT_REVISION"), "A")

    def test_parse_main_wirdoc(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        res = parse_wirdoc(MAIN_WIRDOC)
        self.assertIn("connectors", res)
        self.assertIn("summary", res)
        summary = res["summary"]

        self.assertGreaterEqual(summary["connector_count"], 4)
        self.assertGreater(summary["total_pins"], 50)
        self.assertGreater(summary["connected_pins"], 20)

        # Verify specific connectors and designators
        designators = {c["designator"]: c for c in res["connectors"]}
        self.assertIn("P1", designators)
        self.assertIn("P2", designators)
        self.assertIn("P3", designators)
        self.assertIn("X1", designators)

        # Check P1 pins and nets
        p1 = designators["P1"]
        self.assertEqual(len(p1["pins"]), 26)
        p1_nets = {p["designator"]: p["net"] for p in p1["pins"]}
        self.assertEqual(p1_nets.get("18"), "BMSM1_CAN1_H")
        self.assertEqual(p1_nets.get("14"), "BMSM1_MPI2_PROX")

        # Check P2 (Current sensor / remote)
        p2 = designators["P2"]
        self.assertEqual(len(p2["pins"]), 28)
        p2_nets = {p["designator"]: p["net"] for p in p2["pins"]}
        self.assertEqual(p2_nets.get("27"), "BMSM1_CURR_SENSE_GND")
        self.assertEqual(p2_nets.get("28"), "BMSM1_CURR_SENSE_5V")

    def test_parse_slave_wirdoc(self):
        if not SLAVE_WIRDOC.exists():
            self.skipTest("Sample Slave WirDoc not found in workspace")

        res = parse_wirdoc(SLAVE_WIRDOC)
        summary = res["summary"]
        # Crimps nested in cavities (RECORD 106 with an owner) are not connectors
        self.assertEqual(summary["connector_count"], 7)
        self.assertEqual(
            sorted(c["designator"] for c in res["connectors"]),
            ["J1", "P1", "P2", "P3", "P4", "P5", "P6"],
        )
        p1 = next(c for c in res["connectors"] if c["designator"] == "P1")
        pin1 = next(p for p in p1["pins"] if p["designator"] == "1")
        self.assertEqual(len(pin1["cavity_parts"]), 1)
        self.assertTrue(pin1["cavity_parts"][0]["part_number"])
        # Connector titles are owned text, not sheet labels
        self.assertEqual(summary["net_label_count"], 93)
        self.assertTrue(all(w["design_item_id"] for w in res["wires"]))

    def test_parse_synthetic_records(self):
        """Connector with a pin, a cavity crimp (written twice, as Altium does)
        and a wire; no sample files needed."""
        from harness_parser import parse_wirdoc_records
        recs = [
            {"HEADER": "Protel for Windows - Harness"},
            {"RECORD": "106", "LibReference": "CONN", "Location.X": "100", "Location.Y": "200", "UniqueID": "C1"},
            {"RECORD": "34", "OwnerIndex": "0", "Name": "Designator", "Text": "P1"},
            {"RECORD": "113", "OwnerIndex": "0", "Designator": "1", "Name": "SIG",
             "Location.X": "100", "Location.Y": "200", "PinLength": "20", "UniqueID": "PIN1"},
            {"RECORD": "131", "OwnerIndex": "2"},
            {"RECORD": "106", "OwnerIndex": "3", "DesignItemId": "TERM-1", "UniqueID": "K1"},
            {"RECORD": "106", "OwnerIndex": "3", "DesignItemId": "TERM-1", "UniqueID": "K1"},
            {"RECORD": "107", "LocationCount": "2", "X1": "120", "Y1": "200", "X2": "300",
             "Y2": "200", "Y2_Frac": "50000", "DesignItemId": "WIRE-1", "UniqueID": "W"},
            {"RECORD": "34", "OwnerIndex": "6", "Name": "Designator", "Text": "W1"},
            {"RECORD": "41", "OwnerIndex": "6", "Name": "Comment", "Text": "NET1"},
            {"RECORD": "4", "OwnerIndex": "0", "Text": "Connector title"},
            {"RECORD": "4", "Text": "Sheet note"},
        ]
        res = parse_wirdoc_records(recs, "sheet.WirDoc")
        self.assertEqual(res["summary"]["connector_count"], 1)
        pin = res["connectors"][0]["pins"][0]
        self.assertEqual(pin["hot_end"], {"x": 120, "y": 200})
        self.assertEqual([p["part_number"] for p in pin["cavity_parts"]], ["TERM-1"])
        self.assertEqual(pin["connected_wire"], "W1")
        self.assertEqual(pin["net"], "NET1")
        self.assertEqual(res["wires"][0]["points"], [120, 200, 300, 200.5])
        self.assertEqual(res["wires"][0]["design_item_id"], "WIRE-1")
        self.assertEqual([n["text"] for n in res["net_labels"]], ["Sheet note"])

    def test_decode_record_encodings(self):
        from harness_parser import _decode_record
        # Bytes as Altium writes them: plain field cp1252 with 0x8E for '|',
        # %UTF8% field with U+00A6 for '|'
        rec = _decode_record(b"|RECORD=41|%UTF8%Text=200\xc2\xb0C \xc2\xa6 x|Text=200\xb0C \x8e x|Name=T")
        self.assertEqual(rec["Text"], "200°C | x")
        self.assertEqual(_decode_record(b"|Text=a \x8e b|")["Text"], "a | b")

    def test_coord_fraction(self):
        from harness_parser import _coord
        self.assertEqual(_coord({}, "Location.X"), 0)
        self.assertEqual(_coord({"Location.X": "12"}, "Location.X"), 12)
        self.assertEqual(_coord({"Location.X_Frac": "50000"}, "Location.X"), 0.5)
        self.assertEqual(
            _coord({"Location.X": "12", "Location.X_Frac": "50000"}, "Location.X"), 12.5
        )

    def test_get_harness_components(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        components = get_harness_components(MAIN_WIRDOC)
        self.assertIsInstance(components, list)
        self.assertGreaterEqual(len(components), 4)
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
        self.assertEqual(res[0]["sheet"], MAIN_WIRDOC.name)
        self.assertGreaterEqual(len(res[0]["pins"]), 26)

    def test_get_harness_component_pins(self):
        if not MAIN_WIRDOC.exists():
            self.skipTest("Sample WirDoc not found in workspace")

        from harness_parser import get_harness_component_pins
        res = get_harness_component_pins(MAIN_WIRDOC, ["P1"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["designator"], "P1")
        self.assertEqual(len(res[0]["pins"]), 26)
        pin18 = [p for p in res[0]["pins"] if p["designator"] == "18"][0]
        self.assertEqual(pin18["net"], "BMSM1_CAN1_H")

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
        self.assertGreaterEqual(erc["warnings"], 1)

        if SLAVE_WIRDOC.exists():
            # Cavity crimps used to be reported as 148 unannotated connectors
            slave_erc = get_harness_erc_violations(SLAVE_WIRDOC)
            unannotated = [v for v in slave_erc["violations"] if v["type"] == "Unannotated Designator"]
            self.assertEqual(unannotated, [])

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
            "main.altium_bridge._execute_command_locked",
            new_callable=AsyncMock,
            return_value={"success": False, "error": "No PCB document open in Altium"},
        )
        self.mock_bridge = self.bridge_patcher.start()
        # Never write the live bridge's spec file: a running server may be
        # mid-request, and payloads here name real documents.
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.spec_file = Path(self.tmp.name) / "harness_wiring_spec.txt"
        self.spec_patcher = patch("main.HARNESS_SPEC_FILE", self.spec_file)
        self.spec_patcher.start()
        # ...nor wait on (or hold) the live bridge lock
        self.lock_patcher = patch("main.BRIDGE_LOCK_FILE", Path(self.tmp.name) / "bridge.lock")
        self.lock_patcher.start()
        # The active harness comes from Altium (mocked away here), never from
        # a folder search; point the fallbacks at the sample project
        self.active_patcher = patch(
            "main._get_active_harness_path",
            new=AsyncMock(return_value=MAIN_PRJHAR if MAIN_PRJHAR.exists() else None),
        )
        self.active_patcher.start()

    async def asyncTearDown(self):
        self.active_patcher.stop()
        self.lock_patcher.stop()
        self.spec_patcher.stop()
        self.tmp.cleanup()
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
        self.assertGreaterEqual(data_wir["summary"]["connector_count"], 4)

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
            document_path=r"C:\test\sheet.WirDoc",
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
        spec = read_altium_text(self.spec_file)
        self.assertTrue(spec.startswith("DOCUMENT|C:\\test\\sheet.WirDoc"))

    async def test_add_harness_wiring_with_splices_and_parameters(self):
        import main
        self.mock_bridge.return_value = {
            "success": True,
            "result": json.dumps({"success": True, "wires": 1, "splices": 1, "no_connects": 0}),
        }
        res = await main.add_harness_wiring(
            None,
            wires=[{
                "designator": "W1",
                "net": "BMSS_C01_POS",
                "points": [100, 200, 300, 200],
                "parameters": {"Gauge": "22 AWG", "Color": "Orange", "Border": "None"},
            }],
            splices=[{"designator": "SP1", "x": 300, "y": 200}],
            clear_existing=True,
            document_path=r"C:\test\sheet.WirDoc",
        )
        data = json.loads(res)
        self.assertTrue(data.get("success"))
        from altium_files import read_altium_text
        spec = read_altium_text(self.spec_file)
        lines = spec.splitlines()
        self.assertIn("DOCUMENT|C:\\test\\sheet.WirDoc", lines)
        self.assertIn("CLEAR_EXISTING", lines)
        self.assertIn("SPLICE|SP1|300|200", lines)
        self.assertIn("WIREPARAM|Border|None", lines)

    async def test_add_harness_wiring_side_by_side_label_fallbacks(self):
        import main
        self.mock_bridge.return_value = {
            "success": True,
            "result": json.dumps({"success": True, "wires": 1, "no_connects": 1}),
        }
        res = await main.add_harness_wiring(
            None,
            wires=[{
                "designator": "W1",
                "net": "BMSS_C01_POS",
                "points": [1000, 2000, 3000, 2000],
            }],
            no_connects=[{"name": "NC1", "x": 1000, "y": 2000, "show_name": False}],
            document_path=r"C:\test\sheet.WirDoc",
        )
        data = json.loads(res)
        self.assertTrue(data.get("success"))
        from altium_files import read_altium_text
        spec = read_altium_text(self.spec_file)
        lines = spec.splitlines()
        wire_line = [l for l in lines if l.startswith("WIRE|W1|")][0]
        parts = wire_line.split("|")
        # Format: WIRE|W1|BMSS_C01_POS|AT|<des_x>|<des_y>|<com_x>|<com_y>|...
        self.assertEqual(parts[3], "AT")
        des_x, des_y = int(parts[4]), int(parts[5])
        com_x, com_y = int(parts[6]), int(parts[7])
        # Crucial invariant: des_x and com_x MUST NOT be equal (side-by-side, no stacking)
        self.assertNotEqual(des_x, com_x)
        self.assertEqual(des_y, com_y)  # same baseline above wire
        self.assertLess(des_x, com_x)

        # No-connect directive must hide name (0)
        nc_line = [l for l in lines if l.startswith("NOCONNECT|")][0]
        nc_parts = nc_line.split("|")
        self.assertEqual(nc_parts[3], "NC1")
        self.assertEqual(nc_parts[4], "0")


    async def test_add_harness_wiring_cavity_crimps_serialization(self):
        import main
        self.mock_bridge.return_value = {
            "success": True,
            "result": json.dumps({"success": True, "wires": 0, "cavity_crimps": 2}),
        }
        res = await main.add_harness_wiring(
            None,
            cavity_crimps=[
                {
                    "connector": "P1",
                    "pin": "1",
                    "crimp_part_number": "13065S",
                    "source_library": "my-components.DbLib",
                    "source_table": "terminals-db.csv",
                },
                {
                    "connector": "P1",
                    "pin": "2",
                    "crimp_part_number": "13065S",
                },
            ],
            document_path=r"C:\test\sheet.WirDoc",
        )
        data = json.loads(res)
        self.assertTrue(data.get("success"))
        from altium_files import read_altium_text
        spec = read_altium_text(self.spec_file)
        lines = spec.splitlines()
        self.assertIn("CAVITYCRIMP|P1|1|13065S|my-components.DbLib|terminals-db.csv", lines)
        self.assertIn("CAVITYCRIMP|P1|2|13065S", lines)

    async def test_add_harness_wiring_requires_document_path(self):
        import main
        res = json.loads(await main.add_harness_wiring(
            None,
            wires=[{"designator": "W1", "net": "N", "points": [0, 0, 100, 0]}],
        ))
        self.assertFalse(res["success"])
        self.assertIn("document_path", res["error"])
        self.mock_bridge.assert_not_called()

    async def test_add_harness_wiring_encodes_special_characters(self):
        import main
        from altium_files import read_altium_text
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        await main.add_harness_wiring(
            None,
            wire_updates=[{"designator": "W1", "parameters": {"Description": "A|B 50%\nx"}}],
            document_path=r"C:\test\sheet.WirDoc",
        )
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertIn("UPDATE_WIREPARAM|W1|Description|A%7CB 50%25%0Ax", lines)

    def test_spec_line_round_trip(self):
        import main
        line = main._spec_line("X", "a|b", "100%", "l1\r\nl2")
        fields = line.split("|")
        self.assertEqual(len(fields), 4)
        decoded = [
            f.replace("%7C", "|").replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")
            for f in fields
        ]
        self.assertEqual(decoded, ["X", "a|b", "100%", "l1\r\nl2"])

    async def test_reroute_dry_run_changes_nothing(self):
        import main
        if not SLAVE_WIRDOC.exists():
            self.skipTest("Sample Slave WirDoc not found in workspace")
        res = json.loads(await main.reroute_harness_wiring(
            None, document_path=str(SLAVE_WIRDOC), dry_run=True,
        ))
        self.assertTrue(res["dry_run"])
        self.assertEqual(len(res["reroutes"]), 40)
        self.mock_bridge.assert_not_called()

    async def test_reroute_moves_wires_in_place(self):
        import main
        from altium_files import read_altium_text
        if not SLAVE_WIRDOC.exists():
            self.skipTest("Sample Slave WirDoc not found in workspace")
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        await main.reroute_harness_wiring(None, document_path=str(SLAVE_WIRDOC))
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertEqual(sum(l.startswith("REROUTE|") for l in lines), 40)
        self.assertNotIn("CLEAR_EXISTING", lines)
        self.assertFalse(any(l.startswith(("WIRE|", "SPLICE|", "NOCONNECT|", "LABEL|")) for l in lines))

    async def test_sync_harness_library_from_dblib(self):
        import tempfile

        import main
        tmp = Path(tempfile.mkdtemp())
        (tmp / "wires.csv").write_text(
            "PartNumber,Library Ref,Wire Gauge,Color,Description\n"
            "WB-22,SYM,22 AWG,Black,Wire 22 black\n",
            encoding="utf-8",
        )
        (tmp / "terms.csv").write_text(
            "PartNumber,Description,Manufacturer 1\nT-1,Socket,Acme\n", encoding="utf-8"
        )
        (tmp / "lib.DbLib").write_text(
            "[DatabaseLinks]\n"
            "ConnectionString=Provider=MSDASQL.1;Extended Properties=\"Driver={Microsoft Access "
            "Text Driver (*.txt, *.csv)};Dbq=Z:\\missing\\;Extensions=csv;\"\n"
            "[Table1]\nTableName=wires.csv\nEnabled=True\n"
            "[Table2]\nTableName=terms.csv\nEnabled=True\n"
            "[FieldMap1]\nOptions=FieldName=wires.csv.Library Ref|TableNameOnly=wires.csv|"
            "FieldNameOnly=Library Ref|FieldType=1|ParameterName=[Library Ref]\n",
            encoding="utf-8",
        )
        doc = tmp / "h.WirDoc"
        doc.write_bytes(b"")
        fake = {
            "wires": [
                {"designator": "W1", "parameters": {"Gauge": "22 AWG", "Color": "Black"}},
                {"designator": "W2", "parameters": {"PartNumber": "NOPE"}},
            ],
            "connectors": [
                {"designator": "P1", "pins": [
                    {"designator": "1", "cavity_parts": [{"part_number": "T-1"}]},
                    {"designator": "2", "cavity_parts": []},
                ]},
            ],
        }
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        with patch("main.parse_wirdoc", return_value=fake):
            res = json.loads(await main.sync_harness_library(None, document_path=str(doc)))
        from altium_files import read_altium_text
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertEqual(res["dblib_path"], str(tmp / "lib.DbLib"))
        self.assertIn("UPDATE_WIREPARAM|W1|Design Item Id|WB-22", lines)
        self.assertIn("UPDATE_WIREPARAM|W1|Source Table|wires.csv", lines)
        self.assertIn("UPDATE_WIREPARAM|W1|Source Library|lib.DbLib", lines)
        self.assertIn("WIRECOLOR|W1|0", lines)
        # [Library Ref] is a system field, not a parameter
        self.assertFalse(any("|Library Ref|" in l for l in lines))
        crimp = [l for l in lines if l.startswith("CAVITYCRIMP|")]
        self.assertEqual(
            crimp,
            ["CAVITYCRIMP|P1|1|T-1|lib.DbLib|terms.csv|PartNumber|T-1|Description|Socket|Manufacturer 1|Acme"],
        )
        self.assertEqual(res["unresolved"][0]["designator"], "W2")
        self.assertFalse(res["success"])

    async def test_sync_harness_library_refreshes_connector_parameters(self):
        import tempfile

        import main
        tmp = Path(tempfile.mkdtemp())
        (tmp / "conns.csv").write_text(
            "PartNumber,Description,Max Operating Temperature,Gender\n"
            "C-1,Housing,105°C,Female\n",
            encoding="utf-8",
        )
        (tmp / "lib.DbLib").write_text(
            "[DatabaseLinks]\n"
            "ConnectionString=Provider=MSDASQL.1;Extended Properties=\"Driver={Microsoft Access "
            "Text Driver (*.txt, *.csv)};Dbq=Z:\\missing\\;Extensions=csv;\"\n"
            "[Table1]\nTableName=conns.csv\nEnabled=True\n",
            encoding="utf-8",
        )
        doc = tmp / "h.WirDoc"
        doc.write_bytes(b"")
        fake = {
            "wires": [],
            "connectors": [
                # mangled value -> refreshed; equal value -> untouched; Gender not
                # placed on the connector -> not added
                {"designator": "P1", "pins": [], "parameters": {
                    "PartNumber": "C-1", "Description": "Housing",
                    "Max Operating Temperature": "105Â°C"}},
                {"designator": "P9", "pins": [], "parameters": {}},
            ],
        }
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        with patch("main.parse_wirdoc", return_value=fake):
            res = json.loads(await main.sync_harness_library(None, document_path=str(doc)))
        from altium_files import read_altium_text
        lines = [l for l in read_altium_text(self.spec_file).splitlines() if l.startswith("UPDATE_COMPPARAM|")]
        self.assertEqual(lines, ["UPDATE_COMPPARAM|P1|Max Operating Temperature|105°C"])
        self.assertEqual(res["connectors_refreshed"], {"P1": ["Max Operating Temperature"]})
        self.assertTrue(res["success"])

    async def test_add_harness_wiring_sheet_style_only_when_asked(self):
        import main
        from altium_files import read_altium_text
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        await main.add_harness_wiring(
            None,
            wire_updates=[{"designator": "W1", "parameters": {"Gauge": "22 AWG"}}],
            document_path=r"C:\test\sheet.WirDoc",
        )
        self.assertNotIn("SHEET_STYLE", read_altium_text(self.spec_file))
        await main.add_harness_wiring(
            None, document_path=r"C:\test\sheet.WirDoc", sheet_style="d",
        )
        self.assertIn("SHEET_STYLE|8", read_altium_text(self.spec_file).splitlines())
        res = json.loads(await main.add_harness_wiring(
            None, document_path=r"C:\test\sheet.WirDoc", sheet_style="Z9",
        ))
        self.assertFalse(res["success"])

    async def test_sync_harness_wires_reports_ambiguous_wires(self):
        import tempfile

        import main
        if not SLAVE_WIRDOC.exists():
            self.skipTest("Sample Slave WirDoc not found in workspace")
        # Two rows share gauge and color: a wire without a PartNumber must not
        # be guessed, only reported.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write("PartNumber,Wire Gauge,Color\n")
            f.write("A-1,22 AWG,Black\nA-2,22 AWG,Black\n")
            temp_db = f.name
        try:
            fake = {"wires": [{"designator": "W1", "parameters": {"Gauge": "22 AWG", "Color": "Black"}}]}
            with patch("main.parse_wirdoc", return_value=fake):
                res = json.loads(await main.sync_harness_wires(
                    None, document_path=str(SLAVE_WIRDOC), wires_db_path=temp_db,
                ))
            self.assertFalse(res["success"])
            self.assertEqual(res["unresolved"][0]["designator"], "W1")
            self.mock_bridge.assert_not_called()
        finally:
            os.remove(temp_db)

    async def test_sync_harness_wires_generic_dispatch(self):
        import tempfile

        import main
        # Create a temporary wire database CSV
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write("PartNumber,Gauge,Color,Border,Voltage Rating,Outer Diameter\n")
            f.write("W-TEST-01,22 AWG,Black,None,600V,1.2mm\n")
            temp_db = f.name

        try:
            self.mock_bridge.return_value = {
                "success": True,
                "result": json.dumps({"success": True, "wires": 1}),
            }
            res = await main.sync_harness_wires(
                None,
                document_path=str(SLAVE_WIRDOC),
                wires_db_path=temp_db,
                source_library_name="custom.DbLib",
                wire_designator_map={"W1": "W-TEST-01"},
            )
            data = json.loads(res)
            self.assertEqual(data.get("wires_synced_count"), 1)
            # The other wires' part numbers are not in this one-row database:
            # reported, not skipped silently, so the sync is not a success
            unresolved = {u["designator"] for u in data["unresolved"]}
            self.assertNotIn("W1", unresolved)
            self.assertEqual(len(unresolved), 39)
            self.assertFalse(data["success"])
            from altium_files import read_altium_text
            spec = read_altium_text(self.spec_file)
            lines = spec.splitlines()
            self.assertIn("UPDATE_WIREPARAM|W1|Design Item Id|W-TEST-01", lines)
            self.assertIn("UPDATE_WIREPARAM|W1|Source Library|custom.DbLib", lines)
            self.assertIn("UPDATE_WIREPARAM|W1|Gauge|22 AWG", lines)
            self.assertIn("UPDATE_WIREPARAM|W1|Color|Black", lines)
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)


if __name__ == "__main__":
    unittest.main()

