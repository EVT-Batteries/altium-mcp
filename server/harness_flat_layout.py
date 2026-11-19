"""Flattened (formboard-style) 2D layout of a harness layout drawing's bundle tree.

The physical harness is a tree: connection points joined by bundles. This module lays it out
the way a harness is drawn on a formboard, without the glitches of a 3D-projected flatten:

- the trunk (from the root connector along the bundle with the most wires at each node,
  ties: the shorter bundle, since a branch is drawn with its bend) is horizontal,
  root connector on the left facing left, every other connector on the right facing right;
- every side branch leaves its node at 45 degrees and turns parallel to the trunk at the
  nearest offset that is clear of everything placed before it (bundles, connector pictures
  and their texts), so branches never cross or lie on top of each other: on each side the
  branch nearest the trunk end is placed first (innermost), a branch is placed together with
  its own branches (it moves out when they find no room), and a bend that is blocked
  whatever the offset turns steeper (60, 75, 90 degrees);
- branches alternate above / below the trunk (first_side), node by node;
- drawn lengths follow the physical lengths (one scale; a branch's slanted part counts) but
  never fall below min_bundle_mils, so a 4 mm bundle between two breakouts stays readable;
  the length text carries the true value and the break symbol marks a bundle not drawn to
  scale;
- each connector is turned (quarter turns) so that its body lies beyond its connection point,
  continuing the bundle, and moved with it; its texts move and turn with it (each keeps its
  place relative to the body, upright);
- bundle designator and length as a pair (designator above, length below the bundle) at the
  first clear place along it: the middle of its longest straight, then towards its ends,
  then beside a slanted part; connection point names beside the point (above first); texts
  keep clear of bundles, connector pictures and each other (sizes estimated from the text).

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
    location and moving the location to (nx, ny): the part it shows (image content_half)
    when known, else the whole picture; None without a picture"""
    views = conn.get("views") or []
    v = next((v for v in views if v.get("main") and not v.get("hidden") and v.get("image")), None)
    v = v or next((v for v in views if not v.get("hidden") and v.get("image")), None)
    if not v:
        return None
    cx, cy = _mils(conn["location"])
    im = v["image"]
    xs0, ys0 = (im["x1"], im["x2"]), (im["y1"], im["y2"])
    if im.get("content_half"):
        # only the part of the picture the part takes (the rest is plain background)
        mx, my = sum(xs0) / 2, sum(ys0) / 2
        hx, hy = im["content_half"]
        xs0, ys0 = (mx - hx, mx + hx), (my - hy, my + hy)
    corners = [_rot(px * 10.0 - cx, py * 10.0 - cy, k) for px in xs0 for py in ys0]
    xs = [nx + p[0] for p in corners]
    ys = [ny + p[1] for p in corners]
    return min(xs), min(ys), max(xs), max(ys)


