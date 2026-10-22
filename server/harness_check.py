"""Read-only drawing check for Altium Harness Wiring Diagrams (.WirDoc).

Wires are routed by the agent (skill altium-harness-routing) and moved in place
with add_harness_wiring(reroutes=...). This module reports what is wrong with a
sheet's saved drawing, against the AGENTS.md drawing rules:

errors   - diagonal segments, wire ends on neither a pin hot end nor a splice,
           wires sharing a stretch with another wire (drawn on top of it),
           wires running through another connector's pins;
warnings - crossings too close to a bend, parallel wires closer than 100 mils,
           vertical drops closer than 200 mils, channels in front of pin names,
           wire labels on other wires, labels, pins or sheet text, labels away
           from their wire, twist marks not in the sheet style (rectangle 100
           mils beyond each wire so the hook tips land on the wires, 500 mils
           along them) or on wire labels.

Crossings are counted and listed but are not problems in themselves: a crossing
that comes from the pin order of two connectors cannot be routed away.
All coordinates here are mils.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_parser import parse_wirdoc

# Designator -> comment distance along the wire (AGENTS.md: 350-400 mils)
LABEL_SPACING = 375
LABEL_ABOVE = 15
LABEL_BELOW = -85
LABEL_CHAR_WIDTH = 60
LABEL_HEIGHT = 80
TEXT_HEIGHT = 100
PIN_NAME_CHAR_WIDTH = 65
NAME_CLEARANCE = 150
MIN_PARALLEL = 100
MIN_CHANNEL_PITCH = 200
MIN_STRAIGHT = 100
LABEL_REACH = 120
# Twist mark (user's style): 500 mils along the pair, rectangle 100 mils beyond
# each wire so the hook tips land on the wires
TWIST_LENGTH = 500
TWIST_OVERHANG = 100
MAX_LISTED = 200

Segment = tuple[int, int, int, int]
Box = tuple[int, int, int, int]


def label_positions(
    points: list[int], mode: str = "above", spacing: int = LABEL_SPACING
) -> tuple[list[int], list[int]]:
    """Side-by-side designator and comment positions for a wire, in mils.

    Centred on the midpoint of the longest horizontal segment, just above it
    (or below for mode="below"). A wire without a horizontal segment gets its
    labels to the right of the midpoint of its longest vertical segment.
    """
    best = None
    for k in range(0, len(points) - 2, 2):
        xa, ya, xb, yb = points[k : k + 4]
        horizontal = ya == yb
        length = abs(xb - xa) if horizontal else abs(yb - ya)
        key = (horizontal, length)
        if best is None or key > best[0]:
            best = (key, xa, ya, xb, yb)
    _, xa, ya, xb, yb = best
    if ya == yb:
        mid_x = (xa + xb) // 2
        y = ya + (LABEL_BELOW if mode == "below" else LABEL_ABOVE)
        des_x = mid_x - spacing // 2
        return [des_x, y], [des_x + spacing, y]
    mid_y = (ya + yb) // 2
    return [xa + 30, mid_y], [xa + 30 + spacing, mid_y]


def _mils(v: float) -> int:
    return round(v * 10)


def _segments(points: list[int]) -> list[Segment]:
    return [tuple(points[k : k + 4]) for k in range(0, len(points) - 2, 2)]


def _crossing_point(a: Segment, b: Segment) -> tuple[int, int] | None:
    """Point where a horizontal and a vertical segment properly cross."""
    for h, v in ((a, b), (b, a)):
        if h[1] == h[3] and v[0] == v[2]:
            hx1, hx2, hy = min(h[0], h[2]), max(h[0], h[2]), h[1]
            vx, vy1, vy2 = v[0], min(v[1], v[3]), max(v[1], v[3])
            if hx1 < vx < hx2 and vy1 < hy < vy2:
                return vx, hy
    return None


def _shared_stretch(a: Segment, b: Segment) -> int:
    """Length two collinear segments share (0 when not collinear)."""
    if a[1] == a[3] == b[1] == b[3]:
        lo = max(min(a[0], a[2]), min(b[0], b[2]))
        hi = min(max(a[0], a[2]), max(b[0], b[2]))
    elif a[0] == a[2] == b[0] == b[2]:
        lo = max(min(a[1], a[3]), min(b[1], b[3]))
        hi = min(max(a[1], a[3]), max(b[1], b[3]))
    else:
        return 0
    return max(0, hi - lo)


def _parallel_gap(a: Segment, b: Segment) -> tuple[str, int] | None:
    """('h' | 'v', distance) of two parallel, side-by-side, non-collinear segments."""
    if a[1] == a[3] and b[1] == b[3] and a[1] != b[1]:
        lo = max(min(a[0], a[2]), min(b[0], b[2]))
        hi = min(max(a[0], a[2]), max(b[0], b[2]))
        return ("h", abs(a[1] - b[1])) if hi > lo else None
    if a[0] == a[2] and b[0] == b[2] and a[0] != b[0]:
        lo = max(min(a[1], a[3]), min(b[1], b[3]))
        hi = min(max(a[1], a[3]), max(b[1], b[3]))
        return ("v", abs(a[0] - b[0])) if hi > lo else None
    return None


def _segment_in_box(s: Segment, box: Box) -> bool:
    """True when the segment passes through the inside of the box."""
    x1, y1, x2, y2 = box
    return (
        max(s[0], s[2]) > x1
        and min(s[0], s[2]) < x2
        and max(s[1], s[3]) > y1
        and min(s[1], s[3]) < y2
    )


def _boxes_overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _text_box(x: int, y: int, text: str, height: int = LABEL_HEIGHT) -> Box:
    return (x, y, x + max(1, len(text)) * LABEL_CHAR_WIDTH, y + height)


def _pins(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Every pin with its connector, hot end, base and the clear distance in
    front of it (pin name and sheet text next to the pin, + NAME_CLEARANCE)."""
    texts = [
        (_mils(t["x"]), _mils(t["y"]), len(t.get("text", "")))
        for t in parsed.get("net_labels", [])
    ]
    out = []
    for c in parsed.get("connectors", []):
        for pin in c.get("pins", []):
            hx, hy = _mils(pin["hot_end"]["x"]), _mils(pin["hot_end"]["y"])
            bx, by = _mils(pin["location"]["x"]), _mils(pin["location"]["y"])
            horizontal = hy == by
            extent = max(0, len(pin.get("name", "")) * PIN_NAME_CHAR_WIDTH - abs(hx - bx))
            facing_right = hx >= bx
            if horizontal:
                for tx, ty, n in texts:
                    if abs(ty - hy) > 60:
                        continue
                    if facing_right and hx <= tx <= hx + 4000:
                        extent = max(extent, tx + n * LABEL_CHAR_WIDTH - hx)
                    elif not facing_right and hx - 4000 <= tx <= hx:
                        extent = max(extent, hx - tx)
            out.append(
                {
                    "connector": c.get("designator", "?"),
                    "pin": pin.get("designator", ""),
                    "hot": (hx, hy),
                    "base": (bx, by),
                    "horizontal": horizontal,
                    "facing_right": facing_right,
                    "keep": extent + NAME_CLEARANCE,
                }
            )
    return out


