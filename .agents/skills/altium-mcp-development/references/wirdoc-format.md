# Altium harness document format notes (.WirDoc / .LdrDoc)

Facts established by reading files Altium Designer 26 wrote, and by round-tripping values through
the API. Read this before treating something odd in a file as a bug.

## Container and records

- OLE compound file; the `FileHeader` stream holds records of `[uint32 length][payload]\0`.
- Each payload is `|KEY=VALUE|KEY=VALUE|...`. Record 0 is the stream header; `OwnerIndex` values
  count from record 1 (index 0 = first record after the header).
- Use `harness_parser.extract_ole_records` (it decodes each field correctly, see below).

| RECORD | API ObjectId | What |
|---|---|---|
| 4 | 4 (eLabel) | text string; with `OwnerIndex` it belongs to a component (e.g. a connector title) |
| 14 | - | rectangle (user graphics) |
| 25 | 25 (eNetLabel) | net label - invalid in harness sheets |
| 34 / 41 | eParameter | designator / other parameter; `Name`, `Text`, `OwnerIndex` |
| 106 | 81 | harness connector **or** a cavity component (see ownership) |
| 107 | 82 | harness wire: `LocationCount`, `X1..Xn`/`Y1..Yn`, `EndVertex1/2ConnectedObjectUniqueID`, `DesignItemId`, `SourceLibraryName`, `Color` |
| 108 | 83 | harness splice |
| 113 | 88 | connector pin: `Location`, `PinLength`, `PinConglomerate & 3` = orientation |
| 117 | 92 | harness shield (rectangle) |
| 118 | 93 | harness twist (rectangle) |
| 119 | 94 | harness no-connect (`Style=1` thick cross) |
| 123 | 98 | harness cable (rectangle) |
| 125 | 101 | associated-parts container owned by a connector, splice or pin |
| 130 | 106 | wire break (cross-sheet connector) |
| 131 | 107 | cavity container owned by a pin (`AssociatedObjectType` = slot) |

## Ownership

- A top-level `106` (no `OwnerIndex`) is a placed connector.
- `106 -> 131 -> 113`: a `106` owned by a `131` is a cavity component (crimp terminal, seal, plug,
  other), the `131` is owned by the pin. `106 -> 125 -> 106/108`: an associated part of a
  connector or splice. These are *not* connectors - counting every `106` as a connector was a bug.
- Each cavity component is written **twice with the same UniqueID**; the second copy carries its
  parameters, each name appearing twice. This is serialization, not duplication: a correct upsert
  keeps it at exactly two.

## Coordinates

- Integer fields are DXP units (1 unit = 10 mils). Each may have a `<key>_Frac` companion in
  1/100000 units: value = `int(key) + int(key_Frac) / 100000`. Either part is omitted when zero.
- The API works in internal coords: `MilsToCoord(10 * dxp)`.

## Text encoding

- Plain fields are written in the ANSI code page (cp1252). A `|` inside a value is written as byte
  `0x8E`.
- Values with non-ASCII characters also get a `%UTF8%<key>` field holding the UTF-8 form; there a
  `|` is written as `U+00A6` (`¦`). Prefer the `%UTF8%` value.
- Decoding a whole record as UTF-8 (falling back to Latin-1) mangles one of the two and makes
  correct values look like mojibake (`200Â°C`). Decode field by field.
- Real mojibake does occur: values that entered Altium through the DbLib ODBC "Access Text
  Driver" are read as ANSI unless the CSV folder's `schema.ini` says `CharacterSet=65001`. In the
  file, such a value shows `Â°` in the *decoded* `%UTF8%` field (UTF-8 bytes `C3 82 C2 B0`).

## Wires and attachments

- Wires registered through the API attach to pins/splices automatically when an end lands exactly
  on the hot end / splice point (`EndVertex*ConnectedObjectUniqueID` set).
- Moving a wire's vertices in place (same endpoints, same order) keeps the attachments and the
  UniqueID; deleting and recreating a wire changes the UniqueID and breaks `.LdrDoc` links.
- A new harness wire comes with Altium's own default parameters; adding a parameter by name
  without looking for an existing one creates a duplicate with possibly conflicting value.

## Harness objects beyond wires (verified on Altium 26.7, 2026-10-21)

