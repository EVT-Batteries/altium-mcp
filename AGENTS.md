# altium-mcp: instructions for agents

A Model Context Protocol server that drives Altium Designer (fork of
[coffeenmusic/altium-mcp](https://github.com/coffeenmusic/altium-mcp); branch
`feature/harness-support` adds harness projects, multi-board schematics and Draftsman
harness drawings). Upstream `main` is merged in regularly: keep changes mergeable.

## Project-agnostic: a hard rule

This server, its tests, docs and skills serve any Altium user and any design. Nothing in
this repository may depend on, name or assume a particular project, workspace, company or
person:

- No design-specific names in code, tool descriptions, defaults, examples, tests or skills:
  no project, sheet, connector, net or part names taken from a real design. Examples use
  generic names (`P1`, `J1`, `W1`, `SP1`, `SIG_A`, `PWR`, `CONN-4P`, `my.DbLib`).
- No absolute paths of a machine, user or workspace (`C:\Users\<name>\...`,
  `C:\<project>\...`), and no relative paths that leave the repository.
- No library, database or template of one organisation as a default. Libraries are found
  generically (`dblib.find_dblib`, `ALTIUM_DBLIB_PATH`) or passed by the caller.
- No team's drawing conventions as hard rules: `check_harness_wiring`'s thresholds are a
  documented default style; a project's conventions (wire colours, label spacing,
  drawing standards, pinout formats) belong to that project's own instructions.
- Tests use synthetic data only: `server/tests/harness_fixture.py` (a generic harness
  project written as real compound files at test time) or records passed to
  `harness_parser.parse_wirdoc_records`. Never a real design file; a test must give the
  same result in any checkout.
- Findings from work on a real design go into the code and docs in generic form ("a
  multi-part fuse holder", "a pigtail whose free ends sit on no-connects"). The design
  itself, its state and its decisions stay in the workspace that uses the server (its
  own AGENTS.md / HANDOVER / skills).

Before committing, `git grep` the change for project, company and user names and for
absolute paths.

## Working on the server

- Read `.agents/skills/altium-mcp-development/SKILL.md` (how a call flows, testing on
  scratch copies, verifying writes, DelphiScript survival) and `HANDOVER.md` (history,
  decisions, failed approaches, open bugs) before changing anything.
- One Altium operation at a time: every server process and agent shares one Altium and
  one exchange folder (`C:\Users\Public\altium_mcp`).
- Never test a write on a real document: use renamed scratch copies, diff them, close
  them in Altium afterwards (never close a project by script).
- Register tools with `_tool()` / `_tool(read_only=True)`; Pascal units are bundled into
  one script file (unique routine and global names, no call cycles).
- Unit tests: `uv run --with pytest python -m pytest server/tests -q` from this folder;
  they must pass without Altium and without any design files.
- The user commits and pushes; do not commit unless asked.
