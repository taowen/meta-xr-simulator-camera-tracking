"""Regenerate the files under evidence/ from a local extraction.

Everything in ``evidence/`` is produced by this script: bytecode listings for
the functions named in ``claims/claims.json``, the module inventory of the PYZ
archive, and the string constants of the two modules that describe the sidecar's
own CLI.  Nothing is hand-edited, so the snippets in the article can be traced
straight back to the binary.
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
EVIDENCE = os.path.join(ROOT, "evidence")


def module_path(modules_dir, module):
    """Resolve a module to its .pyc, falling back to the CArchive entry point."""
    in_pyz = os.path.join(modules_dir, module.replace("/", os.sep) + "c")
    if os.path.isfile(in_pyz):
        return in_pyz
    # main.py is the CArchive entry point, not a PYZ member.
    return os.path.join(os.path.dirname(modules_dir), module.replace("/", os.sep) + "c")


def dump_function(modules_dir, module, function, out_path):
    code = pycutils.load_code(module_path(modules_dir, module))
    target = pycutils.find(code, function)
    first, last = pycutils.source_span(target)
    lines: list[str] = [
        f"# {module}:{first}-{last}  {function}",
        f"# extraction: Meta XR Simulator v207.1 sidecar, CPython 3.12",
        "",
    ]
    for line, ops in sorted(pycutils.instruction_lines(target).items()):
        lines.append(f"{line:5d}  {'; '.join(ops)}")
    lines.append("")
    lines.append("=" * 78)
    lines.append(f"verbatim dis listing: {module}:{first}-{last} {function}")
    lines.append("=" * 78)
    lines.append(pycutils.text(target))
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def dump_inventory(modules_dir, out_dir):
    rows = []
    for base, dirs, files in os.walk(modules_dir):
        dirs.sort()
        for name in sorted(files):
            if not name.endswith(".pyc"):
                continue
            rel = os.path.relpath(os.path.join(base, name), modules_dir)
            rel = rel[:-4].replace(os.sep, ".")
            rows.append(rel)
    with open(os.path.join(out_dir, "pyz-modules.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(rows) + "\n")
    return len(rows)


def dump_strings(modules_dir, module, out_path):
    code = pycutils.load_code(module_path(modules_dir, module))
    seen = []
    for name, sub in pycutils.walk(code):
        for const in sub.co_consts:
            if isinstance(const, str) and len(const) >= 6 and const not in seen:
                seen.append(const)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# string constants of {module} (unique, >= 6 chars)\n\n")
        for value in seen:
            fh.write(json.dumps(value, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", default=os.path.join(ROOT, "build", "extracted", "PYZ-00.pyz_extracted"))
    parser.add_argument("--claims", default=os.path.join(ROOT, "claims", "claims.json"))
    parser.add_argument("--install", help="MetaXRSimulator version dir, for the model hashes")
    args = parser.parse_args()

    os.makedirs(os.path.join(EVIDENCE, "disassembly"), exist_ok=True)
    os.makedirs(os.path.join(EVIDENCE, "inventory"), exist_ok=True)
    os.makedirs(os.path.join(EVIDENCE, "strings"), exist_ok=True)

    with open(args.claims, encoding="utf-8") as fh:
        spec = json.load(fh)

    done = set()
    for claim in spec["claims"]:
        key = (claim["file"], claim.get("function", "<module>"))
        if key in done:
            continue
        done.add(key)
        stem = claim["file"].split("/")[-1].replace(".py", "")
        function = claim.get("function", "<module>").replace(".", "_").strip("<>")
        out = os.path.join(EVIDENCE, "disassembly", f"{stem}-{function}.txt")
        dump_function(args.modules, claim["file"], claim.get("function", "<module>"), out)
        print(f"wrote {os.path.relpath(out, ROOT)}")

    count = dump_inventory(args.modules, os.path.join(EVIDENCE, "inventory"))
    print(f"wrote evidence/inventory/pyz-modules.txt ({count} modules)")

    for module in ("main.py", "server/model_download.py"):
        stem = module.split("/")[-1].replace(".py", "")
        dump_strings(args.modules, module, os.path.join(EVIDENCE, "strings", f"{stem}.txt"))
        print(f"wrote evidence/strings/{stem}.txt")

    if args.install:
        internal = os.path.join(args.install, "webcam_tracking_server", "_internal")
        rows = ["# .task files shipped with the sidecar, compared with Google's published models", ""]
        for name, entry in spec["models"].items():
            path = os.path.join(internal, name)
            digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
            verdict = "identical" if digest == entry["sha256"] else "DIFFERS"
            rows.append(f"{digest}  {name}  {verdict}")
            rows.append(f"    bytes   : {os.path.getsize(path)} (expected {entry['bytes']})")
            rows.append(f"    upstream: {entry['source']}")
        with open(os.path.join(EVIDENCE, "models.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(rows) + "\n")
        print("wrote evidence/models.txt")


if __name__ == "__main__":
    main()
