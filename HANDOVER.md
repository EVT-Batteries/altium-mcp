# Handover: altium-mcp harness support

Updated 2026-10-30 (session 16: `import_harness_changes`; session 15: Draftsman harness
drawings, `.HarDwf`) for a fresh agent session.
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
`check_harness_wiring`, `get_harness_data`), the layout-drawing tool (`edit_harness_layout`:
connectors, views, connection points, bundles, coverings, labels, texts, fixed wire lengths,
connection point pin repair, crash-safe view rendering) and the multi-board schematic tools
(`get_multiboard_data`, `edit_multiboard_schematic`) are done and committed. Since session 13
`add_harness_wiring` also copies placed connectors, frames, texts, notes and pictures from
another wiring diagram (`copy_objects`): the way to add a connector by script, since Altium
cannot place a harness connector from a desktop DbLib. Since session 14 `edit_harness_layout`
places texts bottom-left unless given a `justification`, `show_name` really hides a connection
point's name, and unannotated objects (`B?`, `CP?`) are addressed and checked by UniqueID.
Since session 15 (uncommitted) `get_harness_drawing_data` / `edit_harness_drawing` review and
edit Draftsman harness drawings (`.HarDwf`) on the saved file, including the stored
projections that harness component views draw. Open work: §8.

## 2. State at hand-over

| Path | Branch | HEAD | Uncommitted |
|---|---|---|---|
| `altium-mcp/` | `feature/harness-support` | `1a93e86` (sessions 10-16 committed) | 2026-10-30 doc pass: README tool list, `references/wirdoc-format.md`, this file |

Commits: session 10 multi-board `81280f9`, upstream 0.5.1 merge repair `0bdbf41`, layout CPs
holding no-connects `8681502`, session 11 `aedbea7`, session 12 `e2e1203`, session 13
`a29063e`, session 14 `1fa3dfe`, session 15 `15653b2`..`5b1f71e`, session 16 `1a93e86`. Unit
tests (2026-10-29): 192
passed, 7 skipped, 13 pre-existing failures (§8 item 2).

The user commits themselves. **Do not commit or push unless asked.** Test on renamed scratch copies
of real documents first; files the user is editing are not ours.

## 3. History (newest first)

### Session 16 (2026-10-29/30): `import_harness_changes` (`1a93e86`)
Asked: run Altium's "Import Changes From ..." for layout and Draftsman drawings (the three HVIL
layouts had been imported before their wires were synced: no wire part in the layout, so none
in the drawings' BOM / wiring list).
- **Altium's commands** (decompiled `Altium.Har.LayoutDrawing` / `Altium.Har.Core`, Draftsman
  `.rcs`): layout `Sch:ImportChangesFromWiringDiagram` (`HarnessLayoutDrawingServerDocument.
  ImportChangesFromWiringDiagram` -> `HarnessChangesWatcher.SynchroniseWiringDiagramToLayoutDrawing`
  -> `WiringLayoutSynchronizer`, applied directly, no ECO dialog; updates wires in place, same
  UniqueIDs); Draftsman `PcbDrawing:UpdateBoard` ("Import Changes From <layout>", self-closing
  progress form; stored component projections survive, tables grow down from their top edge).
- **Tool** `import_harness_changes(document_path)`: `.LdrDoc`, `.HarDwf` or `.PrjHar` (layouts
  then drawings). Bridge command `import_harness_changes` = read-only check (kind, open project,
  no unsaved document in the project, was_open); then three standalone scripts through
  `run_altium_script` (`_OPEN_SCRIPT` / `_IMPORT_SCRIPT` / `_SAVE_SCRIPT` on `_FOCUS_PRELUDE`:
  open a closed document with `DM_OpenAndFocusDocument`, show an open one, check document and
  project focus). A layout is then compared with the saved wiring diagrams
  (`_layout_sync_gaps`: missing wires, other part) and imported again after a longer wait while
  they differ (`HARNESS_IMPORT_WAITS`); pin repair via `edit_harness_layout` when a connector
  has unassigned pins; documents that were not open are closed again. Report:
  `_layout_import_report` / `_drawing_import_report`.
