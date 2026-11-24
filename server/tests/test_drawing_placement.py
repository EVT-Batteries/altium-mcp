"""Unit tests for the drawing placement geometry (harness_drawing: item
rectangles, layout view content, covers_layout, move_item clear) and the
offline drawing render helpers (drawing_render). Synthetic data only.

Run from server/:  python -m unittest tests.test_drawing_placement -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import drawing_render as dr
import harness_drawing as hd

NS = (
    'xmlns="http://schemas.datacontract.org/2004/07/Altium.Designer.PcbDrawing.'
    'DataSerialization.V1" xmlns:i="http://www.w3.org/2001/XMLSchema-instance"'
)
U = hd._units  # mm -> page units


def _table(item_id, x_mm, y_mm, w_mm=40.0, h_mm=20.0):
    return (
        f'<Item i:type="ConnectionTableData"><Id>{item_id}</Id><Anchor>Top Left</Anchor>'
        "<OriginatedFromTemplate>false</OriginatedFromTemplate>"
        f"<Location><X>{U(x_mm)}</X><Y>{U(y_mm)}</Y></Location>"
        f"<Size><Height>{U(h_mm)}</Height><Width>{U(w_mm)}</Width></Size>"
        "<Component>P1</Component></Item>"
    )


def _view(item_id, x_mm, y_mm, scale=2, show_title=True):
    return (
        f'<Item i:type="HarnessComponentViewData"><Id>{item_id}</Id>'
        "<Anchor>Top Left</Anchor><OriginatedFromTemplate>false"
        "</OriginatedFromTemplate><CustomScale>1</CustomScale>"
        f"<Location><X>{U(x_mm)}</X><Y>{U(y_mm)}</Y></Location>"
        f"<Scale><Denominator>1</Denominator><Numenator>{scale}</Numenator></Scale>"
        f"<ShowTitle>{'true' if show_title else 'false'}</ShowTitle>"
        "<Size><Height>0</Height><Width>0</Width></Size>"
        "<Title>P1 Rear View (Scale =ViewScale)</Title><UseCustomScale>false"
        "</UseCustomScale><Designator>P1</Designator><ViewSide>5</ViewSide></Item>"
    )


def _callout(item_id, view_id, sx, sy, leader=(-10.0, 5.0)):
    return (
        f'<Item i:type="Callout"><Id>{item_id}</Id><Anchor>Top Left</Anchor>'
        "<OriginatedFromTemplate>false</OriginatedFromTemplate>"
        f"<Leader><X>{U(leader[0])}</X><Y>{U(leader[1])}</Y></Leader>"
        "<Sources><CalloutSource><CustomText>Pin 1</CustomText></CalloutSource></Sources>"
        "<TargetAnchor><PointOnPage><X>0</X><Y>0</Y></PointOnPage><Snap><Points>"
        f"<PointData><X>{U(sx)}</X><Y>{U(sy)}</Y></PointData></Points></Snap>"
        f"<SnapTargetItemId>{view_id}</SnapTargetItemId></TargetAnchor></Item>"
    )


def _layout_view(item_id, x_mm, y_mm, source, scale=0.9):
    return (
        f'<Item i:type="LayoutDrawingViewData"><Id>{item_id}</Id><Anchor>Top Left</Anchor>'
        "<OriginatedFromTemplate>false</OriginatedFromTemplate>"
        f"<BoardSourceName>{source}</BoardSourceName><CustomScale>{scale}</CustomScale>"
        f"<Location><X>{U(x_mm)}</X><Y>{U(y_mm)}</Y></Location>"
        "<Scale><Denominator>1</Denominator><Numenator>1</Numenator></Scale>"
        "<ShowTitle>false</ShowTitle><Size><Height>100</Height><Width>100</Width></Size>"
        "<UseCustomScale>true</UseCustomScale></Item>"
    )


# P1 Back (ViewSide 5): bound x -5..5 mm, y -8..0 mm (stored y negated)
PROJ = (
    "<ComponentProjectionData><Bound "
    'xmlns:a="http://schemas.datacontract.org/2004/07/System.Windows">'
    f"<a:_height>{U(8)}</a:_height><a:_width>{U(10)}</a:_width>"
    f"<a:_x>{U(-5)}</a:_x><a:_y>{U(-8)}</a:_y>"
    "</Bound><Designator>P1</Designator><DistanceToBoardEdge>0"
    "</DistanceToBoardEdge><DrawingLines><DrawingLineData/></DrawingLines>"
    "<ViewSide>Back</ViewSide></ComponentProjectionData>"
)


def _document(items, width_mm=400.0, height_mm=300.0):
    return (
        f"<Document {NS}><Id>0</Id><ArrowStyles/><FontStyles/>"
        "<BoardProjectionInformation><BoardOrigin><X>0</X><Y>0</Y></BoardOrigin>"
        f"<ComponentProjections>{PROJ}</ComponentProjections><LastUpdate>x</LastUpdate>"
        "</BoardProjectionInformation><Pages>"
        f"<Page><Id>1</Id><Items>{''.join(items)}</Items>"
        f"<Size><Height>{U(height_mm)}</Height><Width>{U(width_mm)}</Width></Size>"
        "<TemplateFileName></TemplateFileName></Page></Pages></Document>"
    )


def _mm_rect(r):
    return [round(hd._mm(v), 3) for v in r]


# A layout: a 2000 x 1000 mil note at (1000, 1000) and a bundle along y 5000 from x 2000
LAYOUT = {
    "notes": [{"x1": 100, "y1": 100, "x2": 300, "y2": 200, "text": "N"}],
    "bundles": [{"designator": "B1", "points": [200, 500, 600, 500], "texts": {}}],
    "connectors": [],
    "connection_points": [],
    "pictures": [],
    "frames": [],
    "lines": [],
    "coverings": [],
    "layout_labels": [],
}


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ldr = self.dir / "H.LdrDoc"
        self.ldr.write_bytes(b"x")
        hd._LAYOUT_CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()
        hd._LAYOUT_CACHE.clear()

    def _doc(self, items):
        path = self.dir / "H.HarDwf"
        path.write_bytes(hd.encode_stream(_document(items).encode("utf-8")))
        return hd.HarnessDrawing(path)

    def test_view_rect_is_bound_times_scale_with_title(self):
        doc = self._doc([_view(1, 100, 50, scale=2)])
        r = hd.item_rects(doc)["1"]["rects"]
        self.assertEqual(_mm_rect(r[0]), [100, 50, 120, 66])
        # title strip above, as long as the resolved title ("... (Scale 2:1)")
        title = "P1 Rear View (Scale 2:1)"
        self.assertAlmostEqual(hd._mm(r[1][1]), 66.5, places=3)
        self.assertAlmostEqual(
            hd._mm(r[1][2] - r[1][0]), hd.PAGE_CHAR_MM * len(title), places=3
        )

    def test_callout_follows_its_view(self):
        # snap point (0, -8 stored) = projection x 0, y 8 mm: page view.x + 10, view.y + 16
        doc = self._doc([_view(1, 100, 50, scale=2), _callout(2, 1, 0.0, -8.0)])
        rects = hd.item_rects(doc)
        self.assertEqual(rects["2"]["target"], "1")
        _, y1, x2, y2 = _mm_rect(rects["2"]["rects"][0])
        # leader (-10, +5): text ends at x 100 and is centred on y 71
        self.assertAlmostEqual(x2, 100.0, places=2)
        self.assertAlmostEqual((y1 + y2) / 2, 71.0, places=2)

    def test_layout_content_mapped_at_view_scale(self):
        doc = self._doc([_layout_view(1, 20, 30, str(self.ldr), scale=0.9)])
        view = doc.item(1)
        content, extent = hd.layout_view_content(view, LAYOUT)
        note = next(r for label, r in content if label.startswith("note"))
        # the note sets the content's minimum: its corner lands on the view location
        self.assertEqual(_mm_rect(note)[:2], [20.0, 30.0])
        k = 0.0254 * 0.9
        self.assertAlmostEqual(hd._mm(note[2]) - 20.0, 2000 * k, places=3)
        bundle = next(r for label, r in content if label == "bundle B1")
        self.assertAlmostEqual(
            hd._mm(bundle[1]), 30.0 + (5000 - 1000 - hd.BUNDLE_MILS) * k, places=3
        )
        self.assertAlmostEqual(hd._mm(extent[0]), 20.0, places=3)

    def test_review_reports_items_over_the_layout(self):
        # page bundle B1 at y 30 + 4000 * 0.02286 = 121.4 mm, x 20 .. 134.3 mm
        items = [
            _layout_view(1, 20, 30, str(self.ldr)),
            _table(2, 60, 110),  # over the bundle
            _table(3, 200, 200),  # clear
        ]
        doc = self._doc(items)
        with patch("harness_layout.parse_ldrdoc", return_value=LAYOUT):
            r = hd.review_drawing(doc.path)
        covers = [i for i in r["issues"] if i["kind"] == "covers_layout"]
        self.assertEqual([i["item"] for i in covers], ["2"])
        self.assertIn("bundle B1", covers[0]["covers"])
        self.assertEqual(r["pages"][0]["layout_views"][0]["item"], "1")

    def test_move_item_clear_finds_the_nearest_free_place(self):
        items = [
            _layout_view(1, 20, 30, str(self.ldr)),
            _table(2, 60, 110),
            _table(3, 60, 150),
        ]
        doc = self._doc(items)
        with patch("harness_layout.parse_ldrdoc", return_value=LAYOUT):
            res = hd.apply_operations(
                doc,
                [
                    {
                        "op": "move_item",
                        "item_id": 2,
                        "x_mm": 60,
                        "y_mm": 110,
                        "clear": True,
                    }
                ],
            )
        moved = res[0]
        self.assertEqual(moved["requested_mm"], [60, 110])
        self.assertNotEqual((moved["x_mm"], moved["y_mm"]), (60, 110))
        # clear of the bundle (by the 3 mm margin) and of table 3
        box = doc.item(2).box()
        rect = (box[0], box[1], box[0] + box[2], box[1] + box[3])
        with patch("harness_layout.parse_ldrdoc", return_value=LAYOUT):
            content = hd.page_layout_content(doc, 1)[0][2]
        self.assertFalse(any(hd._overlaps(rect, c) for _, c in content))
        other = doc.item(3).box()
        self.assertFalse(
            hd._overlaps(
                rect, (other[0], other[1], other[0] + other[2], other[1] + other[3])
            )
        )

    def test_move_item_clear_fails_without_room(self):
        doc = self._doc(
            [_layout_view(1, 20, 30, str(self.ldr)), _table(2, 60, 110, w_mm=380)]
        )
        with (
            patch("harness_layout.parse_ldrdoc", return_value=LAYOUT),
            self.assertRaises(hd.DrawingEditError),
        ):
            hd.apply_operations(
                doc,
                [
                    {
                        "op": "move_item",
                        "item_id": 2,
                        "x_mm": 60,
                        "y_mm": 110,
                        "clear": True,
                        "search_mm": 20,
                    }
                ],
            )


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.drawing = self.dir / "H.HarDwf"
        self.drawing.write_bytes(b"x")
        (self.dir / "H.PrjHar").write_text(
            "[Document1]\nDocumentPath=H.HarDwf\n[Document2]\nDocumentPath=H.OutJob\n",
            encoding="utf-8",
        )
        (self.dir / "H.OutJob").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _outjob(self, doc_path=None):
        return {
            "containers": [
                {"name": "Print", "type": "Printer"},
                {
                    "name": "Docs",
                    "type": "Publish",
                    "output_path": str(self.dir / "Out" / "H.PDF"),
                },
            ],
            "outputs": [
                {
                    "type": "PCBDrawing",
                    "document_path": doc_path,
                    "connected_containers": ["Docs"],
                },
                {
                    "type": "BOM_PartType",
                    "document_path": None,
                    "connected_containers": ["Print"],
                },
            ],
        }

    def test_finds_the_container_of_the_drawing_output(self):
        with patch("drawing_render.parse_outjob", return_value=self._outjob()):
            t = dr.find_publish_container(self.drawing)
        self.assertEqual(t["container"], "Docs")
        self.assertEqual(Path(t["output_dir"]), self.dir / "Out")
        self.assertEqual(Path(t["outjob"]), self.dir / "H.OutJob")

    def test_no_container_for_another_drawing(self):
        with (
            patch(
                "drawing_render.parse_outjob", return_value=self._outjob("Other.HarDwf")
            ),
            self.assertRaises(dr.RenderError),
        ):
            dr.find_publish_container(self.drawing)

    def test_renders_and_crops_an_area(self):
        import pypdfium2 as pdfium

        pdf_path = self.dir / "t.pdf"
        doc = pdfium.PdfDocument.new()
        doc.new_page(400 / 25.4 * 72, 300 / 25.4 * 72)
        doc.new_page(200 / 25.4 * 72, 100 / 25.4 * 72)
        doc.save(str(pdf_path))
        doc.close()
        self.assertEqual(dr.newest_pdf(self.dir, 0), pdf_path)
        info, images = dr.render_pages(
            pdf_path, area={"page": 1, "area": [10, 20, 110, 70]}
        )
        self.assertEqual(len(images), 1)
        self.assertEqual(info[0]["page_mm"], [400.0, 300.0])
        w, h = info[0]["px"]
        self.assertAlmostEqual(w / h, 2.0, delta=0.02)  # 100 x 50 mm
        self.assertLessEqual(abs(w - dr.MAX_WIDTH_PX), 2)
        info, images = dr.render_pages(pdf_path, pages=[2], max_width_px=400)
        self.assertEqual(info[0]["px"][0], 400)
        with self.assertRaises(dr.RenderError):
            dr.render_pages(pdf_path, pages=[3])


class RenderToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_then_returns_page_images(self):
        import main
        import pypdfium2 as pdfium

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            drawing = tmp / "H.HarDwf"
            drawing.write_bytes(
                hd.encode_stream(_document([_table(2, 60, 110)]).encode("utf-8"))
            )
            pdf_path = tmp / "H.pdf"
            doc = pdfium.PdfDocument.new()
            doc.new_page(400 / 25.4 * 72, 300 / 25.4 * 72)
            doc.save(str(pdf_path))
            doc.close()
            target = {
                "outjob": str(tmp / "H.OutJob"),
                "container": "Docs",
                "output_dir": str(tmp),
            }
            bridge = patch.object(
                main.altium_bridge,
                "execute_command",
                new=AsyncMock(return_value={"success": True, "result": {}}),
            )
            with (
                bridge as run,
                patch("drawing_render.find_publish_container", return_value=target),
                patch("drawing_render.wait_for_pdf", return_value=pdf_path),
            ):
                out = await main.render_harness_drawing(None, str(drawing), pages=[1])
            run.assert_awaited_once()
            self.assertEqual(run.await_args.args[0], "run_output_jobs")
            self.assertEqual(run.await_args.args[1]["container_names"], ["Docs"])
            info = json.loads(out[0])
            self.assertTrue(info["success"])
            self.assertEqual(info["pages"][0]["page"], 1)
            self.assertEqual(len(out), 2)
            self.assertNotIn("warning", info)

    async def test_no_container_is_reported(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            drawing = Path(tmp) / "H.HarDwf"
            drawing.write_bytes(b"x")
            with patch(
                "drawing_render.find_publish_container",
                side_effect=dr.RenderError("no OutJob"),
            ):
                out = await main.render_harness_drawing(None, str(drawing))
            self.assertFalse(json.loads(out)["success"])


if __name__ == "__main__":
    unittest.main()
