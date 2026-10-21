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

# altium-mcp Development & Verification

altium-mcp drives a live, single Altium Designer GUI through files and scripts. Its failure modes
are quiet: a script error pauses Altium's debugger with no dialog, a tool can report success while
writing nothing (or writing into the wrong document), and repeated runs can stack duplicate data.
Real designs are binary files in git where a bad write is easy to miss. So the workflow below is
about one thing: make a change, prove on disk what it did, and never let an experiment touch a
real document.

## 1. How a tool call flows

```
MCP tool (server/main.py)
  -> AltiumBridge.exclusive()        asyncio lock + OS lock on C:\Users\Public\altium_mcp\bridge.lock
  -> request.json (+ spec files)     in C:\Users\Public\altium_mcp\
  -> X2.EXE -RScriptingSystem:RunScript(Altium_API.PrjScr | Altium_API>Run)
  -> Altium_API.pas ExecuteCommand   EnsureDocumentFocused (skip-list!) then `case CommandName of`
  -> response.json                   polled by Python (120 s timeout)
```

- Pure-Python tools (`get_harness_data`, parser, router, `dblib.py`) read **saved files** with
  olefile and never touch Altium; unsaved Altium edits are invisible to them.
- `add_harness_wiring` writes `harness_wiring_spec.txt`: one record per line, `|`-separated,
  every field percent-encoded by `_spec_line` (`%`→`%25`, `|`→`%7C`, CR/LF→`%0D`/`%0A`) and decoded
  by `SpecField` in Pascal. Build new records with `_spec_line`, read them with `SpecField`.
- A new bridge command needs: a `case` entry in `Altium_API.pas`, and - if it manages its own
  document - an entry in the `EnsureDocumentFocused` skip-list in `other_utils.pas` (otherwise
  Altium first tries to focus a PCB and fails with "No PCB document found").
- Write tools must take an explicit `document_path`, find it with `FindOpenDocument` before
  `Client.OpenDocument` (re-opening an open document reloads it from disk and discards unsaved
  edits), resolve it with `SchServer.GetSchDocumentByPath`, and compare **full** paths. Never use
  `GetCurrentSchDocument` as the target: after a script run the focus is often elsewhere.

## 2. Ground rules for testing

1. **One Altium operation at a time.** Every agent, subagent and server process shares one Altium
   and one exchange folder. Do not run Altium-touching calls in parallel.
2. **Never test a write on a real document.** Copy it to a scratch folder under a *different file
   name* (`MCP_Test_<what>.WirDoc`) and keep an untouched `.orig` copy for diffing. A different
   name matters: Altium and older code match documents by file name, so a same-named copy can
   make a "test" land in the real document.
3. **Check the real file is untouched** afterwards (`git status`, file mtime), and close scratch
   documents in Altium when done (see §5).
4. **Tool results are claims, not evidence.** Results now carry `saved`, `not_found` and counts;
   still diff the file on disk (§4) before concluding a change works.

## 3. Running the code you just changed

The MCP server is a long-lived process started by the IDE. Editing `.py` files does not reload it:
calls through the MCP keep running the old Python until the IDE/server restarts. The DelphiScript
side, by contrast, is reloaded from disk on every script run.

To exercise new Python immediately, call the tool function directly with the server's own venv
(`~/.altium-mcp/venvs/<key>/Scripts/python.exe`; the key changes with the requirements, so glob
for it) using the bundled runner:

```
<venv python> .agents/skills/altium-mcp-development/scripts/call_tool.py <tool_name> '<json kwargs>'
<venv python> .agents/skills/altium-mcp-development/scripts/call_tool.py <tool_name> @kwargs.json
```

It imports `server/main.py` from this checkout and prints the tool's return value. This is the
official tool entry point, not an ad-hoc script: it gets the same validation, encoding and locking.
Pure-Python modules (`harness_parser`, `harness_router`, `dblib`) can be imported the same way.

Remember the old daemon is still running: if you call the *MCP* tool `run_altium_script` before a
restart, an old server may rewrite the tracked `server/SandboxScript/Sandbox.pas`; restore it with
`git checkout -- server/SandboxScript/Sandbox.pas`.

## 4. Verifying what was written

Run the bundled diff on the scratch copy before and after an operation:

```
<venv python> .agents/skills/altium-mcp-development/scripts/wirdoc_diff.py before.WirDoc after.WirDoc [W1 W2 ...]
<venv python> .agents/skills/altium-mcp-development/scripts/wirdoc_diff.py doc.WirDoc          # single-file audit
```

It reports record-count changes per record type, top-level text lost or added, wire UniqueIDs
changed, wire end attachments changed, objects carrying duplicate parameter names, and per-wire
detail for the designators you list. Read it against what the operation was supposed to do:

