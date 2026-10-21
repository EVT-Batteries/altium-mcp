---
name: altium-mcp-development
description: >-
  How to change, debug and verify the altium-mcp server itself (Python tools in server/main.py,
  the harness parser/router/DbLib modules, and the DelphiScript bridge in server/AltiumScript/)
  without damaging real Altium documents. Use it whenever you modify or add an altium-mcp tool or
  bridge command, investigate why a tool misbehaves or reports success without effect, test a
  change against Altium, verify what a tool really wrote into a .WirDoc/.SchDoc, recover a wedged
  Altium script executor, or puzzle over odd characters or duplicated records in Altium files -
  even if the user only says "fix the MCP", "the harness tool broke" or "check what it did".
---

<!-- Claude Code shim: the canonical skill is .agents/skills/altium-mcp-development/SKILL.md in this repository (shared with other agents). Edit that file; keep the frontmatter above in sync with it. -->

Read `.agents/skills/altium-mcp-development/SKILL.md` (relative to the altium-mcp repository root) in full now and follow it. Run its bundled scripts with the PowerShell or Bash tool; load MCP tool schemas with ToolSearch before calling them.
