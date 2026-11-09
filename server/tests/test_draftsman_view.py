"""Unit tests for draftsman_view (Draftsman screenshot navigation geometry).

Run from server/:  python -m unittest tests.test_draftsman_view -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import draftsman_view as dv

VIEW = (400, 100, 1900, 1000)  # centre (1150, 550)


class TestDraftsmanView(unittest.TestCase):
    def test_parse_state(self):
        s = dv.parse_state("pages=5;page=571,98,1712,980;area=600,200,700,260")
        self.assertEqual(s["pages"], 5)
        self.assertEqual(s["page"], (571, 98, 1712, 980))
        self.assertEqual(s["area"], (600, 200, 700, 260))
        self.assertEqual(
            dv.parse_state("error=page out of range;pages=2"),
            {"error": "page out of range", "pages": 2},
        )
        self.assertEqual(dv.parse_state(""), {})

    def test_rect_spec_in_thousandths_of_mm(self):
        self.assertEqual(dv.rect_spec((559.0, 432.0)), "0,0,559000,432000")
        self.assertEqual(
            dv.rect_spec((559.0, 432.0), [160, 300, 140.5, 270]),
            "0,0,559000,432000,140500,270000,160000,300000",
        )

    def test_scroll_notches_bring_target_to_centre(self):
        # A page centred 1600 px below the view centre: wheel down 32 notches
        page = (571, 1700, 1712, 2600)
        self.assertEqual(dv.scroll_notches(page, VIEW, 50), -32)
        self.assertEqual(dv.scroll_notches((0, 500, 10, 600), VIEW, 50), 0)
        self.assertEqual(dv.scroll_notches((0, -900, 10, -800), VIEW, 50), 28)
        # Horizontally: an area left of the centre needs the content moved right
        self.assertEqual(dv.scroll_notches((837, 0, 959, 10), VIEW, 50, axis="x"), 5)

    def test_zoom_steps_keep_the_area_in_view(self):
        # 100 x 60 px area at the view centre: x8 = 800 x 480 fits, x16 does not
        area = (1100, 520, 1200, 580)
        self.assertEqual(dv.zoom_steps(area, VIEW), 3)
        # Off-centre areas leave the view sooner (zoom is around the centre)
        self.assertEqual(dv.zoom_steps((1700, 520, 1800, 580), VIEW), 0)
        self.assertEqual(dv.zoom_steps((1300, 520, 1400, 580), VIEW), 1)
        tiny = (1149, 549, 1151, 551)
        self.assertEqual(dv.zoom_steps(tiny, VIEW), dv.MAX_ZOOM_STEPS)

    def test_zoomed_is_around_the_view_centre(self):
        self.assertEqual(
            dv.zoomed((1150, 550, 1250, 600), VIEW, 1), (1150, 550, 1350, 650)
        )

    def test_crop_rect(self):
        self.assertEqual(dv.crop_rect((500, 200, 900, 400), VIEW), (480, 190, 920, 410))
        # Clipped to the view
        self.assertEqual(dv.crop_rect((300, 50, 900, 400), VIEW), (400, 100, 930, 418))
        self.assertIsNone(dv.crop_rect((2000, 50, 2100, 90), VIEW))

    def test_inside(self):
        self.assertTrue(dv.inside((404, 98, 1890, 1000), VIEW))
        self.assertFalse(dv.inside((404, -300, 1890, 1000), VIEW))


if __name__ == "__main__":
    unittest.main()
