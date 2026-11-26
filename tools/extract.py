"""Unpack the Python side of ``webcam_tracking_server.exe``.

The sidecar is a PyInstaller one-file bundle: the bootloader appends a CArchive
that holds ``PYZ-00.pyz`` (a zlib-compressed, marshal-serialised module table).
Everything we are interested in -- ``server/*.py`` -- lives in that PYZ.

Usage
-----
    python tools/extract.py --install "C:\\Program Files\\MetaXRSimulator\\v207.1"
    python tools/extract.py --exe path\\to\\webcam_tracking_server.exe

The unpacking itself is delegated to pyinstxtractor, downloaded on demand and
pinned by commit so results stay comparable between runs.  Run everything with
CPython 3.12: the payload *is* 3.12 bytecode and older/newer ``dis`` versions
will render different opcodes for the same file.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request

PINNED_COMMIT = "815d31cf26bc71e62f851b2e549452e7b7c9dd98"
EXTRACTOR_URL = (
    "https://raw.githubusercontent.com/extremecoders-re/pyinstxtractor/"
    f"{PINNED_COMMIT}/pyinstxtractor.py"
)

SIDECAR = os.path.join("webcam_tracking_server", "webcam_tracking_server.exe")


def locate_exe(args):
    if args.exe:
        return os.path.abspath(args.exe)
    if args.install:
        candidate = os.path.join(args.install, SIDECAR)
        if os.path.isfile(candidate):
            return candidate
        raise SystemExit(f"no sidecar at {candidate}")
    raise SystemExit("pass --exe or --install")


def fetch_extractor(cache_dir):
    path = os.path.join(cache_dir, "pyinstxtractor.py")
    if not os.path.isfile(path):
        os.makedirs(cache_dir, exist_ok=True)
        print(f"downloading {EXTRACTOR_URL}")
        with urllib.request.urlopen(EXTRACTOR_URL) as response, open(path, "wb") as fh:
            shutil.copyfileobj(response, fh)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", help="MetaXRSimulator version directory")
    parser.add_argument("--exe", help="explicit path to webcam_tracking_server.exe")
    parser.add_argument("--out", default="build", help="output directory (default: build)")
    parser.add_argument("--extractor", help="use a local pyinstxtractor.py")
    args = parser.parse_args()

    if sys.version_info[:2] != (3, 12):
        print(f"warning: running under {sys.version.split()[0]}, payload is CPython 3.12")

    exe = locate_exe(args)
    out = os.path.abspath(args.out)
    work = os.path.join(out, "work")
    os.makedirs(work, exist_ok=True)

    copy = os.path.join(work, os.path.basename(exe))
    if not os.path.isfile(copy) or os.path.getsize(copy) != os.path.getsize(exe):
        shutil.copy2(exe, copy)

    extractor = args.extractor or fetch_extractor(os.path.join(out, "_tools"))
    subprocess.run([sys.executable, extractor, copy], cwd=work, check=True)

    unpacked = copy + "_extracted"
    modules = os.path.join(out, "extracted")
    if os.path.isdir(modules):
        shutil.rmtree(modules)
    shutil.move(unpacked, modules)
    print(f"\nmodules: {modules}")
    print(f"server:  {os.path.join(modules, 'PYZ-00.pyz_extracted', 'server')}")


if __name__ == "__main__":
    main()
