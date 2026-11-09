"""Unit tests for the project_path of get_project_components / get_erc_violations
(no Altium needed: the bridge is faked).

A .PrjPcb goes to Altium; a .PrjHar is read from its saved files and never
sent to the bridge; a failed bridge compile falls back to the focused harness.

Run from server/:  python -m unittest tests.test_compile_project_path -v
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import harness_fixture
import main


class _FakeBridge:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, command: str, params: dict, timeout: float | None = None):
        self.calls.append((command, params))
        return self.response


class CompileProjectPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dir = tempfile.TemporaryDirectory()
        cls.fixture = harness_fixture.make_fixture_project(cls._dir.name)
        cls.prjpcb = os.path.join(cls._dir.name, "Board.PrjPcb")
        with open(cls.prjpcb, "w", encoding="utf-8") as f:
            f.write("[Design]\n")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._dir.cleanup()

    def run_tool(self, tool, bridge: _FakeBridge, **kwargs) -> dict:
        with mock.patch.object(main.altium_bridge, "execute_command", bridge):
            return json.loads(asyncio.run(tool(None, **kwargs)))

    def test_project_path_kinds(self) -> None:
        self.assertEqual(main._project_path(""), ("", ""))
        self.assertEqual(main._project_path(self.prjpcb)[1], "")
        self.assertEqual(main._project_path(str(self.fixture.prjhar))[1], "")
        self.assertIn(".PrjPcb or .PrjHar", main._project_path("x.PrjScr")[1])
        self.assertIn("not found", main._project_path("missing.PrjHar")[1])

    def test_prjpcb_goes_to_altium(self) -> None:
        bridge = _FakeBridge(
            {"success": True, "result": {"total": 0, "violations": []}}
        )
        out = self.run_tool(main.get_erc_violations, bridge, project_path=self.prjpcb)
        self.assertEqual(out["total"], 0)
        self.assertEqual(bridge.calls[0][0], "get_erc_violations")
        self.assertTrue(bridge.calls[0][1]["project_path"].endswith("Board.PrjPcb"))

    def test_prjhar_is_read_from_disk(self) -> None:
        bridge = _FakeBridge({"success": False, "error": "must not be called"})
        erc = self.run_tool(
            main.get_erc_violations, bridge, project_path=str(self.fixture.prjhar)
        )
        self.assertEqual(erc["source"], "harness_file")
        comps = self.run_tool(
            main.get_project_components,
            bridge,
            project_path=str(self.fixture.prjhar),
            designators=["P1"],
        )
        self.assertEqual(comps["source"], "harness_file")
        self.assertEqual([c["designator"] for c in comps["components"]], ["P1"])
        self.assertEqual(bridge.calls, [])

    def test_failed_compile_falls_back_to_focused_harness(self) -> None:
        bridge = _FakeBridge({"success": False, "error": "no flattened document"})

        async def focused():
            return self.fixture.prjhar

        with mock.patch.object(main, "_get_active_harness_path", focused):
            comps = self.run_tool(main.get_project_components, bridge)
        self.assertEqual(comps["source"], "harness_file")
        self.assertGreater(comps["component_count"], 0)
        self.assertEqual(bridge.calls[0][1]["project_path"], "")


if __name__ == "__main__":
    unittest.main()
