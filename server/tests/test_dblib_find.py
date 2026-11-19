"""find_dblib: a '*.template.DbLib' is never taken as the library (synthetic files)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dblib import find_dblib  # noqa: E402

DBLIB = """[DatabaseLinks]
ConnectionString=Provider=MSDASQL.1;Extended Properties="Driver={Microsoft Access Text Driver (*.txt, *.csv)};Dbq=X:\\nowhere\\;Extensions=csv;"
[Table1]
TableName=wires-db.csv
Enabled=True
"""


def test_template_dblib_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("ALTIUM_DBLIB_PATH", raising=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "wires-db.csv").write_text("PartNumber,Color\nW-1,Black\n", encoding="utf-8")
    (lib / "my.template.DbLib").write_text(DBLIB, encoding="utf-8")
    doc = tmp_path / "proj" / "sheet.WirDoc"
    doc.parent.mkdir()
    doc.write_text("", encoding="utf-8")
    found = find_dblib(doc)                             # only the template here: not taken
    assert found is None or tmp_path not in found.parents
    (lib / "my.DbLib").write_text(DBLIB, encoding="utf-8")
    assert find_dblib(doc).name == "my.DbLib"
