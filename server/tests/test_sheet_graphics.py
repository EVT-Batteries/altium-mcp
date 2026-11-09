"""Unit tests for the sheet graphics readers: notes, frames, pictures and texts
of a wiring diagram or layout drawing, connector bounding boxes and the
objects_in_region listing (synthetic records only).

Run from server/:  python -m unittest tests.test_sheet_graphics -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from harness_parser import (
    object_boxes,
    objects_in_region,
    parse_wirdoc_records,
    sheet_graphics,
)


def _records() -> list[dict[str, str]]:
    """Header, then: a connector P1 at (100, 400) with a body rectangle and two
    pins facing right, a sheet frame around it, a two-line note, a picture,
    a text and a template rectangle (owned, not the sheet's)."""
    return [
        {"HEADER": "x"},
        {
            "RECORD": "104",
            "FontIdCount": "12",
            "FontName3": "Arial",
            "Size3": "10",
            "FontName12": "Times New Roman",
            "Size12": "14",
            "Bold12": "T",
        },  # sheet
        {
            "RECORD": "106",
            "Location.X": "100",
            "Location.Y": "400",
            "UniqueID": "P1UID",
        },  # 0
        {"RECORD": "34", "OwnerIndex": "1", "Name": "Designator", "Text": "P1"},  # 1
        {
            "RECORD": "14",
            "OwnerIndex": "1",
            "OwnerPartId": "1",
            "Location.X": "90",
            "Location.Y": "330",
            "Corner.X": "100",
            "Corner.Y": "410",
        },  # 2: body
        {
            "RECORD": "113",
            "OwnerIndex": "1",
            "Designator": "1",
            "Location.X": "100",
            "Location.Y": "400",
            "PinLength": "20",
            "UniqueID": "PIN1",
        },  # 3
        {
            "RECORD": "113",
            "OwnerIndex": "1",
            "Designator": "2",
            "Location.X": "100",
            "Location.Y": "340",
            "PinLength": "20",
            "UniqueID": "PIN2",
        },  # 4
        {
            "RECORD": "14",
            "Location.X": "300",
            "Location.Y": "450",
            "Corner.X": "60",
            "Corner.Y": "300",
            "LineStyleExt": "1",
            "LineWidth": "2",
            "Color": "128",
            "AreaColor": "255",
            "UniqueID": "FRAME1",
        },  # 5: frame (corners reversed)
        {
            "RECORD": "209",
            "Location.X": "400",
            "Location.Y": "500",
            "Corner.X": "600",
            "Corner.Y": "540",
            "Text": "First line~1Second line",
            "FontID": "3",
            "AreaColor": "9895935",
            "IsSolid": "T",
            "ShowBorder": "T",
            "Alignment": "1",
            "WordWrap": "T",
            "UniqueID": "NOTE1",
        },  # 6
        {
            "RECORD": "30",
            "Location.X": "700",
            "Location.Y": "100",
            "Corner.X": "800",
            "Corner.Y": "180",
            "Corner.Y_Frac": "50000",
            "FileName": "pic.png",
            "EmbedImage": "T",
            "KeepAspect": "T",
            "UniqueID": "PIC1",
        },  # 7
        {
            "RECORD": "4",
            "Location.X": "80",
            "Location.Y": "460",
            "Text": "PLUG",
            "FontID": "12",
            "Justification": "2",
            "UniqueID": "TXT1",
        },  # 8
        {"RECORD": "39"},  # 9: sheet template
        {
            "RECORD": "14",
            "OwnerIndex": "10",
            "Location.X": "0",
            "Location.Y": "0",
            "Corner.X": "50",
            "Corner.Y": "50",
            "UniqueID": "TPL",
        },  # 10
    ]


class TestSheetGraphics(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = parse_wirdoc_records(_records(), "sheet.WirDoc")

    def test_notes_frames_pictures_texts(self):
        p = self.parsed
        self.assertEqual(p["summary"]["note_count"], 1)
        note = p["notes"][0]
        self.assertEqual(note["text"], "First line\nSecond line")
        self.assertEqual(
            (note["x1"], note["y1"], note["x2"], note["y2"]), (400, 500, 600, 540)
        )
        self.assertEqual((note["font"]["name"], note["font"]["size"]), ("Arial", 10))
        self.assertEqual((note["alignment"], note["unique_id"]), ("left", "NOTE1"))
        self.assertTrue(note["solid"] and note["show_border"] and note["word_wrap"])
        # Frame corners are normalised; the template's rectangle is not a frame
        self.assertEqual([f["unique_id"] for f in p["frames"]], ["FRAME1"])
        frame = p["frames"][0]
        self.assertEqual(
            (frame["x1"], frame["y1"], frame["x2"], frame["y2"]), (60, 300, 300, 450)
        )
        self.assertEqual(frame["line_style"], "dashed")
        self.assertEqual(p["pictures"][0]["y2"], 180.5)
        self.assertEqual(p["pictures"][0]["file_name"], "pic.png")
        text = p["texts"][0]
        self.assertEqual(
            text["font"],
            {"name": "Times New Roman", "size": 14, "bold": True, "italic": False},
        )
        self.assertEqual(
            (text["justification"], text["unique_id"]), ("bottom_right", "TXT1")
        )

    def test_connector_bbox_covers_body_and_pins(self):
        self.assertEqual(self.parsed["connectors"][0]["bbox"], [90, 330, 120, 410])

    def test_objects_in_region(self):
        res = objects_in_region(self.parsed, [500, 2900, 3100, 4800])
        inside = {(o["type"], o["unique_id"]) for o in res["inside"]}
        self.assertIn(("connector", "P1UID"), inside)
        self.assertIn(("frame", "FRAME1"), inside)
        # PLUG is right-justified at x 800 mils: it runs from 560 to 800
        self.assertIn(("text", "TXT1"), inside)
        self.assertNotIn(("note", "NOTE1"), inside)
        part = {
            o["unique_id"]
            for o in objects_in_region(self.parsed, [0, 0, 5000, 5200])["partly_inside"]
        }
        self.assertEqual(part, {"NOTE1"})
        # Boxes are in mils
        box = next(o for o in object_boxes(self.parsed) if o["unique_id"] == "PIC1")[
            "bbox"
        ]
        self.assertEqual(box, [7000, 1000, 8000, 1805])

    def test_layout_and_wiring_share_the_reader(self):
        graphics = sheet_graphics(_records()[1:])
        self.assertEqual(
            {k: len(v) for k, v in graphics.items()},
            {"notes": 1, "frames": 1, "pictures": 1, "texts": 1},
        )


if __name__ == "__main__":
    unittest.main()
