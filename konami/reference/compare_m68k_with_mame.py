# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        compare_m68k_with_mame.py
Description: decodes every even offset of the 68000 program with our disassembler and MAME's, and diffs them
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Usage: python tools/core/konami/reference/compare_m68k_with_mame.py [rom folder or .zip]
Needs build/bin/oracle_m68k (built by CMake with the reference tools): MAME's own m68k_disassembler,
built for a 68000 (TYPE_68000).
The 68000 program is decoded as one linear image from address 0, as the 68000 sees it: both sides read
the words that follow each even offset, $FF past the end of the image, and print the offset. A second
image, built here, holds every 16-bit opcode followed by four extension words, once per kind of
extension words (small positive values; values with the sign bits set; brief extension words with a
scale, which a 68000 does not take; full extension words, bit 8 set, which it does not take either), so
that the encodings and addressing modes the program never uses are compared too. Every opcode starts at
a known even offset, and every even offset of the image is compared, the ones inside the extension
words too. A last part of that image gives the 68851 instructions MAME prints for a 68000 (PMOVE,
PLOAD, PTEST, PFLUSH, PVALID: $F000-$F03F) a set of second words that reaches each of their forms.
"""
import subprocess
import sys
import time
from pathlib import Path

CORE = Path(__file__).resolve().parents[2]    # (arcade-decomp, the game's tools/core)
sys.path[:0] = [str(CORE), str(CORE.parent)]
import game_profile  # noqa: E402  (the game's: its root, its ROM set)
from m68000 import disassembler  # noqa: E402

ROOT = game_profile.ROOT
rom = game_profile.rom
ORACLE = ROOT / "build" / "bin" / ("oracle_m68k.exe" if sys.platform == "win32" else "oracle_m68k")

# The four extension words after each opcode, one set per kind. The first word is the extension of the
# first (or only) effective address; the second, of a MOVE's destination after a one-word source. As
# brief extension words (index modes): 0x4834 is D4.l with $34, 0x3012 D3.w with $12; 0x88E8 is A0.l
# with -$18, 0xF880 A7.l with -$80; 0x2A10 has scale 2 and 0x9E80 scale 8; 0x0130, 0x0170, 0x8DE5,
# 0x0300 and 0xF7A2 have bit 8 set (the full format; 0xF7A2 a scale too). As words of their own:
# immediates and absolute addresses, small and with the sign bit set, MOVEM masks, and the second word
# of the 68851 instructions (0x4834 is a PMOVE, 0x88E8 a PTEST).
EXTENSION_WORDS = (
    ("positive", (0x4834, 0x3012, 0x0056, 0x7078)),
    ("negative", (0x88E8, 0xF880, 0xD0F0, 0x8081)),
    ("scaled", (0x2A10, 0x0130, 0x9E80, 0xF7A2)),
    ("full", (0x0170, 0x2A10, 0x8DE5, 0x0300)),
)
RECORD_SIZE = 10        # the opcode and four words: the longest 68000 instruction

# Second words of the 68851 instructions at $F000-$F03F: PFLUSH, PLOAD (both directions), PFLUSHR,
# PVALID, PTEST (each kind of function code, with and without an address register), PMOVE to and from
# the transparent translation registers, the 68851 registers (with and without FD) and MMUSR, and the
# unknown forms
MMU_WORDS = (
    0x2000, 0x2010, 0x2210, 0x2400, 0x2600, 0x3012, 0x2201, 0x2A1F, 0xA000, 0x2800, 0x2C03, 0x8000,
    0x8001, 0x8209, 0x8312, 0x8D17, 0x9F5F, 0x0800, 0x0A00, 0x0C00, 0x0F00, 0x0000, 0x4000, 0x4200,
    0x4500, 0x4B00, 0x5C00, 0x5F00, 0x6000, 0x6300, 0x6E00, 0xE000, 0xC000, 0x1000,
)


def opcode_image():
    """Every opcode with each kind of extension words, then the 68851 second words."""
    records = []
    for _kind, words in EXTENSION_WORDS:
        tail = b"".join(word.to_bytes(2, "big") for word in words)
        records.extend(opcode.to_bytes(2, "big") + tail for opcode in range(0x10000))
    for modes in MMU_WORDS:
        tail = b"".join(word.to_bytes(2, "big") for word in (modes, 0x3012, 0x4834, 0x0056))
        records.extend(opcode.to_bytes(2, "big") + tail for opcode in range(0xF000, 0xF040))
    return b"".join(records)


def our_line(mem, offset):
    instruction = disassembler.decode(mem, offset)
    return f"{offset:06X} {instruction.length} {instruction.text}"


def compare(name, image, work):
    """Decode every even offset of the image on both sides; returns the number of differences."""
    image_path = work / f"m68k_{name}.bin"
    image_path.write_bytes(image)
    mame_path = work / f"mame_m68k_{name}.txt"
    subprocess.run([str(ORACLE), str(image_path), "0", hex(len(image)), str(mame_path)], check=True)
    mem = disassembler.FlatMemory(image)

    offsets = range(0, len(image), 2)
    differences = 0
    compared = 0
    with open(mame_path, encoding="ascii") as mame_file:
        for offset, mame_line in zip(offsets, mame_file):
            mame_line = mame_line.rstrip("\n")
            ours = our_line(mem, offset)
            compared += 1
            if ours != mame_line:
                differences += 1
                if differences <= 20:
                    print(f"MAME: {mame_line}\nOURS: {ours}\n")
    if compared != len(offsets):
        print(f"{name}: MAME printed {compared} lines for {len(offsets)} offsets")
        differences += len(offsets) - compared
    print(f"{name}: {compared} offsets compared, {differences} differences")
    return differences


def main():
    if not ORACLE.exists():
        raise SystemExit(f"{ORACLE} not found")
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "rom"
    program = rom.build_program(rom.load_set(source))

    work = ROOT / "build" / "reference"
    work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    differences = compare("program", program, work)
    differences += compare("opcodes", opcode_image(), work)
    print(f"({time.perf_counter() - started:.1f} s)")
    return 0 if differences == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
