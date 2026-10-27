# Handover: altium-mcp harness support

Updated 2026-10-27 (session 12: multi-part connectors, wire kinds, wire break direction) for a
fresh agent session.
Read this, then the skill `.agents/skills/altium-mcp-development/SKILL.md`
(+ `references/wirdoc-format.md`) before touching anything. The harness projects the tools were
developed and tested on (P42C-1 workspace) have their own handover: `../HANDOVER.md` in the
workspace that holds this submodule.

## 1. Goal

Make the `altium-mcp` server (branch `feature/harness-support`, fork of coffeenmusic/altium-mcp)
reliably create, edit, sync and verify **Altium Designer 26.7.1 harness projects** (`.PrjHar`,
`.WirDoc`, `.LdrDoc`) using a **fully local CSV-backed DbLib** (`*-db.csv` + `.DbLib`), and keep it
**generic** (no project specifics in code).

State: the wiring-diagram tools (`add_harness_wiring`, `sync_harness_library`,
`check_harness_wiring`, `get_harness_data`) and the layout-drawing tool (`edit_harness_layout`:
connectors, views, connection points, bundles, coverings, labels, texts, fixed wire lengths,
connection point pin repair, crash-safe view rendering) are done and committed. Open work: §8.

## 2. State at hand-over

| Path | Branch | HEAD | Uncommitted |
|---|---|---|---|
| `altium-mcp/` | `feature/harness-support` | `4bb4847` (sessions 7-8 committed) | this file |

The user commits themselves. **Do not commit or push unless asked.** Test on renamed scratch copies
of real documents first; files the user is editing are not ours.

## 3. History (newest first)

### Session 12 (2026-10-27): multi-part connectors, wire kinds, wire break direction (uncommitted)
- **Multi-part connectors fixed** (§8 item 1 of session 9): `PinOnPart` (pin `OwnerPartId` 0 or the
  object's `CurrentPartID`), `FindComponentWithPin` (CAVITYPART uses the part showing the pin) and
  `SiblingParts` (same designator + `LibReference`, other part id): `designator_updates` on one
  part renames every part (`part_only` for one), `component_updates` reach every part.
  `get_harness_data` reports `current_part_id` / `part_count` per part object, lists only its
  visible pins and reports `hidden_pin_cavity_parts`. New `clear_hidden_cavity_parts` (record
  `CLEARHIDDEN`); `sync_harness_library` uses it automatically and reports
  `hidden_pin_cavity_parts_removed`. The old FH1 workaround left 64 stray terminals (4 per pin)
  on part A's hidden pins 11-20 of P42C-1 Top; sync removed them.
- **Wire component kind**: `component_kind` on `wires` / `wire_updates` (record `WIREKIND`,
  `SetState_ComponentKind`; the `ComponentKind` property does not exist in DelphiScript);
  `standard_no_bom` draws a connection that is not a BOM wire (bus bar links). Parser:
  wire `component_kind`.
- **Parameter visibility**: `hide_parameters` / `show_parameters` on wires (record `WIREPARAMVIS`).
  Parser: wire `shown_parameters` (visible parameters besides designator / comment).
- **Wire break direction**: `wire_breaks` take `direction` (`out` = style 1 `>>` away from the
  wire end, `in` = style 0 `<<` towards it, verified on screenshots) with the orientation derived
  from the wire ending there (0 wire runs left, 2 right, 1 down, 3 up), or explicit `style` /
  `orientation`; an existing break is updated in place. Parser: break `style` / `orientation`.
- **Altium's built-in wire parameters** (Color, Gauge, Min. Bending Radius, Thickness, Include
  Cut: what a GUI-placed wire gets, `RegisterDefaultParameters`) are now marked name-read-only
  (`ReadOnlyState = 1`) when written, like Altium does, so script and GUI wires match.
- **Compiler sees parameter changes only after a reload**: after a sync, ERC reported
  "Mismatched parameters in connected wire segments" on wire-break pairs whose saved parameters
  were identical; `relink=True` (reload) cleared it. `RELINK` now always reloads (also sheets
  without twists / breaks) and `sync_harness_library` passes `relink=True`.
- Tests: 3 new (`test_parse_multipart_connector`, `..._kinds_visibility_breaks`,
  `..._rejects_unknown_kind_and_style`); 153 passed, same 13 old failures. Verified live on
  scratch copies of P42C-1 Top (`MCP_Test_Multi`, `MCP_Test_TopRun`), then applied to the
  four P42C-1 Main harness sheets.

### Session 11 (2026-10-27): harness entry components (uncommitted)
- `edit_multiboard_schematic` gains `assign_entry_parts` and the tool-level shortcut
  `assign_harness_parts`, which expands to one `assign_entry_parts` per connection from the
  review's `harness_review` match. `assign_entry_parts` writes what Altium writes when a
  harness entry carrying a component mates a device entry (decompiled
  `EntriesConnectionEditor.MateEntries`, `PhysicalConnectionEditor.MatePinsByCreatingConnection`):
  - a component of the connection, its pins mapped to the entry in `entryPinMap`;
  - one `__logicalPinToPinConnection` per pin in the `entriesConnections` link, with entry1's
    pin as `pin1`;
  - each harness pin's `ExternalNetName` taken from the device pin.

  Unlike Altium, which pairs pins in sorted order, it pairs by pin number. The entry and the
  component are renamed after the connector designator.
- Review: an entry's component designator settles its connector (no `entry_map` needed).
  New issues: `harness_entry_without_part` (info) and `entry_part_mismatch` (error).
  `harness_review` rows gain `connector_part`.
- Verified on scratch copy `mbs_test\MCP_Test_Assign.MbsDoc`: Altium loads it, and an Altium
  save keeps the 20 components, 228 pin pairs and net names. Then applied to
  `P42C-1_System.MbsDoc`: 0 errors, 82 nets.
- Also found by decompiling: Cable and Direct connections can start only on a module entry
  that has no connection yet (`ModuleEntryDragDropBehavior.CanEnter`). Direct lines never
  branch, and a branch only joins a line of the same type (`CanConnectToHost`).

