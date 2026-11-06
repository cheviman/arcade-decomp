# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        compare_konami_with_mame.py
Description: decodes every offset of the Konami CPU's program, bank by bank as the CPU sees it, with our
             disassembler and MAME's, and diffs them
Version:     0.1.0
Created:     05-11-2026
Modified:    05-11-2026
Author:      Sebastian Toyos

Usage: python tools/core/konami/reference/compare_konami_with_mame.py [rom folder or .zip]
Needs build/bin/oracle_konami (built by CMake with the reference tools, K_CPU 053248): MAME's own
konami_disassembler.
The CPU sees 64 KB: each of the program's banks in its window (BANK_WINDOW) with the fixed program above it
(the ROM set's module gives the regions: FIXED_PROGRAM, PROGRAM_BANK_SIZE), so every bank's window is decoded
in a view of its own, where an instruction that runs past the window's end reads the fixed program as the CPU
does; the fixed program once more on its own. Both sides read the bytes that follow each offset and print the
offset. A second image, built here, holds every opcode followed by every postbyte, then bytes of each kind
(small positive values; values with the sign bit set), so that the encodings and addressing modes the program
never uses are compared too; it is decoded in pieces of the CPU's 64 KB, and every offset of each is compared,
the ones inside an instruction's operands too.
"""
import subprocess
import sys
import time
from pathlib import Path

CORE = Path(__file__).resolve().parents[2]    # (arcade-decomp, the game's tools/core)
sys.path[:0] = [str(CORE), str(CORE.parent)]
import game_profile  # noqa: E402  (the game's: its root, its ROM set)
from konami_cpu import disassembler  # noqa: E402

ROOT = game_profile.ROOT
rom = game_profile.rom
ORACLE = ROOT / "build" / "bin" / ("oracle_konami.exe" if sys.platform == "win32" else "oracle_konami")
BANK_WINDOW = (0x6000, 0x8000)
FIXED_AT = 0x8000

# the bytes after each opcode and postbyte, one set per kind: the offsets, absolute and direct page addresses
# the indexed forms read, and the immediates
TAILS = (("positive", bytes((0x34, 0x12, 0x56))), ("negative", bytes((0x88, 0xF8, 0xD0))))
RECORD_SIZE = 5         # the opcode, the postbyte and three bytes: the longest Konami instruction is 4


def our_line(mem, offset):
    instruction = disassembler.decode(mem, offset)
    return f"{offset:04X} {instruction.length} {instruction.text}"


def compare(name, image, first, end, work):
    """Decode every offset from 'first' to 'end' of the CPU view 'image' on both sides; returns the number of
    differences."""
    image_path = work / f"konami_{name}.bin"
    image_path.write_bytes(image)
    mame_path = work / f"mame_konami_{name}.txt"
    subprocess.run([str(ORACLE), str(image_path), hex(first), hex(end), str(mame_path)], check=True)
    mem = disassembler.FlatMemory(image)

    differences = 0
    compared = 0
    with open(mame_path, encoding="ascii") as mame_file:
        for offset, mame_line in zip(range(first, end), mame_file):
            mame_line = mame_line.rstrip("\n")
            ours = our_line(mem, offset)
            compared += 1
            if ours != mame_line:
                differences += 1
                if differences <= 20:
                    print(f"{name}\nMAME: {mame_line}\nOURS: {ours}\n")
    if compared != end - first:
        print(f"{name}: MAME printed {compared} lines for {end - first} offsets")
        differences += end - first - compared
    return compared, differences


def opcode_records():
    """Every opcode with every postbyte, once per kind of the bytes after them"""
    for _kind, tail in TAILS:
        for opcode in range(0x100):
            for postbyte in range(0x100):
                yield bytes((opcode, postbyte)) + tail


def main():
    if not ORACLE.exists():
        raise SystemExit(f"{ORACLE} not found")
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "rom"
    program = rom.build_program(rom.load_set(source))
    fixed = program[rom.FIXED_PROGRAM[0]:rom.FIXED_PROGRAM[1]]

    work = ROOT / "build" / "reference"
    work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    total_compared = total_differences = 0

    # every bank in the window, the fixed program above it
    banks = len(program) // rom.PROGRAM_BANK_SIZE
    for bank in range(banks):
        view = bytearray(0x10000)
        view[BANK_WINDOW[0]:BANK_WINDOW[1]] = program[bank * rom.PROGRAM_BANK_SIZE:(bank + 1) * rom.PROGRAM_BANK_SIZE]
        view[FIXED_AT:] = fixed
        compared, differences = compare(f"bank{bank:02X}", bytes(view), *BANK_WINDOW, work)
        total_compared += compared
        total_differences += differences
    view = bytearray(0x10000)
    view[FIXED_AT:] = fixed
    compared, differences = compare("fixed", bytes(view), FIXED_AT, 0x10000, work)
    total_compared += compared
    total_differences += differences
    print(f"program: {banks} banks and the fixed program, {total_compared} offsets compared, "
          f"{total_differences} differences")

    # the opcodes with every postbyte, in pieces the CPU's 64 KB hold
    records = list(opcode_records())
    per_piece = 0xFFFF // RECORD_SIZE
    image_compared = image_differences = 0
    for piece, start in enumerate(range(0, len(records), per_piece)):
        image = b"".join(records[start:start + per_piece])
        compared, differences = compare(f"opcodes{piece:02d}", image, 0, len(image), work)
        image_compared += compared
        image_differences += differences
    print(f"opcodes: {len(records)} records, {image_compared} offsets compared, {image_differences} differences")
    print(f"({time.perf_counter() - started:.1f} s)")
    return 0 if total_differences + image_differences == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
