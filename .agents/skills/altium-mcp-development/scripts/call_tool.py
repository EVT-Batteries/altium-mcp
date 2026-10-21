"""Call an altium-mcp tool function from this checkout, bypassing a stale MCP server.

Usage (with the server's venv python):
    python call_tool.py <tool_name> '<json kwargs>'
    python call_tool.py <tool_name> @kwargs.json
    python call_tool.py <tool_name>                 # no arguments

Prints the tool's return value. Uses the same code path as the MCP tool
(validation, spec encoding, bridge lock), so it is safe alongside a running
server: the bridge lock makes the two take turns.
"""

import asyncio
import json
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[4] / "server"
sys.path.insert(0, str(SERVER))

import main  # noqa: E402


def _kwargs(arg: str | None) -> dict:
    if not arg:
        return {}
    if arg.startswith("@"):
        return json.loads(Path(arg[1:]).read_text(encoding="utf-8"))
    return json.loads(arg)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tool = getattr(main, sys.argv[1], None)
    if tool is None or not asyncio.iscoroutinefunction(tool):
        sys.exit(f"no async tool function {sys.argv[1]!r} in {SERVER / 'main.py'}")
    print(asyncio.run(tool(None, **_kwargs(sys.argv[2] if len(sys.argv) > 2 else None))))
