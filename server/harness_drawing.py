"""Read and edit Altium Draftsman drawings (.HarDwf, .PCBDwf) on the saved file.

Draftsman has no script interface. Its document is the DataContract XML of
``Altium.Designer.PcbDrawing.DataSerialization.V1.DrawingDocumentData``
(decompiled ``Altium.Designer.PcbDrawing.dll``, ``DataSerializationService``),
written through K4os ``LZ4Stream`` (legacy lz4net format): a sequence of
chunks, each ``varint flags`` (1 = compressed, 2 = high compression),
``varint raw length``, ``varint packed length`` (compressed chunks only) and
the chunk's LZ4 block (or its raw bytes). Draftsman also loads the plain XML:
it decompresses only when the file does not start with ``<Doc``.

Page geometry is in 1/96 inch from the sheet's bottom-left corner, y up
(checked in Altium 26.7: a view moved to y = 100 mm sat 100 mm above the
bottom edge); results report millimetres. Items carry ``<Location>`` +
``<Size>`` (views, tables; Location is the bottom-left corner) or ``<Rect>``
(texts); those taken from the sheet template are marked
``OriginatedFromTemplate``. Item ids are unique in the document; Draftsman
takes max + 1 for a new one.

Component views (``HarnessComponentViewData``) draw the stored projection of
their designator and side (``BoardProjectionInformation`` /
``ComponentProjections``). Draftsman computes one only when none is stored
for that designator and side: it asks the harness document for the
connector's PCB library (``GetLocalComponentPCBLibraryPath(SourceLibraryName,
"", DesignItemId)``, a DbLib lookup without its table name), exports the
footprint's 3D body and projects its visible edges along the side's axis
(``ProjectionByOCC``: Top +Z, Bottom -Z, Front -Y, Back +Y, Left -X, Right
+X; view x = up x dir, y = up; Top/Bottom up +Y, others up +Z). When the
library lookup fails it keeps the stored lines, and an empty entry stays
empty for good. ``set_component_projection`` writes lines computed outside
Altium (e.g. build123d ``project_to_viewport`` of the footprint's STEP model
in the same frame: origin, dir, up), in mm with y up; the view draws stored
y downwards, so they are stored with y negated. Draftsman keeps them across
updates.

Edits splice the original XML text (the serializer's own formatting and
namespace prefixes stay), and the result is written back LZ4-compressed.
"""

from __future__ import annotations

import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

CHUNK_COMPRESSED = 1
CHUNK_HIGH_COMPRESSION = 2
# LZ4Stream's default block size; Draftsman writes 1 MiB chunks.
CHUNK_SIZE = 1 << 20

_MIN_MATCH = 4
# LZ4 block rules: the last match starts at least 12 bytes before the end and
# the last 5 bytes are literals.
_MF_LIMIT = 12
_LAST_LITERALS = 5
_MAX_OFFSET = 0xFFFF
_HASH_BITS = 16


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint in LZ4 stream")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def lz4_block_decompress(src: bytes, raw_length: int) -> bytes:
    """Decode one LZ4 block of known decompressed size."""
    out = bytearray()
    pos = 0
    end = len(src)
    while pos < end:
        token = src[pos]
        pos += 1
        literals = token >> 4
        if literals == 15:
            while True:
                extra = src[pos]
                pos += 1
                literals += extra
                if extra != 255:
                    break
        out += src[pos : pos + literals]
        pos += literals
        if pos >= end:
            break
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(out):
            raise ValueError("corrupt LZ4 block: bad match offset")
        match = (token & 0x0F) + _MIN_MATCH
        if token & 0x0F == 15:
            while True:
                extra = src[pos]
                pos += 1
                match += extra
                if extra != 255:
                    break
        start = len(out) - offset
        if match <= offset:
            out += out[start : start + match]
        else:
            # Overlapping copy (run-length style): repeat the window.
            for i in range(match):
                out.append(out[start + i])
    if len(out) != raw_length:
        raise ValueError(
            f"corrupt LZ4 block: {len(out)} bytes decoded, {raw_length} expected"
        )
    return bytes(out)


def _put_length(out: bytearray, value: int) -> None:
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)


def lz4_block_compress(src: bytes) -> bytes:
    """Encode one LZ4 block (greedy single-hash matcher, LZ4 'fast' style)."""
    n = len(src)
    out = bytearray()
    anchor = 0
    if n > _MF_LIMIT:
        table: dict[bytes, int] = {}
        match_limit = n - _LAST_LITERALS
        pos = 0
        last_start = n - _MF_LIMIT
        while pos < last_start:
            key = src[pos : pos + _MIN_MATCH]
            ref = table.get(key)
            table[key] = pos
            if ref is None or pos - ref > _MAX_OFFSET:
                pos += 1
                continue
            # Extend the match forward, then back over pending literals.
            length = _MIN_MATCH
            while pos + length < match_limit and src[ref + length] == src[pos + length]:
                length += 1
            while pos > anchor and ref > 0 and src[pos - 1] == src[ref - 1]:
                pos -= 1
                ref -= 1
                length += 1
            literals = pos - anchor
            ml = length - _MIN_MATCH
            out.append((min(literals, 15) << 4) | min(ml, 15))
            if literals >= 15:
                _put_length(out, literals - 15)
            out += src[anchor:pos]
            offset = pos - ref
            out.append(offset & 0xFF)
            out.append(offset >> 8)
            if ml >= 15:
                _put_length(out, ml - 15)
            pos += length
            anchor = pos
            # Seed the table inside the match so the next one can find it.
            if pos - 2 < last_start:
                table[src[pos - 2 : pos + 2]] = pos - 2
    literals = n - anchor
    out.append(min(literals, 15) << 4)
    if literals >= 15:
        _put_length(out, literals - 15)
    out += src[anchor:]
    return bytes(out)


