"""Unit tests for multiboard (multi-board schematic reader and review) and the
get_multiboard_data tool. Synthetic documents only (logical.json shape of
Altium 26.7); harness projects and the database library are patched in.

Run from server/:  python -m unittest tests.test_multiboard -v
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import multiboard as mb
from dblib import DbLib, DbTable


def _pin(pid, number, name):
    return {
        "pinId": number,
        "name": name,
        "number": number,
        "parameters": [{"name": "ExternalNetName", "value": name, "id": pid + 1000}],
        "id": pid,
    }


def _comp(cid, part, pins):
    return {
        "designator": "",
        "comment": "=PartNumber",
        "pins": pins,
        "parameters": [
            {"name": "DesignItemId", "value": part, "id": cid + 1000},
            {"name": "SourceLibraryName", "value": "lib.DbLib", "id": cid + 1001},
        ],
        "id": cid,
    }


def _logical():
    """MOD_1.J1 -H_1- inline (INL_M / INL_F) -H_2- MOD_2.J1; H_1 has a loose end."""
    return {
        "uniqueId": "x",
        "modules": [],
        "virtualModules": [
            {
                "designator": "MOD_1",
                "title": "Device A",
                "components": [
                    _comp(10, "DEV_A", [_pin(11, "1", "SIG_A"), _pin(12, "2", "NC")]),
                    _comp(20, "DEV_A", [_pin(21, "1", "SIG_A"), _pin(22, "2", "NC")]),
                ],
                "entries": [
                    {
                        "logicalConnection": 100,
                        "linkedComponent": 10,
                        "calculatedDesignator": "J1",
                        "id": 30,
                    },
                    {"linkedComponent": 20, "calculatedDesignator": "J2", "id": 31},
                ],
                "entryPinMap": {
                    "map": [
                        {"key": 11, "value": 30},
                        {"key": 12, "value": 30},
                        {"key": 21, "value": 31},
                        {"key": 22, "value": 31},
                    ]
                },
                "id": 1,
            },
            {
                "designator": "MOD_2",
                "title": "Device B",
                "components": [
                    _comp(40, "DEV_B", [_pin(41, "1", "SIG_B"), _pin(42, "2", "OTHER")])
                ],
                "entries": [
                    {
                        "logicalConnection": 101,
                        "linkedComponent": 40,
                        "calculatedDesignator": "J1",
                        "id": 50,
                    }
                ],
                "entryPinMap": {
                    "map": [{"key": 41, "value": 50}, {"key": 42, "value": 50}]
                },
                "id": 2,
            },
        ],
        "connections": [
            {
                "$type": "__logicalHarnessConnection",
                "designator": "H_1",
                "source": {},
                "components": [
                    _comp(110, "INL_M", [_pin(111, "1", "1"), _pin(112, "2", "2")])
                ],
                "entries": [
                    {"definedDesignator": "Entry_1", "entryType": 1, "id": 120},
                    {
                        "isInterConnection": True,
                        "definedDesignator": "Entry_2",
                        "id": 121,
                    },
                    {"definedDesignator": "Entry_3", "id": 122},
                ],
                "entryPinMap": {
                    "map": [{"key": 111, "value": 121}, {"key": 112, "value": 121}]
                },
                "entriesConnections": [{"entry1": 30, "entry2": 120, "id": 130}],
                "interConnectedEntriesConnections": [
                    {
                        "entry1": 121,
                        "entry2": 221,
                        "physicalConnections": [
                            {"pinPair": {"pin1": 211, "pin2": 111}, "id": 131},
                            {"pinPair": {"pin1": 212, "pin2": 112}, "id": 132},
                        ],
                        "id": 133,
                    }
                ],
                "connectionEntriesConnections": [],
                "id": 100,
            },
            {
                "$type": "__logicalHarnessConnection",
                "designator": "H_2",
                "source": {"sourceProject": "HAR_B.PrjHar"},
                "components": [
                    _comp(210, "INL_F", [_pin(211, "1", "1"), _pin(212, "2", "2")])
                ],
                "entries": [
                    {"definedDesignator": "Entry_1", "entryType": 1, "id": 220},
                    {
                        "isInterConnection": True,
                        "definedDesignator": "Entry_2",
                        "entryType": 1,
                        "id": 221,
                    },
                ],
                "entryPinMap": {
                    "map": [{"key": 211, "value": 221}, {"key": 212, "value": 221}]
                },
                "entriesConnections": [{"entry1": 50, "entry2": 220, "id": 230}],
                "interConnectedEntriesConnections": [],
                "connectionEntriesConnections": [],
                "id": 101,
            },
        ],
    }


def _db():
    rows = {
        "DEV_A": {"Mating Part": "HDR_A_MATE", "Manufacturer Part Number 1": "DEVA-1"},
        "DEV_B": {"Mating Part": "", "Manufacturer Part Number 1": "DEVB-1"},
        "HDR_A_MATE": {"Mating Part": ""},
        # DEV_B is matched from the harness side's Mating Part
        "HDR_B_MATE": {"Mating Part": "DEVB-1"},
    }
    return DbLib(
        path=None,
        name="lib.DbLib",
        tables={"c": DbTable(name="c", key="PartNumber", rows=rows)},
    )


HARNESS = {
    "HAR_A": [
        {
            "designator": "P1",
            "part_number": "HDR_A_MATE",
            "sheet": "a",
            "pins": {"1": "NET_A", "2": "NET_X"},
        },
        {
            "designator": "J1",
            "part_number": "INL_M",
            "sheet": "a",
            "pins": {"1": "NET_A", "2": "NET_X"},
        },
    ],
    "HAR_B": [
        {
            "designator": "P1",
            "part_number": "HDR_B_MATE",
            "sheet": "b",
            "pins": {"1": "NET_B", "2": None},
        },
        {
            "designator": "J1",
            "part_number": "INL_F",
            "sheet": "b",
            "pins": {"1": "NET_B", "2": None},
        },
    ],
}


class MultiboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.mbs = tmp / "Sys.MbsDoc"
        self.write_mbs(_logical())
        for name in HARNESS:
            (tmp / name).mkdir()
            (tmp / name / f"{name}.PrjHar").write_text("[Design]\n", encoding="utf-8")
        (tmp / "Sys.PrjMbd").write_text(
            "[Document1]\nDocumentPath=Sys.MbsDoc\n\n"
            "[Document2]\nDocumentPath=HAR_A\\HAR_A.PrjHar\n\n"
            "[Document3]\nDocumentPath=HAR_B\\HAR_B.PrjHar\n",
            encoding="utf-8",
        )
        self.harness = HARNESS
        self.patches = [
            patch(
                "multiboard._harness_connectors",
                side_effect=lambda p: self.harness[Path(p).stem],
            ),
            patch("multiboard.load_dblib", return_value=_db()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write_mbs(self, logical):
        with zipfile.ZipFile(self.mbs, "w") as z:
            z.writestr("logical.json", json.dumps(logical))
            z.writestr("Pages\\page_1.json", json.dumps({"items": []}))

    def review(self, **kw):
        return mb.review_multiboard(self.mbs, dblib_path="lib.DbLib", **kw)

    def issue_types(self, result):
        return {(i["type"], i["where"]) for i in result["issues"]}

    def test_parse_labels_mates_and_inline_pairs(self):
        model = mb.parse_mbsdoc(self.mbs)
        self.assertEqual(model["page_count"], 1)
        h1 = model["connections"][0]
        self.assertEqual(h1["kind"], "harness")
        self.assertIsNone(h1["source"])
        e1, e2, e3 = h1["entries"]
        self.assertEqual(
            (e1["label"], e1["gender"], e1["mates"]),
            ("H_1.Entry_1", "female", ["MOD_1.J1"]),
        )
        self.assertTrue(e2["inline"])
        self.assertEqual(e2["inline_part_numbers"], ["INL_M"])
        self.assertEqual(e2["mates"], ["H_2.Entry_2"])
        self.assertEqual(e3["mates"], [])
        self.assertEqual(
            model["connections"][1]["source"], {"sourceProject": "HAR_B.PrjHar"}
        )
        j1 = model["blocks"][0]["entries"][0]
        self.assertEqual((j1["part_number"], j1["mated_to"]), ("DEV_A", "H_1.Entry_1"))
        self.assertEqual([p["net"] for p in j1["pins"]], ["SIG_A", "NC"])
        pairs = model["inline_links"][0]["pin_pairs"]
        self.assertEqual(
            [(p["a"], p["b"]) for p in pairs],
            [("H_2:INL_F.1", "H_1:INL_M.1"), ("H_2:INL_F.2", "H_1:INL_M.2")],
        )

    def test_structural_issues(self):
        result = self.review(include_pins=False)
        types = self.issue_types(result)
        self.assertIn(("dangling_entry", "H_1.Entry_3"), types)
        self.assertIn(("unconnected_entry", "MOD_1.J2"), types)
        self.assertIn(("harness_not_linked", "H_1"), types)
        self.assertNotIn(("harness_not_linked", "H_2"), types)
        self.assertNotIn("pin_review", result)
        self.assertEqual(result["blocks"][0]["entries"][0]["pin_count"], 2)

    def test_matches_projects_and_traces_through_inline_pair(self):
        result = self.review()
        review = {r["connection"]: r for r in result["harness_review"]}
        self.assertEqual(review["H_1"]["matched_project"], "HAR_A")
        self.assertEqual(review["H_2"]["matched_project"], "HAR_B")
        connectors = {
            e["entry"]: e.get("connector")
            for r in review.values()
            for e in r["entries"]
        }
        self.assertEqual(connectors["H_1.Entry_1"], "P1")
        self.assertEqual(connectors["H_1.Entry_2"], "J1")
        self.assertEqual(connectors["H_2.Entry_1"], "P1")
        net = next(
            n for n in result["system_nets"] if "MOD_2.J1.1 (SIG_B)" in n["devices"]
        )
        self.assertEqual(net["devices"], ["MOD_1.J1.1 (SIG_A)", "MOD_2.J1.1 (SIG_B)"])
        self.assertEqual(net["harness_nets"], ["H_1:NET_A", "H_2:NET_B"])
        types = self.issue_types(result)
        # NET_X lands on the device pin named NC
        self.assertIn(("wired_to_unused_pin", "MOD_1.J1.2"), types)
        pins = next(p for p in result["pin_review"] if p["entry"] == "MOD_2.J1")
        self.assertEqual(pins["unwired_pins"], ["2"])

    def test_ambiguous_connector_and_entry_map(self):
        self.harness = {
            **HARNESS,
            "HAR_A": HARNESS["HAR_A"] + [{**HARNESS["HAR_A"][0], "designator": "P2"}],
        }
        types = self.issue_types(self.review())
        self.assertIn(("ambiguous_harness_connector", "H_1.Entry_1"), types)
        result = self.review(entry_map={"H_1": {"Entry_1": "P2"}})
        self.assertNotIn(
            ("ambiguous_harness_connector", "H_1.Entry_1"), self.issue_types(result)
        )
        h1 = next(r for r in result["harness_review"] if r["connection"] == "H_1")
        self.assertEqual(h1["entries"][0]["connector"], "P2")

    def test_assigned_component_settles_connector(self):
        self.harness = {
            **HARNESS,
            "HAR_A": HARNESS["HAR_A"] + [{**HARNESS["HAR_A"][0], "designator": "P2"}],
        }
        types = self.issue_types(self.review())
        self.assertIn(("harness_entry_without_part", "H_2.Entry_1"), types)
        logical = _logical()
        h1 = logical["connections"][0]
        comp = _comp(140, "HDR_A_MATE", [_pin(141, "1", "1"), _pin(142, "2", "2")])
        comp["designator"] = "P2"
        h1["components"].append(comp)
        h1["entryPinMap"]["map"] += [
            {"key": 141, "value": 120},
            {"key": 142, "value": 120},
        ]
        self.write_mbs(logical)
        result = self.review()
        types = self.issue_types(result)
        self.assertNotIn(("ambiguous_harness_connector", "H_1.Entry_1"), types)
        self.assertNotIn(("harness_entry_without_part", "H_1.Entry_1"), types)
        row = next(r for r in result["harness_review"] if r["connection"] == "H_1")
        self.assertEqual(
            (row["entries"][0]["connector"], row["entries"][0]["connector_part"]),
            ("P2", "HDR_A_MATE"),
        )
        # A component of another part than the matched connector is an error
        comp["parameters"][0]["value"] = "OTHER"
        self.write_mbs(logical)
        result = self.review(entry_map={"H_1": {"Entry_1": "P1"}})
        self.assertIn(("entry_part_mismatch", "H_1.Entry_1"), self.issue_types(result))

    def test_wrong_part_reports_no_harness_connector(self):
        logical = _logical()
        logical["virtualModules"][1]["components"][0]["parameters"][0]["value"] = (
            "DEV_A"
        )
        self.write_mbs(logical)
        result = self.review(harness_map={"H_2": "HAR_B"})
        self.assertIn(("no_harness_connector", "H_2.Entry_1"), self.issue_types(result))

    def test_unknown_harness_map_project(self):
        result = self.review(harness_map={"H_1": "NOPE"})
        self.assertIn(("harness_map_unknown", "H_1"), self.issue_types(result))

    def test_prjmbd_input_finds_mbsdoc(self):
        result = mb.review_multiboard(
            self.mbs.with_suffix(".PrjMbd"), dblib_path="x", include_pins=False
        )
        self.assertEqual(Path(result["file_path"]), self.mbs.resolve())
        self.assertEqual(
            [c["name"] for c in result["child_projects"]], ["HAR_A", "HAR_B"]
        )


class MultiboardToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_rejects_other_files(self):
        import main

        raw = await main.get_multiboard_data(None, file_path="C:/nope/x.WirDoc")
        self.assertFalse(json.loads(raw)["success"])

    async def test_tool_reads_document(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            mbs = Path(tmp) / "Lone.MbsDoc"
            with zipfile.ZipFile(mbs, "w") as z:
                z.writestr("logical.json", json.dumps(_logical()))
            with patch("multiboard.find_dblib", return_value=None):
                data = json.loads(
                    await main.get_multiboard_data(None, file_path=str(mbs))
                )
        self.assertTrue(data["success"])
        self.assertEqual(data["summary"]["blocks"], 2)
        self.assertEqual(data["harness_review"][0]["matched_project"], None)
        self.assertIn("source_path", data)


if __name__ == "__main__":
    unittest.main()