ObjectIds come from `SCH.TObjectId` in `System/Altium.SDK.Interfaces.dll` (readable with
`System.Reflection.Metadata`; no decompiler needed). Interfaces and parameter names come from the
same assembly (`SCH.ISch_Harness*`). All of the below was exercised live on scratch copies.

| Feature | ObjectId | File RECORD | How to create from DelphiScript |
|---|---|---|---|
| Cavity seal / plug / other | 81 (part) in a 107 container | 106 in a 131 with `AssociatedObjectType=1/2/3` (crimp = 0, field omitted) | `Pin.AddItem(type, Part)` with `Part := SchObjectFactory(81, 0)`, `Part.DesignItemId := PN`; `Pin.GetCount(type)`, `Pin.RemoveAllOfType(type)` (no callable item getter: replace a slot as a whole). One crimp/seal/plug per pin, any number of "other". Set a `Comment` parameter to the PN (Altium leaves `*`); `ComponentDescription`, `SourceLibraryName`, `DatabaseTableName` persist. |
| Associated part (boot, backshell, wedgelock, heatshrink...) | 81 | 106 in a 125 container owned by the connector (106) or splice (108); pins carry empty 125s | `Owner.AddAssociatedPart(Part)`, `GetAssociatedPartsCount`, `RemoveAllAssociatedParts`, `RemoveAssociatedPart(i)` |
| Twist | 93 | 118 | factory(93), set `Location`/`Corner` to a rectangle crossing the wires, register. Every wire the rectangle crosses **or touches** is linked (`ConnectedWiresUniqueIdsCount/ConnectedWireUniqueId<n>` on the 118; a wire lists its shields under `ConnectedShieldsUniqueIdsCount/ConnectedShieldUniqueId<n>`) - but only on load, see below |
| Shield | 92 | 117 | same as twist; `Style := 1` = "Shield with Connection" (stored `Style=1`). Its pin: middle of the bottom edge `((x1+x2)/2, min y)`; `Orientation := 1/2/3` (stored `Rotation=1/2/3`) moves it to the right/top/left edge. A drain wire ending there is listed under `ConnectedPinWiresUniqueIdsCount/ConnectedPinWireUniqueId<n>` and its `EndVertex<n>ConnectedObjectUniqueID` is the shield's UID |
| Cable | 98 | 123 | same as twist; `Designator.Text`, `Comment.Text`, `DesignItemId`, `SourceLibraryName`, parameters (MPN, ...); it has a default `Description` parameter, which persists. **No** `DatabaseTableName` / `ComponentDescription` (runtime error) |
| Wire break | 106 | 130 (`Text` = wire designator, `IsCrossSheetConnector=T`, `ConnectedObjectUniqueId`) | factory(106), `Location` exactly on a free wire end, register; it takes the wire's designator (on load). Wires with the same designator on different sheets of one project become one connection (compiler checks them: "Mismatched parameters in connected wire segments") |

Other TObjectId values: 84 layout label, 85 layout connection point, 86 bundle, 87 logical signal
(RECORD 112 in multiboard-imported sheets), 89 wire label, 101 associated parts, 104 covering,
107 associated objects (cavity container).

## Layout drawings (.LdrDoc) (verified on Altium 26.7, 2026-10-21)

Read with `harness_layout.parse_ldrdoc`; written by `edit_harness_layout`
(`ExecuteEditHarnessLayout` in `schematic_utils.pas`). Open kind `HarnessLayoutDrawing`; open the
harness project first so the drawing has its wiring diagram. Document ObjectId 80.

