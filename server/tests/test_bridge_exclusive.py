"""AltiumBridge.exclusive: the bridge hold shared by execute_command and the
harness tools that make several bridge runs (no Altium needed).

Run from altium-mcp/:  uv run --with pytest python -m pytest server/tests/test_bridge_exclusive.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from exchange_lock import ExchangeLock


class TestBridgeExclusive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main

        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_file = Path(self.tmp.name) / "bridge.lock"
        self.patchers = [
            patch("main.BRIDGE_LOCK_FILE", self.lock_file),
            patch("main.LOCK_TIMEOUT", 0.3),
            patch.object(
                main.altium_bridge, "ensure_paths", AsyncMock(return_value=None)
            ),
            patch("main.sandbox.altium_hung", return_value=False),
        ]
        for p in self.patchers:
            p.start()
        self.run_locked = patch.object(
            main.altium_bridge,
            "_execute_command_locked",
            AsyncMock(return_value={"success": True, "result": {"ok": 1}}),
        )
        self.mock_run = self.run_locked.start()

    async def asyncTearDown(self):
        self.run_locked.stop()
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    async def test_command_runs_under_the_lock_and_releases_it(self):
        response = await self.main.altium_bridge.execute_command("get_all_nets", {})
        self.assertTrue(response["success"])
        self.assertEqual(self.mock_run.await_args.args[:2], ("get_all_nets", {}))
        # Released afterwards: another holder gets it at once
        other = ExchangeLock(self.lock_file)
        self.assertTrue(await other.acquire(0.1))
        other.release()

    async def test_another_server_holding_the_lock_is_reported_not_run(self):
        other = ExchangeLock(self.lock_file)
        self.assertTrue(await other.acquire(0.1))
        try:
            response = await self.main.altium_bridge.execute_command("get_all_nets", {})
            self.assertFalse(response["success"])
            self.assertIn("another Altium MCP server", response["error"])
            self.mock_run.assert_not_awaited()
            # A harness tool holding the bridge for several runs gets the same
            # reason as a TimeoutError (what its callers catch)
            with self.assertRaises(TimeoutError) as caught:
                async with self.main.altium_bridge.exclusive():
                    pass
            self.assertIn("another Altium MCP server", str(caught.exception))
        finally:
            other.release()

    async def test_hung_altium_is_not_sent_anything(self):
        with patch("main.sandbox.altium_hung", return_value=True):
            response = await self.main.altium_bridge.execute_command("get_all_nets", {})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"], self.main.ALTIUM_HUNG_ERROR)
        self.mock_run.assert_not_awaited()
        # ...and the lock was released on the way out
        other = ExchangeLock(self.lock_file)
        self.assertTrue(await other.acquire(0.1))
        other.release()

    async def test_missing_altium_path_is_reported(self):
        self.main.altium_bridge.ensure_paths.return_value = (
            "Altium Designer was not found"
        )
        response = await self.main.altium_bridge.execute_command("get_all_nets", {})
        self.assertEqual(
            response, {"success": False, "error": "Altium Designer was not found"}
        )
        self.mock_run.assert_not_awaited()

    async def test_command_failure_inside_the_result_becomes_the_failure(self):
        self.mock_run.return_value = {
            "success": True,
            "result": {"success": False, "error": "no layer"},
        }
        response = await self.main.altium_bridge.execute_command(
            "set_pcb_layer_visibility", {}
        )
        self.assertFalse(response["success"])
        self.assertIn("no layer", response["error"])

    async def test_add_harness_wiring_reports_a_busy_bridge(self):
        other = ExchangeLock(self.lock_file)
        self.assertTrue(await other.acquire(0.1))
        try:
            with patch("main.HARNESS_SPEC_FILE", Path(self.tmp.name) / "spec.txt"):
                raw = await self.main.add_harness_wiring(
                    None,
                    document_path=str(Path(self.tmp.name) / "MCP_Test.WirDoc"),
                    wires=[{"designator": "W1", "points": [0, 0, 1000, 0]}],
                )
            res = json.loads(raw)
            self.assertFalse(res["success"])
            self.assertIn("another Altium MCP server", res["error"])
            self.mock_run.assert_not_awaited()
        finally:
            other.release()


if __name__ == "__main__":
    unittest.main()