- **Findings** (also in the dev skill, §5): a save from a bridge run wrote the layout without
  the in-memory import (cause not found; typed `IServerDocument`, focus on the doc did not
  help); sandbox `if Obj.Modified then` on the IDispatch scratch variables always reads False
  (use `BoolToStr`); `DM_OpenAndFocusDocument` on an open document reloads it after the run.
- Verified on 15 renamed scratch copies of `HVIL_Harness_HVSL1200` (scratchpad `imp*`), then on
  the real HVSL1200, Mini MSD and P42C-1 HVIL projects: all 7 wires got 5876 BR005, layout
  connectors / CPs / bundles / routes unchanged against HEAD, drawings carry the wire data,
  projections kept. 17 unit tests (`test_harness_import.py`); suite 192 passed, 13 pre-existing
  failures. After the user's MCP restart (2026-10-30) the tool ran through the MCP on the Main
  harness `.PrjHar`: W61-W63 got 5859/12 BK005, geometry unchanged, drawing updated.
- Docs: README tool list (also `edit_harness_layout`, `get_multiboard_data`,
  `edit_multiboard_schematic`, missing until now), workspace `AGENTS.md`, skill
  `altium-harness-engineering` Steps 7 / 8, dev skill §5.

### Session 15 (2026-10-29): Draftsman harness drawings
Asked: can the MCP review / modify `.HarDwf`, clean up the Slave sense drawing, and fix its
harness component view, which showed nothing (wanted: connector front / back pin views).
- **Format** (`references/wirdoc-format.md`, "Draftsman drawings"): LZ4Stream-framed
  DataContract XML; pure-Python LZ4 block codec in `server/harness_drawing.py` (no new
  dependency; ~1.5 s to write a 19 MB document, output ~ Altium's size). Geometry 1/96 in,
  bottom-left origin, y up.
- **Why component views were empty** (decompiled `Altium.Designer.PcbDrawing.dll`, Draftsman
  extension; `Altium.Sch.Core` `ComponentServiceProvider`; `Altium.Edp.Classes`
  `ComponentServiceByLibraryId`): Draftsman finds the connector's PCB library through the DbLib
  with an empty table name and needs the footprint's 3D body exported to BREP; it computes a
  projection only when none is stored and keeps the old lines on failure. All eight P42C-1
  drawings held an empty P1/J1 Top entry, so no update could ever fill them. The failing step of
  the native lookup was not isolated (live call of `GetLocalComponentPCBLibraryPath` from
  DelphiScript not tried on an open harness project); removing the stale entry lets Draftsman
  retry.
- **Fix**: `set_component_projection` stores lines computed outside Altium (build123d
  `project_to_viewport` of the STEP extracted with altium-society `extract_step_model`, in
  Draftsman's frame); Draftsman keeps them. Calibrated live on renamed scratch copies
  (`dwftest\MCP_Test_Dwf*.HarDwf`, opened as free documents with a non-existent source path):
  lines stored y-up rendered upside down, so the tool stores -y; a view moved to (100, 100) mm
  sat 100 mm from the sheet's bottom-left corner.
- **Tools**: `get_harness_drawing_data` (pages, templates, items, component views, projections,
  issues), `edit_harness_drawing` (ops `set_component_view`, `set_view_title`,
  `set_component_projection`, `remove_component_projection`, `copy_item`, `move_item`,
  `set_text`, `delete_item`, `set_template`; closes / writes / reopens like
  `edit_multiboard_schematic`, backup in `C:\Users\Public\altium_mcp\backups`).
  `get_screenshot(view_type="dwf")`. Bridge: `SchDocumentKind` maps `.HarDwf` ->
  `HarnessDrawing`; `multiboard_document` opens with `SchDocumentKind` (was `SdDoc` only) and
  serves both editors. Live-tested status -> close -> write -> reopen on an open scratch drawing.
- Applied to `P42C-1_Slave_Sense_Harness.HarDwf` (workspace HANDOVER). Tests
  `tests/test_harness_drawing.py` (14); suite 172 passed, same 13 pre-existing failures.
- Second part (after an IDE restart, tools tested through the MCP): `add_callout` (custom-text
  callout snapped to a projection point; page point = view Location + (point - bound min) x
  scale, matched the user's GUI callout exactly; `Leader` = text offset from the anchor; Draftsman
  re-places snapped callouts when the view moves / rescales), `copy_table_format` (`Columns` of
  connection tables / wiring lists, `ShowedColumns` of BOMs; also from another drawing),
  callout text / target in the review. Verified live on page-only scratch copies
  (`dwftest\MCP_Prev_*.HarDwf`, `PcbDrawing:FitSheet` / `Zoom Action=Out`): the JAE MX84 rear view
  (staggered rows) showed Draftsman draws projections unmirrored. Draftsman stores a table's new
  size only when it saves, so the review reads old table sizes until then. Tests 16.
- Third part (rollout to all P42C-1 drawings + template project): fixed `copy_table_format`
  editing several tables at stale offsets (it worked on Slave sense only because all column
  blocks had the same length; now from the end of the document; regression test); `add_callout`
  adds Draftsman's default arrow style (`ArrowStyles/ArrowStyleData Id 1`) when the drawing has
  none - callouts referencing a missing arrow style are not drawn (found on HVIL HVSL1200);
  `set_view_scale` (any view, e.g. a layout at 0.75); `.HarDot` accepted by both drawing tools
  and mapped to `HarnessDrawing` in `SchDocumentKind`. Tests 17; suite 175 passed, same 13
  pre-existing failures. Per-part rear sides / numbering: workspace skill
  `altium-harness-engineering/references/connector-rear-views.md`.

### Session 14 (2026-10-28): layout text justification, CP names, unnamed objects (`1fa3dfe`)
Found while tidying the HV harness layout (P42C-1 workspace HANDOVER):
- **`show_name` on a connection point now hides / shows its name.** Altium draws the name by
  its Designator text's `IsHidden`; `ShowName` is only read when it imports an old drawing
  (decompiled `SchHarnessLayoutConnectionPoint.HandleAfterImportEvent`). `CPOINT` sets both;
  `parse_ldrdoc` reports `show_name` from the text (it reported `ShowName`, so a shown name
  read as hidden).
- **Texts keep a justification, and mirroring flips it** (`SchLabel.Mirror` ->
  `MirrorJustification`): a bundle length placed while a mirrored lug sat on its connection point
  became bottom-right, so `[x, y]` named its right end and the text overlapped its neighbour.
  A placed text is now set bottom-left (what the docs promised) unless `texts` gives
  `"justification"` (9 names, `TEXT_JUSTIFICATIONS`); `TEXT` record field 6, Pascal
  `PlaceChildText(..., Just)`. `get_harness_data` reports each text's `justification`.
  `IsMirrored` alone does not flip the glyphs and is left as it is.
- Verified on the renamed scratch copy `hvt\MCP_Test_HV` (§7) with `call_tool.py`: raw records
  and `parse_ldrdoc` read back as intended (CP_X1 shown, CP1 hidden, B14 length J 2 -> 0, B15
  kept bottom-right on request, B13 name centred), no wire unrouted, real files untouched. Not
  re-checked by screenshot then (the user was working in Altium); after the IDE restart the
  MCP daemon reported `justification` / `show_name` from the new code and a screenshot showed
  B14's length starting at its anchor, B15's ending there, CP_X1 named and CP1 not.
- **Unnamed objects sharing a designator ('B?', 'CP?') are told apart by UniqueID.**
  `edit_harness_layout` keyed connection point positions and the bundles it checks after the
  call by designator, so of 22 bundles all named `B?` it checked one arbitrary bundle against
  another's ends (false `problems`, `success: false` on the HV layout), and naming a 'CP?'
  by unique_id in `from` / `to` or moving one could use another 'CP?''s position. Now
  `cp_pos` and `expect["bundles"]` are keyed by UniqueID (new objects: `DES:<designator>`),
  `check_layout` compares bundle ends by UniqueID and labels shared names `B? (<uid>)`, and an
  item, `from` / `to`, covering bundle or delete naming a shared designator is an error
  ("give the one meant by its unique_id"); with `unique_id` given too the uid is used.
  Re-ran the HV lug swap on the scratch copy (`hvt\MCP_Test_HV.pre.LdrDoc` = the layout
  before, `hvt\t2.json`): `success: true`, 12 bundles checked one by one, no problems, no
  unrouted wire (the same call had reported a false problem). Test
  `test_unnamed_objects_sharing_a_designator`; `expect` assertions now by UniqueID.
- Tests `test_justification`, `TestParseTexts`, `test_unnamed_objects_sharing_a_designator`;
  suite 158 passed, same 13 pre-existing failures.

### Session 13 (2026-10-27/28): copy objects between wiring diagrams (`a29063e`)
- **`add_harness_wiring(copy_objects=[...])`**: copies onto the target every object the source
  sheet owns whose bounding box lies inside `region` (mils), moved by `offset`, with
  `designators` renames (connectors, wires, splices) and `replace_text` (texts, notes, text
  frames). Harness wiring (82, 83, 94, 92, 93, 98, 106) is left out unless `include_wiring`.
  Spec: `COPYDES` / `COPYTEXT` records before their `COPYFROM` (Pascal `CopyHarnessObjects`,
  `RenewUniqueIds`, `NewSchUniqueId` in `schematic_utils.pas`); emitted before connector moves
  and wires, so a call can copy lugs and wire them. Result: `"copied"`; an empty region or
  unopenable source goes to `not_found` (`copy_region:` / `copy_source:`).
- Why: Altium harness connectors cannot be placed from a desktop DbLib by script; copying a
  placed one (Replicate) is how a sheet is replicated (HV harness Master1 -> Master2, 23 objects).
- Verified on renamed scratch copies in a scratch project, then on the real sheet: record counts
  per type exact, 0 UniqueIds shared with the source (377 new), wires drawn in the same call
  attached to the copied pins, `check_harness_wiring` identical to the source sheet. Unit tests
  `test_add_harness_wiring_copy_objects{,_malformed}`; suite 155 passed, same 13 pre-existing
  failures.
- Limits: the source must be a wiring diagram of an open harness project (a lone `.WirDoc`
  opens empty, §6); only sheet-owned objects whose whole bounding box is inside `region` are
  taken; copied wires (`include_wiring`) keep their Comment (net) and parameters, so rename and
  re-net them with `wire_updates`; layout drawings are not touched (update the `.LdrDoc` from the
  wiring diagram in Altium afterwards).
- Docs (2026-10-28): AGENTS / skill `altium-harness-engineering` (Step 3, troubleshooting),
  dev skill (§2 scratch project for a `.WirDoc`, §5 `Replicate`), `references/wirdoc-format.md`
  (ObjectIds: text label is 22, not 4), `dev/SHEET_DUPLICATION.md` (harness sheets).

### Session 12 (2026-10-27): multi-part connectors, wire kinds, wire break direction (`e2e1203`)
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

### Session 11 (2026-10-27): harness entry components (`aedbea7`)
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

### Session 10 (2026-10-26): multi-board schematic, upstream merge repair (`81280f9`, `0bdbf41`, `8681502`)
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
- **Upstream 0.5.1 merged** (`6a76414`, repaired in `0bdbf41`): our Ctrl+F3 recovery,
  `_blocking_altium_dialogs` and `test_script_recovery.py` are gone; script recovery is upstream's
  (`server/sandbox.py` for sandbox runs, the `mcp_script_stuck.txt` marker + `EditScript:Stop`
  for bridge timeouts; see the dev skill §5). `requirements.txt` re-exported from `uv.lock`
  (olefile was missing).
- Layout: a connection point may hold a no-connect (`8681502`; the HVSL1200 HVIL pigtail's free
  wire ends TERM1/TERM2 sit in CP1/CP3).

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
  user" text with the titles). Verified live that it returns [] when Altium is free. (Removed
  with the upstream 0.5.1 merge, session 10.)
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
- Our own script-engine recovery (Ctrl+F3 injection, `_recover_script_executor`) was replaced by
  upstream's recovery in session 10 (see there).
- **Layout drawings open/screenshot**: `other_utils.pas` `SchDocumentKind(path)`
  ('HarnessWiringDiagram' / 'HarnessLayoutDrawing' / 'SCH') + `IsHarnessDocumentKind`; used by
  `EnsureDocumentFocused`, the screenshot path in `Altium_API.pas` and the zoom check.
  `get_screenshot(view_type="sch", document_path=<.LdrDoc>)` works.
- `call_tool.py` saves image results to `%TEMP%\altium_mcp_<tool>_<ns>.png` and prints the path.
- **Pre-existing test failures (13 since session 12, were 15):** they read a real WirDoc of the
  development workspace that has since changed. Fix the tests to use synthetic records
  (`parse_wirdoc_records`).

## 4. Key decisions and why
- **DB is the single source of truth**; parameters written by `sync_harness_library`, never typed.
- **Edits move objects in place** (keep UniqueIDs): the `.LdrDoc` links every object to its WirDoc
  original by UniqueID (`WiringDiagramOriginUniqueId`). Never recreate WirDoc wires.
- **New connectors are copied, never placed from the library** (session 13): Altium places a
  harness connector only from its own picker; `copy_objects` replicates a placed one (with its
  cavity parts and parameters) and renews every UniqueId, so copies never share ids with the
  source. Copying a whole `.WirDoc` on disk would duplicate every UniqueId inside one project.
- **No automatic router** (session 8): the agent routes by the drawing style, `check_harness_wiring`
  proves it.
- **Layout objects are tracked by UniqueID, designators only name them** (session 14): an
  imported layout carries many unannotated `B?` / `CP?` objects, so anything keyed by
  designator (positions, post-call checks) picks the wrong one. A shared designator in a request
  is refused rather than guessed.
- **A placed text is bottom-left unless asked otherwise** (session 14): `[x, y]` must mean the
  same corner whatever mirroring left on the text, or the agent cannot plan positions.
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
- Sandbox (upstream's since session 10): scratch variables `S1..S9`, `I1..I9`, `B1..B3`,
  `F1..F4`, `Obj1..Obj9`, `List1..List3` (nil until you create them), `IntMan`, `DbDoc`; an
  undeclared name is refused before Altium runs. `GetFieldFromPipeString` not available.
- Harness wire `ComponentKind` is no DelphiScript property ("Undeclared identifier"): use
  `GetState_ComponentKind` / `SetState_ComponentKind` (session 12).
- Passing a `TList` item straight into a typed routine parameter (`Proc(List[j], ...)`) paused the
  script; assign it to a variable first (session 12).
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
  "off its wire". Annotate the wires before trusting those warnings. Identical `X?` connectors
  also gave a false `through_connector` error (session 13; gone once annotated).
- **In-memory ObjectIds on a wiring diagram (probed session 13):** the document is 79 (a plain
  schematic is 32), text label 22, rectangle 4, note 2, image 11, text frame 13, template 42,
  document parameter 27, harness connector 81, wire 82. (File RECORD numbers differ: label 4,
  rectangle 14, image 30, note 209.) Sheet-owned objects have `Container.ObjectId` = 79.
- **`Replicate` keeps every UniqueId** (object, pins, parameters): a copy must renew them
  (`RenewUniqueIds`, `SetState_UniqueId`), or pins share ids across sheets.
- **A `.WirDoc` opened outside its harness project** (`Client.OpenDocument` on a free file) loads
  as an empty plain schematic (ObjectId 32, only document parameters). Test on scratch copies
  inside a scratch `.PrjHar` (copy the project file, keep `[Document*]` of the copied sheets,
  rename their paths), and ask the user to close that project afterwards.

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
- `main.py` - MCP tools (`add_harness_wiring` spec records via `_spec_line`, `_wire_extra_lines`,
  `_wire_break_orientation`; `_check_harness_links`; `sync_harness_library` engine
  `_sync_harness_from_library`; `run_altium_script` via `sandbox.py`; `get_harness_data`;
  `get_screenshot`; `edit_harness_layout` incl. the multi-run render sequence;
  `get_multiboard_data`, `edit_multiboard_schematic`; `import_harness_changes` with
  `_harness_import_targets`, `_run_harness_import`, the standalone scripts `_FOCUS_PRELUDE` /
  `_OPEN_SCRIPT` / `_IMPORT_SCRIPT` / `_SAVE_SCRIPT` run by `_harness_import_script`,
  `_layout_sync_gaps`, `_layout_import_report`, `_drawing_import_report`).
- `harness_parser.py` - `.WirDoc` reader `parse_wirdoc` / `parse_wirdoc_records` (connectors per
  part object with visible pins, cavity / associated parts, wires, twists, shields, cables, wire
  breaks), `extract_ole_records`.
- `multiboard.py` / `multiboard_edit.py` - `.MbsDoc` review (`review_multiboard`) and edits.
- `sandbox.py` - upstream sandbox runner and script recovery.
- `harness_layout.py` - `.LdrDoc` reader `parse_ldrdoc`, spec builder `build_layout_spec`, check
  `check_layout`, helpers `_follow_end`, `_wire_length`, `_shortest_route`.
- `harness_check.py` - `check_harness_wiring` and `label_positions`.
- `AltiumScript/schematic_utils.pas` - `ExecuteAddHarnessWiring` and helpers (`SpecField`,
  `UpsertSchParameter`, `SetSchParameterHidden`, `ApplyHarnessWireProperty`, `PlacedDesignator`,
  `FindPlacedComponent`, `PinOnPart`, `FindComponentWithPin`, `SiblingParts`,
  `ClearHiddenCavityParts`, `AddCavityPart`, `SetChildDesignator`, `FindHarnessObjectByUid`,
  `FindWireBreakAt`, `NewSchUniqueId`, `RenewUniqueIds`, `CopyHarnessObjects` (record
  `COPYFROM`), ...); at the end the layout part:
  `ChildDesignatorText`, `FindLayoutObject`, `PlaceChildDesignator`, `PlaceChildText`,
  `SetBundleVertices`, `ProjectPathOfDocument`, `UnsavedProjectDocuments`, `RefreshFromLibrary`,
  `ConnectorPinIds`, `SyncConnectionPointPins`, `ExecuteEditHarnessLayout` (records incl. `VIEW`,
  `WIRELEN`, `RELOADPROJECT`, `RENDER`, `TEXT`; result `render_pending` drives the extra runs);
  `ExecuteImportHarnessChanges` (read-only check before an import: kind, open project, unsaved
  documents, was_open).
- `AltiumScript/Altium_API.pas` - `case CommandName of` dispatch (`'edit_harness_layout'`,
  `'import_harness_changes'`); `other_utils.pas` - `EnsureDocumentFocused` skip-list,
  `SchDocumentKind`, `FindOpenDocument`. All three `.pas` files are CRLF (checked 2026-10-29).
- `tests/` (`test_harness_parser.py` fixtures pattern, `test_harness_layout.py` = synthetic
  layouts + tool with mocked parser/bridge, `test_harness_check.py`, `test_multiboard.py`,
  `test_multiboard_edit.py`, `test_sandbox.py`, `test_harness_import.py` = the import tool with
  mocked bridge and mocked `run_altium_script`).
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
- `sb.py <script.pas> [timeout]` runs a sandbox file (written for the old `main._run_sandbox`;
  session 12's `...\6e56e396-a910-46dd-8442-72b590f81793\scratchpad\sb.py` calls
  `main.run_altium_script`, plus `t\close.pas` / `t2\zoom.pas` = close a scratch document /
  zoom to selected objects before a screenshot); `probe\*.pas` = the
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
- Session 13 `...\3aa743d9-ec13-4f37-94d2-e7f50a027134\scratchpad\`: `MCP_Test_Copy.PrjHar` +
  `cp\MCP_Test_CopySrc.WirDoc` / `cp\MCP_Test_CopyDst(.orig).WirDoc` (scratch project for
  `copy_objects`; the user closes it in Altium), `cp\copy_test.json` / `cp\wires_test.json`
  (`call_tool.py` kwargs), `cp\uids.py` (UniqueIds shared by two sheets).
- Session 14, same scratchpad: `hvt\MCP_Test_HV.*` (renamed copy of the HV harness project;
  open in Altium, the user closes it). Its `.LdrDoc` is now the layout before the review after
  the `t2.json` lug swap; `MCP_Test_HV.pre.LdrDoc` = that layout before (all `B?` / `CP?`),
  `MCP_Test_HV.orig.LdrDoc` = the reviewed layout, `MCP_Test_HV.t1_after.LdrDoc` = after the
  `t1.json` text / name test. `hvt\t1.json` / `hvt\t2.json` (`call_tool.py` kwargs),
  `hvt\readback.py` (raw text records + `parse_ldrdoc` of chosen objects), `raw_texts.py` (texts
  with a non-default justification).

## 8. Next steps
Announce "using Altium now" / "Altium free" around live tests; a desktop screenshot tells
whether an Altium modal window is up ("Altium did not start the command (not executed)" = ask
the user to clear it). Test each change on a scratch copy first, and close the scratch document
in Altium before overwriting it on disk (a file-changed prompt blocks the bridge).
1. Fixes from session 9 (§6); part-aware lookup, parser parts and sync reload done in session 12:
   - `CONNECTOR` (move) and `ASSOCPART` still take the first part object of a multi-part
     connector (no way yet to name the part, e.g. `FH1:B`).
   - `check_harness_wiring`: key wires by UniqueID, not designator (unannotated `W?` wires merge).
   - `check_harness_wiring`: parse the sheet rectangles (RECORD 14, the connector frames) and
     report labels and channels inside another connector's frame (today only the screenshot shows
     them).
   - `get_screenshot(view_type="sch")` on a harness sheet: optional zoom (whole sheet via
     `Sch:Zoom Action=Document`, or an area), so the agent can see every part of a large sheet.
     Session 12 workaround: a sandbox run that selects objects and runs `Sch:Zoom
     Action=Selected` (it zoomed only a little on the P42C-1 Top sheet), then crop the PNG.
   - `check_harness_wiring`: a vertical wire's label must start within 120 mils to its right
     (`beside`); labels left of a vertical run are reported "off its wire".
2. Fix the 13 pre-existing test failures (synthetic records instead of a real WirDoc).
3. Open question: should view options (side, zoom) be copied from a template connector?
4. Not supported yet: LdrDoc notes / text frames; wire `LengthOffset` / user length / manual wire
   paths (no script setter, §5).
5. `copy_objects` was tested within one harness project only: try a source sheet of another open
   project before relying on it. A `get_harness_data` option listing the objects inside a region
   would help choose `region` without a screenshot.
6. Draftsman (session 15): isolate why the native component-view lookup fails (probe
   `GetLocalComponentPCBLibraryPath` on the open harness document with and without the table
   name; test a `Footprint Path` DbLib field) - if a library change fixes it, removing the
   stored projections lets Draftsman draw them itself. Projection computation is the agent's
   job today (build123d session); a helper that takes footprint + side and returns the lines
   would save steps. Cavity numbers on face views would need Text items (no op yet); page
   navigation / zoom to a view for screenshots (only `ViewFit`, all sheets).
7. `import_harness_changes` (session 16): find why a save from a bridge run drops the import
   (probe the harness model's active project, `HarnessChangesWatcher.ActiveProjectFilePath`,
   in a bridge run vs a sandbox run); if fixed, the open / import / save could move into the
   bridge. The pin repair also runs when a connector's unassigned pins carry no wire (Main J3 /
   J4 pins 3-6: one extra save cycle, 0 pins synced): repair only for pins with wires. Not
   tried yet: a layout whose import adds or removes connectors / wires (expect new
   connectors placed by Altium at its default spot).

## 9. How to verify
```powershell
# unit tests (altium-mcp root) - 158 passed, 7 skipped, 13 pre-existing failures (§3)
cd C:\evt_gits\electrical-p42c-1\altium-mcp
uv run --with pytest python -m pytest server/tests -q
uvx ruff check --extend-select I server/harness_layout.py server/harness_parser.py server/harness_check.py
# main.py has ~50 pre-existing ruff findings: compare counts before / after a change

# call working-tree tool code directly (the MCP daemon keeps old code until the IDE restarts)
$py = (Get-ChildItem "$env:USERPROFILE\.altium-mcp\venvs\*\Scripts\python.exe" | Select-Object -First 1).FullName
& $py .agents/skills/altium-mcp-development/scripts/call_tool.py get_screenshot '{"view_type": "sch", "document_path": "<path>.LdrDoc"}'
& $py .agents/skills/altium-mcp-development/scripts/call_tool.py edit_harness_layout '@kwargs.json'
# read a layout drawing (get_harness_data on .LdrDoc does the same through the MCP)
& $py -c "import sys, json; sys.path.insert(0, 'server'); import harness_layout as hl; print(json.dumps(hl.parse_ldrdoc(r'<path>.LdrDoc')['summary']))"
```
Live checks: `success: true`, `saved: true`, empty `not_found`; reread with `parse_ldrdoc`;
screenshot; `git status`; close scratch documents in Altium (`WorkspaceManager:CloseObject`,
`PromptToSave=False`).