def decode_stream(data: bytes) -> bytes:
    """Return the document XML of a Draftsman file's bytes (plain or LZ4Stream)."""
    if data.startswith((b"<Doc", b"\xef\xbb\xbf<Doc")):
        return data
    out = bytearray()
    pos = 0
    while pos < len(data):
        flags, pos = _read_varint(data, pos)
        raw_length, pos = _read_varint(data, pos)
        compressed = bool(flags & CHUNK_COMPRESSED)
        packed_length = raw_length
        if compressed:
            packed_length, pos = _read_varint(data, pos)
        if flags >> 2:
            raise ValueError("LZ4 chunks with multiple passes are not supported")
        chunk = data[pos : pos + packed_length]
        if len(chunk) != packed_length:
            raise ValueError("truncated LZ4 stream")
        pos += packed_length
        out += lz4_block_decompress(chunk, raw_length) if compressed else chunk
    return bytes(out)


def encode_stream(xml: bytes) -> bytes:
    """Write document XML as Draftsman does (LZ4Stream, 1 MiB chunks)."""
    out = bytearray()
    for start in range(0, len(xml), CHUNK_SIZE):
        raw = xml[start : start + CHUNK_SIZE]
        packed = lz4_block_compress(raw)
        if len(packed) < len(raw):
            out += _write_varint(CHUNK_COMPRESSED)
            out += _write_varint(len(raw))
            out += _write_varint(len(packed))
            out += packed
        else:
            out += _write_varint(0)
            out += _write_varint(len(raw))
            out += raw
    return bytes(out)


def read_drawing_xml(path: Path) -> bytes:
    return decode_stream(path.read_bytes())


# --- document model -------------------------------------------------------

UNITS_PER_MM = 96.0 / 25.4
XSI = "http://www.w3.org/2001/XMLSchema-instance"
# HarnessComponentViewData.ViewSide (BoardAssemblyViewSide, stored as int);
# projections store BoardAssemblyProjectionSide by name.
VIEW_SIDES = ["Top", "Bottom", "Left", "Right", "Front", "Back"]
_ITEM_OPEN = re.compile(r'<Item i:type="([^"]+)"><Id>(-?\d+)</Id>')
_VIEW_TYPES = {
    "HarnessComponentViewData",
    "LayoutDrawingViewData",
    "WiringDiagramViewData",
}


class DrawingEditError(ValueError):
    pass


def _mm(units: float) -> float:
    return round(units / UNITS_PER_MM, 3)


def _units(mm: float) -> float:
    return mm * UNITS_PER_MM


def _fmt(value: float) -> str:
    """Number text as the serializer writes it (shortest round-trip form)."""
    text = repr(float(value))
    return text.removesuffix(".0")


def _element_end(xml: str, start: int, tag: str) -> int:
    """Index just past the element named ``tag`` that opens at ``start``."""
    open_re = re.compile(rf"<{tag}(?=[\s>/])")
    close = f"</{tag}>"
    gt = xml.index(">", start)
    if xml[gt - 1] == "/":
        return gt + 1
    depth = 1
    pos = gt + 1
    while depth:
        nxt_close = xml.index(close, pos)
        m = open_re.search(xml, pos, nxt_close)
        if m:
            gt = xml.index(">", m.start())
            if xml[gt - 1] != "/":
                depth += 1
            pos = gt + 1
        else:
            depth -= 1
            pos = nxt_close + len(close)
    return pos


def _child_span(xml: str, start: int, end: int, tag: str) -> tuple[int, int] | None:
    """Span of the first direct child ``tag`` of the element spanning start:end."""
    pos = xml.index(">", start) + 1
    while pos < end:
        m = re.compile(r"<(/?)([A-Za-z_][\w.:-]*)").search(xml, pos, end)
        if not m or m.group(1):
            return None
        name = m.group(2)
        child_end = _element_end(xml, m.start(), name)
        if name == tag:
            return m.start(), child_end
        pos = child_end
    return None


def _parse_fragment(fragment: str) -> ET.Element:
    """Parse an element cut out of the document (prefixes bound to dummies)."""
    prefixes = set(re.findall(r"</?([A-Za-z_][\w-]*):", fragment))
    prefixes |= set(re.findall(r'\s([A-Za-z_][\w-]*):[\w-]+="', fragment))
    prefixes -= {"xmlns", "xml"}
    decl = " ".join(
        f'xmlns:{p}="{XSI if p == "i" else "urn:" + p}"' for p in sorted(prefixes)
    )
    return ET.fromstring(f"<root {decl}>{fragment}</root>")[0]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, *path: str) -> str | None:
    cur: ET.Element | None = el
    for name in path:
        if cur is None:
            return None
        cur = next((c for c in cur if _local(c.tag) == name), None)
    return None if cur is None else cur.text


def _float(text: str | None) -> float | None:
    try:
        return None if text is None else float(text)
    except ValueError:
        return None


