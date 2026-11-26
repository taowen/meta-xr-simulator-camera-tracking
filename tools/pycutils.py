"""Shared helpers for reading PyInstaller-extracted ``.pyc`` files.

Works against CPython 3.12 bytecode (the interpreter Meta XR Simulator v207.1
ships inside ``webcam_tracking_server.exe``).  Nothing here executes the
bytecode: files are unmarshalled with :mod:`marshal` and inspected with
:mod:`dis`, so no proprietary code ever runs on your machine.
"""

from __future__ import annotations

import dis
import marshal
import types

# PyInstaller writes a plain marshal stream for PYZ members, while loose .pyc
# files carry a 16 byte header (12 bytes on 3.7+, 8 on 3.3+).  Try the common
# offsets until something unmarshals into a code object.
HEADER_OFFSETS = (16, 12, 8, 0)


def load_code(path):
    """Unmarshal a ``.pyc``/marshal blob into its top-level code object."""
    with open(path, "rb") as fh:
        data = fh.read()
    for offset in HEADER_OFFSETS:
        try:
            code = marshal.loads(data[offset:])
        except Exception:
            continue
        if isinstance(code, types.CodeType):
            return code
    raise ValueError(f"not a marshal code object: {path}")


def walk(code, prefix=""):
    """Yield ``(qualified_name, code)`` for a code object and its nested ones."""
    yield prefix or "<module>", code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            name = f"{prefix}.{const.co_name}" if prefix else const.co_name
            yield from walk(const, name)


def find(code, qualname):
    """Look up a nested code object by dotted name (``Class.method.inner``)."""
    for name, sub in walk(code):
        if name == qualname:
            return sub
    raise KeyError(qualname)


def instruction_lines(code):
    """Map source line number -> list of normalised instruction strings.

    ``dis`` only sets ``starts_line`` on the first instruction of each source
    line, so carry the last seen value forward.
    """
    out: dict[int, list[str]] = {}
    line = code.co_firstlineno
    for ins in dis.get_instructions(code):
        if ins.starts_line is not None:
            line = ins.starts_line
        if ins.opname in ("RESUME", "CACHE"):
            continue
        out.setdefault(line, []).append(fmt(ins))
    return out


def fmt(ins):
    """Normalise one instruction to ``OPNAME(argrepr)`` for stable assertions."""
    return f"{ins.opname}({ins.argrepr})" if ins.argrepr else ins.opname


def text(code):
    """Full ``dis`` listing as text, prefixed with the original file name."""
    return dis.Bytecode(code).dis()


def source_span(code):
    """First and last source line covered by a code object."""
    lines = [line for _, _, line in code.co_lines() if line]
    return (code.co_firstlineno, max(lines) if lines else code.co_firstlineno)