### Session 10 (2026-10-26): multi-board schematic review (uncommitted)
- New read-only `get_multiboard_data` (`multiboard.py`, tests `tests/test_multiboard.py`): parses
  a `.MbsDoc` (ZIP of JSON: `logical.json` + `Pages/*.json`, member names stored with `\`),
  matches harness connections to the `.PrjHar` children of the `.PrjMbd` by connector part
  numbers (DbLib `Mating Part`, either way), assigns entries to harness connectors (`entry_map`
  for identical ones) and traces nets through wired pins only (no-connect pins keep callout
  text as net name) and inline connector pin pairs. Each harness connection is one instance,
  so a project used twice (slave sense harness) is traced twice.
- Format and editor behaviour from decompiled `Altium.Designer.SystemDesign.dll`
  (`LogicalObjects.Serialization.V1`, `CommandBuilder.CreateInlineConnection`,
  `ModuleEntryCreator.PlaceEntryOnConnection`): inline connectors only come from placing an
  entry on a harness line (split); no join of two harnesses exists.
- Write support: `edit_multiboard_schematic` (`multiboard_edit.py`, tests
  `tests/test_multiboard_edit.py`) edits the saved JSON: `swap_entry_parts`, `join_harnesses`
  (inline pair + new branch, splits the target line), `set_title`. New bridge command
  `multiboard_document` (status / close / open, kind `SdDoc`) so the file is only written while
  Altium does not hold it (refused with unsaved edits); backup in
  `C:\Users\Public\altium_mcp\backups`. `get_screenshot(view_type="mbs")` captures a `.MbsDoc`;
  `get_multiboard_data(include_geometry=True)` lists entry points, junctions and segments.
- Format facts used: ids are max + 1 per space on load (logical / drawing); lines run between
  connect positions (entry position + direction x gridSize); Python `ZipInfo()` turns `\` into
  `/` in member names on Windows (set `.filename` afterwards). Verified live on scratch copies:
  Altium loads the result, and an Altium re-save keeps every added object (it only renumbers
  system parameters and styles). Then applied to `P42C-1_System.MbsDoc` (MOD_5 J3/J4 swap,
  H_1.Entry_4 -> H_5 inline DTM04-6P / DTM06-6S): 0 errors, all 82 traced nets closed.

### Session 9 (2026-10-22): limitations found while wiring a real harness (no code change)
Three tool limitations, details in §6 "Multi-part connectors", "Cavity parts and ERC",
"Checker and duplicate designators"; fixes in §8.

### Session 8 (2026-10-22): router replaced by a checker
- Twists and shields are found by designator (`FindHarnessByChildDesignator`), so resending one
  with its designator moves / resizes it. Verified on scratch `twist\MCP_Test_Twist.WirDoc`.
- `reroute_harness_wiring` and `harness_router.py` removed (user decision): on every real sheet its
  dry run was worse than the routing in place (16 / 41 wire stretches drawn on top of each other on
  two sense sheets, 6 crossings vs 1 and 7 segments through connectors on a comm sheet).
  New read-only `check_harness_wiring` (`harness_check.py`, also home of `label_positions`);
  parser reports visible wire label positions (`designator_pos` / `comment_pos`). Routing is done
  by the agent following the workspace skill `altium-harness-routing`.
  Tests: 11 new in `test_harness_check.py`; 106 passed, same 15 old failures.
- Twist style (user's correction): rectangle 100 mils beyond each wire so the hook tips land on
  the wires, 500 mils along (vertical pair `rotation: 1`). `check_harness_wiring` checks the style
  and twists on labels.

### Session 7 (2026-10-22): text placement
- `edit_harness_layout` takes `texts` on connectors, connection points, bundles, coverings and
  layout labels: {name: [x, y] or {x, y, hidden}} (mils, bottom-left of the text; names as
  Altium's script names them: Designator, Comment, Length, any parameter, any case). New
  `TEXT|owner ref|name|x|y|hidden` record (`PlaceChildText` in `schematic_utils.pas`: children
  of ObjectId 25 + `eParameter` matched by `Name`), emitted after all other records; result
  count `texts_placed`. `parse_ldrdoc` / `get_harness_data` report each object's `texts`
  (a bundle's `LengthParameter` on disk is reported as `Length`). 2 new tests (104 passed, same
  15 old failures). Verified on the scratch copy `txt\MCP_Test_Txt` (positions survive save +
  close/reopen), then on a real drawing.
- `get_screenshot` once returned the same stale frame three times (title changed, pixels not);
  it recovered by itself. A view-only sandbox `RunProcess('Sch:Zoom')` with `Action=Document`
  shows more of the sheet for a screenshot.

### Session 6 (2026-10-22): wires routed in Altium, render crash fixed (committed `50148ce`)
- **User report: bundles showed no wires in Altium.** Cause: the connection point stream names,
  per connector held, the UniqueIDs of its pins, and Altium maps wire ends to connection points
  only through those (`HarnessBundlesContentCollector`). Altium's own import and every update
  from the library leave ids of pins that no longer exist -> no wire routed, bundles empty, wire
  lengths 0. Our parser had routed by connector, so it hid it.
- Fix: Pascal `ConnectorPinIds` + `SyncConnectionPointPins(Doc, Release)` (drop stale ids; an
  entry left empty gets all the connector's pins unless another CP holds some); runs in every
  `edit_harness_layout` call (`connection_point_pins_synced`); a call with only
  `document_path` runs when pins are unassigned. `read_connection_point_streams` returns
  `pins`; `parse_ldrdoc` routes by pin (`pin_holder`) and reports connectors'
  `unassigned_pins`; `check_layout` flags wired pins held by no CP.
- Verified in Altium: `Doc.GetBundleContent(B).GetSubLinesContentCount` per bundle = parser
  (4/22/37/10/8/9/9/12), and all 40 wires' `GetState_TotalLength` = parser `length` (incl. manual
  bundle length and fixed wire length).
- **Fabrication drawing:** Draftsman's wiring list (decompiled `Altium.Designer.PcbDrawing.dll`,
  Draftsman extension folder under ProgramData) shows the BOM "Length" parameter, which Altium
  builds from the routed total (`HarnessComponentBridgeToLine`) and which ignores a "Length"
  parameter on the wire.
- **Render crash (the .NET error):** mapped the dialog address (`Altium.Sch.DataModel.dll` + 0x28B842)
  through the ReadyToRun runtime-function table (scratch `r2rmap.py`, `pefile` + `dnfile`) to
  `SchHarnessComponent.GetState_OwnBoundingRectangle`: a main view that is shown but has no
  picture yet. Once pins were held (wires routed) Altium asks for it; a file saved so even
  crashes on opening. Fix: a new / unrendered view is created and kept **hidden** until it is
  rendered; the render is split into separate bridge runs (edits + save -> `RELOADPROJECT` ->
  `RENDER|uid` (refresh + show) -> a run with no records (pin repair)), orchestrated in
  `main.edit_harness_layout`; the connector to render holds no pins until the last run.
  Verified live: P2 -> physical in one call, no dialog, project reload from disk clean.
- `_blocking_altium_dialogs` worked live twice (reported the .NET "Error" instead of hanging).
- Tests: 102 passed, 7 skipped, same 15 pre-existing failures (new: pin check, repair-only
  call, nothing-to-change, 4-run render).
- Scratch: `ldr\s6_crash_on_open.LdrDoc.bak` (the file that crashed on opening),
  `ldr\s7_after_render.LdrDoc.bak` (good, current), `r2rmap.py <dll> <rva hex>`, `dwf\`
  (decompiled Draftsman), probes `pins1`, `route1-3`, `prjclose`, `prjopen`.

### Session 5 (2026-10-21): .NET error fixed, views + wire lengths finished (committed `50148ce`)
- **Cause of the .NET error:** the VIEW record removed image parameters with `RemoveSchObject`.
  The connector caches its main view (`SchDataHarnessComponent.GetMainPhysicalModel`, reset only
  by Altium's own `RemovePhysicalModel` or a load from disk), so the cache pointed at a removed
  parameter. Decompiled GUI code (`SchHarnessPhysicalModelsCollectionViewModel.VisibleModelType`)
  shows Altium's Symbol / Physical switch never removes views: it only sets `IsHidden` on all of
  them. A hidden main view = symbol drawn (`SchDataIteratorFilterCurrentPartPrimitives`).
- **Fix (Pascal VIEW):** symbol = `IsHidden := True` on every image parameter; physical =
  `IsHidden := False` on all, a new parameter only when there is none; the project reload +
  library refresh only for field 4 = `1` (no rendered main view yet) or a new parameter. Object
  references are set to nil before the project closes. Record: `VIEW|uid|physical|symbol|render`.
- Python: `parse_ldrdoc` views get `hidden` (`IsHidden=T`); helpers `_rendered_main_view`,
  `_shows_physical` used by the builder and `check_layout`.
- **Modal-dialog guard (`main.py`):** `_blocking_altium_dialogs()` (Altium's main window is
  disabled while a modal window is up; closable boxes are dismissed first, then the titles of
  what stays are returned) is checked before the bridge writes a request, before a sandbox run
  and in `_recover_script_executor` (no `X2.EXE` launched; `executor_wedged: true` + "ask the
  user" text with the titles). Verified live that it returns [] when Altium is free.
- Tests: 10 new (`TestViewsAndWireLengths` 7, recovery 3). Docs: `wirdoc-format.md` (view / wire
  data rows + "Connector views"), dev skill §5 (guard).
- **Verified live on the scratch project** (each read back, no dialog after any call): P1 ->
  symbol (hidden, no reload) and back to physical; then one call J1 -> symbol + P2 -> physical
  (new view: project reload + render) + W1 600 mm + W2 calculated + B1 300 mm manual: `success`,
  `reloaded`, P2 rendered, all 8 bundles connected, 0 unrouted wires.
- Seen, not investigated: the Projects panel lists `Sandbox.PrjScr` several times (one more
  entry after sandbox runs / project reloads).

### Session 4 (2026-10-21): connector views + wire lengths (finished in session 5)
User request: "find a way to change the connector view to physical (we made footprints with 3D
models; default is the symbol view), same for wire length offsets or any missing LdrDoc feature;
reverse-engineer / disassemble Altium DLLs if needed". Findings in §6 "Physical views and wire
lengths":
- `edit_harness_layout(connectors=[{"designator", "view": "physical"|"symbol"}],
  wires=[{"designator", "length_mm": number|None}])`. Builder (`harness_layout.build_layout_spec`):
  `VIEW|connector uid|physical|symbol` (only when the view differs), `WIRELEN|wire data uid|coord`
  (0 = calculated); CONNECTOR records only when x/y/orientation/mirrored given;
  `expect.views` / `expect.wire_lengths` checked by `check_layout`.
- Pascal (`schematic_utils.pas`): helpers `ProjectPathOfDocument`, `UnsavedProjectDocuments`,
  `RefreshFromLibrary` above `ExecuteEditHarnessLayout`; WIRELEN = `SetState_Length` on the wire
  data (ObjectId 90; `FindLayoutObject` includes 90). Physical views added: refuse when a project
  document is unsaved (`physical_view_not_rendered:` in not_found), else close the **project** +
  reopen it + reopen the drawing, `RefreshFromLibrary` each such connector, save (`reloaded`).
  Result adds `views_changed`, `wire_lengths_set`.
- `parse_ldrdoc`: wires get `length_type` (calculated / user / mcad), `fixed_length`,
  `user_length`, `length_offset`, `via_bundles` (manual path constraints) and `length` (total per
  type); connector views get `image` (box of the rendered picture, None = not rendered).
- First live run saved the right result but hit a .NET "Object reference not set" dialog; the
  script hung behind it ~3.5 h until the user dismissed it. Fixed in session 5.

### Session 3 (2026-10-21), committed in `d071658`: `edit_harness_layout`
- **`edit_harness_layout` MCP tool** (`main.py`): connectors (move / orientation deg / mirror; their
  own CP follows with the turned offset, bundles on it follow), connection_points (new / move /
  style / show_name / orientation / rename / `objects` assign / `release`, designator placed),
  bundles (`from`/`to` CP or connector + `via`, or `points`; `length_mm` number or None = auto;
  break symbol, line width, comment; new ones auto-named B<n>), coverings (bundles, part, library,
  thickness, start/end offsets mm), layout_labels, delete, dry_run. Spec builder
  `harness_layout.build_layout_spec` (records + errors + warnings + expected bundle ends), check
  `check_layout` (expected ends, and bundles that LOST a connection), result has `bundles`,
  `problems`, `unrouted_wires`, `newly_unrouted_wires`.
- Pascal `ExecuteEditHarnessLayout` + helpers `ChildDesignatorText`, `FindLayoutObject`
  (uid or `DES:<designator>`), `PlaceChildDesignator`, `SetBundleVertices` at the end of
  `schematic_utils.pas`; `case` entry in `Altium_API.pas`; skip-list entry in `other_utils.pas`.
  Saves, then close + reopen + save when geometry changed (`reloaded`).
- `get_harness_data` on `.LdrDoc` returns `parse_ldrdoc` output (designators / summary_only).
- Tests `server/tests/test_harness_layout.py` (17). Docs: `references/wirdoc-format.md`
  "Layout drawings" section.
- One bug found and fixed on the way (a bundle named only for a property change did not follow
  its moved CP and got disconnected).

### Earlier sessions (committed in `bc7853f` / `249e925` and before, condensed)
- Rework: honest results, parameter upsert, `document_path` required, `dblib.py` +
  `sync_harness_library`, `get_sheet_style`, bridge lock.
- WirDoc features: `add_harness_wiring(cavity_parts, associated_parts, twists, shields, cables,
  wire_breaks, label_updates, relink, designator_updates)`; `get_harness_data` returns all of them.
- **Automatic script-engine recovery** (`server/main.py`): `_recover_script_executor()` =
  `X2.EXE -REditScript:Stop` -> ping -> Ctrl+F3 injected into the Altium main window (scan codes;
  virtual-key-only input is ignored by Altium) -> ping; `executor_wedged: true` + "ask the user"
  text only if both fail. Wired into `_run_sandbox` (failure paths; `recover=False` for the ping)
  and into the production bridge timeout. Sandbox stops waiting ~3 s after an Altium "Error" box.
  Dialog texts of Win32 `#32770` dialogs reported as `dismissed_dialog_texts` (Altium's themed
  `TMessageForm` exposes no text). Helpers `_dialog_text`, `_altium_main_window`, `_send_ctrl_f3`,
  `_script_engine_responds`. Tests `server/tests/test_script_recovery.py`. The user never develops
  Altium scripts while an agent works, so Ctrl+F3 is OK.