@dataclass
class DrawingItem:
    page: int  # 1-based page index
    page_id: str
    id: str
    type: str
    start: int
    end: int
    element: ET.Element

    @property
    def from_template(self) -> bool:
        return _child_text(self.element, "OriginatedFromTemplate") == "true"

    def box(self) -> tuple[float, float, float, float] | None:
        """(x, y, width, height) in page units, when the item has one."""
        e = self.element
        x = _float(_child_text(e, "Location", "X"))
        y = _float(_child_text(e, "Location", "Y"))
        w = _float(_child_text(e, "Size", "Width"))
        h = _float(_child_text(e, "Size", "Height"))
        if x is None:
            x = _float(_child_text(e, "Rect", "X"))
            y = _float(_child_text(e, "Rect", "Y"))
            w = _float(_child_text(e, "Rect", "Width"))
            h = _float(_child_text(e, "Rect", "Height"))
        if x is None or y is None:
            return None
        return x, y, w or 0.0, h or 0.0

    def summary(self) -> dict:
        e = self.element
        out: dict = {"id": self.id, "type": self.type}
        title = _child_text(e, "Title")
        text = _child_text(e, "Text")
        if title:
            out["title"] = title
        if text:
            out["text"] = text
        box = self.box()
        if box:
            out["x_mm"], out["y_mm"] = _mm(box[0]), _mm(box[1])
            out["width_mm"], out["height_mm"] = _mm(box[2]), _mm(box[3])
        if self.type == "HarnessComponentViewData":
            out["designator"] = _child_text(e, "Designator")
            side = _child_text(e, "ViewSide")
            out["view_side"] = (
                VIEW_SIDES[int(side)]
                if side and side.isdigit() and int(side) < len(VIEW_SIDES)
                else side
            )
        if self.type == "Callout":
            out["text"] = _child_text(e, "Sources", "CalloutSource", "CustomText")
            out["target_item"] = _child_text(e, "TargetAnchor", "SnapTargetItemId")
            out["target"] = _child_text(e, "TargetAnchor", "Snap", "ItemId")
        source = _child_text(e, "BoardSourceName")
        if source:
            out["source"] = source
        return out


class HarnessDrawing:
    """A Draftsman document's XML with item and projection lookup."""

    def __init__(self, path: Path):
        self.path = path
        self.xml = read_drawing_xml(path).decode("utf-8")
        self.changed = False

    # pages and items are re-derived from the text after every edit
    def pages(self) -> list[dict]:
        out = []
        start = self.xml.index("<Pages>")
        end = _element_end(self.xml, start, "Pages")
        pos = start + len("<Pages>")
        while True:
            p = self.xml.find("<Page>", pos, end)
            if p < 0:
                break
            p_end = _element_end(self.xml, p, "Page")
            out.append({"start": p, "end": p_end})
            pos = p_end
        for i, page in enumerate(out, 1):
            seg = self.xml[page["start"] : page["end"]]
            page["index"] = i
            page["id"] = re.search(r"<Id>(-?\d+)</Id>", seg).group(1)
            m = re.search(r"<TemplateFileName>([^<]*)</TemplateFileName>", seg)
            page["template"] = m.group(1) if m else ""
            sz = _child_span(self.xml, page["start"], page["end"], "Size")
            size = _parse_fragment(self.xml[sz[0] : sz[1]]) if sz else None
            page["width"] = (
                _float(_child_text(size, "Width")) if size is not None else None
            )
            page["height"] = (
                _float(_child_text(size, "Height")) if size is not None else None
            )
        return out

    def items(self) -> list[DrawingItem]:
        out = []
        for page in self.pages():
            items_span = _child_span(self.xml, page["start"], page["end"], "Items")
            if not items_span:
                continue
            pos = items_span[0] + len("<Items>")
            while True:
                m = _ITEM_OPEN.search(self.xml, pos, items_span[1])
                if not m:
                    break
                end = _element_end(self.xml, m.start(), "Item")
                out.append(
                    DrawingItem(
                        page=page["index"],
                        page_id=page["id"],
                        id=m.group(2),
                        type=m.group(1),
                        start=m.start(),
                        end=end,
                        element=_parse_fragment(self.xml[m.start() : end]),
                    )
                )
                pos = end
        return out

    def item(self, item_id: str | int) -> DrawingItem:
        found = [i for i in self.items() if i.id == str(item_id)]
        if not found:
            raise DrawingEditError(f"no item with id {item_id}")
        return found[0]

    def projections(self) -> list[dict]:
        out = []
        for m in re.finditer(
            r"<ComponentProjectionData>.*?</ComponentProjectionData>",
            self.xml,
            re.DOTALL,
        ):
            el = _parse_fragment(m.group(0))
            lines = next((c for c in el if _local(c.tag) == "DrawingLines"), None)
            bound = next((c for c in el if _local(c.tag) == "Bound"), None)
            bvals = (
                {_local(c.tag): _float(c.text) for c in bound}
                if bound is not None
                else {}
            )
            out.append(
                {
                    "designator": _child_text(el, "Designator"),
                    "view_side": _child_text(el, "ViewSide"),
                    "lines": 0 if lines is None else len(lines),
                    "width_mm": _mm(bvals.get("_width") or 0.0),
                    "height_mm": _mm(bvals.get("_height") or 0.0),
                    "start": m.start(),
                    "end": m.end(),
                }
            )
        return out

    def _replace(self, start: int, end: int, text: str) -> None:
        self.xml = self.xml[:start] + text + self.xml[end:]
        self.changed = True

    def _set_child(self, item: DrawingItem, path: list[str], value: str) -> None:
        start, end = item.start, item.end
        for name in path:
            span = _child_span(self.xml, start, end, name)
            if not span:
                raise DrawingEditError(
                    f"item {item.id} ({item.type}) has no {'/'.join(path)}"
                )
            start, end = span
        tag = path[-1]
        self._replace(start, end, f"<{tag}>{_escape(value)}</{tag}>")

    def save(self, path: Path | None = None) -> None:
        (path or self.path).write_bytes(encode_stream(self.xml.encode("utf-8")))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- review ---------------------------------------------------------------


