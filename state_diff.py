"""
Name:        state_diff.py
Description: the board's state of two runs of the game on processors of either byte order (the headless game's
             --dump-frame dumps), compared as the values each one holds: where the C on a big-endian processor
             has a value another than on the PC
Version:     0.2.0
Created:     06-11-2026
Modified:    06-11-2026
Author:      Sebastian Toyos

Usage: python tools/state_diff.py LITTLE_DIR BIG_DIR   (the dumps of the same frame: the PC's, a big-endian one's;
       STATE_DIFF_SHOWN=N lists N of each memory's, 40 when it is not set)

The work RAM and the RAM the sound program shares keep each value in their processor's order at the width the
C declares it (runtime/include/s32/machine.h). A place symbols.txt names (a variable, a field of the game's
state's structures, an element of an array of them; a family's field where every family that names the offset
gives it one width) is compared as a value of its width: one that holds another value on the big-endian
processor is a place the C writes or reads at another width than its own (through a view of the memory, or a
field of another family), which the PC does not show. The bytes nothing names are compared by 32-bit words,
agreeing when their bytes on the big-endian processor are the PC's as they are, with either halfword turned
round, or turned round whole. Each one that holds another value is listed by the place it is in (the
structure and its field: tools/core/width_census.py); one whose bytes are the PC's as they are says so (bytes
moved there a byte or a halfword at a time, which another layout than the place's names: the backup's course
records in a race record the link sends). The video RAM, the sprite RAM, the palettes and the mixers are
16-bit words in each processor's order, compared as words.
"""
import os
import struct
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import disassemble  # noqa: E402
import game_profile  # noqa: E402
import width_census  # noqa: E402
from decompiler import memory as state_memory  # noqa: E402

PLAIN = (("work_ram.bin", 0x200000, "work RAM"), ("shared_ram.bin", 0x700000, None))
WORDS = (("video_ram.bin", 0x300000), ("sprite_ram.bin", 0x400000), ("palette.bin", None), ("mixer.bin", None))
SHOWN = int(os.environ.get("STATE_DIFF_SHOWN", "40"))     # how many of each memory's are listed


