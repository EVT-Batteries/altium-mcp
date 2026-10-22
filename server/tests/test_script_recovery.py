"""Unit tests for the automatic script-engine recovery (never reaches Altium).

Run from the altium-mcp root:  uv run --with pytest python -m pytest server/tests -q
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import main


class TestRecoverScriptExecutor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patchers = [
            patch("main._dismiss_altium_dialogs", return_value=[]),
            patch("main._blocking_altium_dialogs", return_value=[]),
            patch("main.subprocess.run", MagicMock()),
            patch("main.asyncio.sleep", AsyncMock()),
        ]
        for p in self.patchers:
            p.start()
        self.stop_cmd = main.subprocess.run

    async def asyncTearDown(self):
        for p in self.patchers:
            p.stop()

    async def test_stop_is_enough(self):
        with (
            patch("main._script_engine_responds", AsyncMock(return_value=True)),
            patch("main._send_ctrl_f3") as keys,
        ):
            result = await main._recover_script_executor()
        self.assertEqual(result, {"recovered": True, "method": "stop"})
        self.assertIn("-REditScript:Stop", self.stop_cmd.call_args[0][0])
        keys.assert_not_called()

    async def test_falls_back_to_ctrl_f3(self):
        with (
            patch("main._script_engine_responds", AsyncMock(side_effect=[False, True])),
            patch("main._send_ctrl_f3", return_value=True) as keys,
        ):
            result = await main._recover_script_executor()
        self.assertEqual(result, {"recovered": True, "method": "ctrl_f3"})
        keys.assert_called_once()

    async def test_asks_the_user_when_nothing_works(self):
        with (
            patch("main._script_engine_responds", AsyncMock(return_value=False)),
            patch("main._send_ctrl_f3", return_value=True),
        ):
            result = await main._recover_script_executor()
        self.assertFalse(result["recovered"])
        self.assertIn("ask the user", result["ask_user"])

    async def test_no_keys_when_altium_cannot_be_focused(self):
        # _send_ctrl_f3 refuses to type into another application: no second check
        responds = AsyncMock(return_value=False)
        with (
            patch("main._script_engine_responds", responds),
            patch("main._send_ctrl_f3", return_value=False),
        ):
            result = await main._recover_script_executor()
        self.assertFalse(result["recovered"])
        self.assertEqual(responds.await_count, 1)

    async def test_modal_dialog_launches_nothing(self):
        # X2.EXE -REditScript:Stop would wait behind Altium's .NET error box
        with (
            patch("main._blocking_altium_dialogs", return_value=["Error"]),
            patch("main._script_engine_responds", AsyncMock()) as responds,
            patch("main._send_ctrl_f3") as keys,
        ):
            result = await main._recover_script_executor()
        self.assertFalse(result["recovered"])
        self.assertEqual(result["blocking_dialogs"], ["Error"])
        self.assertIn("Error", result["ask_user"])
        self.stop_cmd.assert_not_called()
        responds.assert_not_awaited()
        keys.assert_not_called()


class TestSandboxRecovers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The sandbox copy is written under EXCHANGE_DIR: keep it off the live bridge folder
        self.tmp = tempfile.TemporaryDirectory()
        self.exchange = patch("main.EXCHANGE_DIR", Path(self.tmp.name))
        self.exchange.start()

    async def asyncTearDown(self):
        self.exchange.stop()
        self.tmp.cleanup()

    async def test_failed_run_reports_recovery(self):
        recovery = {"recovered": True, "method": "stop"}
        with (
            patch("main.subprocess.Popen"),
            patch("main._dismiss_altium_dialogs", return_value=[]),
            patch("main._blocking_altium_dialogs", return_value=[]),
            patch(
                "main._recover_script_executor", AsyncMock(return_value=recovery)
            ) as rec,
            patch(
                "main.SANDBOX_RESULT", MagicMock(exists=MagicMock(return_value=False))
            ),
            patch("main.SANDBOX_LOG", MagicMock(exists=MagicMock(return_value=False))),
        ):
            out = json.loads(await main._run_sandbox("ResultText := 'x';", 0))
        rec.assert_awaited_once()
        self.assertEqual(out["error"], "script never started")
        self.assertFalse(out["executor_wedged"])
        self.assertIn("Recovered automatically (stop)", out["recovery"])

    async def test_no_recursion_without_recover(self):
        with (
            patch("main.subprocess.Popen"),
            patch("main._dismiss_altium_dialogs", return_value=[]),
            patch("main._blocking_altium_dialogs", return_value=[]),
            patch("main._recover_script_executor", AsyncMock()) as rec,
            patch(
                "main.SANDBOX_RESULT", MagicMock(exists=MagicMock(return_value=False))
            ),
            patch("main.SANDBOX_LOG", MagicMock(exists=MagicMock(return_value=False))),
        ):
            out = json.loads(
                await main._run_sandbox("ResultText := 'x';", 0, recover=False)
            )
        rec.assert_not_awaited()
        self.assertTrue(out["executor_wedged"], out)

    async def test_modal_dialog_blocks_the_run(self):
        with (
            patch("main.subprocess.Popen") as launch,
            patch("main._blocking_altium_dialogs", return_value=["Error"]),
            patch("main._recover_script_executor", AsyncMock()) as rec,
        ):
            out = json.loads(await main._run_sandbox("ResultText := 'x';", 0))
        launch.assert_not_called()
        rec.assert_not_awaited()
        self.assertTrue(out["executor_wedged"])
        self.assertIn("ask the user", out["recovery"])


class TestBridgeRefusesWhenBlocked(unittest.IsolatedAsyncioTestCase):
    async def test_no_request_while_blocked(self):
        with (
            patch("main._blocking_altium_dialogs", return_value=["Error"]),
            patch.object(main.altium_bridge, "run_altium_script", AsyncMock()) as run,
        ):
            out = await main.altium_bridge._execute_command_locked("ping", {})
        run.assert_not_awaited()
        self.assertFalse(out["success"])
        self.assertTrue(out["executor_wedged"])


if __name__ == "__main__":
    unittest.main()
