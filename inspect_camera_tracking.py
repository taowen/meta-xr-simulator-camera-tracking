"""Inspect a local Meta XR Simulator v207.1 webcam tracking server.

Use CPython 3.12. The script reads code objects and disassembles them without
importing or executing Meta's bundled modules. Only inspect trusted binaries.
"""

import argparse
import dis
import hashlib
import marshal
from pathlib import Path
import struct
import sys
import types
from urllib.request import urlopen
import zipfile
import zlib


COOKIE = struct.Struct("!8sIIII64s")
ENTRY = struct.Struct("!IIIIBc")
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
DEFAULT_INSTALL = Path(r"C:\Program Files\MetaXRSimulator\v207.1")
MODEL_NAMES = ("hand_landmarker", "face_landmarker")
MODEL_ROOT = "https://storage.googleapis.com/mediapipe-models"
EVIDENCE = {
    "mediapipe_detector": ("MediaPipeDetector.start", "MediaPipeDetector._on_result"),
    "head_pose_detector": ("HeadPoseDetector.start", "HeadPoseDetector._on_result"),
    "positioning": (
        "WorkspaceConfig", "_palm_width", "_estimate_depth_offset", "_solve_translation",
        "_rotation_onto_ray", "solve_camera_pose", "estimate_hand_placement",
    ),
    "retargeting": ("_map_positions", "_child_joint_frame", "_compute_orientations", "retarget"),
    "hand_identity_tracker": ("HandIdentityTracker._cost", "HandIdentityTracker._best_assignment"),
    "one_euro_filter": ("HandJointPositionFilter._filter_with_root",),
    "pinch": ("_strength", "pinch_strengths"),
    "hand_tracking_service": (
        "_apply_head_pose", "_hand_state_from_detection",
        "TrackingFrameProcessor._update_hand", "CameraTrackingServicer.StreamFrames",
    ),
}


def find_pyz(executable):
    """Read the embedded PyInstaller archive and return its PYZ payload."""
    cookie_offset = executable.rfind(COOKIE_MAGIC)
    if cookie_offset < 0:
        raise ValueError("PyInstaller archive cookie not found")
    _, total_size, toc_offset, toc_length, python_version, _ = COOKIE.unpack_from(
        executable, cookie_offset
    )
    if python_version != 312:
        raise ValueError(f"expected bundled Python 3.12, found archive tag {python_version}")
    archive_start = cookie_offset + COOKIE.size - total_size
    cursor = archive_start + toc_offset
    toc_end = cursor + toc_length
    if not 0 <= archive_start <= cursor <= toc_end <= cookie_offset:
        raise ValueError("invalid PyInstaller table of contents")
    while cursor < toc_end:
        entry_size, offset, size, _, compressed, _ = ENTRY.unpack_from(executable, cursor)
        if entry_size < ENTRY.size or cursor + entry_size > toc_end:
            raise ValueError("invalid PyInstaller entry size")
        name = executable[cursor + ENTRY.size:cursor + entry_size].split(b"\0", 1)[0]
        if name == b"PYZ-00.pyz":
            start = archive_start + offset
            if not archive_start <= start <= start + size <= archive_start + toc_offset:
                raise ValueError("PYZ payload is outside the archive data")
            data = executable[start:start + size]
            return zlib.decompress(data) if compressed else data
        cursor += entry_size
    raise ValueError("PYZ-00.pyz not found")


def read_code_objects(pyz, modules):
    if pyz[:4] != b"PYZ\0":
        raise ValueError("invalid PYZ header")
    toc_offset, = struct.unpack_from("!I", pyz, 8)
    if not 12 <= toc_offset < len(pyz):
        raise ValueError("invalid PYZ table of contents")
    toc = dict(marshal.loads(pyz[toc_offset:]))
    for module in modules:
        _, offset, length = toc["server." + module]
        if not 12 <= offset <= offset + length <= toc_offset:
            raise ValueError(f"invalid module bounds: {module}")
        yield module, marshal.loads(zlib.decompress(pyz[offset:offset + length]))


def walk_code(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from walk_code(value)


def inspect_models(directory, verify_public):
    for name in MODEL_NAMES:
        model = directory / f"{name}.task"
        digest = hashlib.sha256(model.read_bytes()).hexdigest()
        with zipfile.ZipFile(model) as archive:
            members = ", ".join(archive.namelist())
        print(f"{model.name}: {digest}")
        print(f"  members: {members}")
        if verify_public:
            url = f"{MODEL_ROOT}/{name}/{name}/float16/1/{name}.task"
            with urlopen(url, timeout=30) as response:
                public_digest = hashlib.file_digest(response, "sha256").hexdigest()
            if digest != public_digest:
                raise ValueError(f"{model.name} differs from Google's float16/1 file")
            print("  SHA-256 matches Google's public float16/1 file")


def inspect_functions(pyz, targets):
    for module, root in read_code_objects(pyz, EVIDENCE):
        functions = {code.co_qualname: code for code in walk_code(root)}
        for name in EVIDENCE[module]:
            label = f"{module}:{name}"
            if targets and label not in targets:
                continue
            code = functions[name]
            print(f"\n{label}  [{code.co_filename}:{code.co_firstlineno}]")
            if targets:
                dis.dis(code, depth=0)
            else:
                print("  names:", ", ".join(code.co_names))
                constants = [value for value in code.co_consts if type(value) in (int, float)]
                if constants:
                    print("  numeric constants:", constants)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("install_dir", type=Path, nargs="?", default=DEFAULT_INSTALL)
    parser.add_argument("--verify-public-models", action="store_true")
    parser.add_argument(
        "--disassemble", action="append", default=[],
        choices=[f"{module}:{name}" for module, names in EVIDENCE.items() for name in names],
        help="Show instructions for a selected function. Can be repeated.",
    )
    args = parser.parse_args()
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        parser.error("run with CPython 3.12 to read this sample's bytecode")
    server = args.install_dir / "webcam_tracking_server"
    try:
        executable = server / "webcam_tracking_server.exe"
        data = executable.read_bytes()
        print(f"{executable.name} SHA-256: {hashlib.sha256(data).hexdigest()}")
        inspect_models(server / "_internal", args.verify_public_models)
        inspect_functions(find_pyz(data), set(args.disassemble))
    except (OSError, ValueError, KeyError, EOFError, struct.error, zlib.error, zipfile.BadZipFile) as error:
        parser.exit(1, f"Inspection failed: {error}\n")


if __name__ == "__main__":
    main()
