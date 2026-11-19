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
  continuing the bundle, and moved with it; its texts move and turn with it (each keeps its
  place relative to the body, upright);
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


def _view_box(conn: dict[str, Any], k: int, nx: float, ny: float) -> tuple[float, float, float, float] | None:
    """the connector's main view picture (mils) after turning it k quarter turns about its
    location and moving the location to (nx, ny); None without a picture"""
    views = conn.get("views") or []
    v = next((v for v in views if v.get("main") and not v.get("hidden") and v.get("image")), None)
    v = v or next((v for v in views if not v.get("hidden") and v.get("image")), None)
    if not v:
        return None
    cx, cy = _mils(conn["location"])
    im = v["image"]
    corners = [_rot(px * 10.0 - cx, py * 10.0 - cy, k) for px in (im["x1"], im["x2"]) for py in (im["y1"], im["y2"])]
    xs = [nx + p[0] for p in corners]
    ys = [ny + p[1] for p in corners]
    return min(xs), min(ys), max(xs), max(ys)


def plan_flat_layout(layout: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]:
    """options: root (connector designator), lengths ({bundle: mm}, applied as length_mm),
    origin ([x, y] mils of the root connection point, default its current place),
    scale (mils per mm, default: the trunk fills width_mils), width_mils (default 8000),
    min_bundle_mils (400), lane_spacing (700), first_side ('up' | 'down'), place_texts (True),
    connector_texts ('around': designator above the connector's main view picture, comment and
    shown parameters in rows below it (text_gap_mils 50, text_row_mils 130); 'keep': each text
    keeps its place relative to the body, turned with the connector; a connector without a
    view picture always keeps),
    attach_mils (6000: notes / pictures whose centre lies within it of a connector move with its
    nearest connector; 0 = none),
    break_symbols (True: on bundles whose drawn length differs from the physical one by > 2 %)."""
    o = dict(options or {})
    lengths_in = {str(k): float(v) for k, v in (o.get("lengths") or {}).items()}
    min_draw = float(o.get("min_bundle_mils", 400))
    lane = float(o.get("lane_spacing", 700))
    first_up = str(o.get("first_side", "up")).lower() != "down"
    place_texts = bool(o.get("place_texts", True))
    conn_texts = str(o.get("connector_texts", "around")).lower()
    if conn_texts not in ("around", "keep"):
        raise FlatLayoutError("connector_texts must be 'around' or 'keep'")
    text_gap = float(o.get("text_gap_mils", 50))
    text_row = float(o.get("text_row_mils", 130))
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
    keep_out: list[tuple[float, float, float, float]] = []
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
        vbox = _view_box(c, best, nx, ny)
        if vbox:
            # what graphics must keep clear of: the picture, and bands for its texts above
            # (designator) and below (comment, parameters), a typical label width wide
            n_below = sum(1 for nm, t in (c.get("texts") or {}).items() if not t.get("hidden") and nm.lower() != "designator")
            keep_out.append(vbox)
            keep_out.append((vbox[0], vbox[3], max(vbox[2], vbox[0] + 600), vbox[3] + text_gap + text_row))
            if n_below:
                keep_out.append((vbox[0], vbox[1] - text_gap - text_row * n_below - 40,
                                 max(vbox[2], vbox[0] + 1500), vbox[1]))
        if place_texts:
            tx = {}
            shown = [(name, t) for name, t in (c.get("texts") or {}).items() if not t.get("hidden")]
            box = vbox if conn_texts == "around" else None
            if box:
                # designator just above the connector's picture, the other shown texts
                # (comment, parameters) in rows just below it, all left-aligned on the picture
                bx1, by1, bx2, by2 = box
                below = 0
                for name, t in shown:
                    if name.lower() == "designator":
                        tx["designator"] = {"x": round(bx1), "y": round(by2 + text_gap), "justification": "bottom_left"}
                    else:
                        tx[name.lower()] = {"x": round(bx1), "y": round(by1 - text_gap - text_row * below), "justification": "top_left"}
                        below += 1
            else:
                for name, t in shown:
                    # the text keeps its place relative to the body: it turns with the connector
                    tdx, tdy = _rot(t["x"] * 10.0 - cx0, t["y"] * 10.0 - cy0, best)
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
            keep_out.append((mx - 450, my - 160, mx + 450, my + 160))      # its name and length
        out_bundles.append(be)

    # notes and pictures beside a connector or a free end (its label, a photo) travel with it:
    # each group (a graphic and the graphics lying inside it, e.g. an inset photo) is moved by
    # the offset of the anchor nearest its centre (within attach_mils), then pushed until it
    # clears the bundles, the connectors with their texts and the groups placed before it:
    # vertically away from the anchor's point or outward, whichever is the shorter move
    move_objects = []
    shifted = []
    attach = float(o.get("attach_mils", 6000))
    clear = float(o.get("graphic_clearance_mils", 100))
    # anchors: every connector (moved by its own offset) and every free end, a leaf connection
    # point without a connector (a wire end on a no-connect, a terminal): its own offset
    anchor_at = {c["designator"]: _mils(conns[c["designator"]]["location"]) for c in out_conns}
    moved = {c["designator"]: (c["x"] - anchor_at[c["designator"]][0],
                               c["y"] - anchor_at[c["designator"]][1]) for c in out_conns}
    cp_at = {c["designator"]: (c["connection_point"]["x"], c["connection_point"]["y"]) for c in out_conns}
    for n, (x, y) in pos.items():
        if n not in conn_of_cp and len(adj.get(n, [])) == 1 and n in cps and n not in anchor_at:
            anchor_at[n] = _mils(cps[n]["location"])
            moved[n] = (x - anchor_at[n][0], y - anchor_at[n][1])
            cp_at[n] = (x, y)
    graphics = [g for g in list(layout.get("notes", [])) + list(layout.get("pictures", []))
                if all(k in g for k in ("x1", "y1", "x2", "y2", "unique_id"))]
    rect = {g["unique_id"]: (min(g["x1"], g["x2"]) * 10.0, min(g["y1"], g["y2"]) * 10.0,
                             max(g["x1"], g["x2"]) * 10.0, max(g["y1"], g["y2"]) * 10.0) for g in graphics}

    def inside(a, b):
        return a != b and rect[b][0] <= rect[a][0] and rect[b][1] <= rect[a][1] and rect[a][2] <= rect[b][2] and rect[a][3] <= rect[b][3]

    groups: dict[str, list[str]] = {}
    for g in graphics:
        u = g["unique_id"]
        hosts = [h["unique_id"] for h in graphics if inside(u, h["unique_id"])]
        if hosts:
            top = max(hosts, key=lambda h: (rect[h][2] - rect[h][0]) * (rect[h][3] - rect[h][1]))
            while any(inside(top, h["unique_id"]) for h in graphics):
                top = next(h["unique_id"] for h in graphics if inside(top, h["unique_id"]))
            groups.setdefault(top, []).append(u)
        else:
            groups.setdefault(u, []).insert(0, u)
    segs = [(p[i], p[i + 1]) for p in bundle_pts.values() for i in range(len(p) - 1)]

    def hits(r):
        """(x1, y1, x2, y2, clearance) of the first obstacle the box r touches, else None:
        bundles keep `clear` (graphic_clearance_mils), boxes (pictures, texts, placed
        graphics) a third of it, so a label may sit close beside a connector picture. A
        bundle segment's box is the part of it beside r (its x range over r's height, its y
        range over r's width)."""
        c = clear / 3.0
        x1, y1, x2, y2 = r[0] - c, r[1] - c, r[2] + c, r[3] + c
        for k in keep_out:
            if x1 < k[2] and k[0] < x2 and y1 < k[3] and k[1] < y2:
                return (*k, c)
        x1, y1, x2, y2 = r[0] - clear, r[1] - clear, r[2] + clear, r[3] + clear
        for (ax, ay), (bx, by) in segs:
            if _seg_hits_box(ax, ay, bx, by, x1, y1, x2, y2):
                ylo, yhi = _seg_y_range_in(ax, ay, bx, by, x1, x2)
                xlo, xhi = _seg_y_range_in(ay, ax, by, bx, y1, y2)
                return xlo, ylo, xhi, yhi, clear
        return None

    def settle(r, way):
        """push r one way (up, down, right, left) until it is clear: (shift, box) or None"""
        total = 0.0
        for _ in range(200):
            h = hits(r)
            if h is None:
                return total, r
            ox1, oy1, ox2, oy2, c = h
            if way in ("up", "down"):
                step = (oy2 + c - r[1] + 1) if way == "up" else (oy1 - c - r[3] - 1)
                r = (r[0], r[1] + step, r[2], r[3] + step)
            else:
                step = (ox2 + c - r[0] + 1) if way == "right" else (ox1 - c - r[2] - 1)
                r = (r[0] + step, r[1], r[2] + step, r[3])
            total += step
        return None

    face_of = {d: facing.get(cp_of_conn.get(d), 1) for d in moved if d in conns}
    face_of.update({n: facing.get(n, 1) for n in moved if n not in conns})
    if attach > 0 and moved:
        for host, members in groups.items():
            r0 = rect[host]
            gx, gy = (r0[0] + r0[2]) / 2, (r0[1] + r0[3]) / 2
            d, best = min((math.dist((gx, gy), anchor_at[k]), k) for k in moved)
            if d > attach:
                continue
            dx, dy = moved[best]
            r = (r0[0] + dx, r0[1] + dy, r0[2] + dx, r0[3] + dy)
            # away from the anchor's point vertically, or outward (the way its connector faces):
            # whichever moves the group least
            vert = "up" if (r[1] + r[3]) / 2 >= cp_at[best][1] else "down"
            out = "right" if face_of.get(best, 1) > 0 else "left"
            tries = [(w, settle(r, w)) for w in (vert, out)]
            tries = [(w, t) for w, t in tries if t is not None]
            px = py = 0.0
            if tries:
                way, (shift, r) = min(tries, key=lambda wt: abs(wt[1][0]))
                if shift:
                    px, py = (0.0, shift) if way in ("up", "down") else (shift, 0.0)
                    shifted.append({"graphics": members, "connector": best, "pushed_mils": round(shift), "way": way})
            keep_out.append(r)
            off = (round(dx + px), round(dy + py))
            if off != (0, 0):
                for u in members:
                    move_objects.append({"unique_id": u, "offset": list(off)})

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
            **({"graphics_pushed": shifted} if shifted else {}),
            "extent_mils": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))],
        },
    }


def _seg_hits_box(ax: float, ay: float, bx: float, by: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    """does the segment a-b touch the box (Liang-Barsky clip)"""
    t0, t1 = 0.0, 1.0
    dx, dy = bx - ax, by - ay
    for p, q in ((-dx, ax - x1), (dx, x2 - ax), (-dy, ay - y1), (dy, y2 - ay)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _seg_y_range_in(ax: float, ay: float, bx: float, by: float, x1: float, x2: float) -> tuple[float, float]:
    """y range of the segment a-b over the x span [x1, x2] (a vertical segment: its whole span)"""
    if abs(bx - ax) < 1e-9:
        return min(ay, by), max(ay, by)
    ys = []
    for x in (max(min(ax, bx), x1), min(max(ax, bx), x2)):
        ys.append(ay + (by - ay) * (x - ax) / (bx - ax))
    return min(ys), max(ys)


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
