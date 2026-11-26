# src/ — the decompiled `server` package

These files are machine-decompiled Python, not the original source. They exist so
that the claims in [../article/bytecode-notes.md](../article/bytecode-notes.md) can be
read as code instead of as instruction listings, and so that the reconstruction can be
reproduced and argued with.

```
webcam_tracking_server.exe  (PyInstaller, CPython 3.12)
  -> PYZ-00.pyz/server/*.pyc        tools/extract.py
  -> repaired .pyc                  tools/decompile.py  (step 1)
  -> Decompyle++ output             tools/decompile.py  (step 2)
  -> src/*.py
```

## Two things had to be fixed before any source came out

**1. `IMPORT_NAME` is overwritten with `CACHE`.** In every module, opcode 108
(`IMPORT_NAME`) has been replaced by 0 (`CACHE`); the operand — the index into
`co_names` — is left intact. CPython never executes that byte, because PyInstaller
resolves imports ahead of time, but every bytecode tool stops at the first `import`.
The repair is a byte substitution in place, so offsets, line tables and exception
tables stay valid. `tools/decompile.py` does it before handing the file to the
decompiler; without it, Decompyle++ recovers nothing but the module docstring.

**2. Upstream Decompyle++ does not support 3.12.** Its 3.12 opcode map is complete,
but the AST layer still assumes 3.11 semantics. [pycdc-3.12.patch](pycdc-3.12.patch)
carries the fixes, against `zrax/pycdc` @ `b428976`:

| fix | why it is needed |
| --- | --- |
| `MAKE_FUNCTION` operand is a flag word, not a count | 3.6+ pushes defaults/kwdefaults/annotations/closure on the stack; without this every signature with a default came out as `def f(self=None, a=('x','int',...))` |
| 3.12 packs a one-argument call into the `self` slot | `@decorator` compiles to `CALL 0` with the decorated object in the self slot; this is what recovers `@property`, `@dataclass` |
| the `PUSH_NULL` before `LOAD_BUILD_CLASS` belongs to that call | otherwise a decorated class leaves a stray `None` and prints as `(class)()` |
| relative jump targets are relative to the *next* instruction | pycdc added `pos` but not the two bytes of the instruction itself, so every block boundary was two bytes early |
| `RETURN_VALUE` must not swallow the next instruction | that hack exists to eat the `POP_BLOCK` of older Pythons; 3.12 has none, so it ate the first statement after the branch (`y = None`) |
| `POP_JUMP_IF_NONE` / `POP_JUMP_IF_NOT_NONE` | unmapped in the AST layer, so `if x is None:` was dropped |
| `SWAP` is a real swap, not an unpacking primitive | 3.12 uses it to keep the return value above the exception state |
| `STORE_DEREF` can be the `for` target | loop variables that are cells printed as `for None in ...` |
| subscript tuples print without parens | `image[(:, 0)]` vs `image[:, 0]` |

## Coverage

| module | lines | parses | gaps |
| --- | ---: | :---: | ---: |
| `__init__` | 1 | yes | 0 |
| `positioning` | 324 | yes | 0 |
| `retargeting` | 338 | yes | 1 |
| `pinch` | 136 | yes | 1 |
| `pipeline_telemetry` | 243 | yes | 1 |
| `model_download` | 93 | no | 4 |
| `webcam_capture` | 193 | no | 1 |
| `one_euro_filter` | 237 | no | 2 |
| `hand_identity_tracker` | 265 | no | 4 |
| `synthetic_source` | 245 | no | 6 |
| `head_pose_detector` | 318 | no | 7 |
| `mediapipe_detector` | 243 | no | 7 |
| `hand_tracking_service` | 552 | no | 7 |
| `selftest` | 293 | no | 7 |
| `session_recorder` | 429 | no | 23 |

Line counts exclude the 11-line provenance header. "gaps" counts the markers Decompyle++ leaves where it gave up
(`# WARNING: Decompyle incomplete`, `<NODE:n>`). Each file states its own status in
its header, and `python tools/decompile.py ...` reprints this table.

The "parses" column is `compile()`, not `ast.parse()`: three modules fail for no
other reason than the decompiler pushing `from __future__ import annotations` down
the file, which the parser accepts and the compiler rejects.

## What is faithful, and what is not

Recovered faithfully: control flow that survives compilation (`if`/`else`, `for`,
`while`, `try`/`except`), call sites with keyword arguments, attribute and subscript
expressions, literal constants, the module/function/class nesting, docstrings, and all
names — locals, globals and attributes come straight out of `co_varnames` /
`co_names`, so `_solve_translation`, `_MIN_CAMERA_DEPTH_M` and friends are the real
identifiers.

Not recovered, in rough order of how much damage it does:

1. **Inlined comprehensions (PEP 709).** 3.12 compiles list/set/dict comprehensions
   inside the enclosing function instead of into a `<listcomp>` code object. pycdc
   looks for that code object to recognise a comprehension, so the loop body is
   printed as statements followed by a stray `continue` (see `pinch.pinch_strengths`).
   This is the largest gap: 49 `LOAD_FAST_AND_CLEAR` sites across the package.
2. **Generator expressions** come out as `(lambda .0: pass)(...)`.
3. **`with` statements.** `BEFORE_WITH` (3.11+) is a no-op in pycdc, so
   `with open(...) as f:` prints as `with None:` and the body loses its subject
   (`model_download`).
4. **`try` directly under an early `return`.** The `except` can land one level too
   deep (`session_recorder`, `hand_tracking_service`).
5. **Conditions are the least reliable part.** `not` fused into a conditional jump is
   occasionally dropped, and chained comparisons are flattened: `positioning._solve_translation`
   prints `if np.all(np.isfinite(depth)) or ...` where the module does
   `if not np.all(...) or ...`. Read every `if` against
   [../evidence/disassembly/](../evidence/disassembly/) before trusting it.

All of these are decompiler limitations, not properties of the binary: the bytecode
for each construct is present and decodes cleanly with `dis`.

## Reproducing

```sh
python tools/extract.py --install "C:\Program Files\MetaXRSimulator\v207.1" --out build

git clone https://github.com/zrax/pycdc
cd pycdc && git checkout b428976 && git apply /path/to/tools/pycdc-3.12.patch
cmake -B build && cmake --build build --config Release

python tools/decompile.py --pycdc pycdc/build/pycdc \
    --modules build/extracted/PYZ-00.pyz_extracted --out src
```

The patch touches only `ASTree.cpp` and `FastStack.h` (+172/-13). It is a
work-in-progress against a decompiler that is not ours; the 3.12 gaps belong upstream.

## Licence

These files are derived from Meta's `webcam_tracking_server.exe` and remain Meta's
code. They are included for commentary and verification only — see
[../NOTICE.md](../NOTICE.md), which also says how to ask for removal.
