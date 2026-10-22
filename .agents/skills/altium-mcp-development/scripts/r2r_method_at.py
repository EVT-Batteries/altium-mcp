# /// script
# requires-python = ">=3.10"
# dependencies = ["pefile", "dnfile"]
# ///
"""Name the .NET method whose native code holds an address from an Altium error dialog.

Altium's .NET assemblies are ReadyToRun images: their methods are precompiled into the DLL,
so a crash dialog's "at <address> ... <dll>, Base Address: <base>" points into the file.
offset = address - base is an RVA; this maps it through the R2R runtime-function table and
the method entry points to Type::Method.

    uv run .agents/skills/altium-mcp-development/scripts/r2r_method_at.py \
        "C:/Program Files/Altium/ADAgile/System/Altium.Sch.DataModel.dll" 28B842

Several offsets may be given. Methods of generic instantiations are not resolved.
"""

import struct
import sys

import dnfile
import pefile

RUNTIME_FUNCTIONS = 102
METHOD_DEF_ENTRY_POINTS = 103


def main(path: str, offsets: list[int]) -> None:
    pe = pefile.PE(path, fast_load=True)
    data = pe.__data__

    def off(rva: int) -> int:
        return pe.get_offset_from_rva(rva)

    cor = off(pe.OPTIONAL_HEADER.DATA_DIRECTORY[14].VirtualAddress)
    header_rva, _ = struct.unpack_from("<II", data, cor + 64)  # ManagedNativeHeader
    if not header_rva:
        sys.exit(f"{path}: not a ReadyToRun image (code is JIT-compiled at run time)")
    hdr = off(header_rva)
    sig, _major, _minor, _flags, count = struct.unpack_from("<IHHII", data, hdr)
    if sig != 0x00525452:  # 'RTR'
        sys.exit(f"{path}: no ReadyToRun header")
    sections = {}
    for i in range(count):
        kind, rva, size = struct.unpack_from("<III", data, hdr + 16 + 12 * i)
        sections[kind] = (rva, size)

    rf_rva, rf_size = sections[RUNTIME_FUNCTIONS]
    functions = [
        struct.unpack_from("<III", data, off(rf_rva) + 12 * i)
        for i in range(rf_size // 12)
    ]

    def unsigned(o: int) -> tuple[int, int]:
        """NativeFormat variable-length unsigned integer at o -> (value, next offset)."""
        v = data[o]
        if v & 1 == 0:
            return v >> 1, o + 1
        if v & 2 == 0:
            return (v >> 2) | (data[o + 1] << 6), o + 2
        if v & 4 == 0:
            return (v >> 3) | (data[o + 1] << 5) | (data[o + 2] << 13), o + 3
        if v & 8 == 0:
            return (
                (v >> 4)
                | (data[o + 1] << 4)
                | (data[o + 2] << 12)
                | (data[o + 3] << 20),
                o + 4,
            )
        return struct.unpack_from("<I", data, o + 1)[0], o + 5

    md_rva, _ = sections[METHOD_DEF_ENTRY_POINTS]
    val, base = unsigned(off(md_rva))  # NativeArray header
    n_elements, index_size = val >> 2, val & 3
    block = 16

    def element(index: int) -> int | None:
        if index >= n_elements:
            return None
        b = index // block
        if index_size == 0:
            o = data[base + b]
        elif index_size == 1:
            o = struct.unpack_from("<H", data, base + 2 * b)[0]
        else:
            o = struct.unpack_from("<I", data, base + 4 * b)[0]
        o += base
        bit = block >> 1
        while bit:
            v, o2 = unsigned(o)
            if index & bit:
                if v & 2:
                    o += v >> 2
                    bit >>= 1
                    continue
            elif v & 1:
                o = o2
                bit >>= 1
                continue
            if v & 3 == 0 and (v >> 2) == (index & (block - 1)):
                return o2
            return None
        return o

    dn = dnfile.dnPE(path)
    methods = dn.net.mdtables.MethodDef.rows
    owner = {}
    for t in dn.net.mdtables.TypeDef.rows:
        for ref in t.MethodList or []:
            owner[ref.row_index] = f"{t.TypeNamespace}.{t.TypeName}"
    method_at = {}  # runtime function index -> method
    for rid in range(1, len(methods) + 1):
        o = element(rid - 1)
        if o is None:
            continue
        v, _ = unsigned(o)
        method_at[v >> 2 if v & 1 else v >> 1] = (
            f"{owner.get(rid, '?')}::{methods[rid - 1].Name}"
        )

    for target in offsets:
        hit = next(
            (i for i, (begin, end, _) in enumerate(functions) if begin <= target < end),
            None,
        )
        if hit is None:
            print(f"{target:#x}: in no runtime function")
            continue
        i = hit
        while i >= 0 and i not in method_at:
            i -= 1  # funclets (catch/finally blocks) follow their method
        name = method_at.get(i, "? (generic instantiation)")
        print(f"{target:#x}: {name} +{target - functions[max(i, 0)][0]:#x}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], [int(a, 16) for a in sys.argv[2:]])