def review_drawing(path: Path, include_template_items: bool = False) -> dict:
    """Pages, items, component views and their projections, and issues."""
    doc = HarnessDrawing(path)
    pages = doc.pages()
    items = doc.items()
    projections = doc.projections()
    issues: list[dict] = []

    out_pages = []
    for page in pages:
        template = page["template"]
        exists = bool(template) and Path(template).exists()
        if template and not exists:
            issues.append(
                {
                    "kind": "missing_template",
                    "page": page["index"],
                    "template": template,
                    "detail": "the page's sheet template file does not exist; "
                    "Altium cannot update the page from it",
                }
            )
        page_items = [i for i in items if i.page == page["index"]]
        own = [i for i in page_items if not i.from_template]
        out_pages.append(
            {
                "page": page["index"],
                "id": page["id"],
                "width_mm": _mm(page["width"] or 0.0),
                "height_mm": _mm(page["height"] or 0.0),
                "template": template,
                "template_exists": exists,
                "template_items": len(page_items) - len(own),
                "items": [
                    i.summary() for i in (page_items if include_template_items else own)
                ],
            }
        )
        # placement checks on the page's own items
        boxes = [(i, i.box()) for i in own if i.box()]
        for i, (x, y, w, h) in boxes:
            if page["width"] and (
                x < 0 or y < 0 or x + w > page["width"] or y + h > page["height"]
            ):
                issues.append(
                    {
                        "kind": "outside_page",
                        "page": page["index"],
                        "item": i.id,
                        "type": i.type,
                    }
                )
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                (ia, ba), (ib, bb) = boxes[a], boxes[b]
                if min(ba[2], ba[3], bb[2], bb[3]) <= 0:
                    continue
                # a layout / wiring view's box includes its whitespace, where
                # tables and notes are placed on purpose
                if {ia.type, ib.type} & {
                    "LayoutDrawingViewData",
                    "WiringDiagramViewData",
                }:
                    continue
                ox = min(ba[0] + ba[2], bb[0] + bb[2]) - max(ba[0], bb[0])
                oy = min(ba[1] + ba[3], bb[1] + bb[3]) - max(ba[1], bb[1])
                if ox > 0 and oy > 0:
                    issues.append(
                        {
                            "kind": "overlap",
                            "page": page["index"],
                            "items": [ia.id, ib.id],
                            "types": [ia.type, ib.type],
                            "overlap_mm": [_mm(ox), _mm(oy)],
                        }
                    )
        for i in own:
            text = (_child_text(i.element, "Text") or "") + (
                _child_text(i.element, "Title") or ""
            )
            if "TODO" in text.upper():
                issues.append(
                    {
                        "kind": "todo_text",
                        "page": page["index"],
                        "item": i.id,
                        "text": text,
                    }
                )

    views = []
    for i in items:
        if i.type != "HarnessComponentViewData":
            continue
        s = i.summary()
        proj = next(
            (
                p
                for p in projections
                if p["designator"] == s["designator"]
                and p["view_side"] == s["view_side"]
            ),
            None,
        )
        s["page"] = i.page
        s["projection_lines"] = proj["lines"] if proj else None
        views.append(s)
        if not proj or not proj["lines"]:
            issues.append(
                {
                    "kind": "empty_component_view",
                    "page": i.page,
                    "item": i.id,
                    "designator": s["designator"],
                    "view_side": s["view_side"],
                    "detail": (
                        "an empty projection is stored for this designator and side; "
                        "Draftsman never recomputes it (its library lookup failed). "
                        "Write lines with set_component_projection"
                        if proj
                        else "no projection stored; Draftsman computes one on update"
                    ),
                }
            )

    return {
        "document": str(path),
        "source": next(
            (s for i in items if (s := _child_text(i.element, "BoardSourceName"))), None
        ),
        "pages": out_pages,
        "component_views": views,
        "projections": [
            {k: v for k, v in p.items() if k not in ("start", "end")}
            for p in projections
        ],
        "issues": issues,
    }


# --- edits ----------------------------------------------------------------


def _side_name(value: str | int) -> str:
    if isinstance(value, int) or str(value).isdigit():
        idx = int(value)
        if 0 <= idx < len(VIEW_SIDES):
            return VIEW_SIDES[idx]
    for name in VIEW_SIDES:
        if str(value).lower() == name.lower():
            return name
    raise DrawingEditError(f"view_side must be one of {VIEW_SIDES}: {value!r}")


def _projection_xml(designator: str, side: str, lines_mm: list) -> str:
    if not lines_mm:
        raise DrawingEditError("lines_mm is empty")
    parts = []
    xs: list[float] = []
    ys: list[float] = []
    for line in lines_mm:
        if not isinstance(line, (list, tuple)) or len(line) != 4:
            raise DrawingEditError(f"each line is [x1, y1, x2, y2] in mm: {line!r}")
        # The view draws stored y downwards (checked in Altium 26.7: a Back
        # view stored y-up showed the latch at the bottom), so store -y.
        x1, y1, x2, y2 = (_units(float(v)) for v in line)
        y1, y2 = -y1, -y2
        xs += [x1, x2]
        ys += [y1, y2]
        parts.append(
            "<DrawingLineData>"
            f"<End><X>{_fmt(x2)}</X><Y>{_fmt(y2)}</Y></End><EndZ>0</EndZ>"
            f"<Start><X>{_fmt(x1)}</X><Y>{_fmt(y1)}</Y></Start><StartZ>0</StartZ>"
            "</DrawingLineData>"
        )
    bound = (
        '<Bound xmlns:a="http://schemas.datacontract.org/2004/07/System.Windows">'
        f"<a:_height>{_fmt(max(ys) - min(ys))}</a:_height>"
        f"<a:_width>{_fmt(max(xs) - min(xs))}</a:_width>"
        f"<a:_x>{_fmt(min(xs))}</a:_x><a:_y>{_fmt(min(ys))}</a:_y></Bound>"
    )
    return (
        f"<ComponentProjectionData>{bound}<Designator>{_escape(designator)}</Designator>"
        f"<DistanceToBoardEdge>0</DistanceToBoardEdge>"
        f"<DrawingLines>{''.join(parts)}</DrawingLines><ViewSide>{side}</ViewSide>"
        "</ComponentProjectionData>"
    )


