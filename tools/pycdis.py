"""Print the bytecode of extracted modules, keeping CPython's original line numbers.

``dis`` numbers the *source* lines recorded in the code object's line table, so
these are the line numbers of Meta's ``server/*.py`` files -- not offsets into
any reconstructed listing.  That is what makes a claim like
"``positioning.py:206`` calls ``np.linalg.lstsq``" checkable.

Usage
-----
    python tools/pycdis.py build/extracted/PYZ-00.pyz_extracted/server/positioning.pyc --list
    python tools/pycdis.py build/extracted/PYZ-00.pyz_extracted/server/positioning.pyc -f _solve_translation
    python tools/pycdis.py build/extracted/PYZ-00.pyz_extracted/server/pinch.pyc -f pinch_strengths --lines
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pycutils  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pyc")
    parser.add_argument("-f", "--function", help="dotted qualname, e.g. Class.method")
    parser.add_argument("--list", action="store_true", help="list every code object")
    parser.add_argument("--lines", action="store_true", help="one line per source line")
    args = parser.parse_args()

    code = pycutils.load_code(args.pyc)
    if args.list or not args.function:
        print(f"# {code.co_filename}  (python {sys.version.split()[0]})")
        for name, sub in pycutils.walk(code):
            first, last = pycutils.source_span(sub)
            print(f"{first:5d}-{last:5d}  {name}")
        return

    target = pycutils.find(code, args.function)
    first, last = pycutils.source_span(target)
    print(f"# {code.co_filename}:{first}-{last}  {args.function}")
    if args.lines:
        for line, ops in sorted(pycutils.instruction_lines(target).items()):
            print(f"{line:5d}  {'; '.join(ops)}")
    else:
        print(pycutils.text(target))


if __name__ == "__main__":
    main()
