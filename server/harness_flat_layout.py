"""Flattened (formboard-style) 2D layout of a harness layout drawing's bundle tree.

The physical harness is a tree: connection points joined by bundles. This module lays it out
the way a harness is drawn on a formboard, without the glitches of a 3D-projected flatten:

- the trunk (from the root connector along the bundle with the most wires at each node,
  ties: the longer way) is horizontal,
  root connector on the left facing left, every other connector on the right facing right;
- every side branch leaves its node at 45 degrees and turns parallel to the trunk, each in a
  lane of its own (lane_spacing apart), so branches never cross or lie on top of each other:
  on each side the branch nearest the trunk end takes the innermost lane, and a branch with
  branches of its own reserves the lanes they need, outward of its own run;
- branches alternate above / below the trunk (first_side), node by node;
- drawn lengths follow the physical lengths (scale) but never fall below min_bundle_mils, so
  a 4 mm bundle between two breakouts stays readable; the length text carries the true value
  and the break symbol marks a bundle not drawn to scale;
- each connector is turned (quarter turns) so that its body lies beyond its connection point,
  continuing the bundle, and moved with it; its texts move with it;
- bundle designator above and length below the middle of its longest straight, connection
  point names above the point.

plan_flat_layout returns connectors / connection_points / bundles entries in
edit_harness_layout's request format (mils, degrees); the caller merges explicit entries over
them. Pure geometry: no Altium, no file access.
"""
from __future__ import annotations

import math
from typing import Any


def _mils(p: dict[str, float]) -> tuple[float, float]:
    return p["x"] * 10.0, p["y"] * 10.0


def _rot(dx: float, dy: float, k: int) -> tuple[float, float]:
    for _ in range(k % 4):
        dx, dy = -dy, dx
    return dx, dy


class FlatLayoutError(ValueError):
    pass


