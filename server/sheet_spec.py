"""Spec records for sheet graphics (notes, moves, region copies) shared by
add_harness_wiring (.WirDoc) and edit_harness_layout (.LdrDoc).

Records are field lists; the caller encodes them (``main._spec_line``). The
bridge reads them in ``schematic_utils.pas`` (``PlaceSheetNote``,
``UpdateSheetNote``, ``MoveSheetObject``, ``CopyHarnessObjects``). Coordinates
are mils; '' keeps a value (or takes the default).
"""

from __future__ import annotations

from typing import Any

# Note alignment (THorizontalAlign)
NOTE_ALIGNMENTS = {"center": 0, "centre": 0, "left": 1, "right": 2}
# Sheet graphics types -> ObjectIds (eNote 2, eRectangle 4 = frame, eImage 11
# = picture, eTextFrame 13, eLabel 22 = text)
GRAPHICS_TYPES = {"note": 2, "frame": 4, "picture": 11, "text_frame": 13, "text": 22}


def flag01(value: Any) -> str:
    return "" if value is None else ("1" if value else "0")


def font_spec(value: Any) -> str:
    """'<name>:<size>' of a font given as that text or {name, size} ('' for
    None). Raises ValueError. Fonts go by name and size: the font ids in a
    saved file are its own table's, not Altium's."""
    if value is None:
        return ""
    if isinstance(value, dict):
        name, size = str(value.get("name") or "").strip(), value.get("size")
    else:
        name, _, size = str(value).rpartition(":")
        name = name.strip()
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 0
    if not name or not 0 < size < 1000:
        raise ValueError(f'font is "<name>:<size>" or {{"name", "size"}}: {value!r}')
    return f"{name}:{size}"


def note_style_fields(item: dict) -> list[Any]:
    """area colour, text colour, font, border, solid, alignment, word wrap
    of a note entry ('' = keep / default). Raises ValueError."""
    align = item.get("alignment")
    if align is not None:
        if str(align).lower() not in NOTE_ALIGNMENTS:
            raise ValueError(
                f"note alignment must be one of {sorted(NOTE_ALIGNMENTS)}: {align!r}"
            )
        align = NOTE_ALIGNMENTS[str(align).lower()]

    def num(key: str) -> Any:
        return "" if item.get(key) is None else int(item[key])

    return [
        num("area_color"),
        num("text_color"),
        font_spec(item.get("font")),
        flag01(item.get("show_border")),
        flag01(item.get("solid")),
        "" if align is None else align,
        flag01(item.get("word_wrap")),
    ]


def note_text(text: Any) -> str:
    """Note text with CR LF line breaks."""
    return str(text).replace("\r\n", "\n").replace("\n", "\r\n")


def corners(item: dict, what: str) -> list[int]:
    """Normalised x1, y1, x2, y2 (mils) of an entry. Raises ValueError."""
    try:
        x1, y1, x2, y2 = (int(item[k]) for k in ("x1", "y1", "x2", "y2"))
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{what} needs x1, y1, x2, y2 (mils): {item!r}") from None
    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


def note_records(
    notes: list | None, updates: list | None, moves: list | None
) -> list[list[Any]]:
    """NOTE / NOTEUPD / MOVEOBJ records. Raises ValueError on a bad entry.

    notes: [{x1, y1, x2, y2, text, style_from?, <style>}]; note_updates:
    [{unique_id, text?, x1..y2?, <style>}]; move_objects: [{unique_id,
    offset: [dx, dy]}] (or dx / dy)."""
    records: list[list[Any]] = []
    for n in notes or []:
        if not isinstance(n, dict) or not str(n.get("text", "")):
            raise ValueError(f"notes entry needs text: {n!r}")
        records.append(
            [
                "NOTE",
                *corners(n, "notes entry"),
                str(n.get("style_from") or "").strip(),
                note_text(n["text"]),
                *note_style_fields(n),
            ]
        )
    for u in updates or []:
        uid = str((u or {}).get("unique_id") or "").strip()
        if not uid:
            raise ValueError(f"note_updates entry needs unique_id: {u!r}")
        box: list[Any] = ["", "", "", ""]
        if any(k in u for k in ("x1", "y1", "x2", "y2")):
            box = corners(u, "note_updates entry")
        has_text = "text" in u
        records.append(
            [
                "NOTEUPD",
                uid,
                "1" if has_text else "0",
                note_text(u["text"]) if has_text else "",
                *box,
                *note_style_fields(u),
            ]
        )
    for m in moves or []:
        uid = str((m or {}).get("unique_id") or "").strip()
        offset = m.get("offset")
        if offset is None and ("dx" in m or "dy" in m):
            offset = [m.get("dx", 0), m.get("dy", 0)]
        if not uid or not offset or len(offset) != 2:
            raise ValueError(
                f"move_objects entry needs unique_id and offset [dx, dy]: {m!r}"
            )
        records.append(["MOVEOBJ", uid, int(offset[0]), int(offset[1])])
    return records


def copy_region_records(copies: list | None) -> list[list[Any]]:
    """COPYTEXT / COPYREGION records copying a layout drawing's own graphics
    (notes, pictures, frames, texts) inside a region, moved by an offset:
    [{region: [x1, y1, x2, y2], offset: [dx, dy], types?: [...], exclude?:
    [unique ids], replace_text?: {old: new}}]. Raises ValueError."""
    records: list[list[Any]] = []
    for c in copies or []:
        region = (c or {}).get("region") or []
        offset = c.get("offset") or []
        if len(region) != 4 or len(offset) != 2:
            raise ValueError(
                f"copy_region entries need region [x1, y1, x2, y2] and offset [dx, dy]: {c!r}"
            )
        types = c.get("types") or list(GRAPHICS_TYPES)
        unknown = [t for t in types if str(t).lower() not in GRAPHICS_TYPES]
        if unknown:
            raise ValueError(
                f"copy_region types {unknown} unknown; use {sorted(GRAPHICS_TYPES)}"
            )
        for old, new in (c.get("replace_text") or {}).items():
            if str(old):
                records.append(["COPYTEXT", str(old), str(new)])
        x1, y1, x2, y2 = (int(v) for v in region)
        records.append(
            [
                "COPYREGION",
                min(x1, x2),
                min(y1, y2),
                max(x1, x2),
                max(y1, y2),
                int(offset[0]),
                int(offset[1]),
                ",".join(str(GRAPHICS_TYPES[str(t).lower()]) for t in types),
                ",".join(str(u).strip() for u in c.get("exclude") or []),
            ]
        )
    return records