def _connector_zones(pins: list[dict[str, Any]]) -> dict[str, Box]:
    """Connector -> box over its pins (hot ends to bases, +-50 mils across)."""
    zones: dict[str, Box] = {}
    for p in pins:
        xs = (p["hot"][0], p["base"][0])
        ys = (p["hot"][1], p["base"][1])
        box = (min(xs), min(ys) - 50, max(xs), max(ys) + 50)
        old = zones.get(p["connector"])
        zones[p["connector"]] = (
            box
            if old is None
            else (min(old[0], box[0]), min(old[1], box[1]), max(old[2], box[2]), max(old[3], box[3]))
        )
    return zones


def check_harness_wiring(document_path: Path | str) -> dict[str, Any]:
    """Check the saved drawing of a .WirDoc against the AGENTS.md drawing rules.

    Returns:
        {"clean": bool, "summary": {...}, "problems": [...], "crossings": [...]}
        Each problem is {"severity": "error" | "warning", "type", "wires", "detail"}
        with an "at" point (mils) where one applies.
    """
    doc_path = Path(document_path)
    if not doc_path.exists():
        raise FileNotFoundError(f"WirDoc file not found: {doc_path}")
    parsed = parse_wirdoc(doc_path)

    problems: list[dict[str, Any]] = []

    def report(severity: str, kind: str, wires: list[str], detail: str, at=None) -> None:
        item: dict[str, Any] = {"severity": severity, "type": kind, "wires": wires, "detail": detail}
        if at is not None:
            item["at"] = list(at)
        problems.append(item)

    wires = [
        {
            "designator": w.get("designator", "?"),
            "net": w.get("net", ""),
            "points": [_mils(v) for v in w.get("points", [])],
            "labels": [
                (w.get(key), text)
                for key, text in (
                    ("designator_pos", w.get("designator", "")),
                    ("comment_pos", w.get("net", "")),
                )
                if w.get(key)
            ],
        }
        for w in parsed.get("wires", [])
    ]
    pins = _pins(parsed)
    zones = _connector_zones(pins)
    hot_ends = {p["hot"]: p for p in pins}
    # Wire ends may also sit on splices and on wire breaks (cross-sheet joins)
    splices = {
        (_mils(s["location"]["x"]), _mils(s["location"]["y"])) for s in parsed.get("splices", [])
    } | {(_mils(b["x"]), _mils(b["y"])) for b in parsed.get("wire_breaks", [])}

    # Geometry of each wire on its own
    for w in wires:
        pts = w["points"]
        des = w["designator"]
        if len(pts) < 4:
            report("error", "degenerate", [des], "wire has fewer than two vertices")
            continue
        for s in _segments(pts):
            if s[0] != s[2] and s[1] != s[3]:
                report("error", "diagonal", [des], "segment is neither horizontal nor vertical", s[:2])
        for end in ((pts[0], pts[1]), (pts[-2], pts[-1])):
            if end not in hot_ends and end not in splices:
                report("error", "dangling_end", [des], "end is on no pin, splice or wire break", end)

    # Wires that run through a connector's pins (other than the segment that
    # ends on one of its pins)
    for w in wires:
        segs = _segments(w["points"])
        for i, s in enumerate(segs):
            ends = {(s[0], s[1]), (s[2], s[3])}
            for conn, box in zones.items():
                own_end = any(
                    e in hot_ends and hot_ends[e]["connector"] == conn for e in ends
                ) and i in (0, len(segs) - 1)
                if not own_end and _segment_in_box(s, box):
                    report(
                        "error", "through_connector", [w["designator"]],
                        f"runs through the pins of {conn}", s[:2],
                    )

    # Channels in front of pin names (vertical segments within a pin's keep distance)
    for w in wires:
        flagged: set[str] = set()
        for s in _segments(w["points"]):
            if s[0] != s[2]:
                continue
            x, ylo, yhi = s[0], min(s[1], s[3]), max(s[1], s[3])
            for p in pins:
                if not p["horizontal"] or p["connector"] in flagged:
                    continue
                hx, hy = p["hot"]
                ahead = x - hx if p["facing_right"] else hx - x
                if 0 < ahead < p["keep"] and ylo <= hy <= yhi:
                    flagged.add(p["connector"])
                    report(
                        "warning", "channel_near_pins", [w["designator"]],
                        f"vertical run {ahead} mils in front of {p['connector']} pins "
                        f"(keep >= {p['keep']})", (x, hy),
                    )

    # Pairs of wires: crossings, overlaps, spacing
    crossings: list[dict[str, Any]] = []
    for i, a in enumerate(wires):
        segs_a = _segments(a["points"])
        for b in wires[i + 1 :]:
            segs_b = _segments(b["points"])
            pair = [a["designator"], b["designator"]]
            close_h = close_v = False
            for sa in segs_a:
                for sb in segs_b:
                    point = _crossing_point(sa, sb)
                    if point:
                        crossings.append({"wires": pair, "at": list(point)})
                        room = min(
                            abs(point[0] - sa[0]) + abs(point[1] - sa[1]),
                            abs(point[0] - sa[2]) + abs(point[1] - sa[3]),
                            abs(point[0] - sb[0]) + abs(point[1] - sb[1]),
                            abs(point[0] - sb[2]) + abs(point[1] - sb[3]),
                        )
                        if room < MIN_STRAIGHT:
                            report(
                                "warning", "crossing_near_bend", pair,
                                f"only {room} mils straight next to the crossing", point,
                            )
                        continue
                    shared = _shared_stretch(sa, sb)
                    if shared:
                        report(
                            "error", "overlap", pair,
                            f"drawn on top of each other for {shared} mils", sa[:2],
                        )
                        continue
                    gap = _parallel_gap(sa, sb)
                    if gap and gap[0] == "h" and gap[1] < MIN_PARALLEL and not close_h:
                        close_h = True
                        report("warning", "parallel_spacing", pair, f"horizontal runs {gap[1]} mils apart", sa[:2])
                    if gap and gap[0] == "v" and gap[1] < MIN_CHANNEL_PITCH and not close_v:
                        close_v = True
                        report("warning", "channel_pitch", pair, f"vertical runs {gap[1]} mils apart", sa[:2])

    # Wire labels
    label_boxes: list[tuple[str, Box]] = []
    for w in wires:
        for pos, text in w["labels"]:
            label_boxes.append((w["designator"], _text_box(_mils(pos["x"]), _mils(pos["y"]), text)))
    text_boxes = [
        (t["text"], _text_box(_mils(t["x"]), _mils(t["y"]), t["text"], TEXT_HEIGHT))
        for t in parsed.get("net_labels", [])
    ]
    pin_boxes = []
    for p in pins:
        if not p["horizontal"]:
            continue
        hx, hy = p["hot"]
        bx = p["base"][0]
        lo, hi = (min(hx, bx), hx + p["keep"]) if p["facing_right"] else (hx - p["keep"], max(hx, bx))
        pin_boxes.append((p["connector"], (lo, hy - 40, hi, hy + 40)))
    seg_by_wire = {w["designator"]: _segments(w["points"]) for w in wires}

    def beside(box: Box, s: Segment) -> bool:
        """Label box just above/below a horizontal run, or next to a vertical one."""
        if s[1] == s[3]:
            return (
                min(s[0], s[2]) - LABEL_REACH <= box[0] <= max(s[0], s[2])
                and abs(box[1] - s[1]) <= LABEL_REACH
            )
        return (
            min(s[1], s[3]) - LABEL_REACH <= box[1] <= max(s[1], s[3])
            and abs(box[0] - s[0]) <= LABEL_REACH
        )

    # A wire's designator and comment are checked separately, reported once
    label_seen: set[tuple[str, ...]] = set()

    def label_report(kind: str, names: list[str], detail: str, at) -> None:
        key = (kind, *names, detail)
        if key not in label_seen:
            label_seen.add(key)
            report("warning", kind, names, detail, at)

    for k, (des, box) in enumerate(label_boxes):
        if not any(beside(box, s) for s in seg_by_wire.get(des, [])):
            label_report("label_off_wire", [des], "label is not next to a run of its wire", box[:2])
        hits = sorted(
            {other for other, segs in seg_by_wire.items() if other != des and any(_segment_in_box(s, box) for s in segs)}
        )
        if hits:
            label_report("label_on_wire", [des, *hits], "label overlaps other wires", box[:2])
        for other, obox in label_boxes[k + 1 :]:
            if other != des and _boxes_overlap(box, obox):
                label_report("label_on_label", [des, other], "labels overlap", box[:2])
        for text, tbox in text_boxes:
            if _boxes_overlap(box, tbox):
                label_report("label_on_text", [des], f"label overlaps sheet text {text!r}", box[:2])
        for conn in sorted({c for c, pbox in pin_boxes if _boxes_overlap(box, pbox)}):
            label_report("label_on_pins", [des], f"label overlaps {conn} pin names", box[:2])

    # Twist marks: two wires, hook tips on the two runs (rectangle TWIST_OVERHANG
    # beyond each), TWIST_LENGTH along them, clear of labels
    for t in parsed.get("twists", []):
        des = t.get("designator") or "twist"
        tw = t.get("wires", [])
        box = (_mils(t["x1"]), _mils(t["y1"]), _mils(t["x2"]), _mils(t["y2"]))
        at = box[:2]
        if len(tw) != 2:
            report("warning", "twist_wires", tw, f"{des} links {len(tw)} wires, not 2", at)
            continue
        on_labels = sorted({ld for ld, lbox in label_boxes if _boxes_overlap(box, lbox)})
        if on_labels:
            report("warning", "twist_on_label", tw, f"{des} overlaps the labels of {', '.join(on_labels)}", at)
        runs = {"h": [], "v": []}
        for wd in tw:
            for s in seg_by_wire.get(wd, []):
                if s[1] == s[3] and box[1] <= s[1] <= box[3] and min(s[0], s[2]) <= box[0] and max(s[0], s[2]) >= box[2]:
                    runs["h"].append(s[1])
                elif s[0] == s[2] and box[0] <= s[0] <= box[2] and min(s[1], s[3]) <= box[1] and max(s[1], s[3]) >= box[3]:
                    runs["v"].append(s[0])
        if len(runs["h"]) == 2:
            span, along, across = sorted(runs["h"]), box[2] - box[0], (box[1], box[3])
        elif len(runs["v"]) == 2:
            span, along, across = sorted(runs["v"]), box[3] - box[1], (box[0], box[2])
        else:
            report("warning", "twist_placement", tw, f"{des} is not on a straight parallel stretch of its two wires", at)
            continue
        want = (span[0] - TWIST_OVERHANG, span[1] + TWIST_OVERHANG)
        if across != want:
            report(
                "warning", "twist_placement", tw,
                f"{des} spans {list(across)} across the pair; hook tips land on the wires at {list(want)}", at,
            )
        if along != TWIST_LENGTH:
            report("warning", "twist_placement", tw, f"{des} is {along} mils long, not {TWIST_LENGTH}", at)

    errors = sum(p["severity"] == "error" for p in problems)
    counts: dict[str, int] = {}
    for p in problems:
        counts[p["type"]] = counts.get(p["type"], 0) + 1
    return {
        "clean": errors == 0 and len(problems) == 0,
        "summary": {
            "wire_count": len(wires),
            "crossings": len(crossings),
            "errors": errors,
            "warnings": len(problems) - errors,
            "by_type": counts,
        },
        "problems": problems[:MAX_LISTED],
        "problems_truncated": len(problems) > MAX_LISTED,
        "crossings": crossings[:MAX_LISTED],
    }
