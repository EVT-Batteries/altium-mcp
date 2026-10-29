---
name: altium-mcp-development
description: >-
  How to change, debug and verify the altium-mcp server itself (Python tools in server/main.py,
  the harness parser/router/DbLib modules, and the DelphiScript bridge in server/AltiumScript/)
  without damaging real Altium documents. Use it whenever you modify or add an altium-mcp tool or
  bridge command, investigate why a tool misbehaves or reports success without effect, test a
  change against Altium, verify what a tool really wrote into a .WirDoc/.SchDoc/.LdrDoc, recover
  a wedged Altium script executor, find out how Altium works by decompiling it, trace an Altium
  .NET "Object reference not set" error dialog to its method, or puzzle over odd characters or
  duplicated records in Altium files - even if the user only says "fix the MCP", "the harness
  tool broke" or "check what it did".
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
   make a "test" land in the real document. For a `.LdrDoc`, copy the **whole harness project**
   under the new name (rename every file; in the `.PrjHar`, replace the old base name) and open
   that project first: a layout drawing needs its wiring diagram, and physical views need a
   project reload. A `.WirDoc` needs a project too: opened on its own it loads as an empty
   plain schematic (document ObjectId 32 instead of 79). Copy the `.PrjHar`, keep only the
   `[Document*]` sections of the copied sheets and point them at the renamed copies.
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
Pure-Python modules (`harness_parser`, `harness_check`, `dblib`) can be imported the same way.

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

Layout drawings (`.LdrDoc`): read them back with `harness_layout.parse_ldrdoc` (bundle ends,
`unrouted_wires`, connectors' `unassigned_pins`, wire `length`). The parser mirrors Altium's
routing, but confirm a routing change against Altium itself once with a read-only sandbox probe:
`Doc.GetBundleContent(Bundle).GetSubLinesContentCount` per bundle (wires it carries), then wire
data (ObjectId 90) `GetState_TotalLength` (filled only after the content was asked for). Both
must match the parser. The tool's result alone has been wrong before: the parser once routed by
connector while Altium routes by pin, so it reported every wire routed while Altium routed none.

## 5. DelphiScript survival

- `try/except` does **not** catch runtime errors (bad property, nil object, bad conversion). The
  script pauses in Altium's debugger, nothing comes back, and every later script silently does
  nothing. Recover with: `"<Altium install>\X2.EXE" -REditScript:Stop` (Altium's path is in
  `~/.altium-mcp/config.json` as `altium_exe_path`).
- **Recovery is automatic, and only for scripts the MCP started; ask the user only when it
  fails.** A script the user is running or debugging is never stopped.
  - Sandbox (`sandbox.Sandbox.recover`, `server/sandbox.py`): after a compile or run-time error
    it answers Altium's Error/Information boxes (Confirm and Warning boxes may be the user's
    and are left alone), runs `EditScript:Stop` and confirms with a probe script, up to three
    times. `executor_wedged: false` means the executor is free: fix your script before
    rerunning it. `executor_wedged: true`: **do not retry**; ask the user to close the open
    dialog, stop the paused script (Ctrl+F3 in its tab) or restart Altium, and do non-Altium
    work while you wait.
  - Bridge (`AltiumBridge._execute_command_locked`): a command that started but gave no
    response within 120 s writes `C:\Users\Public\altium_mcp\mcp_script_stuck.txt` and sends
    `EditScript:Stop`. When Altium does not pick up a request within 20 s, the debugger is
    stopped and the launch retried only while that marker exists; otherwise the result is
    "Altium did not start the command (not executed)" and nothing was stopped. Pass that on to
    the user rather than retrying.
- Sandbox scripts are checked before they run: assigning an undeclared plain name is refused.
  Declare your own `var` block or use the scratch variables listed in the
  `run_altium_script` description (`S1..S9`, `I1..I9`, `B1..B3`, `F1..F4`, `Obj1..Obj9`,
  `List1..List3`).
- Within one `.pas` unit, define a function above its first caller; calling one defined further
  down pauses the script at runtime.
- Prototype unknown API calls with the `run_altium_script` sandbox first. Log a step before each
  risky statement (`SandboxLog('...')`); the last logged step tells you what died. Create your own
  `TStringList` objects rather than relying on provided ones. No inline variable declarations.
- Coordinates: `Point(MilsToCoord(mils), MilsToCoord(mils))`; raw integers land in `_Frac` units.
- In the sandbox the `Obj1..Obj9` scratch variables are `IDispatch` (late bound):
  `if Obj1.Modified then` always reads False, while `BoolToStr(Obj1.Modified, True)` reads
  the real value. Test a late-bound Boolean property as `BoolToStr(..., True) = 'True'`, or
  declare a typed variable. This hid a working import for a whole session (2026-10-29).
- `IDocument.DM_OpenAndFocusDocument` (open a document through its project, as the Projects
  panel does) on a document that is already open reloads it from disk once the run returns,
  discarding unsaved changes: call it only for a closed document, `Client.ShowDocument`
  otherwise.
- A save from a bridge run (the `Altium_API.PrjScr` script project) wrote a layout without
  the import (`Sch:ImportChangesFromWiringDiagram`) Altium held in memory; the same save
  from a standalone sandbox script wrote it (cause not found). `import_harness_changes`
  therefore opens, imports and saves through `run_altium_script` and only checks through
  the bridge. Verify any process-driven change (`RunProcess`) on the saved file, not by the
  `Modified` flag.
