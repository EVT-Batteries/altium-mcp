"""Unit tests for the import_harness_changes tool (Altium's "Import Changes
From ..." on layout and Draftsman drawings). Synthetic parser / review output,
a mocked bridge (open step, close) and mocked standalone scripts (import,
save); nothing reaches Altium.

Run from server/:  python -m unittest tests.test_harness_import -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import main


def _layout(part="", unassigned=(), unrouted=()):
    return {
        "wires": [
            {"designator": "W1", "part_number": part, "description": part or "*"},
            {"designator": "W2", "part_number": "5876 BR005", "description": "Brown"},
        ],
        "connectors": [{"designator": "J1", "unassigned_pins": list(unassigned)}],
        "unrouted_wires": list(unrouted),
    }


def _review(lines=901, width=176.26):
    return {
        "pages": [
            {
                "items": [
                    {"id": "2493", "x_mm": 365.0, "y_mm": 245.4, "width_mm": width},
                    {"id": "3583", "x_mm": 140.0, "y_mm": 300.0, "width_mm": 0.0},
                ]
            }
        ],
        "projections": [{"designator": "J1", "view_side": "Front", "lines": lines}],
        "issues": [],
    }


def _opened(was_open=True, **extra):
    """Bridge reply of the check before the import."""
    result = {
        "success": True,
        "was_open": was_open,
        "process": "Sch:Import",
    }
    return {"success": True, "result": {**result, **extra}}


CLOSED = {"success": True, "result": {"success": True, "open": False}}


def _script(text):
    """run_altium_script reply whose ResultText is text."""
    return json.dumps({"success": True, "result": text, "steps": []})


IMPORTED = _script("imported")
SAVED = _script("saved 1")


class TestImportHarnessChanges(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.layout = tmp / "Test.LdrDoc"
        self.drawing = tmp / "Test.HarDwf"
        for doc in (self.layout, self.drawing, tmp / "Test.WirDoc"):
            doc.write_bytes(b"")
        self.project = tmp / "Test.PrjHar"
        self.project.write_text(
            "[Document1]\nDocumentPath=Test.WirDoc\n\n"
            "[Document2]\nDocumentPath=Test.HarDwf\n\n"
            "[Document3]\nDocumentPath=Test.LdrDoc\n",
            encoding="utf-8",
        )
        self.patches = [
            patch("main.BRIDGE_LOCK_FILE", tmp / "bridge.lock"),
            patch("main.HARNESS_IMPORT_WAITS", (0, 0)),
        ]
        bridge = patch(
            "main.altium_bridge._execute_command_locked", new_callable=AsyncMock
        )
        # Open scripts get open_reply; import and save scripts go to mock_script
        self.mock_script = AsyncMock()
        self.open_scripts = []
        self.open_reply = _script("opened")

        async def fake_script(ctx, script, timeout_seconds=120):
            if "ResultText := 'opened'" in script:
                self.open_scripts.append(script)
                return self.open_reply
            return await self.mock_script(ctx, script, timeout_seconds)

        script = patch("main.run_altium_script", fake_script)
        gaps = patch("main._layout_sync_gaps", return_value=[])
        self.patches += [bridge, script, gaps]
        for p in self.patches[:2]:
            p.start()
        self.mock_bridge = bridge.start()
        script.start()
        self.mock_gaps = gaps.start()
        self.gaps = gaps

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def commands(self):
        """(command, action) of every bridge run."""
        return [
            (c.args[0], c.args[1].get("action"))
            for c in self.mock_bridge.await_args_list
        ]

    def scripts(self):
        """'import' / 'save' for every standalone script run."""
        return [
            "import" if "RunProcess" in c.args[1] else "save"
            for c in self.mock_script.await_args_list
        ]

    def test_project_targets_layouts_before_drawings(self):
        targets = main._harness_import_targets(self.project)
        self.assertEqual([t.name for t in targets], ["Test.LdrDoc", "Test.HarDwf"])
        self.assertEqual(main._harness_import_targets(self.drawing), [self.drawing])

    async def test_rejects_other_documents(self):
        data = json.loads(
            await main.import_harness_changes(
                None, str(self.project.with_suffix(".WirDoc"))
            )
        )
        self.assertFalse(data["success"])
        self.mock_bridge.assert_not_awaited()

    async def test_scripts_quote_the_path_and_name_the_process(self):
        self.mock_script.side_effect = [IMPORTED]
        out = await main._harness_import_script(
            main._IMPORT_SCRIPT,
            Path("C:/it's/A.LdrDoc"),
            "Sch:ImportChangesFromWiringDiagram",
        )
        self.assertEqual(out, {"success": True, "text": "imported"})
        script = self.mock_script.await_args.args[1]
        self.assertIn(
            f"S1 := '{Path('C:/it' + chr(39) * 2 + 's/A.LdrDoc')!s}';", script
        )
        self.assertIn("RunProcess('Sch:ImportChangesFromWiringDiagram');", script)
        self.assertIn("DoFileSave", main._SAVE_SCRIPT)

    async def test_layout_import_reports_parts_and_closes(self):
        self.mock_bridge.side_effect = [_opened(was_open=False), CLOSED]
        self.mock_script.side_effect = [IMPORTED, SAVED]
        # before, the sync check after the save, the report
        imported = _layout("5876 BR005")
        with patch("main.parse_ldrdoc", side_effect=[_layout(), imported, imported]):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        self.assertTrue(data["success"], data)
        doc = data["documents"][0]
        self.assertEqual(
            doc["wires_updated"],
            [
                {
                    "designator": "W1",
                    "part_before": "",
                    "part_after": "5876 BR005",
                    "description": "5876 BR005",
                }
            ],
        )
        self.assertEqual(doc["wires_without_part"], [])
        self.assertTrue(doc["saved"])
        self.assertTrue(doc["changed"])
        self.assertFalse(doc["was_open"])
        self.assertTrue(doc["closed"])
        self.assertNotIn("pin_repair", doc)
        self.assertEqual(
            self.commands(),
            [("import_harness_changes", None), ("multiboard_document", "close")],
        )
        self.assertEqual(self.scripts(), ["import", "save"])
        self.assertEqual(
            self.mock_bridge.await_args_list[0].args[1]["document_path"],
            str(self.layout.resolve()),
        )
        self.assertIn(str(self.layout.resolve()), self.mock_script.await_args.args[1])

    async def test_layout_import_repairs_unassigned_pins(self):
        self.mock_bridge.side_effect = [_opened()]
        self.mock_script.side_effect = [IMPORTED, SAVED]
        repair = AsyncMock(
            return_value=json.dumps(
                {"success": True, "saved": True, "connection_point_pins_synced": 2}
            )
        )
        layouts = [
            _layout(),
            _layout("5876 BR005", unassigned=["1"]),
            _layout("5876 BR005", unassigned=["1"]),
            _layout("5876 BR005"),
        ]
        with (
            patch("main.parse_ldrdoc", side_effect=layouts),
            patch("main.edit_harness_layout", repair),
        ):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        self.assertTrue(data["success"], data)
        doc = data["documents"][0]
        self.assertEqual(doc["pin_repair"]["connection_point_pins_synced"], 2)
        self.assertEqual(doc["unassigned_pins"], {})
        repair.assert_awaited_once_with(None, str(self.layout.resolve()))
        self.assertNotIn("closed", doc)

    async def test_newly_unrouted_wires_fail(self):
        self.mock_bridge.side_effect = [_opened()]
        self.mock_script.side_effect = [IMPORTED, SAVED]
        with patch(
            "main.parse_ldrdoc",
            side_effect=[_layout(), *[_layout("5876 BR005", unrouted=["W1"])] * 2],
        ):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        self.assertFalse(data["success"])
        self.assertEqual(data["documents"][0]["newly_unrouted_wires"], ["W1"])

    async def test_layout_out_of_sync_is_imported_again_then_fails(self):
        self.mock_bridge.side_effect = [_opened(project="P.PrjHar")]
        self.mock_script.side_effect = [IMPORTED, SAVED] * 2
        self.mock_gaps.return_value = ["W1: part (none), wiring diagram 5876 BR005"]
        with patch("main.parse_ldrdoc", return_value=_layout()):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        self.assertFalse(data["success"])
        doc = data["documents"][0]
        self.assertEqual(doc["attempts"], 2)
        self.assertEqual(doc["sync_gaps"], self.mock_gaps.return_value)
        self.assertIn("still differs", doc["error"])
        self.assertEqual(self.scripts(), ["import", "save"] * 2)
        self.assertEqual(self.mock_gaps.call_args.args[1], Path("P.PrjHar"))

    async def test_layout_in_sync_on_the_second_attempt(self):
        self.mock_bridge.side_effect = [_opened()]
        self.mock_script.side_effect = [IMPORTED, SAVED, IMPORTED, _script("saved 0")]
        self.mock_gaps.side_effect = [["W1: not in the layout"], []]
        imported = _layout("5876 BR005")
        with patch(
            "main.parse_ldrdoc", side_effect=[_layout(), _layout(), imported, imported]
        ):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        self.assertTrue(data["success"], data)
        doc = data["documents"][0]
        self.assertEqual(doc["attempts"], 2)
        self.assertTrue(doc["changed"])
        self.assertNotIn("sync_gaps", doc)
        self.assertEqual(len(doc["wires_updated"]), 1)

    def test_sync_gaps_compare_parts_and_presence(self):
        wires = [
            {"unique_id": "U1", "designator": "W1", "design_item_id": "5876 BR005"},
            {"unique_id": "U2", "designator": "W2", "design_item_id": "5876 BR005"},
            {"unique_id": "U3", "designator": "W3", "design_item_id": "5876 BR005"},
        ]
        layout = {
            "wires": [
                {"designator": "W1", "origin_uid": "U1", "part_number": ""},
                {"designator": "W2", "origin_uid": "U2", "part_number": "5876 BR005"},
            ]
        }
        self.gaps.stop()
        try:
            with (
                patch("main.get_harness_wiring_docs", return_value=[Path("a.WirDoc")]),
                patch("main.parse_wirdoc", return_value={"wires": wires}),
            ):
                gaps = main._layout_sync_gaps(layout, self.project)
        finally:
            self.mock_gaps = self.gaps.start()
        self.assertEqual(
            gaps,
            ["W3: not in the layout", "W1: part (none), wiring diagram 5876 BR005"],
        )

    async def test_drawing_import_reports_resized_tables(self):
        self.mock_bridge.side_effect = [_opened()]
        self.mock_script.side_effect = [IMPORTED, SAVED]
        with patch(
            "harness_drawing.review_drawing",
            side_effect=[_review(), _review(width=180.78)],
        ):
            data = json.loads(
                await main.import_harness_changes(None, str(self.drawing))
            )
        self.assertTrue(data["success"], data)
        doc = data["documents"][0]
        self.assertEqual(doc["items_resized"], ["2493"])
        self.assertEqual(doc["items_moved"], [])
        self.assertEqual(doc["projections_emptied"], [])
        self.mock_gaps.assert_not_called()

    def test_a_table_growing_downwards_is_resized_not_moved(self):
        def bom(y, height, x=44.029):
            item = {"id": "472", "x_mm": x, "y_mm": y, "width_mm": 377.4}
            item["height_mm"] = height
            return {"pages": [{"items": [item]}], "projections": [], "issues": []}

        grown = main._drawing_import_report(bom(305.754, 85.579), bom(283.847, 107.486))
        self.assertEqual(grown["items_moved"], [])
        self.assertEqual(grown["items_resized"], ["472"])
        shifted = main._drawing_import_report(
            bom(305.754, 85.579), bom(305.754, 85.579, 50)
        )
        self.assertEqual(shifted["items_moved"], ["472"])
        self.assertEqual(shifted["items_resized"], [])

    async def test_drawing_import_that_empties_a_projection_fails(self):
        self.mock_bridge.side_effect = [_opened()]
        self.mock_script.side_effect = [IMPORTED, SAVED]
        with patch(
            "harness_drawing.review_drawing", side_effect=[_review(), _review(lines=0)]
        ):
            data = json.loads(
                await main.import_harness_changes(None, str(self.drawing))
            )
        self.assertFalse(data["success"])
        self.assertEqual(data["documents"][0]["projections_emptied"], ["J1 Front"])

    async def test_project_imports_layout_then_drawing(self):
        self.mock_bridge.side_effect = [_opened(), _opened()]
        self.mock_script.side_effect = [IMPORTED, _script("saved 0")] * 2
        with (
            patch("main.parse_ldrdoc", return_value=_layout()),
            patch("harness_drawing.review_drawing", return_value=_review()),
        ):
            data = json.loads(
                await main.import_harness_changes(None, str(self.project))
            )
        self.assertTrue(data["success"], data)
        self.assertEqual(
            [Path(d["document"]).name for d in data["documents"]],
            ["Test.LdrDoc", "Test.HarDwf"],
        )
        self.assertFalse(data["documents"][0]["changed"])
        self.assertEqual(self.scripts(), ["import", "save"] * 2)

    async def test_refused_open_runs_no_script(self):
        self.mock_bridge.side_effect = [
            {
                "success": True,
                "result": {
                    "success": False,
                    "error": "unsaved changes in Altium (A.WirDoc)",
                    "modified": True,
                },
            }
        ]
        with patch("main.parse_ldrdoc", return_value=_layout()):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        self.assertFalse(data["success"])
        self.assertIn("unsaved", data["documents"][0]["error"])
        self.assertEqual(self.commands(), [("import_harness_changes", None)])
        self.mock_script.assert_not_awaited()

    async def test_open_focuses_through_the_project_and_a_failed_open_stops(self):
        self.mock_bridge.side_effect = [_opened(was_open=False), CLOSED]
        self.open_reply = _script("error: could not open the document")
        with patch("main.parse_ldrdoc", return_value=_layout()):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        self.assertFalse(data["success"])
        self.assertIn("could not open", data["documents"][0]["error"])
        self.assertIn("DM_OpenAndFocusDocument", self.open_scripts[0])
        self.mock_script.assert_not_awaited()

    async def test_unfocused_document_is_not_saved_but_closed(self):
        self.mock_bridge.side_effect = [_opened(was_open=False), CLOSED]
        self.mock_script.side_effect = [
            _script("error: Altium did not focus the document; nothing was imported")
        ]
        with patch("main.parse_ldrdoc", return_value=_layout()):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        self.assertFalse(data["success"])
        self.assertIn("did not focus", data["documents"][0]["error"])
        self.assertEqual(self.scripts(), ["import"])
        self.assertEqual(self.commands()[-1], ("multiboard_document", "close"))

    async def test_script_error_is_reported(self):
        self.mock_bridge.side_effect = [_opened()]
        self.mock_script.side_effect = [
            json.dumps(
                {
                    "success": False,
                    "error": "run-time error",
                    "altium_message": "Undeclared identifier",
                    "executor_wedged": True,
                }
            )
        ]
        with patch("main.parse_ldrdoc", return_value=_layout()):
            data = json.loads(await main.import_harness_changes(None, str(self.layout)))
        doc = data["documents"][0]
        self.assertFalse(data["success"])
        self.assertEqual(doc["error"], "Undeclared identifier")
        self.assertTrue(doc["executor_wedged"])


if __name__ == "__main__":
    unittest.main()
