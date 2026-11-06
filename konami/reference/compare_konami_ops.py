# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        compare_konami_ops.py
Description: the translated program's helpers in C (the runtime's konami/ops.h, through konami_ops_check) against
             konami_cpu/interpreter.py's, which the lift's oracle checks against MAME's core
Version:     0.1.0
Created:     06-11-2026
Modified:    06-11-2026
Author:      Sebastian Toyos

Usage: python compare_konami_ops.py [--bin build/bin]
The shifts by a count (LSRDI, RORDI, ASRDI, ASLDI, ROLDI) with every count from 0 to 15, every flag in and many
values, DAA with every A, H and C, and DIV X,B with every B and many X (and B 0).
"""
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent
sys.path.insert(0, str(CORE))

from konami_cpu import interpreter, lift  # noqa: E402


def cases(rng):
    for op, name in enumerate(lift.SHIFTS):
        for value in [0, 1, 0x7FFF, 0x8000, 0xFFFF] + [rng.randrange(0x10000) for _ in range(300)]:
            for count in range(16):
                for flags in range(16):
                    n, z, v, c = flags >> 3 & 1, flags >> 2 & 1, flags >> 1 & 1, flags & 1
                    full = count | rng.randrange(16) << 4      # (the high nibble the CPU ignores)
                    yield (f"shift {op:X} {value:X} {full:X} {n:X} {z:X} {v:X} {c:X}",
                           interpreter.shift_by(name, value, full, n, z, v, c), count)
    for a in range(256):
        for h in (0, 1):
            for c in (0, 1):
                yield f"daa {a:X} {h:X} {c:X}", interpreter.daa(a, h, c), None
    for b in range(256):
        for x in [0, 0xFFFF] + [rng.randrange(0x10000) for _ in range(40)]:
            yield f"divx {x:X} {b:X}", interpreter.divx(x, b), None


def main():
    arguments = sys.argv[1:]
    binaries = Path(arguments[arguments.index("--bin") + 1]) if "--bin" in arguments \
        else CORE.parent.parent / "build" / "bin"
    name = "konami_ops_check"
    checker = binaries / (f"{name}.exe" if (binaries / f"{name}.exe").exists() else name)
    rng = random.Random(1)
    lines, expected = [], []
    for line, result, _ in cases(rng):
        lines.append(line)
        expected.append(" ".join(f"{value:X}" for value in result))
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        (folder / "in.txt").write_text("\n".join(lines) + "\n")
        subprocess.run([str(checker), str(folder / "in.txt"), str(folder / "out.txt")], check=True)
        got = (folder / "out.txt").read_text().splitlines()
    different = [(line, want, have) for line, want, have in zip(lines, expected, got) if want != have]
    for line, want, have in different[:20]:
        print(f"{line}: Python {want}, C {have}")
    print(f"{len(lines)} inputs, {len(different)} different")
    return 1 if different or len(got) != len(lines) else 0


if __name__ == "__main__":
    sys.exit(main())