| RECORD | ObjectId | Object | Key fields |
|---|---|---|---|
| 106 (top-level) | 81 | connector | `Location`, `Orientation` (0-3), `IsMirrored`, `WiringDiagramOriginUniqueId`; child 41 `LayoutDrawingSymbolModel<n>` (`IsImageParameter=T`, `Text` = `ViewName=..\|ViewType=ePhysicalView\|ViewSide\|Rotation\|Zoom\|MainModel=True..`) + child 30 image |
| 113 | - | pin | `WiringDiagramOriginUniqueId`, `ConnectedWireUniqueId<n>` |
| 110 | 85 | connection point | `Location`, `Orientation`, `Style` (0 circle, 1 square, 2 insulator), `ShowName`, `ConnectedBundleUniqueId<n>`; designator = child 34 |
| 111 | 86 | bundle | `LocationCount`, `X<i>`/`Y<i>`, `LineWidth`, `LengthLong`, `DrawnLength`, `IsLengthSetManually`, `ShowBreakSymbol`, `EndVertex1/2ConnectedObjectUniqueID` (CP uids); children 34 designator, 41 Comment |
| 128 | 104 | covering | `Thickness`, `PhysicalStartDistance`, `PhysicalEndDistance`, `PhysicalLength`, `CoveredItemsCount`, `CoveredItemId<i>` (0-based), `DesignItemId`, `SourceLibraryName`; child 34 |
| 109 | 84 | layout label | `Location`, `Text`, `Alignment`, `Orientation`, `ShowOnlyFirstLine`, `DesignItemId`; child 34 |
| 41 + `IsImageParameter=T` | 100 | connector view (image parameter) | child of the connector, `IsHidden=T` when hidden; child 30 = the rendered picture (bitmap in `Storage`); no model path stored (found from the connector's PCBLIB implementation at render time) |
| 115 | 90 | wire data (no geometry) | `Name`, `Comment`, `EndVertex1/2ConnectedObjectUniqueID` (layout pin / splice uids); when not default `LengthType` (0 calculated = routed + `LengthOffset`, 1 user = `UserLength`, 2 MCAD = `LengthLong`), `BundleToGoThroughUniqueId<n>` (manual path) |
| 116 / 120 / 122 | - | splice / no-connect / twist | `Designator`, `WiringDiagramOriginUniqueId`, `ConnectedWireUniqueId<n>` (no location) |

Lengths (`LengthLong`, `Physical*`) are internal coords: mm = coord / 10000 x 0.0254. Which objects
a connection point holds is in the zlib-compressed `HarnessConnectionPointConnector` stream
(`read_connection_point_streams`). A wire runs through the bundles on the path between the
connection points of its two ends; the reader computes the shortest one (Altium's default) and
cannot see paths re-assigned by hand.

Script interfaces are `Rt_Schematic.ISch_Harness*` in `System/Altium.Edp.Interfaces.dll`.
What works: connection point factory(85) + `Location`, `MoveToXY`, `SetState_Style`,
`SetState_ShowName`, `SetState_Orientation`, `AddConnector(uid)` / `RemoveConnector(uid)`
(connector or splice uid, persisted in the stream); bundle factory(86), `VerticesCount`,
`Vertex[i]` (1-based), `SetState_Length(coord)`, `SetState_IsLengthSetManually`,
`SetState_ShowBreakSymbol`, `LineWidth`, `Comment.Text`; covering factory(104),
`AddBundleItem` / `AddConnectionPointItem` / `RemoveAllItems`, then inside `BeginUpdate` /
`EndUpdate` `SetState_PhysicalStartPointDistance` / `...EndPointDistance` / `SetState_Thickness`,
`DesignItemId`, `SourceLibraryName`; label factory(84), `Text`, `SetState_Orientation`,
`SetState_Alignment`, `SetState_ShowOnlyFirstLine`; connector `MoveToXY`, `Orientation`,
`Mirror(Location)`; `RemoveSchObject` for deletes. Designators of all of them via the child
designator (ObjectId 25).

Gotchas found:
- **Bundle ends connect to connection points by position, and only when Altium loads the
  drawing.** A bundle registered by script, or one whose connection point moved, keeps (or lacks)
  its link until the document is reopened; then an end that is not exactly on a connection point
  is disconnected. Moving a connection point does not move bundle vertices. So the tool moves the
  bundle ends itself, saves, closes, reopens and saves (`reloaded`), and reads the links back.
- Set a bundle's designator after registering it: set before, a bundle has been saved
  unconnected with length 0.
- Covering offsets: `PhysicalLength = covered length + start + end`, so a negative offset insets
  the covering from that end and a positive one extends it past the end.
- Moving a connector moves its views and texts but puts its connection point somewhere
  unrelated (joined by a dashed line); the tool places the connection point explicitly (same
  offset, turned with the connector). `Location :=` on a connection point leaves its designator
  behind; `MoveToXY` moves it along. A new connection point's designator starts near the sheet
  origin.
- Splices are held by no connection point after an import: their wires stay unrouted until one
  is assigned.
- `Doc.GetBundleContent` (runtime error / crash), covering `SetState_StartPointDistance`,
  `SetStartPoint`, `VerticesCount`, and `CP.Designator` are not usable. Interfaces stored with
  `TStringList.AddObject` cannot be read back (`Objects[i]` pauses the script).
- Not reachable from scripts: wire `LengthOffset`, user lengths and manual paths (wire data has
  only `SetState_Length`), connector view settings (side, zoom, rotation, line view: data fields,
  the parameter `Text` is rewritten from them at every save), and every `SCHInterfaces` method
  (`GetMainPhysicalModel`, `UpdatePhysicalModel`, `AddImageParameter`, `RemovePhysicalModel`:
  runtime error; only `Rt_Schematic.ISch_*` members are callable).

Connector views (2026-10-21, from decompiling `Altium.Sch.DataModel` and
`InteractiveProperties.Providers.Harness.*` with ILSpy, then live):
- A connector draws its main image parameter (`IsMainModel`) unless that is hidden, else its
  symbol. The Properties panel's Symbol / Physical switch only sets `IsHidden` on all of them;
  `edit_harness_layout` does the same (`Item.IsHidden := ...` on the ObjectId 100 children).
- **Never remove an image parameter by script** (`RemoveSchObject`): the connector caches its
  main view (`SchDataHarnessComponent.GetMainPhysicalModel`) and only Altium's own
  `RemovePhysicalModel` or a load from disk resets the cache. The stale cache raised a .NET
  "Object reference not set to an instance of an object" dialog, which also blocks the script
  bridge until the user closes it.
- New physical view: factory(100) + `Name := 'LayoutDrawingSymbolModel1'`, `IsHidden := False`,
  `Conn.AddSchObject`, save. It becomes the main model only when the **project** is loaded from
  disk (`SchDataImporterDocumentV5.UpdatePhysicalModelsAfterImport`; closing and reopening just
  the document does not re-import). Then render it with a library update: `DesignItemId :=
  '<other>'; PreProcess_Import_FromUser; DesignItemId := PN; PostProcess_Import_FromUser`
  (ends in `FootprintRetriever.UpdateModels`). Side effects as a GUI update from library: the
  connector's layout pins get new UniqueIDs (wire links and routes stay), DB parameters refresh.
- Fixed wire length: `SetState_Length(coord)` on the wire data object (ObjectId 90, iterable
  from the document) sets length type MCAD; `SetState_Length(0)` returns it to calculated.
  `GetState_AutogeneratedLength` reads 0 from a script, so routed lengths are computed in Python.

Gotchas found:
- **Links to twists/shields/cables/wire breaks are computed only when Altium loads the
  document** (2026-10-21). Objects registered by script - and wires added under existing ones -
  show 0 links in memory and in the saved file, whatever the order of registration, until the
  document is closed and opened again (no API call found that recomputes them; moving the object
  does not). `add_harness_wiring` therefore saves, closes (`WorkspaceManager:CloseObject`),
  reopens and saves again after such a change (`relinked` in its result), also for sheets of an
  open project. There are no setters for the links.
- `Internal_GetState_*` methods and `ShieldPinLocation` are not callable from DelphiScript
  (runtime error); use the property form (`Style`, `Orientation`) or `GetState_*` methods.
- Twists and shields have no `Designator` property in DelphiScript (reading `Obj.Designator`
  crashes); their designator is a child object (ObjectId 25, RECORD 34) found with
  `Obj.SchIterator_Create` + `AddFilter_ObjectSet(MkSet(25))`, and setting its `Text` persists.
  Cables do have the property.
- Twists copied/pasted in the GUI keep the copied twist's links (`ConnectedWireUniqueId<n>` of the
  source) until the sheet is reloaded - another reason to relink after edits.
- A cavity part replaced by a script loses parameters the database does not supply (e.g.
  `StripLength`, `PullOffLength`, which the GUI adds empty as `*`); the sync carries non-empty
  ones over to the new part.
- Altium normalises a rectangle's corners (Location = min, Corner = max) when it saves.
- The sandbox's provided `List1` breaks scripts here; create your own `TStringList` (and use
  `.Strings[i]`; `GetFieldFromPipeString` and other production helpers are not in the sandbox).
- **Altium drops a `Description` parameter on cavity parts at every save** (plain open + save
  removes them). Store the text in the part's component description instead of a parameter.
