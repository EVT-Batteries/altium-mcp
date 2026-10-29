"""Unit tests for harness_drawing (Draftsman .HarDwf: LZ4Stream codec, review,
offline edits) and the get_harness_drawing_data / edit_harness_drawing tools.
Synthetic documents only; the Altium bridge is mocked, nothing reaches Altium.

Run from server/:  python -m unittest tests.test_harness_drawing -v
"""

import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import harness_drawing as hd

NS = (
    'xmlns="http://schemas.datacontract.org/2004/07/Altium.Designer.PcbDrawing.'
    'DataSerialization.V1" xmlns:i="http://www.w3.org/2001/XMLSchema-instance"'
)


def _text(item_id, text, template=False, x=100.0, y=100.0):
    return (
        f'<Item i:type="Text"><Id>{item_id}</Id><Anchor>Top Left</Anchor>'
        f"<OriginatedFromTemplate>{'true' if template else 'false'}"
        "</OriginatedFromTemplate><Rect><Height>20</Height><Width>200</Width>"
        f"<X>{x}</X><Y>{y}</Y></Rect><Text>{text}</Text></Item>"
    )


def _view(item_id, designator="P1", side=0, x=500.0, y=500.0):
    return (
        f'<Item i:type="HarnessComponentViewData"><Id>{item_id}</Id>'
        "<Anchor>Top Left</Anchor><OriginatedFromTemplate>false"
        "</OriginatedFromTemplate><ElementItems><ElementItemData "
        'i:type="LinkedTextDrawingItemData"><Name>Title</Name><ManualOffset><X>0</X>'
        "<Y>0</Y></ManualOffset><ManualPositioning>false</ManualPositioning>"
        "<Title>Component View"
        "</Title></ElementItemData></ElementItems><BoardSourceName>C:\\h\\H.LdrDoc"
        "</BoardSourceName><CustomScale>1</CustomScale><Location>"
        f"<X>{x}</X><Y>{y}</Y></Location><Scale><Denominator>1</Denominator>"
        "<Numenator>1</Numenator></Scale><Size><Height>0</Height><Width>0</Width>"
        "</Size><Title>Component View</Title><UseCustomScale>false</UseCustomScale>"
        f"<Designator>{designator}</Designator><ViewSide>{side}</ViewSide></Item>"
    )


def _page(page_id, items, template):
    return (
        f"<Page><Id>{page_id}</Id><Items>{''.join(items)}</Items>"
        "<Size><Height>1632</Height><Width>2112</Width></Size>"
        f"<TemplateFileName>{template}</TemplateFileName></Page>"
    )


def _document(template_ok, template_missing, projections=""):
    pages = [
        _page(
            1,
            [_text(10, "frame", template=True), _text(11, "TODO: notes")],
            template_ok,
        ),
        _page(
            2,
            [
                _view(20),
                _text(21, "label", x=520, y=520),
                _text(23, "other", x=600, y=510),
            ],
            template_missing,
        ),
    ]
    return (
        f"<Document {NS}><Id>0</Id><BoardProjectionInformation><BoardOrigin><X>0</X>"
        "<Y>0</Y></BoardOrigin><ComponentProjections>"
        f"{projections}</ComponentProjections><LastUpdate>x</LastUpdate>"
        "</BoardProjectionInformation>"
        f"<Pages>{''.join(pages)}</Pages></Document>"
    )


EMPTY_P1_TOP = (
    "<ComponentProjectionData><Bound "
    'xmlns:a="http://schemas.datacontract.org/2004/07/System.Windows">'
    "<a:_height>0</a:_height><a:_width>0</a:_width><a:_x>0</a:_x><a:_y>0</a:_y>"
    "</Bound><Designator>P1</Designator><DistanceToBoardEdge>0"
    "</DistanceToBoardEdge><DrawingLines/><ViewSide>Top</ViewSide>"
    "</ComponentProjectionData>"
)
SQUARE = [[0, 0, 10, 0], [10, 0, 10, 20], [10, 20, 0, 20], [0, 20, 0, 0]]