def plan_flat_layout(layout: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]:
    """options: root (connector designator), lengths ({bundle: mm}, applied as length_mm),
    origin ([x, y] mils of the root connection point, default its current place),
    scale (mils per mm, default: the trunk fills width_mils), width_mils (default 8000),
    min_bundle_mils (400), lane_spacing (700), first_side ('up' | 'down'), place_texts (True),
    attach_mils (6000: notes / pictures whose centre lies within it of a connector move with its
    nearest connector; 0 = none),
    break_symbols (True: on bundles whose drawn length differs from the physical one by > 2 %)."""
    o = dict(options or {})
    lengths_in = {str(k): float(v) for k, v in (o.get("lengths") or {}).items()}
    min_draw = float(o.get("min_bundle_mils", 400))
    lane = float(o.get("lane_spacing", 700))
    first_up = str(o.get("first_side", "up")).lower() != "down"
    place_texts = bool(o.get("place_texts", True))
    breaks = bool(o.get("break_symbols", True))

    cps = {c["designator"]: c for c in layout.get("connection_points", [])}
    conns = {c["designator"]: c for c in layout.get("connectors", [])}
    cp_of_conn = {d: c.get("connection_point") for d, c in conns.items()}
    conn_of_cp: dict[str, str] = {}
    for d, cp in cp_of_conn.items():
        if cp:
            conn_of_cp.setdefault(cp, d)

    # ---- graph
    adj: dict[str, list[tuple[str, str]]] = {}
    phys: dict[str, float] = {}
    bkey: dict[str, str] = {}
    for b in layout.get("bundles", []):
        a, z = b.get("end1_connection_point"), b.get("end2_connection_point")
        if not a or not z:
            raise FlatLayoutError(f"bundle {b.get('designator') or b.get('unique_id')} is not connected at both ends")
        name = b.get("designator") or b["unique_id"]
        if name in bkey or name.endswith("?"):
            name = b["unique_id"]
        bkey[name] = b["unique_id"]
        phys[name] = lengths_in.get(name, lengths_in.get(b.get("designator", ""), float((b.get("length") or {}).get("mm") or 0.0)))
        adj.setdefault(a, []).append((z, name))
        adj.setdefault(z, []).append((a, name))
    if not adj:
        raise FlatLayoutError("the layout has no bundles")
    unknown = sorted(set(lengths_in) - set(phys) - {b.get("designator") for b in layout.get("bundles", [])})
    if unknown:
        raise FlatLayoutError(f"lengths name unknown bundles: {unknown}")
    if len(layout.get("bundles", [])) != len(adj) - 1 or not _connected(adj):
        raise FlatLayoutError("the bundles do not form one tree (a loop or several pieces): lay it out by hand")

    # ---- root
    root_conn = o.get("root")
    if root_conn:
        if root_conn not in conns or not cp_of_conn.get(root_conn):
            raise FlatLayoutError(f"root connector '{root_conn}' not found (or has no connection point)")
        root = cp_of_conn[root_conn]
    else:
        leaves = [n for n in adj if len(adj[n]) == 1 and n in conn_of_cp]
        root = max(leaves or list(adj), key=lambda n: (len(_wires_of(layout, adj[n][0][1])), n))
    if root not in adj:
        raise FlatLayoutError(f"the root's connection point {root} has no bundle")

    # ---- tree, longest physical path (the trunk / each branch's own run)
    children: dict[str, list[tuple[str, str]]] = {}
    parent: dict[str, tuple[str, str] | None] = {root: None}
    order = [root]
    for n in order:
        for m, e in adj[n]:
            if m not in parent:
                parent[m] = (n, e)
                children.setdefault(n, []).append((m, e))
                order.append(m)
    far: dict[str, float] = {}
    for n in reversed(order):
        far[n] = max((phys[e] + far[m] for m, e in children.get(n, [])), default=0.0)

    nwires = {name: len(_wires_of(layout, bkey[name])) for name in phys}

    def chain(n: str) -> list[tuple[str, str]]:
        """the run continuing from n: (node, bundle) steps along the bundle with the most wires
        (the main bundle; ties: the longer way)"""
        out = []
        while children.get(n):
            m, e = max(children[n], key=lambda me: (nwires[me[1]], phys[me[1]] + far[me[0]], me[0]))
            out.append((m, e))
            n = m
        return out

    def sides(n: str, cont: str | None) -> list[tuple[str, str]]:
        return [(m, e) for m, e in children.get(n, []) if m != cont]

    lanes_cache: dict[str, int] = {}

    def lanes_needed(n: str, first_edge_from: str | None = None) -> int:
        """lanes a branch starting at node n (its run = chain(n)) needs beyond its own lane"""
        if n in lanes_cache:
            return lanes_cache[n]
        total = 0
        nodes = [n] + [m for m, _ in chain(n)]
        for i, k in enumerate(nodes):
            cont = nodes[i + 1] if i + 1 < len(nodes) else None
            for m, _ in sides(k, cont):
                total += 1 + lanes_needed(m)
        lanes_cache[n] = total
        return total

    # ---- scale
    trunk = chain(root)
    trunk_len = sum(phys[e] for _, e in trunk) or 1.0
    if o.get("scale"):
        scale = float(o["scale"])
    else:
        scale = float(o.get("width_mils", 8000)) / trunk_len

    def drawn(e: str) -> float:
        return max(min_draw, phys[e] * scale)

    if o.get("origin"):
        ox, oy = float(o["origin"][0]), float(o["origin"][1])
    else:
        ox, oy = _mils(cps[root]["location"])

    pos: dict[str, tuple[float, float]] = {root: (ox, oy)}
    bundle_pts: dict[str, list[tuple[float, float]]] = {}
    facing: dict[str, int] = {root: -1}          # +1 right, -1 left (connector bodies)

    def lay_run(start: str, x: float, y: float, outward: int, up_first: bool) -> None:
        """lay the run of `start` (already placed at x, y) to the right; its side branches go
        outward (+1 up / -1 down) when outward != 0, else alternate starting with up_first."""
        nodes = [start] + [m for m, _ in chain(start)]
        run = chain(start)
        xs = [x]
        for m, e in run:
            xs.append(xs[-1] + drawn(e))
        for (m, e), xm in zip(run, xs[1:]):
            pos[m] = (xm, y)
            bundle_pts[e] = [pos[parent[m][0]], (xm, y)]
        # side branches: collect (node index, child) and assign sides / lanes
        branches = []
        flip = up_first
        for i, k in enumerate(nodes):
            cont = nodes[i + 1] if i + 1 < len(nodes) else None
            for m, e in sorted(sides(k, cont), key=lambda me: -(phys[me[1]] + far[me[0]])):
                s = outward if outward else (1 if flip else -1)
                flip = not flip
                branches.append((i, k, m, e, s))
        for s in (1, -1):
            used = 0
            for i, k, m, e, ss in sorted((b for b in branches if b[4] == s), key=lambda b: -b[0]):
                lane_no = used + 1
                used += 1 + lanes_needed(m)
                dy = s * lane_no * lane
                kx, ky = pos[k]
                bx, by = kx + abs(dy), ky + dy
                run_len = max(min_draw, phys[e] * scale - abs(dy) * math.sqrt(2))
                pos[m] = (bx + run_len, by)
                bundle_pts[e] = [(kx, ky), (bx, by), pos[m]]
                lay_run(m, pos[m][0], by, s, up_first)

    lay_run(root, ox, oy, 0, first_up)
    for n in pos:
        facing.setdefault(n, 1)

    # ---- requests
    # point names a row above the bundle texts (bundle name at +30); a name whose neighbour
    # on the same line is closer than name_gap goes one row higher again
    name_gap = float(o.get("name_gap_mils", 700))
    name_y: dict[str, float] = {}
    for n, (x, y) in sorted(pos.items(), key=lambda kv: (kv[1][1], kv[1][0])):
        row = 0
        for m, yy in name_y.items():
            mx, my = pos[m]
            if abs(my - y) < 1 and abs(mx - x) < name_gap and yy == y + 180 + 130 * row:
                row += 1
        name_y[n] = y + 180 + 130 * row
    out_conns, out_cps, out_bundles = [], [], []
    for n, (x, y) in pos.items():
        cp = cps.get(n)
        entry: dict[str, Any] = {"designator": n, "x": round(x), "y": round(y)}
        if place_texts and cp is not None and cp.get("show_name", True):
            entry["texts"] = {"designator": {"x": round(x), "y": round(name_y[n]), "justification": "bottom_center"}}
        d = conn_of_cp.get(n)
        if d is None:
            out_cps.append(entry)
            continue
        c = conns[d]
        cx0, cy0 = _mils(c["location"])
        px0, py0 = _mils(cp["location"]) if cp else (cx0, cy0)
        ox_, oy_ = cx0 - px0, cy0 - py0                   # body offset from the point, now
        want = facing[n]
        best = max(range(4), key=lambda k: (_rot(ox_, oy_, k)[0] * want, -k))
        rx, ry = _rot(ox_, oy_, best)
        nx, ny = x + rx, y + ry
        ce: dict[str, Any] = {
            "designator": d, "x": round(nx), "y": round(ny),
            "orientation": (c["orientation"] + 90 * best) % 360,
            "connection_point": {"x": round(x), "y": round(y)},
        }
        if place_texts:
            tx = {}
            for name, t in (c.get("texts") or {}).items():
                if t.get("hidden"):
                    continue
                tdx, tdy = t["x"] * 10.0 - cx0, t["y"] * 10.0 - cy0
                tx[name.lower()] = {"x": round(nx + tdx), "y": round(ny + tdy), "justification": t.get("justification", "bottom_left")}
            if tx:
                ce["texts"] = tx
        out_conns.append(ce)
        if place_texts and cp is not None:
            out_cps.append({"designator": n, "texts": entry["texts"]} if "texts" in entry else {"designator": n})

    for e, pts in bundle_pts.items():
        flat = [round(v) for p in pts for v in p]
        dl = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        be: dict[str, Any] = {"points": flat}
        if e == bkey[e]:
            be["unique_id"] = e
        else:
            be["designator"] = e
        if e in lengths_in:
            be["length_mm"] = round(phys[e], 2)
        if breaks:
            be["show_break_symbol"] = abs(dl - phys[e] * 1000 / 25.4) > 0.02 * max(dl, 1.0)
        if place_texts:
            i = max(range(len(pts) - 1), key=lambda j: abs(pts[j + 1][0] - pts[j][0]) if pts[j + 1][1] == pts[j][1] else 0)
            (ax, ay), (zx, zy) = pts[i], pts[i + 1]
            mx, my = (ax + zx) / 2, (ay + zy) / 2
            be["texts"] = {
                "designator": {"x": round(mx), "y": round(my + 30), "justification": "bottom_center"},
                "length": {"x": round(mx), "y": round(my - 30), "justification": "top_center"},
            }
        out_bundles.append(be)

    # notes and pictures beside a connector (its label, a photo) travel with it: each is
    # moved by the offset of the nearest connector within attach_mils of its centre
    move_objects = []
    attach = float(o.get("attach_mils", 6000))
    moved = {c["designator"]: (c["x"] - _mils(conns[c["designator"]]["location"])[0],
                               c["y"] - _mils(conns[c["designator"]]["location"])[1]) for c in out_conns}
    if attach > 0 and moved:
        for g in list(layout.get("notes", [])) + list(layout.get("pictures", [])):
            gx = (g["x1"] + g["x2"]) * 5.0
            gy = (g["y1"] + g["y2"]) * 5.0
            d, best = min((math.dist((gx, gy), _mils(conns[k]["location"])), k) for k in moved)
            if d <= attach and moved[best] != (0, 0):
                move_objects.append({"unique_id": g["unique_id"], "offset": [round(moved[best][0]), round(moved[best][1])]})

    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    return {
        "move_objects": move_objects,
        "connectors": out_conns,
        "connection_points": out_cps,
        "bundles": out_bundles,
        "report": {
            "root": conn_of_cp.get(root, root),
            "trunk": [root] + [m for m, _ in trunk],
            "scale_mils_per_mm": round(scale, 3),
            "extent_mils": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))],
        },
    }


def _connected(adj: dict[str, list[tuple[str, str]]]) -> bool:
    start = next(iter(adj))
    seen = {start}
    stack = [start]
    while stack:
        n = stack.pop()
        for m, _ in adj[n]:
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return len(seen) == len(adj)


def _wires_of(layout: dict[str, Any], bundle: str) -> list[str]:
    for b in layout.get("bundles", []):
        if b.get("designator") == bundle or b.get("unique_id") == bundle:
            return list(b.get("wires") or [])
    return []


def segments_cross(a: tuple, b: tuple, c: tuple, d: tuple) -> bool:
    """True when segment ab properly crosses cd (shared end points do not count)."""
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    if {a, b} & {c, d}:
        return False
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 * o2 < 0 and o3 * o4 < 0
