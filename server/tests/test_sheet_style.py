"""Unit tests for the get_sheet_style tool (bridge mocked, never reaches Altium).

Run from the altium-mcp root:  uv run --with pytest python -m pytest server/tests -q
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _reply(**fields):
    """A bridge response as Altium_API.pas WriteResponse builds it."""
    result = {
        "success": True,
        "document": "",
        "sheet_style_index": 8,
        "use_custom_sheet": False,
        "orientation_index": 0,
        "sheet_size_x_mils": 32000,
        "sheet_size_y_mils": 20000,
        "opened": False,
    }
    result.update(fields)
    return {"success": True, "result": result}


class TestGetSheetStyle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Mock what execute_command calls, so the real lock/exclusive path runs
        self.bridge_patcher = patch(
            "main.altium_bridge._execute_command_locked",
            new_callable=AsyncMock,
            return_value=_reply(),
        )
        self.mock_bridge = self.bridge_patcher.start()
        # Never wait on (or hold) the live bridge lock
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_patcher = patch(
            "main.BRIDGE_LOCK_FILE", Path(self.tmp.name) / "bridge.lock"
        )
        self.lock_patcher.start()
        # Synthetic, empty documents: the tool only checks that the file exists
        self.wirdoc = Path(self.tmp.name) / "MCP_Test_sheet.WirDoc"
        self.wirdoc.write_bytes(b"")
        self.schdoc = Path(self.tmp.name) / "MCP_Test_sheet.SchDoc"
        self.schdoc.write_bytes(b"")

    async def asyncTearDown(self):
        self.lock_patcher.stop()
        self.tmp.cleanup()
        self.bridge_patcher.stop()

    async def test_reports_standard_style(self):
        import main

        res = json.loads(
            await main.get_sheet_style(None, document_path=str(self.wirdoc))
        )
        self.mock_bridge.assert_awaited_once_with(
            "get_sheet_style", {"document_path": str(self.wirdoc.resolve())}
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["sheet_style"], "D")
        self.assertEqual(res["standard_style"], "D")
        self.assertEqual(res["orientation"], "landscape")
        self.assertEqual(res["sheet_size_mils"], [32000, 20000])
        self.assertFalse(res["use_custom_sheet"])
        # the reported name round-trips into add_harness_wiring(sheet_style=...)
        self.assertEqual(main.SHEET_STYLES[res["sheet_style"]], 8)

    async def test_schdoc_and_string_result(self):
        import main

        # WriteResponse may hand the result over as a JSON string
        reply = _reply(sheet_style_index=0, orientation_index=1)
        reply["result"] = json.dumps(reply["result"])
        self.mock_bridge.return_value = reply
        res = json.loads(
            await main.get_sheet_style(None, document_path=str(self.schdoc))
        )
        self.assertEqual(res["sheet_style"], "A4")
        self.assertEqual(res["orientation"], "portrait")

    async def test_custom_sheet(self):
        import main

        self.mock_bridge.return_value = _reply(
            sheet_style_index=0, use_custom_sheet=True, opened=True
        )
        res = json.loads(
            await main.get_sheet_style(None, document_path=str(self.wirdoc))
        )
        self.assertEqual(res["sheet_style"], "CUSTOM")
        self.assertEqual(res["standard_style"], "A4")
        self.assertTrue(res["opened"])

    async def test_unknown_ordinal_is_reported_not_guessed(self):
        import main

        self.mock_bridge.return_value = _reply(sheet_style_index=42)
        res = json.loads(
            await main.get_sheet_style(None, document_path=str(self.wirdoc))
        )
        self.assertTrue(res["success"])
        self.assertIsNone(res["sheet_style"])
        self.assertIn("42", res["warning"])

    async def test_invalid_paths_never_reach_altium(self):
        import main

        bad_suffix = Path(self.tmp.name) / "MCP_Test_sheet.PcbDoc"
        bad_suffix.write_bytes(b"")
        for path in ("", str(bad_suffix), str(Path(self.tmp.name) / "missing.WirDoc")):
            res = json.loads(await main.get_sheet_style(None, document_path=path))
            self.assertFalse(res["success"], path)
        self.mock_bridge.assert_not_awaited()

    async def test_altium_failures_are_errors(self):
        import main

        # bridge-level failure (script did not run, timeout, ...)
        self.mock_bridge.return_value = {"success": False, "error": "timeout"}
        res = json.loads(
            await main.get_sheet_style(None, document_path=str(self.wirdoc))
        )
        self.assertEqual(res, {"success": False, "error": "timeout"})
        # script ran but could not resolve the document (not_found path)
        self.mock_bridge.return_value = {
            "success": True,
            "result": {
                "success": False,
                "error": "could not open requested document: x",
            },
        }
        res = json.loads(
            await main.get_sheet_style(None, document_path=str(self.wirdoc))
        )
        self.assertFalse(res["success"])
        self.assertIn("could not open", res["error"])


if __name__ == "__main__":
    unittest.main()