class CodecTests(unittest.TestCase):
    def test_round_trip_compresses_text(self):
        data = ("<Item><Id>1</Id><Text>hello</Text></Item>" * 5000).encode()
        packed = hd.encode_stream(data)
        self.assertLess(len(packed), len(data) // 10)
        self.assertEqual(hd.decode_stream(packed), data)

    def test_round_trip_random_and_multi_chunk(self):
        rnd = random.Random(1)
        data = bytes(rnd.getrandbits(8) for _ in range(3000)) + b"ab" * (
            hd.CHUNK_SIZE // 2 + 100
        )
        self.assertEqual(hd.decode_stream(hd.encode_stream(data)), data)

    def test_plain_xml_is_read_as_is(self):
        self.assertEqual(hd.decode_stream(b"<Document/>"), b"<Document/>")

    def test_overlapping_match(self):
        # token: 1 literal, match length 4+3=7 at offset 1 (run of 'a')
        self.assertEqual(
            hd.lz4_block_decompress(bytes([0x13]) + b"a\x01\x00" + b"\x10b", 9),
            b"a" * 8 + b"b",
        )


class DrawingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.template = tmp / "Sheet.DwsDot"
        self.template.write_text("t", encoding="utf-8")
        self.missing = str(tmp / "Old.DwsDot")
        self.path = tmp / "H.HarDwf"
        xml = _document(str(self.template), self.missing, EMPTY_P1_TOP)
        self.path.write_bytes(hd.encode_stream(xml.encode("utf-8")))

    def tearDown(self):
        self.tmp.cleanup()

    def test_review_reports_issues(self):
        r = hd.review_drawing(self.path)
        kinds = sorted(i["kind"] for i in r["issues"])
        self.assertEqual(
            kinds,
            ["empty_component_view", "missing_template", "overlap", "todo_text"],
        )
        self.assertEqual([len(p["items"]) for p in r["pages"]], [1, 3])
        self.assertEqual(r["component_views"][0]["view_side"], "Top")
        self.assertEqual(r["component_views"][0]["projection_lines"], 0)
        self.assertEqual(r["source"], "C:\\h\\H.LdrDoc")

    def test_projection_replaces_and_flips_y(self):
        doc = hd.HarnessDrawing(self.path)
        res = hd.apply_operations(
            doc,
            [
                {
                    "op": "set_component_view",
                    "item_id": 20,
                    "view_side": "Back",
                    "scale": 4,
                },
                {
                    "op": "set_component_projection",
                    "designator": "P1",
                    "view_side": "Back",
                    "lines_mm": SQUARE,
                },
                {
                    "op": "remove_component_projection",
                    "designator": "P1",
                    "view_side": "Top",
                },
            ],
        )
        self.assertEqual(res[0]["scale"], "4:1")
        doc.save()
        r = hd.review_drawing(self.path)
        self.assertEqual(r["component_views"][0]["view_side"], "Back")
        self.assertEqual(r["component_views"][0]["projection_lines"], 4)
        self.assertEqual([p["view_side"] for p in r["projections"]], ["Back"])
        self.assertAlmostEqual(r["projections"][0]["height_mm"], 20.0, places=3)
        self.assertNotIn("empty_component_view", [i["kind"] for i in r["issues"]])
        xml = hd.read_drawing_xml(self.path).decode()
        self.assertIn("<ViewSide>5</ViewSide>", xml)
        self.assertIn("<Numenator>4</Numenator>", xml)
        # stored y is negated: the top edge (20 mm) is at -75.59 units
        self.assertIn(f"<Y>{hd._fmt(-20 * hd.UNITS_PER_MM)}</Y>", xml)

    def test_lines_file_by_side(self):
        f = Path(self.tmp.name) / "lines.json"
        f.write_text(json.dumps({"Front": SQUARE}), encoding="utf-8")
        doc = hd.HarnessDrawing(self.path)
        res = hd.apply_operations(
            doc,
            [
                {
                    "op": "set_component_projection",
                    "designator": "P1",
                    "view_side": "front",
                    "lines_file": str(f),
                }
            ],
        )
        self.assertEqual(res[0]["lines"], 4)
        with self.assertRaises(hd.DrawingEditError):
            hd.apply_operations(
                doc,
                [
                    {
                        "op": "set_component_projection",
                        "designator": "P1",
                        "view_side": "Back",
                        "lines_file": str(f),
                    }
                ],
            )

    def test_copy_move_text_template(self):
        doc = hd.HarnessDrawing(self.path)
        res = hd.apply_operations(
            doc,
            [
                {
                    "op": "copy_item",
                    "item_id": 20,
                    "x_mm": 50,
                    "y_mm": 60,
                    "view_side": "Front",
                },
                {"op": "move_item", "item_id": 21, "x_mm": 10, "y_mm": 20},
                {"op": "set_text", "item_id": 11, "text": "Notes & <done>"},
                {"op": "set_template", "from": self.missing, "to": str(self.template)},
            ],
        )
        self.assertEqual(res[0]["new_item_id"], "24")
        self.assertEqual(res[3]["pages"], [2])
        doc.save()
        r = hd.review_drawing(self.path)
        views = r["component_views"]
        self.assertEqual(
            [(v["id"], v["view_side"]) for v in views], [("20", "Top"), ("24", "Front")]
        )
        self.assertAlmostEqual(views[1]["x_mm"], 50.0, places=3)
        self.assertAlmostEqual(views[1]["y_mm"], 60.0, places=3)
        label = next(i for i in r["pages"][1]["items"] if i["id"] == "21")
        self.assertAlmostEqual(label["y_mm"], 20.0, places=3)
        self.assertEqual(r["pages"][0]["items"][0]["text"], "Notes & <done>")
        self.assertTrue(all(p["template_exists"] for p in r["pages"]))

    def test_view_title(self):
        doc = hd.HarnessDrawing(self.path)
        hd.apply_operations(
            doc,
            [
                {
                    "op": "set_view_title",
                    "item_id": 20,
                    "title": "P1 Rear",
                    "title_position": [-5, 10],
                },
            ],
        )
        xml = doc.xml
        self.assertEqual(xml.count("<Title>P1 Rear</Title>"), 2)  # item + linked text
        self.assertIn("<ManualPositioning>true</ManualPositioning>", xml)
        self.assertIn(f"<X>{hd._fmt(-5 * hd.UNITS_PER_MM)}</X>", xml)
        hd.apply_operations(
            doc, [{"op": "set_view_title", "item_id": 20, "title_position": "auto"}]
        )
        self.assertIn("<ManualPositioning>false</ManualPositioning>", doc.xml)
        with self.assertRaises(hd.DrawingEditError):
            hd.apply_operations(
                doc, [{"op": "set_view_title", "item_id": 21, "title": "x"}]
            )

    def test_callout_snaps_to_projection_point(self):
        doc = hd.HarnessDrawing(self.path)
        hd.apply_operations(
            doc,
            [
                {
                    "op": "set_component_view",
                    "item_id": 20,
                    "view_side": "Back",
                    "scale": 2,
                },
                {
                    "op": "set_component_projection",
                    "designator": "P1",
                    "view_side": "Back",
                    "lines_mm": SQUARE,
                },
            ],
        )
        res = hd.apply_operations(
            doc,
            [
                {
                    "op": "add_callout",
                    "item_id": 20,
                    "text": "Pin 1",
                    "point_mm": [10, 20],
                    "leader_mm": [-5, 3],
                }
            ],
        )[0]
        # view at (500, 500) units; point (10, 20) mm of a 10 x 20 mm projection
        # is its top-right corner: 20 mm right, 40 mm up at 2:1
        self.assertAlmostEqual(res["x_mm"], hd._mm(500) + 20, places=2)
        self.assertAlmostEqual(res["y_mm"], hd._mm(500) + 40, places=2)
        callout = doc.item(res["new_item_id"])
        self.assertEqual(callout.type, "Callout")
        self.assertEqual(callout.page, 2)
        s = callout.summary()
        self.assertEqual(
            (s["text"], s["target"], s["target_item"]), ("Pin 1", "P1", "20")
        )
        self.assertIn("<ElbowPosition>Left</ElbowPosition>", doc.xml)
        with self.assertRaises(hd.DrawingEditError):  # no projection for P1 Top
            hd.apply_operations(
                doc,
                [
                    {"op": "copy_item", "item_id": 20, "view_side": "Top"},
                    {
                        "op": "add_callout",
                        "item_id": 24,
                        "text": "x",
                        "point_mm": [0, 0],
                    },
                ],
            )

    def test_copy_table_format(self):
        def table(item_id, component, part_visible):
            return (
                f'<Item i:type="ConnectionTableData"><Id>{item_id}</Id>'
                "<OriginatedFromTemplate>false</OriginatedFromTemplate><Columns><Items>"
                "<DataTableColumnData><Name>Pin</Name><Visible>true</Visible>"
                "</DataTableColumnData><DataTableColumnData><Name>Part</Name>"
                f"<Visible>{part_visible}</Visible></DataTableColumnData></Items>"
                f"</Columns><Component>{component}</Component><Location><X>0</X>"
                "<Y>0</Y></Location></Item>"
            )

        xml = hd.read_drawing_xml(self.path).decode()
        doc = hd.HarnessDrawing(self.path)
        end = xml.index("</Items>", xml.index("<Id>2</Id>"))
        doc.xml = (
            xml[:end] + table(30, "P1", "false") + table(31, "P5", "true") + xml[end:]
        )
        res = hd.apply_operations(
            doc, [{"op": "copy_table_format", "from_item_id": 30}]
        )[0]
        self.assertEqual(res["changed"], ["31"])
        t = doc.item(31)
        self.assertIn("<Component>P5</Component>", doc.xml[t.start : t.end])
        self.assertNotIn(
            "<Visible>true</Visible></DataTableColumnData></Items>",
            doc.xml[t.start : t.end],
        )
        with self.assertRaises(hd.DrawingEditError):  # not a table
            hd.apply_operations(doc, [{"op": "copy_table_format", "from_item_id": 20}])

    def test_refusals(self):
        doc = hd.HarnessDrawing(self.path)
        for op in (
            {"op": "delete_item", "item_id": 10},  # template item
            {
                "op": "set_component_view",
                "item_id": 21,
                "view_side": "Top",
            },  # not a view
            {"op": "set_component_view", "item_id": 20, "view_side": "Diagonal"},
            {"op": "set_template", "pages": [1], "to": self.missing},  # no such file
            {"op": "move_item", "item_id": 999, "x_mm": 0, "y_mm": 0},
            {"op": "nope"},
        ):
            with self.assertRaises(hd.DrawingEditError, msg=op):
                hd.apply_operations(doc, [op])


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        template = tmp / "Sheet.DwsDot"
        template.write_text("t", encoding="utf-8")
        self.path = tmp / "H.HarDwf"
        xml = _document(str(template), str(tmp / "Old.DwsDot"), EMPTY_P1_TOP)
        self.path.write_bytes(hd.encode_stream(xml.encode("utf-8")))
        self.original = self.path.read_bytes()
        self.actions = []
        self.modified = False

        async def bridge(command, params):
            self.assertEqual(command, "multiboard_document")
            self.actions.append(params["action"])
            return {
                "success": True,
                "result": {
                    "success": True,
                    "open": params["action"] != "close",
                    "modified": self.modified,
                },
            }

        self.bridge = patch(
            "main.altium_bridge.execute_command", new=AsyncMock(side_effect=bridge)
        )
        self.bridge.start()
        self.exchange = patch("main.EXCHANGE_DIR", tmp)
        self.exchange.start()

    async def asyncTearDown(self):
        self.exchange.stop()
        self.bridge.stop()
        self.tmp.cleanup()

    async def call(self, operations, **kw):
        import main

        return json.loads(
            await main.edit_harness_drawing(
                None, document_path=str(self.path), operations=operations, **kw
            )
        )

    async def test_closes_writes_reopens(self):
        result = await self.call(
            [{"op": "set_component_view", "item_id": 20, "view_side": "Back"}]
        )
        self.assertTrue(result["saved"], result)
        self.assertEqual(self.actions, ["status", "close", "open"])
        self.assertTrue(result["reopened"])
        self.assertEqual(Path(result["backup"]).read_bytes(), self.original)
        self.assertIn("<ViewSide>5</ViewSide>", hd.read_drawing_xml(self.path).decode())

    async def test_refuses_unsaved_altium_changes(self):
        self.modified = True
        result = await self.call(
            [{"op": "set_component_view", "item_id": 20, "view_side": "Back"}]
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.actions, ["status"])
        self.assertEqual(self.path.read_bytes(), self.original)

    async def test_dry_run_and_failure_write_nothing(self):
        result = await self.call(
            [{"op": "set_component_view", "item_id": 20, "view_side": "Back"}],
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        result = await self.call(
            [{"op": "move_item", "item_id": 999, "x_mm": 0, "y_mm": 0}]
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.actions, [])
        self.assertEqual(self.path.read_bytes(), self.original)

    async def test_review_tool(self):
        import main

        r = json.loads(await main.get_harness_drawing_data(None, str(self.path)))
        self.assertTrue(r["success"])
        self.assertEqual(len(r["pages"]), 2)
        bad = json.loads(
            await main.get_harness_drawing_data(None, str(self.path.with_suffix(".x")))
        )
        self.assertFalse(bad["success"])


if __name__ == "__main__":
    unittest.main()