def declared_places(names):
    """{address: (bytes, name)} of the values symbols.txt declares in the work RAM and the shared RAM, the
    places two declarations overlap left out (an overlay, a field families give other widths)"""
    by_struct = defaultdict(list)
    for (owner, offset), (name, kind) in names.fields.items():
        count = names.field_arrays.get((owner, offset)) or 1
        by_struct[owner].append((offset, int(kind[1:]) // 8, count, name))
    # (a family's field where no other family's, nor a field of every object, has bytes of it at another
    # place or width: the dump does not say which family an object is)
    spans = defaultdict(lambda: defaultdict(set))
    named = defaultdict(lambda: defaultdict(set))
    for owner, fields in by_struct.items():
        for offset, width, count, _ in fields:
            for byte in range(offset, offset + width * count):
                spans[owner][byte].add((offset, width * count))
    for (owner, _), fields in names.scoped.items():
        for offset, (name, kind) in fields.items():
            width = int(kind[1:]) // 8 * (names.scoped_arrays.get((owner, _, offset)) or 1)
            named[owner][(offset, width)].add(name)
            for byte in range(offset, offset + width):
                spans[owner][byte].add((offset, width))
    for owner, found in named.items():
        generic = {(offset, width * count) for offset, width, count, _ in by_struct[owner]}
        # (and a field of every object only where no family's has bytes of it at another place or width)
        by_struct[owner] = [(offset, width, count, name) for offset, width, count, name in by_struct[owner]
                            if all(spans[owner][byte] == {(offset, width * count)}
                                   for byte in range(offset, offset + width * count))]
        for (offset, width), field_names in found.items():
            if (offset, width) not in generic and all(spans[owner][byte] == {(offset, width)}
                                                      for byte in range(offset, offset + width)):
                by_struct[owner].append((offset, width, 1, "/".join(sorted(field_names))))
    members = defaultdict(list)
    for (owner, offset), (name, inner, count) in names.members.items():
        members[owner].append((offset, name, inner, count))
    places, starts = {}, defaultdict(set)

    def put(address, width, name):
        for byte in range(address, address + width):
            starts[byte].add(address)
        places[address] = (width, name)

    def lay(structure, base, prefix, depth=0):
        if depth > 6:
            return
        for offset, width, count, name in by_struct.get(structure, ()):
            for index in range(count):
                put(base + offset + index * width, width, f"{prefix}{name}" + (f"[{index}]" if count > 1 else ""))
        for offset, name, inner, count in members.get(structure, ()):
            size = names.structs.get(inner)
            for index in range(count if size else 0):
                lay(inner, base + offset + index * size, f"{prefix}{name}" + (f"[{index}]" if count > 1 else "") + ".",
                    depth + 1)

    for base, block, structure, count in state_memory.STATE_BLOCKS:
        size = names.structs.get(structure) if structure else None
        for index in range(count if size else 0):
            lay(structure, base + index * size, f"{block}" + (f"[{index}]" if count > 1 else "") + ".")
    for address, (name, kind) in names.variables.items():
        width = int(kind[1:]) // 8
        count = names.counts.get(address, 1)
        for index in range(count):
            put(address + index * width, width, name + (f"[{index}]" if count > 1 else ""))
    overlapping = {address for byte, found in starts.items() if len(found) > 1 for address in found}
    return {address: place for address, place in places.items() if address not in overlapping}


def agrees(little, big):
    """True when the big-endian processor's bytes of a word nothing names hold the PC's values in some widths"""
    if big == little or big == little[::-1]:
        return True
    if len(little) != 4:
        return False
    halves = [little[0:2], little[2:4]]
    for first in (halves[0], halves[0][::-1]):
        for second in (halves[1], halves[1][::-1]):
            if big == first + second:
                return True
    return False


def plain_differences(little, big, base, places):
    """[(address, the PC's bytes, the big-endian processor's, the name or None)] of the values that differ"""
    found = []
    length = min(len(little), len(big))
    named = set()
    for address, (width, name) in sorted(places.items()):
        at = address - base
        if not 0 <= at <= length - width:
            continue
        named.update(range(at, at + width))
        if int.from_bytes(little[at:at + width], "little") != int.from_bytes(big[at:at + width], "big"):
            found.append((address, little[at:at + width], big[at:at + width], name))
    for at in range(0, length - 3, 4):
        free = [byte for byte in range(at, at + 4) if byte not in named]
        if not free:
            continue
        first, last = free[0], free[-1] + 1
        if free == list(range(first, last)) and not agrees(little[first:last], big[first:last]):
            found.append((base + first, little[first:last], big[first:last], None))
        elif free != list(range(first, last)) and any(little[byte] != big[byte] for byte in free):
            found.append((base + at, little[at:at + 4], big[at:at + 4], None))
    return sorted(found)


def main():
    if len(sys.argv) != 3:
        print(__doc__.split("\n\n")[1])
        return 1
    little_dir, big_dir = Path(sys.argv[1]), Path(sys.argv[2])
    symbols = width_census.Symbols(game_profile.ROOT / "symbols.txt")
    places = declared_places(disassemble.load_names(game_profile.ROOT / "symbols.txt"))
    total = 0
    for name, base, memory in PLAIN:
        little, big = (little_dir / name).read_bytes(), (big_dir / name).read_bytes()
        found = plain_differences(little, big, base, places)
        total += len(found)
        print(f"{name}: {len(found)} values hold another value")
        for address, left, right, field in found[:SHOWN]:
            where = f"{address:06X}"
            if field is not None:
                where += f" {field}"
            elif memory is not None:
                place = width_census.place_of(symbols, memory, address)
                if place is not None:
                    where += " " + width_census.where_text(symbols, place[0], place[1]) + \
                        width_census.names_at(symbols, place[0], place[1])
            # (the PC's bytes as they are: moved there a byte, or a halfword, at a time from another layout)
            same = " (the same bytes)" if left == right else ""
            print(f"  {where}: {left.hex()} / {right.hex()}{same}")
    for name, base in WORDS:
        little, big = (little_dir / name).read_bytes(), (big_dir / name).read_bytes()
        count = min(len(little), len(big)) // 2
        left, right = struct.unpack(f"<{count}H", little[:count * 2]), struct.unpack(f">{count}H", big[:count * 2])
        found = [index for index in range(count) if left[index] != right[index]]
        total += len(found)
        print(f"{name}: {len(found)} words hold another value")
        for index in found[:20]:
            where = f"{base + index * 2:06X}" if base is not None else f"+{index * 2:X}"
            print(f"  {where}: {left[index]:04X} / {right[index]:04X}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