def plan_flat_layout(layout: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]:
    """options: root (connector designator), lengths ({bundle: mm}, applied as length_mm),
    origin ([x, y] mils of the root connection point, default its current place),
    scale (mils per mm, default: the trunk fills width_mils), width_mils (default 8000),
    min_bundle_mils (400), first_side ('up' | 'down'), place_texts (True),
    lane_spacing (400: the least offset of a branch's run from the run it leaves, and between
    two runs side by side), clearance_mils (100: between a run and connector pictures / texts),
    min_tail_mils (300: the straight end of a branch after its bend), lane_step_mils (100) and
    lane_steps (60: how far out a branch looks for a clear lane, per bend angle),
    text_char_mils (60) / text_height_mils (100): the text size estimate, text_clearance_mils (30),
    connector_texts ('around': designator above the connector's main view picture, comment and
    shown parameters in rows below it (text_gap_mils 50, text_row_mils 130); 'keep': each text
    keeps its place relative to the body, turned with the connector; a connector without a
    view picture always keeps),
    attach_mils (6000: notes / pictures lying within it of a connector's picture or a free wire
    end move with the nearest one; 0 = none),
    break_symbols (True: on bundles whose drawn length differs from the physical one by > 2 %)."""
    o = dict(options or {})
    lengths_in = {str(k): float(v) for k, v in (o.get("lengths") or {}).items()}
    min_draw = float(o.get("min_bundle_mils", 400))
    lane = float(o.get("lane_spacing", 400))
    min_tail = float(o.get("min_tail_mils", 300))
    run_clear = float(o.get("clearance_mils", 100))
    step = float(o.get("lane_step_mils", 100))
    steps = int(o.get("lane_steps", 60))
    char_w = float(o.get("text_char_mils", 60))
    text_h = float(o.get("text_height_mils", 100))
    text_clear = float(o.get("text_clearance_mils", 30))
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
        (the main bundle; ties: the shorter bundle, as a branch's bend makes it longer)"""
        out = []
        while children.get(n):
            m, e = max(children[n], key=lambda me: (nwires[me[1]], -phys[me[1]], me[0]))
            out.append((m, e))
            n = m
        return out

    def sides(n: str, cont: str | None) -> list[tuple[str, str]]:
        return [(m, e) for m, e in children.get(n, []) if m != cont]

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

    def text_box(x: float, y: float, just: str | None, w: float) -> tuple[float, float, float, float]:
        j = (just or "bottom_left").lower()
        v, hz = ("center", "center") if j == "center" else j.split("_", 1)
        x1 = x if hz == "left" else x - w / 2 if hz == "center" else x - w
        y1 = y if v == "bottom" else y - text_h / 2 if v == "center" else y - text_h
        return x1, y1, x1 + w, y1 + text_h

    def text_value(c: dict[str, Any], name: str) -> str:
        n = name.lower()
        if n == "designator":
            return c["designator"]
        if n == "comment":
            return str(c.get("part_number") or c.get("comment") or "")
        return str((c.get("parameters") or {}).get(name) or "") or "x" * 20

    def conn_place(n: str, x: float, y: float) -> tuple[dict[str, Any], list] | None:
        """the request entry of the connector on node n with its connection point at (x, y),
        and the boxes it takes (its picture and its texts, mils); None: no connector there"""
        d = conn_of_cp.get(n)
        if d is None:
            return None
        c = conns[d]
        cp = cps.get(n)
        cx0, cy0 = _mils(c["location"])
        px0, py0 = _mils(cp["location"]) if cp else (cx0, cy0)
        ox_, oy_ = cx0 - px0, cy0 - py0                   # body offset from the point, now
        want = -1 if n == root else 1                      # the root faces left, the others right
        best = max(range(4), key=lambda k: (_rot(ox_, oy_, k)[0] * want, -k))
        rx, ry = _rot(ox_, oy_, best)
        nx, ny = x + rx, y + ry
        ce: dict[str, Any] = {
            "designator": d, "x": round(nx), "y": round(ny),
            "orientation": (c["orientation"] + 90 * best) % 360,
            "connection_point": {"x": round(x), "y": round(y)},
        }
        vbox = _view_box(c, best, nx, ny)
        shown = [(name, t) for name, t in (c.get("texts") or {}).items() if not t.get("hidden")]
        boxes = [vbox] if vbox else [(min(x, nx) - 100, min(y, ny) - 100, max(x, nx) + 100, max(y, ny) + 100)]
        tx: dict[str, Any] = {}
        if vbox and conn_texts == "around":
            # designator just above the connector's picture, the other shown texts (comment,
            # parameters) in rows just below it, all left-aligned on the picture
            bx1, by1, bx2, by2 = vbox
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
        for name, t in shown:
            p = tx[name.lower()]
            boxes.append(text_box(p["x"], p["y"], p["justification"], char_w * max(1, len(text_value(c, name)))))
        if place_texts and tx:
            ce["texts"] = tx
        if vbox:
            ce["_picture"] = True
        return ce, boxes

    # ---- placement: the trunk, then each side branch in the first lane that is clear of all
    # placed so far (bundles, connector pictures and texts); a nested branch is placed with
    # its parent, so a parent that leaves no room for it moves out
    pos: dict[str, tuple[float, float]] = {root: (ox, oy)}
    bundle_pts: dict[str, list[tuple[float, float]]] = {}
    # a connector's boxes carry its connection point: the bundles ending there may touch them
    boxes: list[tuple[float, float, float, float, tuple[float, float]]] = []
    segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    unclear: list[str] = []

    def box_free(r: tuple, at: tuple) -> bool:
        x1, y1, x2, y2 = r[0] - run_clear, r[1] - run_clear, r[2] + run_clear, r[3] + run_clear
        if any(x1 < k[2] and k[0] < x2 and y1 < k[3] and k[1] < y2 for k in boxes):
            return False
        return not any(_seg_hits_box(a[0], a[1], b[0], b[1], x1, y1, x2, y2) for a, b in segs if at not in (a, b))

    def seg_free(a: tuple, b: tuple) -> bool:
        for k in boxes:
            if k[4] in (a, b):
                continue
            if _seg_hits_box(a[0], a[1], b[0], b[1], k[0] - run_clear, k[1] - run_clear, k[2] + run_clear, k[3] + run_clear):
                return False
        for p, q in segs:
            if {p, q} & {a, b}:
                continue
            if segments_cross(a, b, p, q):
                return False
            if a[1] == b[1] and p[1] == q[1] and abs(a[1] - p[1]) < lane \
                    and min(a[0], b[0]) < max(p[0], q[0]) and min(p[0], q[0]) < max(a[0], b[0]):
                return False                                    # two runs side by side
        return True

    def add_conn(n: str) -> bool:
        g = conn_place(n, *pos[n])
        if g is None:
            return True
        ok = all(box_free(b, pos[n]) for b in g[1])
        boxes.extend((*b, pos[n]) for b in g[1])
        return ok

    def snapshot():
        return len(boxes), len(segs), set(pos), set(bundle_pts), len(unclear)

    def rollback(s) -> None:
        del boxes[s[0]:]
        del segs[s[1]:]
        del unclear[s[4]:]
        for k in set(pos) - s[2]:
            del pos[k]
        for k in set(bundle_pts) - s[3]:
            del bundle_pts[k]

    def lay_run(start: str, x: float, y: float, outward: int, up_first: bool) -> bool:
        """lay the run of `start` (already placed at x, y) to the right, then its side
        branches: outward (+1 up / -1 down) when outward != 0, else alternating starting with
        up_first. False when part of it is not clear."""
        run = chain(start)
        nodes = [start] + [m for m, _ in run]
        ok = True
        xx = x
        for m, e in run:
            a = pos[parent[m][0]]
            xx += drawn(e)
            pos[m] = (xx, y)
            ok = seg_free(a, pos[m]) and ok
            segs.append((a, pos[m]))
            bundle_pts[e] = [a, pos[m]]
        for m, _ in run:
            ok = add_conn(m) and ok
        branches = []
        flip = up_first
        for i, k in enumerate(nodes):
            cont = nodes[i + 1] if i + 1 < len(nodes) else None
            for m, e in sorted(sides(k, cont), key=lambda me: -(phys[me[1]] + far[me[0]])):
                s = outward if outward else (1 if flip else -1)
                flip = not flip
                branches.append((i, k, m, e, s))
        # on each side the branch nearest the run's end first: it takes the innermost lane
        for i, k, m, e, s in sorted(branches, key=lambda b: -b[0]):
            ok = place_branch(k, m, e, s, up_first) and ok
        return ok

    def place_branch(k: str, m: str, e: str, s: int, up_first: bool) -> bool:
        """the branch k -> m leaves k at 45 degrees (steeper when 45 is blocked) and runs
        parallel to the trunk at the nearest clear offset; its drawn length (bend included)
        follows the physical one"""
        kx, ky = pos[k]
        target = drawn(e)
        tries = [(ang, lane + step * i) for ang in (45, 60, 75, 90) for i in range(steps + 1)]
        for n_try, (ang, dy) in enumerate(tries):
            last = n_try == len(tries) - 1
            snap = snapshot()
            bx, by = kx + dy / math.tan(math.radians(ang)), ky + s * dy
            tail = max(min_tail, target - dy / math.sin(math.radians(ang)))
            pos[m] = (bx + tail, by)
            d1, d2 = ((kx, ky), (bx, by)), ((bx, by), pos[m])
            ok = seg_free(*d1) and seg_free(*d2)
            segs.extend([d1, d2])
            bundle_pts[e] = [(kx, ky), (bx, by), pos[m]]
            ok = add_conn(m) and ok
            if ok or last:
                ok = lay_run(m, pos[m][0], by, s, up_first) and ok
            if ok:
                return True
            if last:
                unclear.append(e)
                return False
            rollback(snap)
        return False

    add_conn(root)
    lay_run(root, ox, oy, 0, first_up)
    facing: dict[str, int] = {n: (-1 if n == root else 1) for n in pos}

    # ---- requests
    out_conns, out_cps, out_bundles = [], [], []
    keep_out: list[tuple[float, float, float, float]] = []      # what graphics keep clear of
    conn_entry: dict[str, dict[str, Any]] = {}
    for n, (x, y) in pos.items():
        g = conn_place(n, x, y)
        if g:
            conn_entry[n] = g[0]
            # without a picture the body box is only an estimate: graphics may sit beside it
            keep_out.extend(g[1] if g[0].get("_picture") else g[1][1:])
            g[0].pop("_picture", None)
            out_conns.append(g[0])

    # texts: each bundle's designator and length as a pair, at the first clear place along it
    # (a straight's middle, then towards its ends; beside a slanted part), then each connection
    # point's name beside its point; all clear of bundles, connectors and placed texts
    segs = [(p[i], p[i + 1]) for p in bundle_pts.values() for i in range(len(p) - 1)]
    placed: list[tuple[float, float, float, float]] = []

    def text_free(r: tuple, skip: tuple | None = None) -> bool:
        c = text_clear
        x1, y1, x2, y2 = r[0] - c, r[1] - c, r[2] + c, r[3] + c
        if any(x1 < k[2] and k[0] < x2 and y1 < k[3] and k[1] < y2 for k in keep_out + placed):
            return False
        return not any(_seg_hits_box(a[0], a[1], b[0], b[1], x1, y1, x2, y2) for a, b in segs if (a, b) != skip)

    def bundle_text_spots(pts: list, wd: float, wl: float):
        """candidate (designator, length) text entries along a bundle, best first"""
        sg = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        flat = sorted((q for q in sg if q[0][1] == q[1][1]), key=lambda q: -abs(q[1][0] - q[0][0]))
        slant = sorted((q for q in sg if q[0][1] != q[1][1]), key=lambda q: -math.dist(*q))
        for q in flat:
            (ax, ay), (zx, _) = q
            for t in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8):
                mx = ax + (zx - ax) * t
                yield q, ({"x": round(mx), "y": round(ay + 30), "justification": "bottom_center"},
                          {"x": round(mx), "y": round(ay - 30), "justification": "top_center"})
        side = text_h + 60
        for q in slant:
            (ax, ay), (zx, zy) = q
            for t in (0.5, 0.35, 0.65):
                mx, my = ax + (zx - ax) * t, ay + (zy - ay) * t
                for sx, hz in ((-side, "right"), (side, "left")):
                    yield q, ({"x": round(mx + sx), "y": round(my + 10), "justification": "bottom_" + hz},
                              {"x": round(mx + sx), "y": round(my - 10), "justification": "top_" + hz})

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
            wd = char_w * max(2, len(e if e != bkey[e] else "B?"))
            wl = char_w * len(f"{phys[e]:.2f}mm")
            first = None
            for q, (td, tl) in bundle_text_spots(pts, wd, wl):
                bd = text_box(td["x"], td["y"], td["justification"], wd)
                bl = text_box(tl["x"], tl["y"], tl["justification"], wl)
                first = first or (td, tl, bd, bl)
                if text_free(bd, q) and text_free(bl, q):
                    break
            else:
                td, tl, bd, bl = first
            be["texts"] = {"designator": td, "length": tl}
            placed.extend([bd, bl])
        out_bundles.append(be)

    for n, (x, y) in pos.items():
        cp = cps.get(n)
        entry: dict[str, Any] = {"designator": n}
        if n not in conn_of_cp:
            entry.update({"x": round(x), "y": round(y)})
        if place_texts and cp is not None and cp.get("show_name", True):
            w = char_w * len(n)
            spots = [(0, 180, "bottom_center"), (0, -180, "top_center"), (-120, 90, "bottom_right"),
                     (120, 90, "bottom_left"), (-120, -90, "top_right"), (120, -90, "top_left"),
                     (0, 320, "bottom_center"), (0, -320, "top_center")]
            for dx, dy, j in spots:
                r = text_box(x + dx, y + dy, j, w)
                if text_free(r):
                    break
            else:
                dx, dy, j = spots[0]
                r = text_box(x + dx, y + dy, j, w)
            entry["texts"] = {"designator": {"x": round(x + dx), "y": round(y + dy), "justification": j}}
            placed.append(r)
        if len(entry) > 1:
            out_cps.append(entry)
    keep_out.extend(placed)

    # notes and pictures beside a connector or a free end (its label, a photo) travel with it:
    # each group (a graphic and the graphics lying inside it, e.g. an inset photo) is moved by
    # the offset of the anchor nearest its centre (within attach_mils), then pushed until it
    # clears the bundles, the connectors with their texts and the groups placed before it:
    # in the shortest of the four ways (away from the anchor's point vertically or outward first)
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
    # what a graphic is measured from: the connector's picture where it has one (its place now)
    anchor_box = {k: (_view_box(conns[k], 0, *p) if k in conns else None) or (*p, *p) for k, p in anchor_at.items()}
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
        # the biggest first: a photo has fewer places to go than a one-line note
        for host, members in sorted(groups.items(), key=lambda hm: -(rect[hm[0]][2] - rect[hm[0]][0]) * (rect[hm[0]][3] - rect[hm[0]][1])):
            r0 = rect[host]
            gx, gy = (r0[0] + r0[2]) / 2, (r0[1] + r0[3]) / 2
            d, best = min((_box_dist(r0, anchor_box[k]), k) for k in moved)
            if d > attach:
                continue
            dx, dy = moved[best]
            r = (r0[0] + dx, r0[1] + dy, r0[2] + dx, r0[3] + dy)
            # away from the anchor's point vertically, or outward (the way its connector faces),
            # else the other two ways:
            # whichever moves the group least
            vert = "up" if (r[1] + r[3]) / 2 >= cp_at[best][1] else "down"
            out = "right" if face_of.get(best, 1) > 0 else "left"
            back = "left" if out == "right" else "right"
            other = "down" if vert == "up" else "up"
            tries = [(w, settle(r, w)) for w in (vert, out, other, back)]
            tries = [(w, t) for w, t in tries if t is not None]
            px = py = 0.0
            if tries:
                # the shortest move; on a tie the preferred way (listed first)
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
            **({"branches_not_clear": unclear} if unclear else {}),
            "extent_mils": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))],
        },
    }


def _box_dist(a: tuple, b: tuple) -> float:
    """distance between two boxes (0 when they touch or overlap)"""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy)


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