- **Layout drawings open/screenshot**: `other_utils.pas` `SchDocumentKind(path)`
  ('HarnessWiringDiagram' / 'HarnessLayoutDrawing' / 'SCH') + `IsHarnessDocumentKind`; used by
  `EnsureDocumentFocused`, the screenshot path in `Altium_API.pas` and the zoom check.
  `get_screenshot(view_type="sch", document_path=<.LdrDoc>)` works.
- `call_tool.py` saves image results to `%TEMP%\altium_mcp_<tool>_<ns>.png` and prints the path.
- **Pre-existing test failures (15):** they read a real WirDoc of the development workspace that
  has since changed. Fix the tests to use synthetic records (`parse_wirdoc_records`).

## 4. Key decisions and why
- **DB is the single source of truth**; parameters written by `sync_harness_library`, never typed.
- **Edits move objects in place** (keep UniqueIDs): the `.LdrDoc` links every object to its WirDoc
  original by UniqueID (`WiringDiagramOriginUniqueId`). Never recreate WirDoc wires.
- **No automatic router** (session 8): the agent routes by the drawing style, `check_harness_wiring`
  proves it.
- Layout tool design: coordinates in **mils** (as `add_harness_wiring`); lengths in **mm**
  (`length_mm`, offsets `start_offset_mm`/`end_offset_mm`); bundles may be given as `from`/`to`
  connection-point designators + optional `via` points (Python builds the exact end points from CP
  locations, so ends land exactly on the CPs); Python resolves designators -> UniqueIDs from the
  saved file and sends UIDs to Pascal; save, then **close + reopen + save** (bundle/CP links are
  reliable only after a reload, see §6), then verify by `parse_ldrdoc` and screenshot.