- `Replicate` keeps every UniqueId (the object, its pins, its parameters). Renew them on a copy
  (`RenewUniqueIds` in `schematic_utils.pas`) before registering it, or wires and layout
  drawings cannot tell copy and source apart. `add_harness_wiring(copy_objects=...)` does this;
  reuse `CopyHarnessObjects` rather than writing another copy loop.
- Sheet-owned objects are those whose `Container.ObjectId` is the document's own ObjectId (79
  on a wiring diagram, 32 on a schematic); everything else travels with its parent.
- Persist explicitly: `ServerDoc.Modified := True; ServerDoc.DoFileSave('')`, then report
  `not ServerDoc.Modified` as the real save status.
- Keep CRLF line endings in `.pas` files (check: every line should end in `\r`).
- Close a scratch document without saving:
  ```
  ResetParameters; AddStringParameter('Action', 'Close');
  AddStringParameter('FileName', <full path>); AddStringParameter('PromptToSave', 'False');
  RunProcess('WorkspaceManager:CloseObject');
  ```
  That has left a saved lone scratch `.WirDoc` (in no project) open; close such a document
  with `Obj1 := Client.GetDocumentByPath(<full path>); Client.CloseDocument(Obj1);` and check
  `Client.GetDocumentByPath` returns nil afterwards.
- **Never close a project by script.** `WorkspaceManager:CloseObject` with
  `ObjectKind=Project` ignores `FileName` and closes the *focused* project: on 2026-10-26 it
  closed the user's whole multiboard project group (and raised a save prompt that wedged the
  executor) instead of the scratch `.PrjHar`. Close the scratch documents as above and ask the
  user to close the scratch project in the Projects panel.

## 6. Finding out how Altium works

Altium's documentation of its script API and file formats is thin. Read its code instead of
guessing:

- **Decompile** the .NET assemblies with ILSpy's command line (NuGet package `ilspycmd`, e.g.
  9.0.0.7889; unzip the `.nupkg` and run it with the installed .NET 8 runtime, no SDK needed):
  `dotnet <pkg>/tools/net8.0/any/ilspycmd.dll -p -o <out dir> <dll>`, then grep the C#. Useful
  assemblies in `C:\Program Files\Altium\ADAgile\System\`: `Altium.Sch.DataModel` (engine
  objects, file import/export, physical models), `Altium.Har.Core` (wiring -> layout sync, bundle
  content, BOM bridges), `Altium.Har.LayoutDrawing`, `InteractiveProperties.Providers.Harness.*`
  (what the Properties panel does: the GUI's own way to change something), and
  `Altium.Edp.Interfaces`. Draftsman (fabrication drawings) is an extension:
  `C:\ProgramData\Altium\Altium Designer <...>\Extensions\Draftsman\Altium.Designer.PcbDrawing.dll`.
- **Only `Rt_Schematic.ISch_*` members are callable from DelphiScript.** The objects also
  implement `SCHInterfaces.*`, but calling those members is a runtime error. Check a member exists
  in `Altium.Edp.Interfaces/Rt_Schematic/<interface>.cs` before probing it.
- **Map a .NET error dialog to its method.** The dialog says "Object reference not set to an
  instance of an object at <address>. <dll>, Base Address: <base>". The assemblies are
  ReadyToRun (precompiled), so `<address> - <base>` names a method:
  `uv run .agents/skills/altium-mcp-development/scripts/r2r_method_at.py <dll> <offset hex>`.
  Then read that method in the decompiled code. That is how the layout render crash was found
  (`SchHarnessComponent.GetState_OwnBoundingRectangle`, see `references/wirdoc-format.md`).
- **Probe read-only first, one question per sandbox run**, on the scratch project. Altium
  reacts to some changes after the script returns: a sequence that works as separate runs (edit
  + save, project reload, library update) can fail inside one run. When a multi-step write fails
  but each step works alone, split it into several bridge runs from Python (see
  `edit_harness_layout`'s `RELOADPROJECT` / `RENDER` runs).
- Write probe and patch scripts with the Write tool, not shell heredocs: the shell has eaten
  backslashes (`'\'` in Pascal, `\b` in paths) and silently broken anchors or text.

## 7. Unit tests

```
uv run --with pytest python -m pytest server/tests -q      # from the altium-mcp root
```

Tests must never reach the live bridge. Follow the existing fixtures in
`server/tests/test_harness_parser.py`:

- mock `main.altium_bridge._execute_command_locked` (what both `execute_command` and
  `add_harness_wiring` call), not `execute_command`;
- patch `main.HARNESS_SPEC_FILE` (layout tool: `main.HARNESS_LAYOUT_SPEC_FILE`) and
  `main.BRIDGE_LOCK_FILE` into a temp folder; for the layout tool also patch
  `main.parse_ldrdoc` with synthetic layouts (`tests/test_harness_layout.py`), and give the
  bridge mock a `side_effect` when one call makes several bridge runs;
- patch `main._get_active_harness_path` when a fallback needs an "active" harness;
- prefer synthetic records with `harness_parser.parse_wirdoc_records(records, path)` over sample
  design files, so tests run in any checkout and publish no company data.

The live-Altium suite (`test_altium_script.py`) only runs with `ALTIUM_INTEGRATION=1`.
Lint new modules with `uvx ruff check --extend-select I <files>`.

## 8. Before you call it done

- Unit tests pass; new behaviour has a test (including the failure/`not_found` path).
- The change was exercised live on a scratch copy and diffed (§4); visuals screenshotted.
- Real documents untouched; scratch documents closed in Altium; `Sandbox.pas` clean.
- Report what you verified and what you could not (e.g. "Python side tested directly; the running
  MCP server still has the old code until the IDE restarts").
