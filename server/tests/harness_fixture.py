"""A small synthetic harness project for the offline tests.

The tests must not depend on any user's design files, so this module writes a
harness project of its own: a ``.PrjHar`` and two ``.WirDoc`` sheets stored as
real compound documents (one ``FileHeader`` stream of length-prefixed
``|KEY=VALUE|`` records, as Altium writes them), with generic parts only. The
readers (``harness_parser``, ``harness_check`` and the tool fallbacks in
``main``) open it exactly as they open a saved Altium file.

It is not an Altium-complete document (no storage, sheet template or
additional streams), so do not open it in Altium; it covers what the
file-reading code uses.

Sheet "Fixture_Main.WirDoc" (DXP units; 1 unit = 10 mils)::

    P1 (CONN-4P, x 100, pins face right)          J1 (CONN-4S, x 600, pins face left)
      1 (120, 400) ---- W1  SIG_A  (twist TW1) ---- (580, 400) 1
      2 (120, 370) ---- W2  SIG_B  (twist TW1) ---- (580, 370) 2
      3 (120, 340) ---- W3  PWR ---- SP1 (400, 340) ---- W4  PWR ---- (580, 340) 3
                                         `--- W5 PWR down to y 310 ---- (580, 310) 4
      4 (120, 310) x NC1

    P1 carries crimps TERM-A in cavities 1-3 and the boot BOOT-4P; J1 crimps
    TERM-B in cavities 1-4. Wires use WIRE-22-BK (W1, W2) and WIRE-18-RD (W3-W5).

Sheet "Fixture_Branch.WirDoc": connector P2 (CONN-2P) whose pin 1 has wire W6
(net SIG_C) to a wire break and whose pin 2 is unwired, and an unannotated
connector "X?" with no part number.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import pythoncom
from win32com import storagecon as sc

CREATE = sc.STGM_CREATE | sc.STGM_READWRITE | sc.STGM_SHARE_EXCLUSIVE

HEADER = {
    "HEADER": "Protel for Windows - Harness Wiring Diagram Binary File Version 1.0",
    "MinorVersion": "11",
    "UniqueID": "FIXTURE1",
}

# Part numbers and nets the tests assert on
P1_PART = "CONN-4P"
J1_PART = "CONN-4S"
P2_PART = "CONN-2P"
NETS_MAIN = ["PWR", "SIG_A", "SIG_B"]


class _Sheet:
    """Builds the record list of one sheet; add() returns the record's index
    (the number OwnerIndex refers to: the header record is not counted)."""

    def __init__(self) -> None:
        self.records: list[dict[str, str]] = []

    def add(self, **fields: object) -> int:
        self.records.append({k.replace("__", "."): str(v) for k, v in fields.items()})
        return len(self.records) - 1

    def connector(
        self,
        des: str,
        part: str,
        lib_ref: str,
        x: int,
        y: int,
        uid: str,
        description: str = "",
        parameters: dict[str, str] | None = None,
    ) -> int:
        idx = self.add(
            RECORD=106,
            LibReference=lib_ref,
            DesignItemId=part,
            SourceLibraryName="fixture.DbLib" if part else "",
            ComponentDescription=description,
            Location__X=x,
            Location__Y=y,
            UniqueID=uid,
        )
        self.add(
            RECORD=34,
            OwnerIndex=idx,
            Name="Designator",
            Text=des,
            Location__X=x,
            Location__Y=y + 20,
        )
        for name, value in (parameters or {}).items():
            self.add(RECORD=41, OwnerIndex=idx, Name=name, Text=value, IsHidden="T")
        return idx

    def pin(
        self,
        owner: int,
        des: str,
        x: int,
        y: int,
        facing: int,
        uid: str,
        name: str = "",
    ) -> int:
        # PinConglomerate bits 0-1: 0 = hot end to the right, 2 = to the left
        return self.add(
            RECORD=113,
            OwnerIndex=owner,
            Designator=des,
            Name=name or des,
            Location__X=x,
            Location__Y=y,
            PinLength=20,
            PinConglomerate=facing,
            UniqueID=uid,
        )

    def cavity(self, pin: int, part: str, uid: str) -> None:
        container = self.add(RECORD=131, OwnerIndex=pin)
        for _ in range(2):  # Altium writes every crimp twice, same UniqueID
            self.add(
                RECORD=106,
                OwnerIndex=container,
                DesignItemId=part,
                SourceLibraryName="fixture.DbLib",
                UniqueID=uid,
                ComponentDescription=f"Crimp {part}",
            )

    def associated(self, owner: int, part: str, uid: str) -> None:
        container = self.add(RECORD=125, OwnerIndex=owner)
        self.add(
            RECORD=106,
            OwnerIndex=container,
            DesignItemId=part,
            SourceLibraryName="fixture.DbLib",
            UniqueID=uid,
        )

    def wire(
        self,
        des: str,
        net: str,
        points: list[int],
        uid: str,
        part: str,
        parameters: dict[str, str],
    ) -> int:
        fields: dict[str, object] = {
            "RECORD": 107,
            "LocationCount": len(points) // 2,
            "DesignItemId": part,
            "SourceLibraryName": "fixture.DbLib",
            "UniqueID": uid,
        }
        for i in range(0, len(points), 2):
            fields[f"X{i // 2 + 1}"] = points[i]
            fields[f"Y{i // 2 + 1}"] = points[i + 1]
        idx = self.add(**fields)
        # Labels side by side at the middle of the longest horizontal run, as
        # add_harness_wiring places them
        runs = [
            (abs(points[i + 2] - points[i]), i)
            for i in range(0, len(points) - 2, 2)
            if points[i + 1] == points[i + 3]
        ]
        _, i = max(runs)
        x = (points[i] + points[i + 2]) // 2
        y = points[i + 1]
        self.add(
            RECORD=34,
            OwnerIndex=idx,
            Name="Designator",
            Text=des,
            Location__X=x - 20,
            Location__Y=y + 1,
        )
        self.add(
            RECORD=41,
            OwnerIndex=idx,
            Name="Comment",
            Text=net,
            Location__X=x + 18,
            Location__Y=y + 1,
        )
        for name, value in parameters.items():
            self.add(RECORD=41, OwnerIndex=idx, Name=name, Text=value, IsHidden="T")
        return idx


WIRE_22_BK = {"PartNumber": "WIRE-22-BK", "Gauge": "22 AWG", "Color": "Black"}
WIRE_18_RD = {"PartNumber": "WIRE-18-RD", "Gauge": "18 AWG", "Color": "Red"}


def main_sheet() -> list[dict[str, str]]:
    s = _Sheet()
    p1 = s.connector(
        "P1",
        P1_PART,
        "CONN_PLUG_4",
        100,
        400,
        "P1UID",
        "Generic 4-way plug",
        {"PartNumber": P1_PART, "Manufacturer 1": "Generic Connectors"},
    )
    p1_pins = [
        s.pin(p1, str(n), 100, 430 - 30 * n, 0, f"P1PIN{n}") for n in range(1, 5)
    ]
    for n, pin in enumerate(p1_pins[:3], start=1):
        s.cavity(pin, "TERM-A", f"P1CAV{n}")
    s.associated(p1, "BOOT-4P", "P1BOOT")

    j1 = s.connector(
        "J1",
        J1_PART,
        "CONN_RECEPT_4",
        600,
        400,
        "J1UID",
        "Generic 4-way receptacle",
        {"PartNumber": J1_PART, "Manufacturer 1": "Generic Connectors"},
    )
    j1_pins = [
        s.pin(j1, str(n), 600, 430 - 30 * n, 2, f"J1PIN{n}") for n in range(1, 5)
    ]
    for n, pin in enumerate(j1_pins, start=1):
        s.cavity(pin, "TERM-B", f"J1CAV{n}")

    s.wire("W1", "SIG_A", [120, 400, 580, 400], "W1UID", "WIRE-22-BK", WIRE_22_BK)
    s.wire("W2", "SIG_B", [120, 370, 580, 370], "W2UID", "WIRE-22-BK", WIRE_22_BK)
    s.wire("W3", "PWR", [120, 340, 400, 340], "W3UID", "WIRE-18-RD", WIRE_18_RD)
    s.wire("W4", "PWR", [400, 340, 580, 340], "W4UID", "WIRE-18-RD", WIRE_18_RD)
    s.wire(
        "W5", "PWR", [400, 340, 400, 310, 580, 310], "W5UID", "WIRE-18-RD", WIRE_18_RD
    )
    sp = s.add(
        RECORD=108,
        Location__X=400,
        Location__Y=340,
        UniqueID="SP1UID",
        ConnectedWiresUniqueIdsCount=3,
        ConnectedWireUniqueId1="W3UID",
        ConnectedWireUniqueId2="W4UID",
        ConnectedWireUniqueId3="W5UID",
    )
    s.add(
        RECORD=34,
        OwnerIndex=sp,
        Name="Designator",
        Text="SP1",
        Location__X=402,
        Location__Y=342,
    )
    s.add(
        RECORD=119,
        Location__X=120,
        Location__Y=310,
        Style=1,
        Text="NC1",
        ShowName="F",
        UniqueID="NC1UID",
    )
    # Twist mark across W1 / W2 (10 units beyond each wire, 50 along)
    s.add(
        RECORD=118,
        Location__X=200,
        Location__Y=360,
        Corner__X=250,
        Corner__Y=410,
        UniqueID="TW1UID",
        ConnectedWiresUniqueIdsCount=2,
        ConnectedWireUniqueId1="W1UID",
        ConnectedWireUniqueId2="W2UID",
    )
    s.add(RECORD=4, Text="Fixture main sheet", Location__X=100, Location__Y=200)
    return s.records


def branch_sheet() -> list[dict[str, str]]:
    s = _Sheet()
    p2 = s.connector(
        "P2",
        P2_PART,
        "CONN_PLUG_2",
        100,
        400,
        "P2UID",
        "Generic 2-way plug",
        {"PartNumber": P2_PART},
    )
    s.pin(p2, "1", 100, 400, 0, "P2PIN1")
    s.pin(p2, "2", 100, 370, 0, "P2PIN2")  # unwired on purpose
    s.wire("W6", "SIG_C", [120, 400, 500, 400], "W6UID", "WIRE-22-BK", WIRE_22_BK)
    s.add(
        RECORD=130,
        Location__X=500,
        Location__Y=400,
        Text="W6",
        Style=1,
        Orientation=0,
        UniqueID="BR1UID",
        ConnectedObjectUniqueId="W6UID",
    )
    s.connector("X?", "", "", 100, 200, "XUID")  # unannotated, no part
    return s.records


def encode_records(records: list[dict[str, str]]) -> bytes:
    """The FileHeader stream: header record first, each record a uint32
    length, the '|KEY=VALUE' text (cp1252) and a terminating NUL."""
    out = bytearray()
    for rec in [HEADER, *records]:
        raw = ("|" + "|".join(f"{k}={v}" for k, v in rec.items())).encode(
            "cp1252"
        ) + b"\x00"
        out += struct.pack("<I", len(raw)) + raw
    return bytes(out)


def write_wirdoc(path: Path, records: list[dict[str, str]]) -> Path:
    root = pythoncom.StgCreateDocfile(str(path), CREATE, 0)
    stream = root.CreateStream("FileHeader", CREATE, 0, 0)
    stream.Write(encode_records(records))
    stream = None
    root.Commit(0)
    root = None
    return path


def write_prjhar(path: Path, documents: list[str], parameters: dict[str, str]) -> Path:
    lines = ["[Design]", "Version=1.0", "HierarchyMode=0", ""]
    for n, doc in enumerate(documents, start=1):
        lines += [f"[Document{n}]", f"DocumentPath={doc}", "DoLibraryUpdate=1", ""]
    for n, (name, value) in enumerate(parameters.items(), start=1):
        lines += [f"[Parameter{n}]", f"Name={name}", f"Value={value}", ""]
    path.write_text("\r\n".join(lines), encoding="utf-8")
    return path


@dataclass
class FixtureProject:
    folder: Path
    prjhar: Path
    main: Path
    branch: Path


def make_fixture_project(folder: Path | str) -> FixtureProject:
    """Write the fixture project into folder (which must exist) and return its paths."""
    folder = Path(folder)
    main = write_wirdoc(folder / "Fixture_Main.WirDoc", main_sheet())
    branch = write_wirdoc(folder / "Fixture_Branch.WirDoc", branch_sheet())
    prjhar = write_prjhar(
        folder / "Fixture_Harness.PrjHar",
        [main.name, branch.name, "Fixture_Harness.LdrDoc", "Fixture_Harness.BomDoc"],
        {"PROJECT_TITLE": "Fixture Harness", "PROJECT_REVISION": "B"},
    )
    return FixtureProject(folder, prjhar, main, branch)


if __name__ == "__main__":  # write the project somewhere to look at it
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    target.mkdir(parents=True, exist_ok=True)
    print(make_fixture_project(target))