| Symptom in the diff | Usual meaning |
|---|---|
| Wire `UniqueID changed` | wires were deleted and recreated; `.LdrDoc`/BOM links to them break |
| `RECORD 14` (rectangles) or `RECORD 4` (text) count dropped | a "clear" removed user graphics/text |
| Duplicate parameter names grow on each run | a code path appends instead of upserting (`UpsertSchParameter`) |
| End attachments changed | an edit moved or reversed wire endpoints |
| No change at all after a "successful" write | wrong document, nothing matched, or the save failed |

Some patterns are **normal Altium serialization**, not bugs - check `references/wirdoc-format.md`
before "fixing" them (e.g. every cavity component is written twice with the same UniqueID, so its
parameters appear twice; text with non-ASCII exists in a plain and a `%UTF8%` field).

Finally, for anything visual (routing, labels, graphics) take a screenshot of the scratch document
(`get_screenshot(view_type="sch", document_path=...)`) and look at it.

## 5. DelphiScript survival

- `try/except` does **not** catch runtime errors (bad property, nil object, bad conversion). The
  script pauses in Altium's debugger, nothing comes back, and every later script silently does
  nothing. Recover with: `"<Altium install>\X2.EXE" -REditScript:Stop` (Altium's path is in
  `~/.altium-mcp/config.json` as `altium_exe_path`).
- **Recovery is automatic; ask the user only when it fails.** When a sandbox run fails, and
  when a production command times out, the server unblocks the executor itself
  (`_recover_script_executor`). It runs `EditScript:Stop`, then presses Ctrl+F3 in the Altium
  window (sent with scan codes, because Altium ignores virtual-key-only input), and checks
  after each step with a trivial script. Look at the result:
  - `executor_wedged: false` with `recovery` "Recovered automatically (stop|ctrl_f3)": the
    executor is free. Fix your script before rerunning it.
  - `executor_wedged: true`: both steps failed. **Do not retry.** Pass the `recovery` text on
    to the user (focus the stuck script tab and press Ctrl+F3, close an open Altium dialog, or
    restart Altium), and do non-Altium work while you wait.

  After an Altium "Error" box the sandbox stops waiting about 3 s later rather than running to
  its timeout. It reports dialog texts where Windows exposes them (`dismissed_dialog_texts`);
  Altium's own themed message boxes don't expose theirs, so only their titles are reported.
- Sandbox compile errors block the executor too: use only the declared scratch variables
  (`S1..S3`, `I1..I3`, `B1`, `Obj1..Obj5`; there is no `Obj6`).
- Within one `.pas` unit, define a function above its first caller; calling one defined further
  down pauses the script at runtime.
- Prototype unknown API calls with the `run_altium_script` sandbox first. Log a step before each
  risky statement (`SandboxLog('...')`); the last logged step tells you what died. Create your own
  `TStringList` objects rather than relying on provided ones. No inline variable declarations.
- Coordinates: `Point(MilsToCoord(mils), MilsToCoord(mils))`; raw integers land in `_Frac` units.
- Persist explicitly: `ServerDoc.Modified := True; ServerDoc.DoFileSave('')`, then report
  `not ServerDoc.Modified` as the real save status.
- Keep CRLF line endings in `.pas` files (check: every line should end in `\r`).
- Close a scratch document without saving:
  ```
  ResetParameters; AddStringParameter('Action', 'Close');
  AddStringParameter('FileName', <full path>); AddStringParameter('PromptToSave', 'False');
  RunProcess('WorkspaceManager:CloseObject');
  ```

## 6. Unit tests

```
uv run --with pytest python -m pytest server/tests -q      # from the altium-mcp root
```

Tests must never reach the live bridge. Follow the existing fixtures in
`server/tests/test_harness_parser.py`:

- mock `main.altium_bridge._execute_command_locked` (what both `execute_command` and
  `add_harness_wiring` call), not `execute_command`;
- patch `main.HARNESS_SPEC_FILE` and `main.BRIDGE_LOCK_FILE` into a temp folder;
- patch `main._get_active_harness_path` when a fallback needs an "active" harness;
- prefer synthetic records with `harness_parser.parse_wirdoc_records(records, path)` over sample
  design files, so tests run in any checkout and publish no company data.

The live-Altium suite (`test_altium_script.py`) only runs with `ALTIUM_INTEGRATION=1`.
Lint new modules with `uvx ruff check --extend-select I <files>`.

## 7. Before you call it done

- Unit tests pass; new behaviour has a test (including the failure/`not_found` path).
- The change was exercised live on a scratch copy and diffed (§4); visuals screenshotted.
- Real documents untouched; scratch documents closed in Altium; `Sandbox.pas` clean.
- Report what you verified and what you could not (e.g. "Python side tested directly; the running
  MCP server still has the old code until the IDE restarts").
