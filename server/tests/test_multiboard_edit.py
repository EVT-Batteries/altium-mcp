"""Unit tests for multiboard_edit (offline .MbsDoc edits) and the
edit_multiboard_schematic tool. Synthetic documents only; the Altium bridge
is mocked, nothing reaches Altium.

Run from server/:  python -m unittest tests.test_multiboard_edit -v
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import multiboard as mb
import multiboard_edit as me


def _pin(pid, number):
    return {
        "pinId": number,
        "name": f"S{number}",
        "number": number,
        "parameters": [],
        "id": pid,
    }


def _logical():
    return {
        "virtualModules": [
            {
                "designator": "MOD_1",
                "title": "Device",
                "components": [
                    {
                        "designator": "",
                        "pins": [_pin(11, "1"), _pin(12, "2")],
                        "parameters": [
                            {"name": "DesignItemId", "value": "PART_A", "id": 13}
                        ],
                        "id": 10,
                    },
                    {
                        "designator": "",
                        "pins": [_pin(21, "1"), _pin(22, "2")],
                        "parameters": [
                            {"name": "DesignItemId", "value": "PART_B", "id": 23}
                        ],
                        "id": 20,
                    },
                ],
                "entries": [
                    {
                        "logicalConnection": 100,
                        "linkedComponent": 10,
                        "calculatedDesignator": "J1",
                        "id": 30,
                    },
                    {
                        "logicalConnection": 200,
                        "linkedComponent": 20,
                        "calculatedDesignator": "J2",
                        "id": 31,
                    },
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
            }
        ],
        "connections": [
            {
                "$type": "__logicalHarnessConnection",
                "designator": "H_1",
                "source": {},
                "components": [],
                "entries": [
                    {"definedDesignator": "Entry_1", "entryType": 1, "id": 101},
                    {"definedDesignator": "Entry_2", "id": 102},
                ],
                "entryPinMap": {"map": []},
                "entriesConnections": [
                    {"entry1": 30, "entry2": 101, "physicalConnections": [], "id": 103}
                ],
                "interConnectedEntriesConnections": [],
                "connectionEntriesConnections": [],
                "id": 100,
            },
            {
                "$type": "__logicalHarnessConnection",
                "designator": "H_2",
                "source": {},
                "components": [],
                "entries": [
                    {"definedDesignator": "Entry_1", "entryType": 1, "id": 201}
                ],
                "entryPinMap": {"map": []},
                "entriesConnections": [
                    {"entry1": 31, "entry2": 201, "physicalConnections": [], "id": 203}
                ],
                "interConnectedEntriesConnections": [],
                "connectionEntriesConnections": [],
                "id": 200,
            },
        ],
    }


def _point(pid, x, y, parent, d=None, logical=None, kind=None):
    item = {
        "$type": "__drawingConnectionPoint",
        "position": me._vec(x, y),
        "direction": d or {},
    }
    if kind:
        item["connectionItemType"] = kind
    if logical:
        item["logicalObjectId"] = logical
    item.update(
        {
            "lineStyleId": 3,
            "anchor": 5,
            "parentId": parent,
            "selectable": True,
            "id": pid,
        }
    )
    return item


def _seg(sid, a, b, parent, kind=1):
    item = {
        "$type": "__drawingConnection",
        "endItemId1": a,
        "endItemId2": b,
        "lineStyleId": 5,
    }
    if kind:
        item["connectionItemType"] = kind
    item.update({"anchor": 5, "parentId": parent, "selectable": True, "id": sid})
    return item


def _page():
    """H_1: Entry_1 (100,100) -> corners (150,100), (150,50) -> open Entry_2 at
    (180,50) facing left. H_2: Entry_1 (300,300) -> (250,300) -> (250,0) ->
    point at (300,0)."""
    return {
        "items": [
            {"$type": "__drawingVirtualModule", "logicalObjectId": 1, "id": 1},
            {
                "$type": "__drawingLinkedProperty",
                "directValue": "Device",
                "propertyValuePath": "LogicalObject.Title",
                "parentId": 1,
                "id": 2,
            },
            {
                "$type": "__drawingModuleEntry",
                "logicalObjectId": 30,
                "parentId": 1,
                "id": 3,
            },
            {
                "$type": "__drawingLinkedProperty",
                "directValue": "J1",
                "propertyValuePath": "Designator",
                "parentId": 3,
                "id": 4,
            },
            {
                "$type": "__drawingModuleEntry",
                "logicalObjectId": 31,
                "parentId": 1,
                "id": 5,
            },
            {
                "$type": "__drawingLinkedProperty",
                "directValue": "J2",
                "propertyValuePath": "Designator",
                "parentId": 5,
                "id": 6,
            },
            {
                "$type": "__drawingConnectionGroup",
                "lineStyleId": 5,
                "logicalConnectionId": 100,
                "id": 10,
            },
            _point(11, 100, 100, 10, {"x": 1.0}, logical=101),
            _point(12, 180, 50, 10, {"x": -1.0}, logical=102),
            {
                "$type": "__drawingLinkedProperty",
                "offset": {"x": -40.0},
                "directValue": "Entry_2",
                "propertyValuePath": "Designator",
                "parentId": 12,
                "id": 13,
            },
            _seg(14, 11, 12, 10, kind=None),
            _point(15, 150, 100, 14, kind=1),
            _point(16, 150, 50, 14, kind=1),
            _seg(17, 11, 15, 14),
            _seg(18, 15, 16, 14),
            _seg(19, 16, 12, 14),
            {
                "$type": "__drawingConnectionGroup",
                "lineStyleId": 5,
                "logicalConnectionId": 200,
                "id": 20,
            },
            _point(21, 300, 300, 20, {"x": -1.0}, logical=201),
            _point(22, 300, 0, 20, {"x": -1.0}),
            _seg(23, 21, 22, 20, kind=None),
            _point(24, 250, 300, 23, kind=1),
            _point(25, 250, 0, 23, kind=1),
            _seg(26, 21, 24, 23),
            _seg(27, 24, 25, 23),
            _seg(28, 25, 22, 23),
        ]
    }


def _write(path, logical=None, page=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("core.json", '{\r\n  "version": 2\r\n}')
        z.writestr("logical.json", json.dumps(logical or _logical()))
        # Altium stores the page with a backslash; ZipInfo() would turn it into "/"
        info = zipfile.ZipInfo("page")
        info.filename = "Pages\\page_1.json"
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, json.dumps(page or _page()))
        z.writestr("options.json", json.dumps({"gridSize": 9.6}))


PINS = {"INL_M": ["1", "2"], "INL_F": ["1", "2"]}
JOIN = {
    "op": "join_harnesses",
    "entry": "H_1.Entry_2",
    "to": "H_2",
    "part": "INL_M",
    "mate_part": "INL_F",
    "attach": [250, 50],
}


class EditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "T.MbsDoc"
        _write(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, *ops):
        doc = me.MbsDocument(self.path)
        result = me.apply_operations(
            doc, list(ops), PINS, {"INL_M": ("lib.DbLib", "c.csv")}
        )
        doc.save()
        return result, me.MbsDocument(self.path)

    def items(self, doc):
        return {i["id"]: i for _, i in doc.all_items()}

    def test_save_keeps_members_and_names(self):
        doc = me.MbsDocument(self.path)
        doc.save()
        with zipfile.ZipFile(self.path) as z:
            self.assertEqual(
                [i.orig_filename for i in z.infolist()],
                ["core.json", "logical.json", "Pages\\page_1.json", "options.json"],
            )
            self.assertEqual(z.read("core.json"), b'{\r\n  "version": 2\r\n}')
            self.assertIn(b'\r\n  "virtualModules"', z.read("logical.json"))

    def test_swap_entry_parts(self):
        result, doc = self.apply(
            {"op": "swap_entry_parts", "block": "MOD_1", "entries": ["J1", "J2"]}
        )
        e30, e31 = doc.block("MOD_1")["entries"]
        self.assertEqual(
            (e30["linkedComponent"], e30["calculatedDesignator"]), (20, "J2")
        )
        self.assertEqual(
            (e31["linkedComponent"], e31["calculatedDesignator"]), (10, "J1")
        )
        pin_map = {
            m["key"]: m["value"] for m in doc.block("MOD_1")["entryPinMap"]["map"]
        }
        self.assertEqual(pin_map, {11: 31, 12: 31, 21: 30, 22: 30})
        items = self.items(doc)
        self.assertEqual(
            (items[4]["directValue"], items[6]["directValue"]), ("J2", "J1")
        )
        # The harnesses keep their drawn place: H_1 still mates entry 30, now PART_B
        model = mb.parse_mbsdoc(self.path)
        self.assertEqual(model["blocks"][0]["entries"][0]["part_number"], "PART_B")
        self.assertEqual(model["blocks"][0]["entries"][0]["mated_to"], "H_1.Entry_1")
        self.assertEqual(result[0]["entries"]["J1"], "now J2 (PART_B)")

    def test_set_title(self):
        _, doc = self.apply({"op": "set_title", "block": "MOD_1", "title": "New"})
        self.assertEqual(doc.block("MOD_1")["title"], "New")
        self.assertEqual(self.items(doc)[2]["directValue"], "New")

    def test_join_harnesses(self):
        result, doc = self.apply(JOIN)
        self.assertEqual(result[0]["mate"], "H_2.Entry_2")
        self.assertTrue(result[0]["split"])
        self.assertEqual(result[0]["mate_position"], [199.2, 50.0])
        h1, h2 = doc.connection("H_1"), doc.connection("H_2")
        e_open = doc.entry(h1, "Entry_2")
        self.assertTrue(e_open["isInterConnection"])
        self.assertNotIn("entryType", e_open)
        mate = doc.entry(h2, "Entry_2")
        self.assertEqual((mate["entryType"], mate["isInterConnection"]), (1, True))
        self.assertEqual([c["designator"] for c in h1["components"]], ["J?"])
        self.assertEqual(h1["components"][0]["parameters"][1]["value"], "lib.DbLib")
        link = h1["interConnectedEntriesConnections"][0]
        self.assertEqual((link["entry1"], link["entry2"]), (e_open["id"], mate["id"]))
        self.assertEqual(len(link["physicalConnections"]), 2)
        # ids continue above the document's maxima
        self.assertGreater(mate["id"], 203)
        items = self.items(doc)
        mate_point = next(
            i for i in items.values() if i.get("logicalObjectId") == mate["id"]
        )
        self.assertEqual(mate_point["matedConnectionPointId"], 12)
        self.assertEqual(items[12]["matedConnectionPointId"], mate_point["id"])
        self.assertEqual(mate_point["direction"], {"x": 1.0})
        label = next(i for i in items.values() if i.get("parentId") == mate_point["id"])
        self.assertEqual(
            (label["directValue"], label["offset"]), ("Entry_2", {"x": 40.0})
        )
        # H_2's branch is split at the junction; the rest moved to a new branch
        junction = next(
            i for i in items.values() if i.get("position") == {"x": 250, "y": 50}
        )
        self.assertEqual(items[23]["endItemId2"], junction["id"])
        self.assertEqual(
            {items[27]["endItemId1"], items[27]["endItemId2"]}, {24, junction["id"]}
        )
        rest = next(
            i
            for i in items.values()
            if i.get("endItemId1") == junction["id"] and "connectionItemType" not in i
        )
        self.assertEqual(rest["endItemId2"], 22)
        self.assertEqual(
            (items[28]["parentId"], items[25]["parentId"]), (rest["id"], rest["id"])
        )
        # The reader sees one inline pair and the new straight branch
        model = mb.parse_mbsdoc(self.path)
        self.assertEqual(
            model["inline_links"][0]["entries"], ["H_1.Entry_2", "H_2.Entry_2"]
        )
        segments = mb.drawing_geometry(self.path)["connections"]["H_2"]["segments"]
        self.assertIn([[208.8, 50.0], [250.0, 50.0]], segments)
        self.assertIn([[250.0, 50.0], [250.0, 0.0]], segments)

    def test_join_with_via_corners_on_junction(self):
        page = _page()
        page["items"].append(_point(29, 250, 150, 20))
        _write(self.path, page=page)
        op = {**JOIN, "attach": [250, 150], "via": [[220, 50], [220, 150]]}
        result, doc = self.apply(op)
        self.assertFalse(result[0]["split"])
        corners = [
            i
            for _, i in doc.all_items()
            if i.get("connectionItemType") == 1
            and i["$type"] == "__drawingConnectionPoint"
            and i["position"].get("x") == 220
        ]
        self.assertEqual(len(corners), 2)

    def test_assign_entry_parts(self):
        logical = _logical()
        logical["virtualModules"][0]["components"][0]["pins"][0]["parameters"] = [
            {"name": "ExternalNetName", "value": "NET_A", "id": 14}
        ]
        _write(self.path, logical)
        result, doc = self.apply(
            {
                "op": "assign_entry_parts",
                "connection": "H_1",
                "entries": {
                    "Entry_1": {"part": "INL_F", "designator": "P1"},
                    "Entry_2": {"part": "INL_M", "designator": "J5"},
                },
            }
        )
        rows = result[0]["entries"]
        self.assertEqual(
            (rows["Entry_1"]["mated"], rows["Entry_1"]["pin_pairs"]), ("J1", 2)
        )
        self.assertNotIn("mated", rows["Entry_2"])  # open end: nothing to mate
        h1 = doc.connection("H_1")
        p1, j5 = h1["components"]
        self.assertEqual((p1["designator"], j5["designator"]), ("P1", "J5"))
        self.assertEqual(me._param(j5, "SourceLibraryName"), "lib.DbLib")
        pin_map = {m["key"]: m["value"] for m in h1["entryPinMap"]["map"]}
        self.assertEqual({pin_map[p["id"]] for p in p1["pins"]}, {101})
        pairs = [
            pc["pinPair"] for pc in h1["entriesConnections"][0]["physicalConnections"]
        ]
        # entry1 is the device entry: its pin comes first
        self.assertEqual(
            [(pp["pin1"], pp["pin2"]) for pp in pairs],
            [(11, p1["pins"][0]["id"]), (12, p1["pins"][1]["id"])],
        )
        self.assertEqual(me._param(p1["pins"][0], "ExternalNetName"), "NET_A")
        self.assertEqual([me._entry_name(e) for e in h1["entries"]], ["P1", "J5"])
        self.assertEqual(self.items(doc)[13]["directValue"], "J5")
        model = mb.parse_mbsdoc(self.path)
        self.assertEqual(
            model["connections"][0]["entries"][0]["component_designators"], ["P1"]
        )

    def test_assign_entry_parts_refusals(self):
        self.apply(JOIN)  # H_1.Entry_2 now carries INL_M (J?)
        for entries, message in (
            ({"Entry_2": {"part": "OTHER"}}, "already carries INL_M"),
            ({"Entry_1": {"part": "INL_F", "designator": "Entry_2"}}, "already has"),
            ({"Entry_1": {"part": "NOPINS"}}, "no pins known"),
            ({"Entry_1": {}}, "part is required"),
        ):
            with self.assertRaisesRegex(me.MbsEditError, message):
                me.apply_operations(
                    me.MbsDocument(self.path),
                    [
                        {
                            "op": "assign_entry_parts",
                            "connection": "H_1",
                            "entries": entries,
                        }
                    ],
                    {**PINS, "NOPINS": []},
                )
        # The part an entry already carries only gets the designator
        result, doc = self.apply(
            {
                "op": "assign_entry_parts",
                "connection": "H_1",
                "entries": {"Entry_2": {"part": "INL_M", "designator": "J3"}},
            }
        )
        self.assertTrue(result[0]["entries"]["Entry_2"]["existing"])
        self.assertEqual(doc.connection("H_1")["components"][0]["designator"], "J3")
        # No pin number in common with the mated device entry
        with self.assertRaisesRegex(me.MbsEditError, "no pin number"):
            me.apply_operations(
                me.MbsDocument(self.path),
                [
                    {
                        "op": "assign_entry_parts",
                        "connection": "H_1",
                        "entries": {"Entry_1": "ODD"},
                    }
                ],
                {"ODD": ["7", "8"]},
            )

    def test_join_refusals(self):
        cases = [
            ({**JOIN, "entry": "H_1.Entry_1"}, "already mates"),
            ({**JOIN, "attach": [260, 60]}, "not horizontal or vertical"),
            ({**JOIN, "attach": [400, 50]}, "on no line"),
            ({**JOIN, "mate_part": "UNKNOWN"}, "no pins known"),
            ({**JOIN, "to": "H_1"}, "two different"),
            ({**JOIN, "entry": "Entry_2"}, "<connection>.<entry>"),
        ]
        for op, message in cases:
            with self.subTest(message=message):
                doc = me.MbsDocument(self.path)
                with self.assertRaisesRegex(me.MbsEditError, message):
                    me.apply_operations(doc, [op], PINS)

    def test_pin_mismatch_and_bad_operation(self):
        doc = me.MbsDocument(self.path)
        with self.assertRaisesRegex(me.MbsEditError, "do not match"):
            me.apply_operations(
                doc, [JOIN], {"INL_M": ["1", "2"], "INL_F": ["1", "2", "3"]}
            )
        with self.assertRaisesRegex(me.MbsEditError, "op must be one of"):
            me.apply_operations(doc, [{"op": "explode"}])
        with self.assertRaisesRegex(
            me.MbsEditError, "operation 1 \\(swap_entry_parts\\)"
        ):
            me.apply_operations(
                doc, [{"op": "swap_entry_parts", "block": "MOD_1", "entries": ["J1"]}]
            )


class EditToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.path = tmp / "T.MbsDoc"
        _write(self.path)
        self.original = self.path.read_bytes()
        self.actions = []
        self.modified = False

        async def bridge(command, params):
            self.assertEqual(command, "multiboard_document")
            self.actions.append(params["action"])
            open_ = params["action"] != "close"
            return {
                "success": True,
                "result": {"success": True, "open": open_, "modified": self.modified},
            }

        self.bridge = patch(
            "main.altium_bridge.execute_command", new=AsyncMock(side_effect=bridge)
        )
        self.bridge.start()
        self.exchange = patch("main.EXCHANGE_DIR", tmp)
        self.exchange.start()
        self.pins = patch("multiboard._harness_connectors", return_value=[])
        self.pins.start()
        self.db = patch("main.find_dblib", return_value=None)
        self.db.start()

    async def asyncTearDown(self):
        for p in (self.db, self.pins, self.exchange, self.bridge):
            p.stop()
        self.tmp.cleanup()

    async def call(self, operations, **kw):
        import main

        return json.loads(
            await main.edit_multiboard_schematic(
                None, document_path=str(self.path), operations=operations, **kw
            )
        )

    async def test_closes_writes_reopens(self):
        result = await self.call(
            [{"op": "set_title", "block": "MOD_1", "title": "New"}]
        )
        self.assertTrue(result["saved"], result)
        self.assertEqual(self.actions, ["status", "close", "open"])
        self.assertTrue(result["reopened"])
        self.assertTrue(Path(result["backup"]).exists())
        self.assertEqual(Path(result["backup"]).read_bytes(), self.original)
        self.assertEqual(me.MbsDocument(self.path).block("MOD_1")["title"], "New")

    async def test_refuses_unsaved_altium_changes(self):
        self.modified = True
        result = await self.call(
            [{"op": "set_title", "block": "MOD_1", "title": "New"}]
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.actions, ["status"])
        self.assertEqual(self.path.read_bytes(), self.original)

    async def test_dry_run_and_failure_write_nothing(self):
        result = await self.call(
            [{"op": "set_title", "block": "MOD_1", "title": "New"}], dry_run=True
        )
        self.assertTrue(result["dry_run"])
        result = await self.call([{"op": "set_title", "block": "NOPE", "title": "New"}])
        self.assertFalse(result["success"])
        self.assertEqual(self.actions, [])
        self.assertEqual(self.path.read_bytes(), self.original)

    async def test_join_through_tool(self):
        result = await self.call([JOIN | {"part": "PART_A", "mate_part": "PART_B"}])
        self.assertTrue(result["saved"], result)
        self.assertEqual(result["results"][0]["pin_pairs"], 2)

    async def test_assign_harness_parts_expands_from_review(self):
        review = {
            "harness_review": [
                {
                    "connection": "H_1",
                    "matched_project": "HAR",
                    "candidates": [],
                    "entries": [
                        {
                            "entry": "H_1.Entry_1",
                            "connector": "P1",
                            "connector_part": "PART_B",
                        },
                        {"entry": "H_1.Entry_2", "candidates": ["P2", "P3"]},
                    ],
                },
                {"connection": "H_2", "matched_project": None, "candidates": []},
            ]
        }
        with patch("multiboard.review_multiboard", return_value=review) as rv:
            result = await self.call(
                [{"op": "assign_harness_parts", "connections": ["H_1"]}],
                dry_run=True,
            )
        self.assertTrue(result["success"], result)
        rv.assert_called_once()
        row = result["results"][0]
        self.assertEqual((row["op"], row["connection"]), ("assign_entry_parts", "H_1"))
        self.assertEqual(row["entries"]["Entry_1"]["designator"], "P1")
        self.assertEqual(row["entries"]["Entry_1"]["pin_pairs"], 2)
        self.assertEqual(
            result["skipped"],
            [
                {
                    "entry": "H_1.Entry_2",
                    "reason": "connector candidates ['P2', 'P3']; pass entry_map",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
