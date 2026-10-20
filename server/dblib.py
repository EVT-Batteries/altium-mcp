"""Read CSV-backed Altium database libraries (.DbLib) without Altium.

Harness wires and cavity terminals cannot be picked from a local .DbLib in the
Altium GUI, so the server writes their library link and parameters itself.
This module supplies the rows: it parses the .DbLib (connection string,
tables, field maps), loads each table's CSV, and finds parts by key.
"""

from __future__ import annotations

import configparser
import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Columns that drive Altium's library lookup rather than describe the part
SYSTEM_COLUMNS = {"Library Ref", "Library Path", "Footprint Ref", "Footprint Path"}


@dataclass
class DbTable:
    name: str
    key: str
    rows: dict[str, dict[str, str]]
    # Columns whose field map turns them into a system field such as
    # [Library Ref]; they are not copied as parameters
    system_columns: set[str] = field(default_factory=set)

    def parameters(self, row: dict[str, str]) -> dict[str, str]:
        """The row's columns that become component parameters."""
        return {
            k: v
            for k, v in row.items()
            if k
            and v
            and k not in SYSTEM_COLUMNS
            and k not in self.system_columns
        }


@dataclass
class DbLib:
    path: Path | None
    name: str
    tables: dict[str, DbTable]

    def find(self, part_number: str) -> tuple[DbTable, dict[str, str]] | None:
        for table in self.tables.values():
            row = table.rows.get(part_number)
            if row is not None:
                return table, row
        return None


def _read_csv_table(path: Path, name: str, system_columns: set[str]) -> DbTable:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        key = reader.fieldnames[0] if reader.fieldnames else "PartNumber"
        rows = {}
        for row in reader:
            clean = {k: (v or "").strip() for k, v in row.items() if k}
            if clean.get(key):
                rows[clean[key]] = clean
    return DbTable(name=name, key=key, rows=rows, system_columns=system_columns)


def load_csv_table(path: Path | str) -> DbLib:
    """A single CSV as a one-table library (no .DbLib)."""
    path = Path(path)
    table = _read_csv_table(path, path.name, set())
    return DbLib(path=None, name="", tables={table.name: table})


def load_dblib(path: Path | str) -> DbLib:
    """Parse a CSV-backed .DbLib and load its enabled tables."""
    path = Path(path)
    ini = configparser.ConfigParser(interpolation=None, strict=False)
    ini.optionxform = str
    ini.read(path, encoding="utf-8-sig")

    conn = ini.get("DatabaseLinks", "ConnectionString", fallback="")
    if "Text Driver" not in conn and "Extensions=csv" not in conn:
        raise ValueError(
            f"{path.name}: only CSV-backed database libraries are supported"
        )
    match = re.search(r"Dbq=([^;\"]+)", conn)
    csv_dir = Path(match.group(1)) if match else path.parent
    # The stored folder is absolute and often points at another checkout
    # (template files, other machines); the tables next to the DbLib win then
    if not csv_dir.is_dir():
        csv_dir = path.parent

    system_columns: dict[str, set[str]] = {}
    for section in ini.sections():
        if not section.startswith("FieldMap"):
            continue
        opts = dict(
            part.split("=", 1)
            for part in ini.get(section, "Options", fallback="").split("|")
            if "=" in part
        )
        param = opts.get("ParameterName", "")
        if param.startswith("[") and param != "[Description]":
            system_columns.setdefault(opts.get("TableNameOnly", ""), set()).add(
                opts.get("FieldNameOnly", "")
            )

    tables: dict[str, DbTable] = {}
    for section in ini.sections():
        if not section.startswith("Table"):
            continue
        name = ini.get(section, "TableName", fallback="")
        if not name or ini.get(section, "Enabled", fallback="True") != "True":
            continue
        table_path = csv_dir / name
        if table_path.exists():
            tables[name] = _read_csv_table(
                table_path, name, system_columns.get(name, set())
            )
    return DbLib(path=path, name=path.name, tables=tables)


def find_dblib(start: Path | str) -> Path | None:
    """The database library for a document: $ALTIUM_DBLIB_PATH, or the first
    .DbLib found walking up from start (in each folder, then one and two
    levels below it) whose tables can be loaded."""
    env = os.environ.get("ALTIUM_DBLIB_PATH")
    if env and Path(env).exists():
        return Path(env)
    start = Path(start)
    for folder in [start, *start.parents] if start.is_dir() else start.parents:
        for pattern in ("*.DbLib", "*/*.DbLib", "*/*/*.DbLib"):
            for candidate in sorted(folder.glob(pattern)):
                try:
                    if load_dblib(candidate).tables:
                        return candidate
                except (OSError, ValueError, configparser.Error):
                    continue
    return None


# Default wire drawing colours. Keys are lower-case colour names as used in a
# wire database's Color column; a striped name ("White/Red") uses its base.
DEFAULT_COLORS = {
    "black": "#000000",
    "brown": "#A52A2A",
    "red": "#FF0000",
    "orange": "#FFA500",
    "yellow": "#FFFF00",
    "green": "#008000",
    "blue": "#0000FF",
    "violet": "#EE82EE",
    "purple": "#800080",
    "gray": "#808080",
    "grey": "#808080",
    "white": "#FFFFFF",
    "pink": "#FFC0CB",
    "tan": "#D2B48C",
    "light blue": "#ADD8E6",
    "light green": "#90EE90",
}


def color_to_tcolor(name: str, color_map: dict[str, str | int] | None = None) -> int | None:
    """Altium TColor (0x00BBGGRR) for a colour name, or None if unknown.

    color_map entries ("#RRGGBB" or a TColor int) extend or override the
    defaults; names are matched case-insensitively.
    """
    colors: dict[str, str | int] = dict(DEFAULT_COLORS)
    colors.update({k.lower(): v for k, v in (color_map or {}).items()})
    key = (name or "").strip().lower()
    value = colors.get(key)
    if value is None and "/" in key:
        value = colors.get(key.split("/", 1)[0].strip())
    if value is None:
        return None
    if isinstance(value, int):
        return value
    rgb = value.lstrip("#")
    r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
    return r | (g << 8) | (b << 16)
