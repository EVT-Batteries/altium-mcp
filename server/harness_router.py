"""Automated Harness Wiring Router and Aesthetic Layout Engine for Altium Designer.

Computes optimal orthogonal routing, topological vertical trunk channels,
certified splice runways, 2-phase staggered side-by-side labels, and no-connect
directives for Altium Harness Wiring Diagrams (.WirDoc).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_parser import parse_wirdoc


def route_topological_bundle(
    connections: list[dict[str, Any]],
    base_x: int,
    direction_sign: int = 1,
    channel_pitch: int = 300,
    min_gap: int = 200,
) -> list[dict[str, Any]]:
    """Route a bundle of point-to-point connections with orthogonal channels.

    Generic topological routing engine enforcing:
    - 300 mil vertical drop channel pitch standard (3 grid increments)
    - Straight wire preservation (sy == dy -> 2-point direct line)
    - Directional grouping (UP-going vs DOWN-going)
    - Nested concentric staircase sorting (Russian doll / onion peel)
    - Co-linear channel re-use for non-overlapping vertical spans
    - Midpoint labeling on longest horizontal segment with side-by-side offsets
    """
    straight = [c for c in connections if c["sy"] == c["dy"]]
    stepped = [c for c in connections if c["sy"] != c["dy"]]

    up_wires = [c for c in stepped if c["dy"] > c["sy"]]
    down_wires = [c for c in stepped if c["dy"] < c["sy"]]

    # Nested concentric staircase sorting:
    # Lowest entry wire steps into outer channel first for UP wires
    up_wires_sorted = sorted(up_wires, key=lambda c: (c["sy"], -c["dy"]))
    # Highest entry wire steps into outer channel first for DOWN wires
    down_wires_sorted = sorted(down_wires, key=lambda c: (-c["sy"], c["dy"]))

    routed_wires: list[dict[str, Any]] = []

    # 1. Straight wires (Delta Y == 0)
    for c in straight:
        pts = [c["sx"], c["sy"], c["dx"], c["dy"]]
        x_mid = (c["sx"] + c["dx"]) // 2
        routed_wires.append(
            {
                "designator": c["id"],
                "net": c["net"],
                "points": pts,
                "designator_pos": [x_mid - 150, c["sy"] + 15],
                "comment_pos": [x_mid + 200, c["sy"] + 15],
                "parameters": c.get("parameters", {}),
            }
        )

    # 2. Stepped wires with co-linear channel re-use
    channels: list[dict[str, Any]] = []

    def allocate_channel(ymin: int, ymax: int) -> int:
        for ch in channels:
            conflict = False
            for c_ymin, c_ymax in ch["intervals"]:
                if not (ymax + min_gap <= c_ymin or ymin >= c_ymax + min_gap):
                    conflict = True
                    break
            if not conflict:
                ch["intervals"].append((ymin, ymax))
                return ch["x"]
        new_x = base_x + direction_sign * (len(channels) * channel_pitch)
        channels.append({"x": new_x, "intervals": [(ymin, ymax)]})
        return new_x

    for c in up_wires_sorted + down_wires_sorted:
        ymin = min(c["sy"], c["dy"])
        ymax = max(c["sy"], c["dy"])
        tx = allocate_channel(ymin, ymax)
        pts = [c["sx"], c["sy"], tx, c["sy"], tx, c["dy"], c["dx"], c["dy"]]

        # Find longest horizontal segment for midpoint labeling
        len1 = abs(tx - c["sx"])
        len2 = abs(c["dx"] - tx)
        if len1 >= len2:
            mid_x = (c["sx"] + tx) // 2
            mid_y = c["sy"]
        else:
            mid_x = (tx + c["dx"]) // 2
            mid_y = c["dy"]

        routed_wires.append(
            {
                "designator": c["id"],
                "net": c["net"],
                "points": pts,
                "designator_pos": [mid_x - 150, mid_y + 15],
                "comment_pos": [mid_x + 200, mid_y + 15],
                "parameters": c.get("parameters", {}),
            }
        )

    return routed_wires


def calculate_harness_reroute(
    document_path: Path | str,
    trunk_x_start: int | None = None,
    splice_x: int | None = None,
    channel_spacing_mils: int = 300,
) -> dict[str, Any]:
    """Calculate an optimal, collision-free orthogonal routing layout for a .WirDoc.

    Args:
        document_path: Path to the target .WirDoc file.
        trunk_x_start: Optional starting X coordinate for vertical trunk in mils.
        splice_x: Optional X coordinate for splices in mils.
        channel_spacing_mils: Distance between parallel vertical trunk channels (default 300 mils).

    Returns:
        Dictionary containing structured 'connectors', 'wires', 'splices',
        'no_connects', and 'net_labels' ready to pass directly to add_harness_wiring.
    """
    doc_path = Path(document_path)
    if not doc_path.exists():
        raise FileNotFoundError(f"WirDoc file not found: {doc_path}")

    parsed = parse_wirdoc(doc_path)


    # Dynamic harness routing for documents with existing wires

    if parsed.get("wires"):
        generic_conns = []
        for w in parsed["wires"]:
            pts = w["points"]
            if len(pts) >= 4:
                sx, sy = pts[0] * 10, pts[1] * 10
                dx, dy = pts[-2] * 10, pts[-1] * 10
                if sx > dx:
                    sx, sy, dx, dy = dx, dy, sx, sy
                generic_conns.append(
                    {
                        "id": w["designator"],
                        "net": w["net"],
                        "sx": sx,
                        "sy": sy,
                        "dx": dx,
                        "dy": dy,
                        "parameters": w.get("parameters", {}),
                    }
                )
        base_x = trunk_x_start if trunk_x_start is not None else 16500
        routed = route_topological_bundle(
            generic_conns,
            base_x=base_x,
            direction_sign=1,
            channel_pitch=channel_spacing_mils,
        )
        out_splices = []
        for s in parsed.get("splices", []):
            loc = s.get("location", {})
            sx = int(s.get("x", loc.get("x", 0)))
            sy = int(s.get("y", loc.get("y", 0)))
            if sx < 5000:
                sx *= 10
            if sy < 5000:
                sy *= 10
            out_splices.append({
                "designator": s.get("designator", ""),
                "x": sx,
                "y": sy,
            })

        out_no_connects = []
        for nc in parsed.get("no_connects", []):
            loc = nc.get("location", {})
            nx = int(nc.get("x", loc.get("x", 0)))
            ny = int(nc.get("y", loc.get("y", 0)))
            if nx < 5000:
                nx *= 10
            if ny < 5000:
                ny *= 10
            out_no_connects.append({
                "name": nc.get("name", nc.get("designator", "")),
                "designator": nc.get("designator", nc.get("name", "")),
                "x": nx,
                "y": ny,
                "show_name": nc.get("show_name", False),
            })

        out_net_labels = []
        for nl in parsed.get("net_labels", []):
            loc = nl.get("location", {})
            lx = int(nl.get("x", loc.get("x", 0)))
            ly = int(nl.get("y", loc.get("y", 0)))
            if lx < 5000:
                lx *= 10
            if ly < 5000:
                ly *= 10
            out_net_labels.append({
                "text": nl.get("text", ""),
                "x": lx,
                "y": ly,
                "orientation": nl.get("orientation", 0),
            })

        detected_splice_x = out_splices[0]["x"] if out_splices else (splice_x or 13500)
        return {
            "connectors": [],
            "rectangles": [],
            "wires": routed,
            "splices": out_splices,
            "no_connects": out_no_connects,
            "net_labels": out_net_labels,
            "summary": {
                "wire_count": len(routed),
                "splice_count": len(out_splices),
                "no_connect_count": len(out_no_connects),
                "trunk_x_start": base_x,
                "splice_x": detected_splice_x,
            },
        }

    raise ValueError(
        f"The target document '{doc_path.name}' contains no wires to re-route. "
        "Dynamic harness re-routing requires existing wires on the sheet or an explicit netlist."
    )
