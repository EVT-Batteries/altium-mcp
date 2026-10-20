"""Orthogonal re-router for Altium Harness Wiring Diagrams (.WirDoc).

Routes every existing wire between its two endpoints (which stay where they
are: on their pins or splices) following the AGENTS.md drawing rules:

- straight wires stay 2-point when both ends are level;
- vertical drops live in corridors, one per pair of endpoint columns, placed
  near the destination side so the long horizontal run (and its labels) crosses
  the open middle of the sheet - never one global trunk;
- channels are 300 mils apart; UP-going and DOWN-going wires are grouped and
  nested as concentric staircases (lowest UP start / highest DOWN start in the
  channel closest to the destination), so wires of a group do not cross;
- a channel is shared by wires whose vertical spans are >= 200 mils apart, but
  only when sharing adds no crossing;
- channels and labels keep clear of pin names (len(name) x 65 mils from the
  pin base, + 150 mils) and channels stay >= 300 mils from splices;
- labels sit side by side (375 mils apart) on the longest horizontal segment
  and are staggered by 900 mils when parallel runs are closer than 150 mils.

The result feeds add_harness_wiring(reroutes=...), which moves the wires in
place so their UniqueIDs, parameters and layout-drawing links survive.
All coordinates here are mils.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness_parser import parse_wirdoc

# Designator -> comment distance along the wire (AGENTS.md: 350-400 mils)
LABEL_SPACING = 375
LABEL_ABOVE = 15
LABEL_BELOW = -85
LABEL_CHAR_WIDTH = 60
PIN_NAME_CHAR_WIDTH = 65
NAME_CLEARANCE = 150
MIN_STUB = 300
MIN_VERTICAL_GAP = 200
SPLICE_CLEARANCE = 300
SPLICE_RUNWAY = 2500
STAGGER_STEP = 900
PARALLEL_PITCH = 150
GRID = 100


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


def _snap_down(v: float) -> int:
    return int(v // GRID * GRID)


def _snap_up(v: float) -> int:
    return int(-(-v // GRID) * GRID)


@dataclass
class _Wire:
    designator: str
    net: str
    ax: int  # left endpoint
    ay: int
    bx: int  # right endpoint
    by: int
    reversed: bool  # True when the stored wire runs right -> left
    keep_a: int = 0  # clear distance right of the left endpoint
    keep_b: int = 0  # clear distance left of the right endpoint
    points: list[int] = field(default_factory=list)


def _segments(points: list[int]) -> list[tuple[int, int, int, int]]:
    return [tuple(points[k : k + 4]) for k in range(0, len(points) - 2, 2)]


def _crossings(points: list[int], others: list[list[int]]) -> int:
    """Proper crossings between one orthogonal polyline and others."""
    count = 0
    for x1, y1, x2, y2 in _segments(points):
        for other in others:
            for u1, v1, u2, v2 in _segments(other):
                if y1 == y2 and u1 == u2:  # horizontal x vertical
                    hx1, hx2, hy, vx, vy1, vy2 = min(x1, x2), max(x1, x2), y1, u1, min(v1, v2), max(v1, v2)
                elif x1 == x2 and v1 == v2:  # vertical x horizontal
                    hx1, hx2, hy, vx, vy1, vy2 = min(u1, u2), max(u1, u2), v1, x1, min(y1, y2), max(y1, y2)
                else:
                    continue
                if hx1 < vx < hx2 and vy1 < hy < vy2:
                    count += 1
    return count


def _pin_keepouts(parsed: dict[str, Any]) -> dict[tuple[int, int], int]:
    """Hot end (mils) -> distance beyond it that wires' bends and labels keep
    clear of: the pin's own name and any sheet text standing next to the pin
    on the wire side (pin callouts), plus NAME_CLEARANCE."""
    texts = [
        (round(t["x"] * 10), round(t["y"] * 10), len(t.get("text", "")))
        for t in parsed.get("net_labels", [])
    ]
    keep = {}
    for c in parsed.get("connectors", []):
        for pin in c.get("pins", []):
            hx, hy = round(pin["hot_end"]["x"] * 10), round(pin["hot_end"]["y"] * 10)
            bx = round(pin["location"]["x"] * 10)
            name_extent = len(pin.get("name", "")) * PIN_NAME_CHAR_WIDTH
            extent = max(0, name_extent - abs(hx - bx))
            facing_right = hx >= bx
            for tx, ty, n in texts:
                if abs(ty - hy) > 60:
                    continue
                if facing_right and hx <= tx <= hx + 4000:
                    extent = max(extent, tx + n * LABEL_CHAR_WIDTH - hx)
                elif not facing_right and hx - 4000 <= tx <= hx:
                    extent = max(extent, hx - tx)
            keep[(hx, hy)] = extent + NAME_CLEARANCE
    return keep


def calculate_harness_reroute(
    document_path: Path | str,
    trunk_x_start: int | None = None,
    channel_spacing_mils: int = 300,
) -> dict[str, Any]:
    """Orthogonal routes for every wire of a .WirDoc, endpoints unchanged.

    Args:
        document_path: The .WirDoc to read (its saved state).
        trunk_x_start: Optional X (mils) of the first channel of every corridor;
            by default each corridor starts next to its destination side.
        channel_spacing_mils: Pitch between parallel vertical channels.

    Returns:
        {"reroutes": [...], "summary": {...}, "warnings": [...]} where each
        reroute is {"designator", "points", "designator_pos", "comment_pos"}
        ready for add_harness_wiring(reroutes=...).
    """
    doc_path = Path(document_path)
    if not doc_path.exists():
        raise FileNotFoundError(f"WirDoc file not found: {doc_path}")
    parsed = parse_wirdoc(doc_path)
    if not parsed.get("wires"):
        raise ValueError(f"The target document '{doc_path.name}' contains no wires to re-route.")

    warnings: list[str] = []
    keepouts = _pin_keepouts(parsed)
    splices = [
        (round(s["location"]["x"] * 10), round(s["location"]["y"] * 10))
        for s in parsed.get("splices", [])
    ]

    seen: dict[str, int] = {}
    for w in parsed["wires"]:
        seen[w["designator"]] = seen.get(w["designator"], 0) + 1

    wires: list[_Wire] = []
    for w in parsed["wires"]:
        des = w["designator"]
        pts = w["points"]
        if des in ("", "?") or seen[des] > 1:
            warnings.append(f"wire {des!r}: designator missing or not unique; not rerouted")
            continue
        if len(pts) < 4:
            continue
        sx, sy = round(pts[0] * 10), round(pts[1] * 10)
        ex, ey = round(pts[-2] * 10), round(pts[-1] * 10)
        rev = sx > ex
        if rev:
            sx, sy, ex, ey = ex, ey, sx, sy
        wires.append(
            _Wire(
                designator=des,
                net=w.get("net", ""),
                ax=sx, ay=sy, bx=ex, by=ey,
                reversed=rev,
                keep_a=max(MIN_STUB, keepouts.get((sx, sy), 0)),
                keep_b=max(MIN_STUB, keepouts.get((ex, ey), 0)),
            )
        )

    routed: list[list[int]] = []
    straight = [w for w in wires if w.ay == w.by]
    for w in straight:
        w.points = [w.ax, w.ay, w.bx, w.by]
        routed.append(w.points)

    # Jumpers: both ends in the same column (e.g. adjacent pins of one
    # connector) loop outwards, away from the connector body
    jumpers = [w for w in wires if w.ay != w.by and w.bx - w.ax < 2 * MIN_STUB]
    for w in jumpers:
        x = _snap_up(max(w.ax, w.bx) + max(w.keep_a, w.keep_b))
        w.points = [w.ax, w.ay, x, w.ay, x, w.by, w.bx, w.by]
        routed.append(w.points)

    stepped = [w for w in wires if w.ay != w.by and w not in jumpers]
    corridors: dict[tuple[int, int], list[_Wire]] = {}
    for w in stepped:
        corridors.setdefault((_snap_down(w.ax), _snap_down(w.bx)), []).append(w)

    channel_total = 0
    for (left, right), group in sorted(corridors.items()):
        lo = _snap_up(left + max(w.keep_a for w in group))
        hi = _snap_down(right - max(w.keep_b for w in group))
        start = trunk_x_start if trunk_x_start is not None else hi
        channels: list[dict[str, Any]] = []

        def channel_x(k: int, start: int = start) -> int:
            return start - k * channel_spacing_mils

        def splice_conflict(x: int) -> bool:
            return any(abs(x - sx) < SPLICE_CLEARANCE for sx, _ in splices)

        up = sorted((w for w in group if w.by > w.ay), key=lambda w: (w.ay, w.by))
        down = sorted((w for w in group if w.by < w.ay), key=lambda w: (-w.ay, -w.by))
        for w in up + down:
            ymin, ymax = min(w.ay, w.by), max(w.ay, w.by)
            candidates = [
                ch
                for ch in channels
                if all(
                    ymax + MIN_VERTICAL_GAP <= a or ymin >= b + MIN_VERTICAL_GAP
                    for a, b in ch["spans"]
                )
            ]
            k = len(channels)
            while splice_conflict(channel_x(k)):
                k += 1
            fresh = {"x": channel_x(k), "spans": [], "new": True}
            best = None
            for ch in [*sorted(candidates, key=lambda c: -c["x"]), fresh]:
                pts = [w.ax, w.ay, ch["x"], w.ay, ch["x"], w.by, w.bx, w.by]
                score = _crossings(pts, routed)
                if best is None or score < best[0]:
                    best = (score, ch, pts)
                if score == 0:
                    break
            _, ch, pts = best
            if ch.get("new"):
                ch.pop("new")
                channels.append(ch)
            ch["spans"].append((ymin, ymax))
            w.points = pts
            routed.append(pts)
        channel_total += len(channels)
        if channels and min(c["x"] for c in channels) < lo:
            warnings.append(
                f"corridor x {left}..{right}: {len(channels)} channels do not fit "
                f"clear of pin names (need x >= {lo}); spread the connectors"
            )

    for sx, sy in splices:
        for w in wires:
            for (px, py), (qx, qy) in (((w.ax, w.ay), (w.bx, w.by)), ((w.bx, w.by), (w.ax, w.ay))):
                if (px, py) == (sx, sy) and (qx, qy) in keepouts and abs(qx - sx) < SPLICE_RUNWAY:
                    warnings.append(
                        f"splice at ({sx}, {sy}): {w.designator} runway {abs(qx - sx)} mils "
                        f"< {SPLICE_RUNWAY}"
                    )

    reroutes = _place_labels(wires, warnings)
    crossings = sum(
        _crossings(w.points, [o.points for o in wires[i + 1 :]]) for i, w in enumerate(wires)
    )
    return {
        "reroutes": reroutes,
        "warnings": warnings,
        "summary": {
            "wire_count": len(reroutes),
            "straight": len(straight),
            "jumpers": len(jumpers),
            "corridors": len(corridors),
            "channels": channel_total,
            "crossings": crossings,
            "splice_count": len(splices),
        },
    }


def _place_labels(wires: list[_Wire], warnings: list[str]) -> list[dict[str, Any]]:
    """Designator/comment positions per wire, staggered on tight parallel runs."""
    placed: list[tuple[int, int, int]] = []  # (y, x_start, x_end)
    out = []
    for w in sorted(wires, key=lambda w: (w.ay, w.ax)):
        if not w.points:
            continue
        segs = [s for s in _segments(w.points) if s[1] == s[3]]
        x1, y, x2, _ = max(segs, key=lambda s: abs(s[2] - s[0]))
        seg_lo, seg_hi = min(x1, x2), max(x1, x2)
        # Clear of pin names where the segment starts or ends on a pin
        lo = seg_lo + (w.keep_a if seg_lo == w.ax else GRID)
        hi = seg_hi - (w.keep_b if seg_hi == w.bx else GRID)
        width = LABEL_SPACING + len(w.net) * LABEL_CHAR_WIDTH
        centre = (lo + hi) // 2 - width // 2
        label_y = y + LABEL_ABOVE
        chosen = None
        for shift in (0, STAGGER_STEP, -STAGGER_STEP, 2 * STAGGER_STEP, -2 * STAGGER_STEP):
            xs = centre + shift
            if xs < lo or xs + width > hi - NAME_CLEARANCE:
                continue
            if all(
                abs(py - label_y) >= PARALLEL_PITCH or xs + width <= px1 or xs >= px2
                for py, px1, px2 in placed
            ):
                chosen = xs
                break
        if chosen is None:
            chosen = max(lo, centre)
            warnings.append(f"{w.designator}: no collision-free label slot")
        placed.append((label_y, chosen, chosen + width))
        pts = w.points
        if w.reversed:
            pts = [v for k in range(len(pts) - 2, -1, -2) for v in pts[k : k + 2]]
        out.append(
            {
                "designator": w.designator,
                "points": pts,
                "designator_pos": [chosen, label_y],
                "comment_pos": [chosen + LABEL_SPACING, label_y],
            }
        )
    return out
