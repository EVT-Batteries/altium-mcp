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
| 119 | 94 | harness no-connect (`Style=1` thick cross) |
| 131 | - | cavity container owned by a pin |

## Ownership

- A top-level `106` (no `OwnerIndex`) is a placed connector.
- `106 -> 131 -> 113`: a `106` owned by a `131` is a cavity component (crimp terminal), the `131`
  is owned by the pin. These are *not* connectors - counting every `106` as a connector was a bug.
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