def _projection_lines(op: dict, side: str) -> list:
    """lines_mm, or lines_file: JSON [[x1, y1, x2, y2], ...] or {side: [...]}."""
    if op.get("lines_mm"):
        return op["lines_mm"]
    if not op.get("lines_file"):
        raise DrawingEditError("lines_mm or lines_file is required")
    try:
        data = json.loads(Path(op["lines_file"]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DrawingEditError(f"lines_file not read: {e}") from None
    if isinstance(data, dict):
        data = next((v for k, v in data.items() if k.lower() == side.lower()), None)
        if data is None:
            raise DrawingEditError(f"lines_file has no '{side}' entry")
    return data


def _op_set_component_projection(doc: HarnessDrawing, op: dict) -> dict:
    designator = op.get("designator")
    if not designator:
        raise DrawingEditError("designator is required")
    side = _side_name(op.get("view_side", "Top"))
    lines = _projection_lines(op, side)
    new = _projection_xml(designator, side, lines)
    replaced = 0
    for p in reversed(doc.projections()):
        if p["designator"] == designator and p["view_side"] == side:
            doc._replace(p["start"], p["end"], "")
            replaced += 1
    start = doc.xml.find("<BoardProjectionInformation>")
    if start < 0:
        raise DrawingEditError("the document has no BoardProjectionInformation")
    end = _element_end(doc.xml, start, "BoardProjectionInformation")
    span = _child_span(doc.xml, start, end, "ComponentProjections")
    if span is None:
        raise DrawingEditError("the document has no ComponentProjections list")
    s, e = span
    if doc.xml[s:e].endswith("/>"):
        doc._replace(s, e, f"<ComponentProjections>{new}</ComponentProjections>")
    else:
        close = e - len("</ComponentProjections>")
        doc._replace(close, close, new)
    return {
        "designator": designator,
        "view_side": side,
        "lines": len(lines),
        "replaced": replaced,
    }


def _op_remove_component_projection(doc: HarnessDrawing, op: dict) -> dict:
    designator = op.get("designator")
    side = _side_name(op["view_side"]) if op.get("view_side") is not None else None
    removed = 0
    for p in reversed(doc.projections()):
        if p["designator"] == designator and side in (None, p["view_side"]):
            doc._replace(p["start"], p["end"], "")
            removed += 1
    if not removed:
        raise DrawingEditError(f"no projection stored for {designator} {side or ''}")
    return {"designator": designator, "view_side": side, "removed": removed}


def _op_set_component_view(doc: HarnessDrawing, op: dict) -> dict:
    item = doc.item(op.get("item_id"))
    if item.type != "HarnessComponentViewData":
        raise DrawingEditError(f"item {item.id} is a {item.type}, not a component view")
    out: dict = {"item_id": item.id}
    if op.get("designator"):
        doc._set_child(item, ["Designator"], op["designator"])
        out["designator"] = op["designator"]
        item = doc.item(item.id)
    if op.get("view_side") is not None:
        side = _side_name(op["view_side"])
        doc._set_child(item, ["ViewSide"], str(VIEW_SIDES.index(side)))
        out["view_side"] = side
        item = doc.item(item.id)
    if op.get("scale") is not None:
        out["scale"] = _set_scale(doc, item, op["scale"])
    return out


def _set_scale(doc: HarnessDrawing, item: DrawingItem, value) -> str | float:
    # EffectiveScaleValue: UseCustomScale ? CustomScale : Numenator / Denominator
    scale = float(value)
    if scale <= 0:
        raise DrawingEditError(f"scale must be positive: {scale}")
    num, den = (round(scale), 1) if scale >= 1 else (1, round(1 / scale))
    exact = abs(num / den - scale) < 1e-9
    if exact:
        doc._set_child(item, ["Scale", "Numenator"], str(num))
        doc._set_child(doc.item(item.id), ["Scale", "Denominator"], str(den))
    doc._set_child(doc.item(item.id), ["UseCustomScale"], "false" if exact else "true")
    doc._set_child(doc.item(item.id), ["CustomScale"], _fmt(scale))
    return f"{num}:{den}" if exact else scale


def _op_set_view_scale(doc: HarnessDrawing, op: dict) -> dict:
    """Scale of any view (layout, wiring diagram, component): e.g. 2 (2:1),
    0.5 (1:2), 0.75 (custom)."""
    item = doc.item(op.get("item_id"))
    if item.type not in _VIEW_TYPES:
        raise DrawingEditError(f"item {item.id} is a {item.type}, not a view")
    if op.get("scale") is None:
        raise DrawingEditError("scale is required")
    return {"item_id": item.id, "scale": _set_scale(doc, item, op["scale"])}


def _title_element(doc: HarnessDrawing, item: DrawingItem) -> tuple[int, int]:
    """Span of the view's linked title text (ElementItemData named Title)."""
    items = _child_span(doc.xml, item.start, item.end, "ElementItems")
    if items:
        pos = items[0]
        while m := re.compile(r"<ElementItemData[\s>]").search(doc.xml, pos, items[1]):
            end = _element_end(doc.xml, m.start(), "ElementItemData")
            if "<Name>Title</Name>" in doc.xml[m.start() : end]:
                return m.start(), end
            pos = end
    raise DrawingEditError(f"item {item.id} ({item.type}) has no title")


def _op_set_view_title(doc: HarnessDrawing, op: dict) -> dict:
    """A view's title text and position: title_position "auto" (Draftsman
    places it) or [dx_mm, dy_mm] from the view's position (y up)."""
    item = doc.item(op.get("item_id"))
    if item.type not in _VIEW_TYPES:
        raise DrawingEditError(f"item {item.id} is a {item.type}, not a view")
    out: dict = {"item_id": item.id}
    if op.get("title") is not None:
        title = str(op["title"])
        start, end = _title_element(doc, item)
        span = _child_span(doc.xml, start, end, "Title")
        doc._replace(span[0], span[1], f"<Title>{_escape(title)}</Title>")
        doc._set_child(doc.item(item.id), ["Title"], title)
        out["title"] = title
    position = op.get("title_position")
    if position is not None:
        if position == "auto":
            manual, dx, dy = "false", 0.0, 0.0
        elif isinstance(position, (list, tuple)) and len(position) == 2:
            manual, dx, dy = (
                "true",
                _units(float(position[0])),
                _units(float(position[1])),
            )
        else:
            raise DrawingEditError(
                f'title_position is "auto" or [dx_mm, dy_mm]: {position!r}'
            )
        for path, value in (
            (["ManualPositioning"], manual),
            (["ManualOffset", "X"], _fmt(dx)),
            (["ManualOffset", "Y"], _fmt(dy)),
        ):
            start, end = _title_element(doc, doc.item(item.id))
            for name in path:
                span = _child_span(doc.xml, start, end, name)
                if not span:
                    raise DrawingEditError(f"item {item.id} title has no {name}")
                start, end = span
            doc._replace(start, end, f"<{path[-1]}>{value}</{path[-1]}>")
        out["title_position"] = position
    return out


def _op_move_item(doc: HarnessDrawing, op: dict) -> dict:
    item = doc.item(op.get("item_id"))
    if "x_mm" not in op or "y_mm" not in op:
        raise DrawingEditError("x_mm and y_mm are required")
    x, y = _fmt(_units(float(op["x_mm"]))), _fmt(_units(float(op["y_mm"])))
    holder = (
        "Location"
        if _child_span(doc.xml, item.start, item.end, "Location")
        else "Rect"
        if _child_span(doc.xml, item.start, item.end, "Rect")
        else None
    )
    if not holder:
        raise DrawingEditError(f"item {item.id} ({item.type}) has no position")
    doc._set_child(item, [holder, "X"], x)
    doc._set_child(doc.item(item.id), [holder, "Y"], y)
    return {"item_id": item.id, "x_mm": op["x_mm"], "y_mm": op["y_mm"]}


def _op_copy_item(doc: HarnessDrawing, op: dict) -> dict:
    """Copy an item onto its page under a new id; optional x_mm / y_mm, and
    for a component view designator / view_side / scale."""
    item = doc.item(op.get("item_id"))
    if item.from_template:
        raise DrawingEditError(f"item {item.id} comes from the sheet template")
    ids = [int(i.id) for i in doc.items()] + [int(p["id"]) for p in doc.pages()]
    new_id = str(max(ids) + 1)
    text = doc.xml[item.start : item.end]
    text = text.replace(f"<Id>{item.id}</Id>", f"<Id>{new_id}</Id>", 1)
    doc._replace(item.end, item.end, text)
    out: dict = {"item_id": item.id, "new_item_id": new_id}
    if "x_mm" in op or "y_mm" in op:
        box = doc.item(new_id).box() or (0.0, 0.0, 0.0, 0.0)
        move = {
            "item_id": new_id,
            "x_mm": op.get("x_mm", _mm(box[0])),
            "y_mm": op.get("y_mm", _mm(box[1])),
        }
        _op_move_item(doc, move)
        out["x_mm"], out["y_mm"] = move["x_mm"], move["y_mm"]
    view = {k: op[k] for k in ("designator", "view_side", "scale") if k in op}
    if view:
        out.update(
            {
                k: v
                for k, v in _op_set_component_view(
                    doc, {"item_id": new_id, **view}
                ).items()
                if k != "item_id"
            }
        )
    return out


def _snap_number(value: float) -> str:
    """A double as .NET writes it in a snap id (shortest round-trip)."""
    text = repr(float(value))
    return text.removesuffix(".0")


# Draftsman's default arrow (what the GUI adds with the document's first
# callout); a callout whose ArrowStyleId is not defined is not drawn.
_ARROW_STYLE_1 = (
    "<ArrowStyleData><FullLength>24</FullLength><HeadStyle>FilledArrow</HeadStyle>"
    "<HeadStyleV2>1</HeadStyleV2><Id>1</Id><Length>12.47244094488189</Length>"
    "<Width>4.1574803149606305</Width></ArrowStyleData>"
)


def _ensure_arrow_style(doc: HarnessDrawing) -> None:
    start = doc.xml.find("<ArrowStyles")
    if start < 0:
        raise DrawingEditError("the document has no ArrowStyles list")
    end = _element_end(doc.xml, start, "ArrowStyles")
    if re.search(r"<ArrowStyleData>.*?<Id>1</Id>", doc.xml[start:end], re.DOTALL):
        return
    if doc.xml[start:end].endswith("/>"):
        doc._replace(start, end, f"<ArrowStyles>{_ARROW_STYLE_1}</ArrowStyles>")
    else:
        close = end - len("</ArrowStyles>")
        doc._replace(close, close, _ARROW_STYLE_1)


def _op_add_callout(doc: HarnessDrawing, op: dict) -> dict:
    """A callout with custom text on a component view, snapped to a point of its
    projection: point_mm [x, y] in the projection frame (the frame of
    set_component_projection, mm, y up), leader_mm [dx, dy] the text's offset
    on the page from that point (mm, y up). Draftsman keeps the callout on the
    point when the view moves or is rescaled (it stores the snap)."""
    view = doc.item(op.get("item_id"))
    if view.type != "HarnessComponentViewData":
        raise DrawingEditError(f"item {view.id} is a {view.type}, not a component view")
    text = str(op.get("text", "")).strip()
    if not text:
        raise DrawingEditError("text is required")
    point = op.get("point_mm")
    leader = op.get("leader_mm", [10, 10])
    if not (isinstance(point, (list, tuple)) and len(point) == 2):
        raise DrawingEditError("point_mm is [x, y] in the projection frame (mm)")
    designator = _child_text(view.element, "Designator") or ""
    side = _side_name(_child_text(view.element, "ViewSide") or "0")
    proj = next(
        (
            p
            for p in doc.projections()
            if p["designator"] == designator and p["view_side"] == side
        ),
        None,
    )
    if proj is None or not proj["lines"]:
        raise DrawingEditError(f"no projection stored for {designator} {side}")
    seg = doc.xml[proj["start"] : proj["end"]]
    bound = {k: float(v) for k, v in re.findall(r"<a:_(\w+)>([^<]+)</a:_\w+>", seg)}
    scale = 1.0
    if _child_text(view.element, "UseCustomScale") == "true":
        scale = _float(_child_text(view.element, "CustomScale")) or 1.0
    else:
        num = _float(_child_text(view.element, "Scale", "Numenator")) or 1.0
        den = _float(_child_text(view.element, "Scale", "Denominator")) or 1.0
        scale = num / den
    box = view.box() or (0.0, 0.0, 0.0, 0.0)
    # stored projection: x as given, y negated (see _projection_xml)
    sx, sy = _units(float(point[0])), -_units(float(point[1]))
    page_x = box[0] + (sx - bound["x"]) * scale
    page_y = box[1] + (bound["y"] + bound["height"] - sy) * scale
    lx, ly = _units(float(leader[0])), _units(float(leader[1]))
    ids = [int(i.id) for i in doc.items()] + [int(p["id"]) for p in doc.pages()]
    new_id = str(max(ids) + 1)
    snap_id = f"GI|{designator}|Point: {_snap_number(sx)}, {_snap_number(sy)}"
    anchor = (
        "<AnchorType>Snap</AnchorType><AnchorType2>Snap</AnchorType2>"
        "<AnchorTypeV3>2</AnchorTypeV3>"
        f"<ContentPoint><X>{_fmt(page_x)}</X><Y>{_fmt(page_y)}</Y></ContentPoint>"
        f"<PointOnPage><X>{_fmt(page_x)}</X><Y>{_fmt(page_y)}</Y></PointOnPage>"
        f'<Snap><Arc i:nil="true"/><GroupId>GI</GroupId><ItemId>{_escape(designator)}'
        f"</ItemId><Points><PointData><X>{_fmt(sx)}</X><Y>{_fmt(sy)}</Y></PointData>"
        '</Points><PointsZ xmlns:a="http://schemas.microsoft.com/2003/10/'
        'Serialization/Arrays"><a:double>0</a:double></PointsZ>'
        "<SnapDesignation>0</SnapDesignation><SnapType>0</SnapType></Snap>"
        "<SnapDesignationData>None</SnapDesignationData>"
        "<SnapDesignationDataV2>0</SnapDesignationDataV2>"
        f"<SnapId>{_escape(snap_id)}</SnapId><SnapIdV2>{_escape(snap_id)}</SnapIdV2>"
        "<SnapPointOffset><X>0</X><Y>0</Y></SnapPointOffset>"
        f"<SnapTargetItemId>{view.id}</SnapTargetItemId>"
    )
    source = (
        "<CalloutSource><BorderStyle>Underline</BorderStyle>"
        "<BorderStyleV2>4</BorderStyleV2><ComponentParameter><BomColumnType>"
        "Designator</BomColumnType><BomColumnTypeV2>2</BomColumnTypeV2><DataType>0"
        '</DataType><ParameterSource i:nil="true"/><ParameterSourceV2 i:nil="true"/>'
        f"<SchematicParameter/></ComponentParameter><CustomText>{_escape(text)}"
        f"</CustomText><NoteElementId>{uuid.uuid4().hex}</NoteElementId>"
        "<SourceType>CustomText</SourceType><SourceTypeV2>0</SourceTypeV2>"
        "<SymbolReference>0.5</SymbolReference><TextAboveLine/>"
        "<TextAboveReference>0.5</TextAboveReference><TextBelowLine/>"
        "<TextBelowReference>0.5</TextBelowReference></CalloutSource>"
    )
    _ensure_arrow_style(doc)
    elbow = "Left" if float(leader[0]) < 0 else "Right"
    item = (
        f'<Item i:type="Callout"><Id>{new_id}</Id><Anchor>Top Left</Anchor>'
        "<OriginatedFromTemplate>false</OriginatedFromTemplate>"
        "<ArrowStyleId>1</ArrowStyleId><BackgroundColor><A>0</A><B>255</B><G>255</G>"
        "<R>255</R></BackgroundColor><BorderAutoSize>true</BorderAutoSize>"
        "<BorderHeight>37.79527559055118</BorderHeight><BorderLineStyleId>2"
        "</BorderLineStyleId><BorderWidth>37.79527559055118</BorderWidth>"
        "<ElbowLength>7.559055118110236</ElbowLength>"
        f"<ElbowPosition>{elbow}</ElbowPosition>"
        f"<ElbowPositionV2>{0 if elbow == 'Right' else 1}</ElbowPositionV2>"
        "<ElbowStyle>ElbowBeforeTag</ElbowStyle><ElbowStyleV2>1</ElbowStyleV2>"
        "<FontStyleId>1</FontStyleId><ForegroundTextColor><A>255</A><B>0</B><G>0</G>"
        f"<R>192</R></ForegroundTextColor><Leader><X>{_fmt(lx)}</X><Y>{_fmt(ly)}</Y>"
        "</Leader><LineStyleId>2</LineStyleId><Prefix/>"
        f"<Sources>{source}</Sources><Symbol>0</Symbol>"
        "<SymbolContour>false</SymbolContour><SymbolReference>0.5</SymbolReference>"
        f"<TargetAnchor>{anchor}</TargetAnchor><TargetAnchors><Anchors>"
        f"<DimensionAnchor>{anchor}</DimensionAnchor></Anchors>"
        f"<Sources>{source}</Sources></TargetAnchors>"
        "<UseDocumentFont>true</UseDocumentFont></Item>"
    )
    page = next(p for p in doc.pages() if p["index"] == view.page)
    items = _child_span(doc.xml, page["start"], page["end"], "Items")
    close = items[1] - len("</Items>")
    doc._replace(close, close, item)
    return {
        "item_id": view.id,
        "new_item_id": new_id,
        "text": text,
        "x_mm": _mm(page_x),
        "y_mm": _mm(page_y),
    }


# What a table's "format" is: the column set, order, visibility and sorting.
_TABLE_FORMAT = {
    "ConnectionTableData": "Columns",
    "WiringListData": "Columns",
    "BillOfMaterialsView": "ShowedColumns",
}


def _op_copy_table_format(doc: HarnessDrawing, op: dict) -> dict:
    """Copy a table's columns (set, order, visibility, sort, widths) onto other
    tables of its type: from_item_id in this drawing, or in from_document (a
    .HarDwf); targets item_ids, or every table of that type in this drawing."""
    source_doc = doc
    if op.get("from_document"):
        source_doc = HarnessDrawing(Path(op["from_document"]))
    source = source_doc.item(op.get("from_item_id"))
    tag = _TABLE_FORMAT.get(source.type)
    if not tag:
        raise DrawingEditError(
            f"item {source.id} is a {source.type}; tables are {sorted(_TABLE_FORMAT)}"
        )
    span = _child_span(source_doc.xml, source.start, source.end, tag)
    if not span:
        raise DrawingEditError(f"item {source.id} has no {tag}")
    columns = source_doc.xml[span[0] : span[1]]
    wanted = [str(i) for i in op["item_ids"]] if op.get("item_ids") else None
    changed = []
    # from the end, so a replacement does not move the items still to do
    for item in reversed(doc.items()):
        if item.type != source.type or item.from_template:
            continue
        if wanted is not None and item.id not in wanted:
            continue
        if source_doc is doc and item.id == source.id:
            continue
        target = _child_span(doc.xml, item.start, item.end, tag)
        if not target:
            raise DrawingEditError(f"item {item.id} has no {tag}")
        if doc.xml[target[0] : target[1]] != columns:
            doc._replace(target[0], target[1], columns)
            changed.append(item.id)
    missing = sorted(set(wanted or []) - {i.id for i in doc.items()})
    if missing:
        raise DrawingEditError(f"no item with id {', '.join(missing)}")
    return {"from_item_id": source.id, "type": source.type, "changed": changed[::-1]}


def _op_set_text(doc: HarnessDrawing, op: dict) -> dict:
    item = doc.item(op.get("item_id"))
    if item.type != "Text":
        raise DrawingEditError(
            f"item {item.id} is a {item.type}; set_text edits Text items"
        )
    doc._set_child(item, ["Text"], str(op.get("text", "")))
    return {"item_id": item.id, "text": op.get("text", "")}


def _op_delete_item(doc: HarnessDrawing, op: dict) -> dict:
    item = doc.item(op.get("item_id"))
    if item.from_template:
        raise DrawingEditError(f"item {item.id} comes from the sheet template")
    doc._replace(item.start, item.end, "")
    return {"item_id": item.id, "type": item.type, "page": item.page}


def _op_set_template(doc: HarnessDrawing, op: dict) -> dict:
    """Point pages at another sheet template file (``pages``: 1-based indexes,
    or omit with ``from`` to replace every page using that path)."""
    to = op.get("to")
    if not to or not Path(to).exists():
        raise DrawingEditError(f"template file not found: {to!r}")
    to = str(Path(to).resolve())
    wanted = op.get("pages")
    old = op.get("from")
    if wanted is None and not old:
        raise DrawingEditError("give pages or from")
    changed = []
    for page in reversed(doc.pages()):
        if wanted is not None and page["index"] not in wanted:
            continue
        if old and str(Path(page["template"])).lower() != str(Path(old)).lower():
            continue
        span = _child_span(doc.xml, page["start"], page["end"], "TemplateFileName")
        if not span:
            continue
        doc._replace(
            span[0], span[1], f"<TemplateFileName>{_escape(to)}</TemplateFileName>"
        )
        changed.append(page["index"])
    if not changed:
        raise DrawingEditError("no page matched")
    return {"pages": sorted(changed), "template": to}


OPERATIONS = {
    "set_component_projection": _op_set_component_projection,
    "remove_component_projection": _op_remove_component_projection,
    "set_component_view": _op_set_component_view,
    "set_view_title": _op_set_view_title,
    "set_view_scale": _op_set_view_scale,
    "move_item": _op_move_item,
    "copy_item": _op_copy_item,
    "add_callout": _op_add_callout,
    "copy_table_format": _op_copy_table_format,
    "set_text": _op_set_text,
    "delete_item": _op_delete_item,
    "set_template": _op_set_template,
}


def apply_operations(doc: HarnessDrawing, operations: list) -> list[dict]:
    """Apply operations in order; raises DrawingEditError on the first failure."""
    results = []
    for n, op in enumerate(operations, 1):
        if not isinstance(op, dict) or op.get("op") not in OPERATIONS:
            raise DrawingEditError(
                f"operation {n}: op must be one of {sorted(OPERATIONS)}: {op!r}"
            )
        try:
            result = OPERATIONS[op["op"]](doc, op)
        except DrawingEditError as e:
            raise DrawingEditError(f"operation {n} ({op['op']}): {e}") from None
        results.append({"op": op["op"], **result})
    return results
