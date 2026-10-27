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

    def test_parse_multipart_connector(self):
        """Each part of a multi-part connector is its own object with all pins;
        only those of its current part are listed, cavity parts left in the
        hidden ones are reported, and wires attach to the visible pin even
        where a hidden pin shares its hot end."""
        from harness_parser import parse_wirdoc_records
        def pin(owner, des, part, y, uid):
            return {"RECORD": "113", "OwnerIndex": str(owner), "Designator": des,
                    "OwnerPartId": str(part), "Location.X": "100", "Location.Y": str(y),
                    "PinLength": "20", "UniqueID": uid}
        recs = [
            {"HEADER": "Protel for Windows - Harness"},
            {"RECORD": "106", "LibReference": "FH", "PartCount": "3", "CurrentPartId": "1",
             "Location.X": "100", "Location.Y": "200", "UniqueID": "A"},
            {"RECORD": "34", "OwnerIndex": "0", "Name": "Designator", "Text": "FH1"},
            pin(0, "1", 1, 200, "A1"),
            pin(0, "11", 2, 200, "A11"),                      # hidden on part A
            {"RECORD": "131", "OwnerIndex": "3"},
            {"RECORD": "106", "OwnerIndex": "4", "DesignItemId": "STRAY", "UniqueID": "K1"},
            {"RECORD": "106", "LibReference": "FH", "PartCount": "3", "CurrentPartId": "2",
             "Location.X": "100", "Location.Y": "0", "UniqueID": "B"},
            {"RECORD": "34", "OwnerIndex": "6", "Name": "Designator", "Text": "FH1"},
            pin(6, "1", 1, 0, "B1"),                          # hidden on part B
            pin(6, "11", 2, 0, "B11"),
            {"RECORD": "107", "LocationCount": "2", "X1": "120", "Y1": "200", "X2": "300",
             "Y2": "200", "UniqueID": "W", "ComponentKind": "5"},
            {"RECORD": "34", "OwnerIndex": "10", "Name": "Designator", "Text": "W1"},
            {"RECORD": "41", "OwnerIndex": "10", "Name": "Net", "Text": "*"},
            {"RECORD": "41", "OwnerIndex": "10", "Name": "Gauge", "Text": "16 AWG",
             "IsHidden": "T"},
            {"RECORD": "130", "Location.X": "300", "Location.Y": "200", "Text": "W1",
             "Style": "1", "Orientation": "2", "UniqueID": "BR"},
        ]
        res = parse_wirdoc_records(recs, "sheet.WirDoc")
        part_a, part_b = sorted(res["connectors"], key=lambda c: c["current_part_id"])
        self.assertEqual((part_a["part_count"], part_b["current_part_id"]), (2, 2))
        self.assertEqual([p["designator"] for p in part_a["pins"]], ["1"])
        self.assertEqual([p["designator"] for p in part_b["pins"]], ["11"])
        self.assertEqual(part_a["hidden_pin_cavity_parts"],
                         [{"pin": "11", "part_numbers": ["STRAY"]}])
        self.assertNotIn("hidden_pin_cavity_parts", part_b)
        self.assertEqual(part_a["pins"][0]["connected_wire"], "W1")
        wire = res["wires"][0]
        self.assertEqual(wire["component_kind"], "standard_no_bom")
        self.assertEqual(wire["shown_parameters"], ["Net"])
        brk = res["wire_breaks"][0]
        self.assertEqual((brk["style"], brk["orientation"]), (1, 2))

    def test_parse_harness_objects(self):
        """Seal and associated part (written once, as Altium does for parts
        added by script), twist, shield with drain wire, cable and wire break."""
        from harness_parser import parse_wirdoc_records
        recs = [
            {"HEADER": "Protel for Windows - Harness"},
            {"RECORD": "106", "LibReference": "CONN", "Location.X": "100", "Location.Y": "200", "UniqueID": "C1"},
            {"RECORD": "34", "OwnerIndex": "0", "Name": "Designator", "Text": "P1"},
            {"RECORD": "113", "OwnerIndex": "0", "Designator": "1", "Name": "SIG",
             "Location.X": "100", "Location.Y": "200", "PinLength": "20", "UniqueID": "PIN1"},
            {"RECORD": "131", "OwnerIndex": "2", "AssociatedObjectType": "1"},
            {"RECORD": "106", "OwnerIndex": "3", "DesignItemId": "SEAL-1", "UniqueID": "K1",
             "ComponentDescription": "Wire seal", "SourceLibraryName": "*"},
            {"RECORD": "125", "OwnerIndex": "0"},
            {"RECORD": "106", "OwnerIndex": "5", "DesignItemId": "BOOT-1", "UniqueID": "K2"},
            # 7: wire W1, 9: drain wire W2
            {"RECORD": "107", "LocationCount": "2", "X1": "0", "Y1": "800", "X2": "300", "Y2": "800", "UniqueID": "WA"},
            {"RECORD": "34", "OwnerIndex": "7", "Name": "Designator", "Text": "W1"},
            {"RECORD": "107", "LocationCount": "2", "X1": "0", "Y1": "770", "X2": "125", "Y2": "770", "UniqueID": "WB"},
            {"RECORD": "34", "OwnerIndex": "9", "Name": "Designator", "Text": "W2"},
            {"RECORD": "118", "Location.X": "50", "Location.Y": "780", "Corner.X": "60", "Corner.Y": "820",
             "UniqueID": "TW", "ConnectedWiresUniqueIdsCount": "1", "ConnectedWireUniqueId1": "WA"},
            {"RECORD": "117", "Location.X": "150", "Location.Y": "870", "Corner.X": "100", "Corner.Y": "770",
             "Style": "1", "UniqueID": "SH", "ConnectedWiresUniqueIdsCount": "1", "ConnectedWireUniqueId1": "WA",
             "ConnectedPinWiresUniqueIdsCount": "1", "ConnectedPinWireUniqueId1": "WB"},
            {"RECORD": "123", "Location.X": "200", "Location.Y": "780", "Corner.X": "210", "Corner.Y": "820",
             "DesignItemId": "CB-1", "UniqueID": "CA", "ConnectedWiresUniqueIdsCount": "1",
             "ConnectedWireUniqueId1": "WA"},
            {"RECORD": "34", "OwnerIndex": "13", "Name": "Designator", "Text": "C1"},
            {"RECORD": "41", "OwnerIndex": "13", "Name": "Comment", "Text": "BELDEN"},
            {"RECORD": "41", "OwnerIndex": "13", "Name": "MPN", "Text": "8771"},
            {"RECORD": "130", "Location.X": "300", "Location.Y": "800", "Text": "W1",
             "UniqueID": "BR", "ConnectedObjectUniqueId": "WA"},
        ]
        res = parse_wirdoc_records(recs, "sheet.WirDoc")
        conn = res["connectors"][0]
        self.assertEqual(conn["pins"][0]["cavity_parts"], [
            {"type": "seal", "part_number": "SEAL-1", "source_library": "", "source_table": "",
             "description": "Wire seal", "unique_id": "K1", "parameters": {}}])
        self.assertEqual([p["part_number"] for p in conn["associated_parts"]], ["BOOT-1"])
        self.assertEqual(res["twists"][0]["wires"], ["W1"])
        shield = res["shields"][0]
        # corners normalised; pin in the middle of the bottom edge
        self.assertEqual((shield["x1"], shield["y1"], shield["x2"], shield["y2"]), (100, 770, 150, 870))
        self.assertEqual(shield["pin"], {"x": 125, "y": 770})
        self.assertEqual(shield["drain_wires"], ["W2"])
        cable = res["cables"][0]
        self.assertEqual((cable["designator"], cable["comment"], cable["part_number"]), ("C1", "BELDEN", "CB-1"))
        self.assertEqual(cable["parameters"], {"MPN": "8771"})
        self.assertEqual(res["wire_breaks"][0]["wire"], "W1")
        self.assertEqual(res["summary"]["shield_count"], 1)

    def test_shield_pin_rotations(self):
        from harness_parser import shield_pin
        s = {"x1": 0, "y1": 0, "x2": 100, "y2": 40, "style": 1}
        pins = [shield_pin({**s, "rotation": r}) for r in range(4)]
        self.assertEqual(pins, [{"x": 50, "y": 0}, {"x": 100, "y": 20}, {"x": 50, "y": 40}, {"x": 0, "y": 20}])
        self.assertIsNone(shield_pin({**s, "style": 0}))

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
        # Crimps are cavity parts of type 0
        self.assertIn("CAVITYPART|P1|1|0|13065S|my-components.DbLib|terminals-db.csv|", lines)
        self.assertIn("CAVITYPART|P1|2|0|13065S|||", lines)

    async def test_add_harness_wiring_harness_objects_serialization(self):
        import main
        from altium_files import read_altium_text
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        await main.add_harness_wiring(
            None,
            document_path=r"C:\test\sheet.WirDoc",
            cavity_parts=[
                {"connector": "P1", "pin": "3", "type": "seal", "part_number": "S-1",
                 "source_table": "seals.csv",
                 "parameters": {"Description": "Wire seal", "Color": "Green"}},
                {"connector": "P1", "pin": "4", "type": "plug", "part_number": ""},
                {"connector": "P1", "pin": "4", "type": 3, "part_number": "O-1"},
            ],
            associated_parts=[{"owner": "SP1", "part_number": "HS-1", "description": "Heat shrink"}],
            twists=[{"x1": 100, "y1": 200, "x2": 50, "y2": 400},
                    {"x1": 0, "y1": 0, "x2": 1, "y2": 1, "designator": "TW2"}],
            label_updates=[{"x": 7400, "y": 8200, "text": "OLD|1", "new_text": "NEW"}],
            relink=True,
            shields=[{"x1": 0, "y1": 0, "x2": 500, "y2": 1000, "style": "with_connection",
                      "rotation": 1}],
            cables=[{"designator": "C1", "x1": 0, "y1": 0, "x2": 10, "y2": 10,
                     "comment": "BELDEN 8771", "part_number": "8771",
                     "parameters": {"Manufacturer": "Belden"}, "description": "Cable"}],
            wire_breaks=[{"x": 14700, "y": 7700}],
        )
        lines = read_altium_text(self.spec_file).splitlines()
        # The description is never a parameter on cavity / associated parts
        self.assertIn("CAVITYPART|P1|3|1|S-1||seals.csv|Wire seal|Color|Green", lines)
        self.assertIn("CAVITYPART|P1|4|2||||", lines)
        self.assertIn("CAVITYPART|P1|4|3|O-1|||", lines)
        self.assertIn("ASSOCPART|SP1|HS-1|||Heat shrink", lines)
        self.assertIn("TWIST|100|200|50|400||", lines)
        self.assertIn("TWIST|0|0|1|1||TW2", lines)
        self.assertIn("SHIELD|0|0|500|1000|1|1|", lines)
        self.assertIn("LABELTEXT|7400|8200|OLD%7C1|NEW", lines)
        self.assertEqual(lines[-1], "RELINK")
        self.assertIn(
            "CABLE|0|0|10|10||C1|BELDEN 8771|8771||Manufacturer|Belden|Description|Cable", lines
        )
        self.assertIn("WIREBREAK|14700|7700", lines)

    async def test_add_harness_wiring_designator_updates_first(self):
        import main
        from altium_files import read_altium_text
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        await main.add_harness_wiring(
            None,
            document_path=r"C:\test\sheet.WirDoc",
            wire_updates=[{"designator": "W1", "parameters": {"PartNumber": "X"}}],
            designator_updates=[
                {"unique_id": "ABCDEFGH", "designator": "W1"},
                {"unique_id": " NCUID ", "designator": "NC1", "show_name": False},
                {"unique_id": "CONNUID", "designator": "P|1", "show_name": True},
            ],
        )
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertEqual(lines[1], "DESIGNATOR|ABCDEFGH|W1|")
        self.assertEqual(lines[2], "DESIGNATOR|NCUID|NC1|0")
        self.assertEqual(lines[3], "DESIGNATOR|CONNUID|P%7C1|1")
        # Renames come before records that address the new designators
        self.assertGreater(lines.index("UPDATE_WIREPARAM|W1|PartNumber|X"), 3)

    async def test_add_harness_wiring_kinds_visibility_breaks(self):
        """Wire component kind and parameter visibility, wire break direction
        (orientation from the wire ending there), part-only renames and
        clearing hidden-pin cavity parts serialise as their records."""
        import main
        from altium_files import read_altium_text
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        with patch.object(main, "parse_wirdoc", side_effect=OSError("no file")):
            await main.add_harness_wiring(
                None,
                document_path=r"C:\test\sheet.WirDoc",
                designator_updates=[{"unique_id": "U", "designator": "FH2", "part_only": True}],
                clear_hidden_cavity_parts=["FH1"],
                wires=[{"designator": "W48", "net": "BUS", "points": [100, 900, 100, 500],
                        "component_kind": "standard_no_bom", "hide_parameters": ["Net"]}],
                wire_updates=[{"designator": "W9", "component_kind": 0,
                               "show_parameters": ["Gauge"]}],
                wire_breaks=[
                    {"x": 100, "y": 500, "direction": "out"},     # wire runs up: 3
                    {"x": 7, "y": 8, "style": "left", "orientation": 2},
                    {"x": 9, "y": 9},
                ],
            )
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertEqual(lines[1], "DESIGNATOR|U|FH2||1")
        self.assertEqual(lines[2], "CLEARHIDDEN|FH1")
        wire_at = next(i for i, ln in enumerate(lines) if ln.startswith("WIRE|W48"))
        self.assertEqual(lines[wire_at + 1:wire_at + 3], ["WIREKIND||5", "WIREPARAMVIS||Net|1"])
        self.assertIn("WIREKIND|W9|0", lines)
        self.assertIn("WIREPARAMVIS|W9|Gauge|0", lines)
        self.assertIn("WIREBREAK|100|500|1|3", lines)
        self.assertIn("WIREBREAK|7|8|0|2", lines)
        self.assertIn("WIREBREAK|9|9", lines)

    async def test_add_harness_wiring_rejects_unknown_kind_and_style(self):
        import main
        for kwargs in (
            {"wire_updates": [{"designator": "W1", "component_kind": "virtual"}]},
            {"wire_breaks": [{"x": 0, "y": 0, "direction": "sideways"}]},
        ):
            res = json.loads(await main.add_harness_wiring(
                None, document_path=r"C:\test\sheet.WirDoc", **kwargs))
            self.assertFalse(res["success"])
        self.mock_bridge.assert_not_called()

    async def test_add_harness_wiring_rejects_unknown_types(self):
        import main
        res = json.loads(await main.add_harness_wiring(
            None, document_path=r"C:\test\sheet.WirDoc",
            cavity_parts=[{"connector": "P1", "pin": "1", "type": "gasket", "part_number": "X"}],
        ))
        self.assertFalse(res["success"])
        self.assertIn("gasket", res["error"])
        res = json.loads(await main.add_harness_wiring(
            None, document_path=r"C:\test\sheet.WirDoc",
            shields=[{"x1": 0, "y1": 0, "x2": 1, "y2": 1, "style": "braided"}],
        ))
        self.assertFalse(res["success"])
        self.mock_bridge.assert_not_called()

    async def test_add_harness_wiring_reports_links_from_file(self):
        """Links are read back from the saved file: a rectangle that links no
        wire, or a break on no wire, is not_found."""
        import main
        self.mock_bridge.return_value = {
            "success": True,
            "result": {"success": True, "saved": True, "not_found": []},
        }
        saved = {
            "twists": [{"x1": 10, "y1": 20, "x2": 20, "y2": 60, "wires": ["W1", "W2"]}],
            "shields": [{"x1": 0, "y1": 0, "x2": 50, "y2": 100, "style": 1, "rotation": 0,
                         "wires": [], "drain_wires": [], "pin": {"x": 25.0, "y": 0}}],
            "cables": [],
            "wire_breaks": [{"x": 1470, "y": 770, "wire": "W3"}],
        }
        with patch("main.parse_wirdoc", return_value=saved):
            res = json.loads(await main.add_harness_wiring(
                None, document_path=r"C:\test\sheet.WirDoc",
                twists=[{"x1": 200, "y1": 600, "x2": 100, "y2": 200}],
                shields=[{"x1": 0, "y1": 0, "x2": 500, "y2": 1000, "style": 1}],
                wire_breaks=[{"x": 14700, "y": 7700}, {"x": 0, "y": 0}],
            ))
        self.assertEqual(res["links"]["twists"][0]["wires"], ["W1", "W2"])
        self.assertEqual(res["links"]["shields"][0]["pin_mils"], [250.0, 0])
        self.assertEqual(res["links"]["wire_breaks"][0]["wire"], "W3")
        self.assertEqual(
            res["not_found"],
            ["shield@0,0,500,1000 (links no wire)", "wire_break@0,0 (no wire end there)"],
        )
        self.assertFalse(res["success"])

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

    async def test_check_harness_wiring_is_read_only(self):
        import main
        if not SLAVE_WIRDOC.exists():
            self.skipTest("Sample Slave WirDoc not found in workspace")
        res = json.loads(await main.check_harness_wiring(None, file_path=str(SLAVE_WIRDOC)))
        self.assertTrue(res["success"])
        self.assertEqual(res["summary"]["wire_count"], 40)
        self.mock_bridge.assert_not_called()

    async def test_check_harness_wiring_needs_a_wirdoc(self):
        import main
        res = json.loads(await main.check_harness_wiring(None, file_path=r"C:\x\sheet.SchDoc"))
        self.assertFalse(res["success"])

    async def test_reroute_moves_wires_in_place(self):
        import main
        from altium_files import read_altium_text
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        await main.add_harness_wiring(
            None,
            document_path=r"C:\test\sheet.WirDoc",
            reroutes=[{"designator": "W1", "points": [1000, 500, 3000, 500, 3000, 900, 5000, 900]}],
        )
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertEqual(sum(l.startswith("REROUTE|W1|") for l in lines), 1)
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
        crimp = [l for l in lines if l.startswith("CAVITYPART|")]
        # Description column -> component description, never a parameter
        self.assertEqual(
            crimp,
            ["CAVITYPART|P1|1|0|T-1|lib.DbLib|terms.csv|Socket|PartNumber|T-1|Manufacturer 1|Acme"],
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

    def _stale_fixture(self):
        """A library whose wire column 'Wire Gauge' was renamed 'Gauge' and whose
        terminal/connector column 'Old Col' was deleted, and a sheet still
        carrying the old names."""
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        (tmp / "wires.csv").write_text(
            "PartNumber,Gauge,Color,Insulation\nWB-22,22 AWG,Black,PTFE\n", encoding="utf-8"
        )
        (tmp / "parts.csv").write_text(
            "PartNumber,Description,Manufacturer 1,Finish\nT-1,Socket,Acme,\nC-1,Housing,Acme,\n",
            encoding="utf-8",
        )
        (tmp / "lib.DbLib").write_text(
            "[DatabaseLinks]\n"
            "ConnectionString=Provider=MSDASQL.1;Extended Properties=\"Driver={Microsoft Access "
            "Text Driver (*.txt, *.csv)};Dbq=Z:\\missing\\;Extensions=csv;\"\n"
            "[Table1]\nTableName=wires.csv\nEnabled=True\n"
            "[Table2]\nTableName=parts.csv\nEnabled=True\n",
            encoding="utf-8",
        )
        doc = tmp / "h.WirDoc"
        doc.write_bytes(b"")
        fake = {
            "wires": [{"designator": "W1", "parameters": {
                "PartNumber": "WB-22", "Gauge": "22 AWG", "Wire Gauge": "22 AWG",
                "VoltageRating": "600V", "Insulation": "PTFE", "Design Item Id": "WB-22"}}],
            "connectors": [{
                "designator": "P1",
                "parameters": {"PartNumber": "C-1", "Comment": "C-1", "Old Col": "x"},
                "pins": [{"designator": "1", "cavity_parts": [{
                    "type": "crimp", "part_number": "T-1",
                    # Finish: a column emptied in the database, no longer carried
                    "parameters": {"PartNumber": "T-1", "Old Col": "y", "Finish": "Tin"}}]}],
            }],
        }
        return doc, fake

    async def test_sync_harness_library_reports_stale_parameters(self):
        import main
        from altium_files import read_altium_text
        doc, fake = self._stale_fixture()
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        with patch("main.parse_wirdoc", return_value=fake):
            res = json.loads(await main.sync_harness_library(None, document_path=str(doc)))
        self.assertEqual(
            res["stale_parameters"],
            {"Old Col": ["P1", "P1.1 T-1"], "VoltageRating": ["W1"], "Wire Gauge": ["W1"]},
        )
        self.assertEqual(res["stale_parameters_deleted"], {})
        self.assertTrue(any("ask the user" in w for w in res["warnings"]))
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertFalse(any(l.startswith(("DELETE_WIREPARAM", "DELETE_COMPPARAM")) for l in lines))
        crimp = [l for l in lines if l.startswith("CAVITYPART|P1|1|")]
        # kept stale parameter carried over; emptied database column dropped
        self.assertEqual(crimp, ["CAVITYPART|P1|1|0|T-1|lib.DbLib|parts.csv|Socket|"
                                 "PartNumber|T-1|Manufacturer 1|Acme|Old Col|y"])
        self.assertTrue(res["success"])

    async def test_sync_harness_library_deletes_chosen_stale_parameters(self):
        import main
        from altium_files import read_altium_text
        doc, fake = self._stale_fixture()
        self.mock_bridge.return_value = {
            "success": True, "result": {"success": True, "parameters_deleted": 2}}
        with patch("main.parse_wirdoc", return_value=fake):
            res = json.loads(await main.sync_harness_library(
                None, document_path=str(doc), delete_stale_parameters=["wire gauge", "Old Col"]))
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertIn("DELETE_WIREPARAM|W1|Wire Gauge", lines)
        self.assertNotIn("DELETE_WIREPARAM|W1|VoltageRating", lines)
        self.assertIn("DELETE_COMPPARAM|P1|Old Col", lines)
        crimp = [l for l in lines if l.startswith("CAVITYPART|P1|1|")]
        self.assertEqual(crimp, ["CAVITYPART|P1|1|0|T-1|lib.DbLib|parts.csv|Socket|"
                                 "PartNumber|T-1|Manufacturer 1|Acme"])
        self.assertEqual(res["stale_parameters"], {"VoltageRating": ["W1"]})
        self.assertEqual(
            res["stale_parameters_deleted"], {"Old Col": ["P1", "P1.1 T-1"], "Wire Gauge": ["W1"]}
        )
        self.assertEqual(res["parameters_deleted_count"], 2)
        # a connector with nothing to refresh is not reported as refreshed
        self.assertEqual(res["connectors_refreshed"], {})

    async def test_sync_harness_library_deletes_all_stale_parameters(self):
        import main
        from altium_files import read_altium_text
        doc, fake = self._stale_fixture()
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        with patch("main.parse_wirdoc", return_value=fake):
            res = json.loads(await main.sync_harness_library(
                None, document_path=str(doc), delete_stale_parameters=True))
        lines = read_altium_text(self.spec_file).splitlines()
        self.assertIn("DELETE_WIREPARAM|W1|VoltageRating", lines)
        self.assertIn("DELETE_WIREPARAM|W1|Wire Gauge", lines)
        # database columns and the library link are never deleted
        self.assertFalse(any(l.startswith("DELETE_") and l.endswith(("|Gauge", "|Insulation",
                         "|PartNumber", "|Design Item Id", "|Comment")) for l in lines))
        self.assertEqual(res["stale_parameters"], {})

    async def test_sync_harness_library_cavity_slots_associated_parts_and_cables(self):
        import tempfile

        import main
        tmp = Path(tempfile.mkdtemp())
        (tmp / "parts.csv").write_text(
            "PartNumber,Description,Manufacturer 1\n"
            "T-1,Socket,Acme\nS-1,Seal,Acme\nPL-1,Plug,Acme\nB-1,Boot,Acme\nCB-1,Cable 2C,Acme\n",
            encoding="utf-8",
        )
        (tmp / "lib.DbLib").write_text(
            "[DatabaseLinks]\n"
            "ConnectionString=Provider=MSDASQL.1;Extended Properties=\"Driver={Microsoft Access "
            "Text Driver (*.txt, *.csv)};Dbq=Z:\\missing\\;Extensions=csv;\"\n"
            "[Table1]\nTableName=parts.csv\nEnabled=True\n",
            encoding="utf-8",
        )
        doc = tmp / "h.WirDoc"
        doc.write_bytes(b"")
        fake = {
            "wires": [],
            "connectors": [
                {"designator": "P1", "parameters": {}, "associated_parts": [], "pins": [
                    # current seal kept and refreshed, crimp replaced by the map
                    {"designator": "1", "cavity_parts": [
                        {"type": "crimp", "part_number": "OLD"},
                        # a value typed in Altium survives, database values win
                        {"type": "seal", "part_number": "S-1",
                         "parameters": {"StripLength": "4mm", "Manufacturer 1": "Old"}}]},
                    # unknown "other" part: that slot is left alone
                    {"designator": "2", "cavity_parts": [
                        {"type": "other", "part_number": "NOPE"}]},
                    {"designator": "3", "cavity_parts": [
                        {"type": "plug", "part_number": "PL-1"}]},
                ]},
            ],
            "splices": [{"designator": "SP1", "associated_parts": [{"part_number": "B-1"}]}],
            "cables": [{"designator": "C1", "x1": 100, "y1": 200, "x2": 110, "y2": 250,
                        "part_number": "CB-1"}],
        }
        self.mock_bridge.return_value = {"success": True, "result": {"success": True}}
        with patch("main.parse_wirdoc", return_value=fake):
            res = json.loads(await main.sync_harness_library(
                None, document_path=str(doc),
                cavity_map={"P1": {"1": "T-1", "3": {"plug": []}}},
                associated_map={"P1": ["B-1"]},
            ))
        from altium_files import read_altium_text
        lines = read_altium_text(self.spec_file).splitlines()
        cav = [l for l in lines if l.startswith("CAVITYPART|")]
        self.assertEqual(cav, [
            "CAVITYPART|P1|1|0|T-1|lib.DbLib|parts.csv|Socket|PartNumber|T-1|Manufacturer 1|Acme",
            "CAVITYPART|P1|1|1|S-1|lib.DbLib|parts.csv|Seal|PartNumber|S-1|Manufacturer 1|Acme"
            "|StripLength|4mm",
            "CAVITYPART|P1|3|2||||",
        ])
        assoc = [l for l in lines if l.startswith("ASSOCPART|")]
        self.assertEqual(assoc, [
            "ASSOCPART|P1|B-1|lib.DbLib|parts.csv|Boot|PartNumber|B-1|Manufacturer 1|Acme",
            "ASSOCPART|SP1|B-1|lib.DbLib|parts.csv|Boot|PartNumber|B-1|Manufacturer 1|Acme",
        ])
        # Cables keep a Description parameter; the rectangle comes back in mils
        self.assertIn(
            "CABLE|1000|2000|1100|2500||C1||CB-1|lib.DbLib|PartNumber|CB-1|Description|Cable 2C"
            "|Manufacturer 1|Acme",
            lines,
        )
        self.assertEqual(res["unresolved"], [{"designator": "P1.2", "reason": "part 'NOPE' not in database"}])

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

