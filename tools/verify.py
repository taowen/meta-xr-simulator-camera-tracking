"""Verify every bytecode claim in claims/claims.json against the shipped sidecar.

Each claim names a module, a function and the source lines recorded in the code
object's line table, then asserts that specific instructions appear on those
lines.  Run it twice: once against your own extraction, once with ``--models``
pointing at ``_internal`` to check the two ``.task`` files against Google's
published models.

    python tools/verify.py
    python tools/verify.py --models "C:\\Program Files\\MetaXRSimulator\\v207.1\\webcam_tracking_server\\_internal"

Exit status is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pycutils  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = os.path.join(ROOT, "build", "extracted", "PYZ-00.pyz_extracted")


def check_claim(claim, modules_dir):
    path = os.path.join(modules_dir, claim["file"].replace("/", os.sep) + "c")
    if not os.path.isfile(path):
        return [f"missing module {path}"]

    code = pycutils.load_code(path)
    function = claim.get("function", "<module>")
    try:
        target = pycutils.find(code, function)
    except KeyError:
        return [f"{claim['file']}: no code object {function}"]

    lines = pycutils.instruction_lines(target)
    whole = "; ".join("; ".join(ops) for _, ops in sorted(lines.items()))
    failures = []
    for check in claim["checks"]:
        if "lines" in check:
            first, last = check["lines"]
            span = [n for n in range(first, last + 1)]
            empty = [n for n in span if not lines.get(n)]
            if empty:
                failures.append(f"lines {first}-{last}: no instructions on {empty}")
            joined = "; ".join("; ".join(lines.get(n, [])) for n in span)
            where = f"lines {first}-{last}"
        else:
            line = check.get("line")
            joined = whole if line is None else "; ".join(lines.get(line, []))
            where = "whole function" if line is None else f"line {line}"
            if line is not None and not joined:
                failures.append(f"{where}: no instructions recorded")
                continue
        for needle in check.get("contains", []):
            if needle not in joined:
                failures.append(f"{where}: expected {needle!r}")
        for needle in check.get("absent", []):
            if needle in joined:
                failures.append(f"{where}: unexpected {needle!r}")
        if "count" in check:
            pattern = check["count"]["pattern"]
            minimum = check["count"]["min"]
            found = joined.count(pattern)
            if found < minimum:
                failures.append(f"{where}: {pattern} x{found} < {minimum}")
    return failures


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_models(spec, internal_dir):
    failures = []
    for name, entry in spec.items():
        path = os.path.join(internal_dir, name)
        if not os.path.isfile(path):
            failures.append(f"{name}: not found in {internal_dir}")
            continue
        actual = sha256(path)
        verdict = "identical to" if actual == entry["sha256"] else "DIFFERS from"
        print(f"  {name}: {actual} {verdict} {entry['source']}")
        if actual != entry["sha256"]:
            failures.append(f"{name}: sha256 mismatch")
        if os.path.getsize(path) != entry["bytes"]:
            failures.append(f"{name}: size mismatch")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", default=os.path.join(ROOT, "claims", "claims.json"))
    parser.add_argument("--modules", default=MODULES, help="extracted PYZ directory")
    parser.add_argument("--models", help="_internal directory holding the .task files")
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args()

    with open(args.claims, encoding="utf-8") as fh:
        spec = json.load(fh)

    subject = spec["subject"]
    print(f"{subject['product']}  {subject['binary']}")
    print(f"sha256 {subject['binary_sha256']}  ({subject['binary_bytes']} bytes)")
    print()

    total = 0
    failed = 0
    for claim in spec["claims"]:
        total += 1
        problems = check_claim(claim, args.modules)
        status = "PASS" if not problems else "FAIL"
        if problems:
            failed += 1
        if not args.quiet or problems:
            print(f"[{status}] {claim['id']}  ({claim['file']}:{claim['line']} {claim.get('function', '<module>')})")
            if problems:
                for problem in problems:
                    print(f"        {problem}")

    if args.models:
        print("\nmodels:")
        problems = check_models(spec["models"], args.models)
        if problems:
            failed += 1
            for problem in problems:
                print(f"        {problem}")

    print(f"\n{total - failed}/{total} claims verified")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
