"""Read-only audit of one Altium harness/schematic document, or diff of two.

Usage (with the server's venv python, or any python with olefile):
    python wirdoc_diff.py doc.WirDoc                       audit one file
    python wirdoc_diff.py before.WirDoc after.WirDoc [W1 ...]  diff, with detail for wires

Reports record counts per type, top-level text, harness wire identity and end
attachments, and objects that carry the same parameter name more than once.
Cavity components (RECORD 106 owned by a cavity) are written twice by Altium
with the same UniqueID; their doubled parameters are not reported as duplicates.
"""

import collections
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[4] / "server"
sys.path.insert(0, str(SERVER))

from harness_parser import extract_ole_records  # noqa: E402

RECORD_NAMES = {
    "4": "text label",
    "14": "rectangle",
    "25": "net label",
    "34": "designator param",
    "41": "parameter",
    "106": "harness connector/component",
    "107": "harness wire",
    "108": "harness splice",
    "113": "harness pin",
    "119": "harness no-connect",
    "131": "cavity container",
}


def summarize(path: str) -> dict:
    body = extract_ole_records(path)[1:]
    children = collections.defaultdict(list)
    for r in body:
        if "OwnerIndex" in r:
            children[r["OwnerIndex"]].append(r)

    def params(i: int) -> list[tuple[str, str]]:
        return [
            (c.get("Name", ""), c.get("Text", ""))
            for c in children[str(i)]
            if c.get("RECORD") in ("34", "41")
        ]

    cavity_uids = collections.Counter(
        r.get("UniqueID") for r in body if r.get("RECORD") == "106" and "OwnerIndex" in r
    )
    duplicates = collections.Counter()
    wires = {}
    for i, r in enumerate(body):
        kind = r.get("RECORD")
        if kind not in ("106", "107", "108", "113"):
            continue
        names = collections.Counter(n for n, _ in params(i))
        doubled_by_format = kind == "106" and cavity_uids.get(r.get("UniqueID"), 0) > 1
        for name, count in names.items():
            if count > (2 if doubled_by_format else 1):
                duplicates[(kind, name)] += 1
        if kind == "107":
            p = dict(params(i))
            des = p.get("Designator", f"#{i}")
            n = int(r.get("LocationCount", 0))
            wires[des] = {
                "uid": r.get("UniqueID"),
                "ends": (r.get("EndVertex1ConnectedObjectUniqueID"), r.get("EndVertex2ConnectedObjectUniqueID")),
                "color": r.get("Color", "0"),
                "part": r.get("DesignItemId", ""),
                "param_count": len(params(i)),
                "points": [(r.get(f"X{k}", "0"), r.get(f"Y{k}", "0")) for k in range(1, n + 1)],
            }
    return {
        "counts": collections.Counter(r.get("RECORD") for r in body),
        "texts": collections.Counter(
            r.get("Text", "") for r in body if r.get("RECORD") == "4" and "OwnerIndex" not in r
        ),
        "wires": wires,
        "duplicates": duplicates,
    }


def label(kind: str) -> str:
    return f"RECORD {kind} ({RECORD_NAMES.get(kind, '?')})"


def audit(path: str) -> None:
    s = summarize(path)
    print(f"== {path}")
    for kind, n in sorted(s["counts"].items(), key=lambda kv: int(kv[0] or 0)):
        print(f"  {label(kind)}: {n}")
    print(f"  harness wires: {len(s['wires'])}, without end attachments: "
          f"{sum(1 for w in s['wires'].values() if not all(w['ends']))}")
    report_duplicates(s)


def report_duplicates(s: dict) -> None:
    if not s["duplicates"]:
        print("  duplicate parameter names: none")
        return
    print("  duplicate parameter names (objects affected):")
    for (kind, name), n in sorted(s["duplicates"].items()):
        print(f"    {label(kind)} '{name}': {n}")


def diff(a_path: str, b_path: str, detail: list[str]) -> None:
    a, b = summarize(a_path), summarize(b_path)
    print(f"== {a_path}\n-> {b_path}")
    changed = False
    for kind in sorted(set(a["counts"]) | set(b["counts"]), key=lambda k: int(k or 0)):
        if a["counts"][kind] != b["counts"][kind]:
            changed = True
            print(f"  {label(kind)}: {a['counts'][kind]} -> {b['counts'][kind]}")
    if not changed:
        print("  record counts: unchanged")
    lost, added = a["texts"] - b["texts"], b["texts"] - a["texts"]
    print(f"  top-level text lost: {sum(lost.values())} {list(lost)[:5]}")
    print(f"  top-level text added: {sum(added.values())} {list(added)[:5]}")
    common = set(a["wires"]) & set(b["wires"])
    print(f"  wires: {len(a['wires'])} -> {len(b['wires'])}; "
          f"UniqueID changed: {sum(a['wires'][d]['uid'] != b['wires'][d]['uid'] for d in common)}; "
          f"end attachments changed: {sum(a['wires'][d]['ends'] != b['wires'][d]['ends'] for d in common)}; "
          f"geometry changed: {sum(a['wires'][d]['points'] != b['wires'][d]['points'] for d in common)}")
    print("  before:", end="")
    report_duplicates(a)
    print("  after: ", end="")
    report_duplicates(b)
    for des in detail:
        wa, wb = a["wires"].get(des), b["wires"].get(des)
        if not wa or not wb:
            print(f"  {des}: missing in {'before' if not wa else 'after'}")
            continue
        print(f"  {des}: uid {wa['uid']} -> {wb['uid']}, color {wa['color']} -> {wb['color']}, "
              f"part {wa['part']} -> {wb['part']}, params {wa['param_count']} -> {wb['param_count']}")
        print(f"      points {wa['points']} -> {wb['points']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if len(args) == 1:
        audit(args[0])
    else:
        diff(args[0], args[1], args[2:])