- Wire routing/lengths are computed in Python (Altium's `GetBundleContent` is unusable from
  DelphiScript, §5). Manually re-assigned wire paths in Altium are not detected - say so in docs.
- Wire lengths come from bundle lengths (the BOM / Draftsman length is Altium's routed total);
  fixed per-wire lengths only on request.
- Single Altium instance kept (user decision); a second install would need its own license seat.
- Working alongside the user: announce "using Altium now" / "Altium free" around Altium test bursts
  (scripts steal tab focus and close Error/Warning/Confirm dialogs while running).

## 5. Approaches that failed (don't retry)
- WirDoc: GUI "Replace Component" for wires; cavity "Edit" pencil; `Internal_GetState_*`,
  `ShieldPinLocation`, `Obj.Designator` on twists/shields, `DatabaseTableName` on cables.
- **Automatic router** (`reroute_harness_wiring`, removed session 8): stacked wires on shared
  channels, ran channels through connector pins, looped same-side wires through bodies.
- **LdrDoc (session 3):**
  - `Doc.GetBundleContent` without argument: runtime error. `Doc.GetBundleContent(Bundle)` returned
    an object with 0 sub-lines for a bundle carrying no wires, and **crashed** (even `if X = nil`)
    for a bundle carrying wires. Don't use it; compute routes in Python.
  - Covering `SetState_StartPointDistance` / `GetState_StartPointDistance` / `SetStartPoint` /
    `SetEndPoint` / `VerticesCount`: runtime errors (not in the script interface). Use
    `SetState_PhysicalStartPointDistance` / `...End...` inside `BeginUpdate` / `EndUpdate`.
  - `CP.Designator` (connection point): runtime error; use the child designator (ObjectId 25).
  - Ctrl+C on Altium's `TMessageForm` to read its text: copies nothing.
  - `keybd_event` Ctrl+F3 without scan codes: ignored by Altium.
  - `TStringList.AddObject(uid, Obj)` then `List.Objects[i]`: pauses the script; keep objects in
    the sandbox `Obj*` variables or act inside the iterator loop.
- **LdrDoc session 4:**
  - Calling `SCHInterfaces` methods from DelphiScript (`Conn.GetMainPhysicalModel`,
    `UpdatePhysicalModel`, `AddImageParameter`, `SetState_IsMainModel`): runtime error. Only the
    `Rt_Schematic.ISch_*` members are callable, even though the objects implement the others.
  - A new image parameter is never the main model by itself (`IsMainModel` defaults to False and
    its text is re-written from the data fields at every save, so `Text := '...MainModel=True'`
    is useless). Closing + reopening the **document** does not reload it from disk (no import
    runs); only closing + reopening the **project** does.
  - Wire `LengthOffset`, `UserLength` / length type "user", `BundlesToGoThroughUniqueIds`: no
    script setter anywhere (wire data `ISch_HarnessWireData` has only `SetState_Length`).
  - `X2.EXE -REditScript:Stop` while Altium shows a modal .NET error: a second X2 starts and shows
    "There is another instance of running, but it is busy" - recovery then waits on that dialog.
- **LdrDoc session 6:**
  - Routing wires by connector (the parser before session 6): Altium routes by the pin ids a
    connection point lists, so the parser reported routes Altium did not have.
  - New view + project reload + library update in **one** script run: .NET error. Rendering a
    connector whose wires are routed while its new main view is shown without a picture: .NET
    error (`SchHarnessComponent.GetState_OwnBoundingRectangle`), and a file saved in that state
    crashes Altium on opening. Keep new views hidden until rendered; split into runs.
  - Shell heredocs for patch scripts: the shell ate backslashes (`'\'` in Pascal anchors,
    `\b` in paths). Write such scripts with the Write tool.
- Sandbox: only `S1..S3`, `I1..I3`, `B1`, `Obj1..Obj5`, `List1`, `IntMan`, `DbDoc` exist (an
  `Obj6` = undeclared-identifier runtime pause). `GetFieldFromPipeString` not available.
- `pdftoppm` missing; render PDF pages with `uv run --with pymupdf python -I -c "..."`.

## 6. Gotchas / non-obvious findings

### General
- **One Altium operation at a time.** Recovery is automatic; if a result still says
  `executor_wedged: true`, stop and ask the user.
- **DelphiScript: define a function above its first caller in the same unit** (calling one defined
  further down pauses at runtime). Units in `Altium_API.PrjScr` order: ... `other_utils.pas` (Doc3),
  `schematic_utils.pas` (Doc4), `Altium_API.pas` (Doc5); later units may call earlier ones.
- `.pas` files are CRLF in the working tree (git `i/lf w/crlf`); edit with tools that keep CRLF.
- **Never `Client.OpenDocument` an open doc** (reloads from disk); `FindOpenDocument` first.
- Coordinates: files in DXP units (1 = 10 mils); write tools take mils; internal coord = 10000/mil;
  lengths (`LengthLong`, `Physical*`) are internal coords: mm = coord / 10000 * 0.0254.
- Bash heredoc + `< /dev/null` on the same python command: stdin goes to /dev/null and python starts
  a REPL (hangs). Write scripts to files in the scratchpad instead.
- **Multi-part connectors (found session 9 on an MTA 0301640 fuse holder, symbol
  `FUSE_HOLDER_2x10_MINIVAL`, parts A = sockets 1-10, B = 11-20; fixed session 12).** Each placed
  part is its own component object (own UniqueID) carrying *all 40 pins*; only the pins whose
  `OwnerPartId` = the part's `CurrentPartId` are visible, the others sit hidden at the same
  relative positions (a hidden pin can share a visible pin's hot end). Every lookup by
  designator + pin must use the part showing the pin (`FindComponentWithPin`); renaming one part
  must rename all (`SiblingParts`). Before session 12 cavity parts landed on hidden pins and sync
  stacked them there (no rename workaround needed any more).
- **Cavity parts, wire parameters and ERC:** what `sync_harness_library` writes is saved, but
  Altium's compiler (`get_erc_violations`) keeps the old state ("Unspecified connector cavities -
  no part choice for active pins", "Mismatched parameters in connected wire segments") until the
  document is reloaded; `add_harness_wiring(relink=True)` reloads it, and sync now does so
  itself (session 12). A connector modelled as a single part (ring lugs, PartNumber on the lug itself)
  always gets this warning when wired: its one cavity is empty by design (adding the lug as crimp
  would count it twice in the BOM).
- **Checker and duplicate designators:** `check_harness_wiring` keys wires by designator, so two
  unannotated `W?` wires merge: a twist on such a pair reports wrong hook positions and a label
  "off its wire". Annotate the wires before trusting those warnings.

### `.LdrDoc` format and API (verified 2026-10-21 on a scratch copy, Altium 26.7.1)
- Open kind: `Client.OpenDocument('HarnessLayoutDrawing', path)`; `SchServer.GetSchDocumentByPath`
  works, document `ObjectId = 80`. Open the scratch **project** first (`WorkspaceManager:OpenObject`,
  `ObjectKind=Project`) so the LdrDoc has its wiring diagram.
- OLE streams: `FileHeader` (records), `HarnessConnectionPointConnector` (zlib after a header:
  int32 version, int32 CP count, per CP: pstr uid, int32 n, per object: pstr uid, int32 pin count,
  pin ids; pstr = length byte + latin-1 text; the pin ids are not referenced anywhere else),
  `Storage` (embedded images/models), `Additional`.

| RECORD | API ObjectId | Object | Key fields |
|---|---|---|---|
| 106 (top-level) | 81 | connector | `Location`, `Orientation` (0-3), `IsMirrored`, `WiringDiagramOriginUniqueId`, `DesignItemId`; child 41 `LayoutDrawingSymbolModel<n>` with `IsImageParameter=T` and `Text` = `ViewName=..|ViewSide=eTopView|Rotation=eRotate0|Zoom=1|Width|Height|MainModel=True|ViewType=ePhysicalView|...` (its own `Location`) + child RECORD 30 image |
| 113 | - | pin | `WiringDiagramOriginUniqueId`, `ConnectedWireUniqueId<n>` |
| 110 | 85 | connection point | `Location`, `Orientation`, `Style` (0 circle, 1 square, 2 insulator), `ShowName`, `Text=CP1`, `ConnectedBundleUniqueId<n>`; designator = child 34 (`CP_J1` for a connector's own CP, `CP1` for a new one) |
| 111 | 86 | bundle | `LocationCount`, `X1..`/`Y1..`, `LineWidth`, `Length` (DXP), `LengthLong`, `DrawnLength`, `IsLengthSetManually`, `ShowBreakSymbol`, `EndVertex1/2ConnectedObjectUniqueID` (= CP uids); children 34 Designator, 41 Comment / Description / `LengthParameter` |
| 128 | 104 | covering | `Thickness`, `BorderWidth`, `AreaColor`, `Transparent`, `Length`, `PhysicalStartDistance`, `PhysicalEndDistance`, `PhysicalLength`, `CoveredItemsCount`, `CoveredItemType=86`, `CoveredItemId<i>` (0-based), `DesignItemId`; child 34 designator |
| 109 | 84 | layout label | `Location`, `Text`, `Alignment`, `TextColor`, `AreaColor`, `DesignItemId`; children 34 designator (auto `L1`), 41 Comment |
| 115 | 90 (eHarnessWireData) | wire (no geometry) | `Name`, `Comment`, `Description`, `Color`, `DesignItemId`, `EndVertex1/2ConnectedObjectUniqueID` (layout pin / splice uids), `ConnectedTwistUniqueId<n>`; when not default: `LengthType` (0 calculated, 1 user, 2 MCAD), `Length`/`LengthLong`, `UserLength`, `LengthOffset`, `BundlesToGoThroughUniqueIdsCount`/`BundleToGoThroughUniqueId<n>` |
| 41 + `IsImageParameter=T` | 100 (eImageParameter) | connector view | child of the connector; name `LayoutDrawingSymbolModel<n>`; child RECORD 30 = rendered picture (`EmbedImage=T`, bitmap in `Storage`); no library path / model name stored (resolved from the connector's PCBLIB implementation at render time) |
| 116 / 120 / 122 | ? | splice / no-connect / twist | `Designator`, `WiringDiagramOriginUniqueId`, `ConnectedWireUniqueId<n>`; no location (they live in connection points) |
| 105 / 39 / 28 / 30 | - | sheet header / template (`.LdrDot`) / text frame / image | template objects are owned by the RECORD 39 |

- Every imported connector gets its own connection point (CP_<designator>) holding it and all its
  pins. **Splices are in no CP after import** -> wires to splices stay unrouted until a CP holds the
  splice (`CP.AddConnector(spliceUid)` works, persisted in the stream).
- **Bundle creation (works):** `SchObjectFactory(86, 1)`, `Location`, `VerticesCount`,
  `Vertex[i] := Point(MilsToCoord(x), MilsToCoord(y))`, register + `SCHM_PrimitiveRegistration`.
  Ends must sit exactly on CP locations. End connections + length were live for the first two
  bundles, but one bundle (designator set via `Obj.Designator.Text` **before** registering) saved
  unconnected with length 0 and only connected after close + reopen -> always set designators after
  registering and reload before verifying.
- Bundle props (work): `Obj.Designator.Text`, `Obj.Comment.Text`, `SetState_IsLengthSetManually`,
  `SetState_Length(coord)`, `SetState_ShowBreakSymbol`, `LineWidth`, `GetState_Length`,
  `GetState_DrawnLength`, `GetState_EndVertex1/2ConnectedConnectionPointUniqueId`.
- **CP (works):** factory(85), `Location`, `SetState_Style(n)`, `GetState_ConnectorsCount`,
  `GetState_Connector(i).GetState_UniqueId` / `.GetState_PinsCount`, `AddConnector(uid)`
  (tested with a splice uid), `RemoveConnector(uid)` (call reached; result not yet read back),
  `GetState_ConnectedBundlesCount`. A CP dropped on a bundle splits it (GUI behaviour).
- **Covering (works):** `F := SchObjectFactory(104, 1); F.AddBundleItem(Bundle)` (also
  `AddConnectionPointItem(CP)`, `AddComponentItem(Comp, FirstPin, LastPin)`, `RemoveAllItems`),
  register, then `BeginUpdate; SetState_PhysicalStartPointDistance(c); SetState_PhysicalEndPointDistance(c);
  SetState_Thickness(n); EndUpdate`; `DesignItemId`; `Designator` property works. Semantics:
  `PhysicalLength = covered length + start + end` -> **negative offset = inset from that end,
  positive = extends past it** (verified visually: -1000/-2000 mils inset the drawn covering).
- **Layout label (works):** factory(84), `Location`, `Text`, register, `Designator.Text`,
  `DesignItemId`, `Orientation`, `GetState_ShowOnlyFirstLine` / `SetState_ShowOnlyFirstLine`,
  `SetState_Alignment`, `SetState_TextColor`, `SetState_ComponentKind`.
- Script-facing interfaces are `Rt_Schematic.ISch_Harness*` in
  `C:\Program Files\Altium\ADAgile\System\Altium.Edp.Interfaces.dll` (not the `SCH.*` SDK ones, not
  `SCHInterfaces.*`). Dump members / signatures with the scratch scripts in §7
  (`dump_meta.ps1 <dll> '<type regex>' -Members`, `sig.ps1 <dll> '<type regex>' '<method regex>'`;
  ECMA type codes: 08 = I4, 0a = I8, 05 = U1, 02 = bool, 0e = string).
- Session 3 probes: connector `MoveToXY` + `Orientation` move its views but put its CP somewhere
  unrelated (dashed line) -> tool places the CP; CP `Location :=` leaves its designator behind,
  `MoveToXY` moves it; moving a CP does NOT move bundle vertices, and on reload a bundle end off
  its CP is disconnected (links are positional, computed on load); `RemoveConnector` and
  `RemoveSchObject` on a bundle work; `SourceLibraryName` works on coverings and labels;
  `TStringList.AddObject` with interfaces cannot be read back. Full list: `wirdoc-format.md`.
- Closing a layout drawing's tab and reopening it (`WorkspaceManager:CloseObject` on the file +
  `Client.OpenDocument`) does NOT re-import the file, but it did recompute bundle links (session
  3 relied on that and it worked). Closing + reopening the project re-imports.

### Physical views and wire lengths (session 4, from decompiling + live probes)
Decompiled with ILSpy (`ilspycmd` 9.0 NuGet package run with the installed .NET 8 runtime:
`dotnet <pkg>/tools/net8.0/any/ilspycmd.dll -p -o <out> <dll>`). Useful assemblies in
`C:\Program Files\Altium\ADAgile\System\`: `Altium.Sch.DataModel` (engine objects, file format
`FileFormatV5`, `PhysicalModelUtils`, `SchPhysicalModelsOwnerImplementation`, `FootprintRetriever`,
`SchDataImporterDocumentV5`), `Altium.Har.Core` (wiring -> layout sync), `Altium.Har.LayoutDrawing`
(server document: `FileSave` -> `UpdatePhysicalModels` only embeds model files),
`InteractiveProperties.Providers.Harness.*` (Properties panel), `Altium.Edp.Interfaces`
(`Rt_Schematic` = script interfaces, `SCHInterfaces` = internal, not scriptable).
- **How Altium draws a physical view:** a connector shows its symbol unless it has a main image
  parameter (`IsMainModel`); `UpdatePhysicalModel(param)` renders a bitmap of the footprint's 3D
  model (`PcbLibraryUtils.Create3DModelBitmap`, 50 x 50 mm box, rotated with the connector) into a
  child image and centres it on the connector. The footprint is found by `FootprintRetriever` from
  the connector's DbLib part + its current PCBLIB implementation.
- **Recipe that works from DelphiScript (verified on P2 of the scratch copy, picture checked):**
  1. `P := SchServer.SchObjectFactory(100, eCreate_Default); P.Name := 'LayoutDrawingSymbolModel1';
     P.IsHidden := False; Conn.AddSchObject(P)` + `SCHM_PrimitiveRegistration`; save.
  2. Close the project (`WorkspaceManager:CloseObject`, `ObjectKind=Project`, `PromptToSave=False`),
     reopen it (`WorkspaceManager:OpenObject`), open the drawing: the import
     (`SchDataImporterDocumentV5.UpdatePhysicalModelsAfterImport`) makes the first image parameter
     of a connector without a main one the main model.
  3. Update the connector from its library: `PN := Conn.DesignItemId; Conn.DesignItemId :=
     'MCP_TMP_REFRESH'; Conn.PreProcess_Import_FromUser; Conn.DesignItemId := PN;
     Conn.PostProcess_Import_FromUser` (`SchComponent.UpdateFromLibraryIfNecessary` updates when the
     part number changed since PreProcess; for harness components it ends in
     `FootprintRetriever.UpdateModels()` = renders every image parameter). Save.
  Without step 2 the picture is rendered but sits at the sheet origin and is not shown.
  Shortcut that also works: `Replicate` an existing main image parameter of another connector,
  add the copy, then step 3 (copies `IsMainModel=True`) - needs one physical connector already.
- **Side effects of step 3** (= Altium's own update from library): the connector's pins and
  symbol primitives in the layout get new UniqueIDs (pins keep `WiringDiagramOriginUniqueId` and
  wire links; wire routes, CP assignment and the CP stream pin ids were unchanged in the diff),
  `ComponentDescription` and DB parameters refreshed from the DbLib. If the part is not in its
  library the update fails and leaves the placeholder part number: `RefreshFromLibrary` restores it.
- **Symbol view:** hide every image parameter (`IsHidden := True`), as the GUI switch does;
  never remove one by script (stale main-model cache -> .NET NullReference dialog, session 5).
- **View options** (side top/bottom/front/..., zoom, rotation, line view `eLineView`): stored in
  the data fields, settable only through the GUI (the text is overwritten at save). Copying a
  parameter with `Replicate` carries them.
- **Wire lengths:** wire data objects (ObjectId 90) are iterable from the document;
  `SetState_Length(coord)` sets `Length` and `LengthType = eMCADCoDesigner` (2) = fixed total length
  (`GetTotalLength`: calculated -> routed + `LengthOffset`, user -> `UserLength`, MCAD -> `Length`);
  `SetState_Length(0)` -> calculated again. Verified live. `GetState_AutogeneratedLength` reads 0
  from a script (computed lazily by the GUI), so routed lengths stay computed in Python.
- **Other LdrDoc objects** placed by the GUI: notes (`eNote`) and text frames (plain schematic
  primitives; text frame = RECORD 28, note record not checked); tables belong to the Draftsman
  drawing (.HarDwf), not the LdrDoc. Not supported by the tool yet.

## 7. Files and purpose
`server/`:
- `main.py` - MCP tools (`add_harness_wiring` spec records via `_spec_line`; `_check_harness_links`;
  `_run_sandbox` + `_recover_script_executor` + `_blocking_altium_dialogs`; `get_harness_data`;
  `get_screenshot`; `edit_harness_layout` incl. the multi-run render sequence).
- `harness_layout.py` - `.LdrDoc` reader `parse_ldrdoc`, spec builder `build_layout_spec`, check
  `check_layout`, helpers `_follow_end`, `_wire_length`, `_shortest_route`.
- `harness_check.py` - `check_harness_wiring` and `label_positions`.
- `AltiumScript/schematic_utils.pas` - `ExecuteAddHarnessWiring` and helpers (`SpecField`,
  `UpsertSchParameter`, `PlacedDesignator`, `FindPlacedComponent`, `AddCavityPart`,
  `SetChildDesignator`, `FindHarnessObjectByUid`, ...); at the end the layout part:
  `ChildDesignatorText`, `FindLayoutObject`, `PlaceChildDesignator`, `PlaceChildText`,
  `SetBundleVertices`, `ProjectPathOfDocument`, `UnsavedProjectDocuments`, `RefreshFromLibrary`,
  `ConnectorPinIds`, `SyncConnectionPointPins`, `ExecuteEditHarnessLayout` (records incl. `VIEW`,
  `WIRELEN`, `RELOADPROJECT`, `RENDER`, `TEXT`; result `render_pending` drives the extra runs).
- `AltiumScript/Altium_API.pas` - `case CommandName of` dispatch (`'edit_harness_layout'`);
  `other_utils.pas` - `EnsureDocumentFocused` skip-list, `SchDocumentKind`, `FindOpenDocument`.
  `Altium_API.pas` / `other_utils.pas` are LF in the working tree (others CRLF) - keep each as is.
- `tests/` (`test_harness_parser.py` fixtures pattern, `test_script_recovery.py`,
  `test_harness_layout.py` = synthetic layouts + tool with mocked parser/bridge,
  `test_harness_check.py`).
- `.agents/skills/altium-mcp-development/` (`scripts/call_tool.py`, `scripts/wirdoc_diff.py`,
  `scripts/r2r_method_at.py` = .NET error dialog address -> method, `references/wirdoc-format.md`
  = file formats incl. layout drawings, views, connection point pins, Draftsman lengths).

Scratch of the development sessions (session scratchpad, not in git; may be gone - recreate if
needed)
`C:\Users\Felix\AppData\Local\Temp\claude\c--evt-gits-electrical-p42c-1\4927401e-1e9d-4146-9d3d-0abdf0020ac1\scratchpad\`:
- `ldr\MCP_Test_Ldr.{PrjHar,WirDoc,LdrDoc,BomDoc,HarDwf}` - renamed scratch copy of a real harness
  project (PrjHar made with `sed 's/<original name>/MCP_Test_Ldr/g'`; no other file references the
  names); `MCP_Test_Ldr.orig.LdrDoc` = untouched import. Backups of states: `ldr\*.LdrDoc.bak`
  (`t1_after`, `t2_after`, `v_before`, `v4_replicated`, `v5_physical`,
  `s6_crash_on_open` = a file that crashes Altium on opening, `s7_after_render` = current good
  layout: pins repaired, all wires routed, J1/P2 physical, W1 600 mm / W2 700 mm fixed, B1 300 mm
  manual). It is open in Altium inside its own project `MCP_Test_Ldr.PrjHar`.
- `txt\MCP_Test_Txt`, `twist\MCP_Test_Twist.WirDoc` - scratch copies for sessions 7-8.
- `sb.py <script.pas> [timeout]` runs a sandbox file (`main._run_sandbox`); `probe\*.pas` = the
  session 3-6 probes (`open.inc` = open scratch project + drawing into `Obj1`/`Obj2`;
  `v2`-`v5` = physical view steps, `w1` = wire length, `prjreload`, `close`, `reload`,
  `pins1`, `route1`-`route3`, `prjclose`, `prjopen`).
- `re2\strsearch.py <terms>` (string search in all Altium binaries, ASCII + UTF-16),
  `re2\imgparams.py <ldrdoc>` (image parameters + their image child per connector),
  `re2\subtree.py <before> <after> <uid>` (record diff of one object's subtree + wires/CP check),
  `re2\impl.py <ldrdoc> <conn uid>` (PCBLIB implementation records + CP stream pin ids),
  `r2rmap.py <dll> <rva hex>`.
- `ilspy\pkg\tools\net8.0\any\ilspycmd.dll` (decompiler) and `decomp\<assembly>\` (decompiled C#
  of the assemblies listed in §6), `dwf\` (decompiled Draftsman) - grep these instead of
  guessing APIs; recreate per the dev skill §6 if the scratchpad is gone.
- `ldr_counts.py`, `ldr_dump.py <file> <records,csv> [limit]`, `ldr_range.py <file> <a> <b>`,
  `ldr_test.py <file>` (prints `parse_ldrdoc`), `cpc.py` (stream decode), `uidfind.py`, `ping.py`,
  `err.json`; `re\` (SDK dumps, `dump_meta.ps1`, `params2.ps1`, `sig.ps1`, `rt_harness.txt` = all
  `Rt_Schematic.ISch_Harness*` members).

## 8. Next steps
Announce "using Altium now" / "Altium free" around live tests; `_blocking_altium_dialogs()`
(or a desktop screenshot) tells whether an Altium modal window is up. Test each change on a
scratch copy first.
1. Fixes from session 9 (§6); part-aware lookup, parser parts and sync reload done in session 12:
   - `CONNECTOR` (move) and `ASSOCPART` still take the first part object of a multi-part
     connector (no way yet to name the part, e.g. `FH1:B`).
   - `check_harness_wiring`: key wires by UniqueID, not designator (unannotated `W?` wires merge).
   - `check_harness_wiring`: parse the sheet rectangles (RECORD 14, the connector frames) and
     report labels and channels inside another connector's frame (today only the screenshot shows
     them).
   - `get_screenshot(view_type="sch")` on a harness sheet: optional zoom (whole sheet via
     `Sch:Zoom Action=Document`, or an area), so the agent can see every part of a large sheet.
2. Fix the 15 pre-existing test failures (synthetic records instead of a real WirDoc).
3. Open question: should view options (side, zoom) be copied from a template connector?
4. Not supported yet: LdrDoc notes / text frames; wire `LengthOffset` / user length / manual wire
   paths (no script setter, §5).

## 9. How to verify
```powershell
# unit tests (altium-mcp root) - 106 passed, 7 skipped, 15 pre-existing failures (§3)
cd C:\evt_gits\electrical-p42c-1\altium-mcp
uv run --with pytest python -m pytest server/tests -q
uvx ruff check --extend-select I server/harness_layout.py server/harness_parser.py server/harness_check.py

# call working-tree tool code directly (the MCP daemon keeps old code until the IDE restarts)
$py = "C:\Users\Felix\.altium-mcp\venvs\1459a62ba579\Scripts\python.exe"
& $py .agents/skills/altium-mcp-development/scripts/call_tool.py get_screenshot '{"view_type": "sch", "document_path": "<path>.LdrDoc"}'
& $py .agents/skills/altium-mcp-development/scripts/call_tool.py edit_harness_layout '@kwargs.json'
# read a layout drawing (get_harness_data on .LdrDoc does the same through the MCP)
& $py -c "import sys, json; sys.path.insert(0, 'server'); import harness_layout as hl; print(json.dumps(hl.parse_ldrdoc(r'<path>.LdrDoc')['summary']))"
```
Live checks: `success: true`, `saved: true`, empty `not_found`; reread with `parse_ldrdoc`;
screenshot; `git status`; close scratch documents in Altium (`WorkspaceManager:CloseObject`,
`PromptToSave=False`).
