"""
Name:        compare_order.py
Description: finds where a traced build of the decompiled 053248 program first runs other code than the reference
Version:     0.1.0
Created:     07-11-2026
Modified:    07-11-2026
Author:      Sebastian Toyos

The reference writes every instruction a few frames run, in order, with its registers (kref --order FIRST LAST
FILE); a build of the C that tools/decompile.py --trace writes reports every function and block it runs
(SIMPSONS_TRACE=FILE SIMPSONS_TRACE_FRAMES=FIRST-LAST, runtime/src/game_program.c). The reference's places are kept
where the traced C has a game_trace or game_trace_block, the two are walked together, and the first place they part
is shown with what came before it on both sides and the reference's registers. A block the structurer copied into
another branch has no trace call of its own there: up to two reference places in a row are let go by when the next
one matches.

Usage: python compare_order.py TRACED_C_DIR REFERENCE_ORDER DECOMPILED_TRACE [CONTEXT]
"""
import re
import sys
from pathlib import Path

SKIP = 2        # reference places in a row the C may have copied without their trace call


def traced_places(folder):
    """The places the traced C reports"""
    places = set()
    for path in Path(folder).rglob("*.c"):
        for match in re.finditer(r"game_trace(?:_block)?\(0x([0-9A-F]+)u\)", path.read_text(encoding="utf-8")):
            places.add(int(match.group(1), 16))
    return places


def reference_order(path, places):
    """(frame, place, line) for each reference instruction at a traced place"""
    found = []
    for line in open(path, encoding="ascii"):
        parts = line.split()
        place = int(parts[1], 16)
        if place in places:
            found.append((int(parts[0]), place, line.rstrip()))
    return found


def decompiled_order(path):
    """(frame, place, kind) for each function (F) and block (B) the C ran; a function's entry and its first block,
    one place"""
    found = []
    for line in open(path, encoding="ascii"):
        frame, kind, place = line.split()
        place = int(place, 16)
        if found and found[-1][1] == place and found[-1][2] == "F" and kind == "B":
            continue
        found.append((int(frame), place, kind))
    return found


def main(arguments):
    if len(arguments) < 3:
        raise SystemExit(__doc__.split("Usage: ", 1)[1].strip())
    context = int(arguments[3]) if len(arguments) > 3 else 25
    places = traced_places(arguments[0])
    reference = reference_order(arguments[1], places)
    decompiled = decompiled_order(arguments[2])
    first = max(reference[0][0], decompiled[0][0])
    reference = [entry for entry in reference if entry[0] >= first]
    decompiled = [entry for entry in decompiled if entry[0] >= first]
    print(f"{len(places)} traced places; reference {len(reference)} places, decompiled {len(decompiled)}, "
          f"from frame {first}")
    i = j = skipped = 0
    while i < len(reference) and j < len(decompiled):
        r, d = reference[i], decompiled[j]
        if (r[0], r[1]) == (d[0], d[1]):
            i += 1
            j += 1
            continue
        ahead = next((k for k in range(1, SKIP + 1) if i + k < len(reference)
                      and (reference[i + k][0], reference[i + k][1]) == (d[0], d[1])), None)
        if ahead is not None:
            skipped += ahead
            i += ahead
            continue
        print(f"first difference: reference {i} (frame {r[0]}, {r[1]:06X}), decompiled {j} (frame {d[0]}, {d[2]} "
              f"{d[1]:06X}); {skipped} reference places let go by before it")
        for back in range(max(0, i - context), min(len(reference), i + 4)):
            print(f"  reference {back:7d}  {reference[back][2]}")
        for back in range(max(0, j - context), min(len(decompiled), j + 4)):
            frame, place, kind = decompiled[back]
            print(f"  decompiled {back:6d}  {frame} {kind} {place:06X}")
        return 1
    print(f"no difference over {min(len(reference), len(decompiled))} places ({skipped} reference places let go by)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
