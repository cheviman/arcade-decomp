# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        compare_konami_lift_with_mame.py
Description: the Konami CPU lift's oracle: every instruction run on MAME's core (oracle_konami_step) and on the lift's
             IR (konami_cpu/interpreter.py) from the same states, the states they leave and their data accesses
             compared one by one
Version:     0.1.0
Created:     06-11-2026
Modified:    06-11-2026
Author:      Sebastian Toyos

Usage: python compare_konami_lift_with_mame.py [--bin build/bin] [--states N] [--seed S] [--program rom]
  Every opcode with every second byte (the indexed postbyte, an immediate, an offset...), the bytes after them
  random, in images of the CPU's 64 KB (random bytes, the instructions 8 apart), N states each (2 by default): random
  registers and CC, the IRQ and FIRQ lines held after the instruction starts a quarter of the time each, U below 48
  for the block instructions. With --program (the game's ROM folder or .zip, through the game's profile), the
  instructions the static analysis finds in the program too, their own bytes, in the bank they run in.
Compared: the next PC (an interrupt's handler when one is taken), CC, A, B, DP, X, Y, U, S, and the data accesses
(R/W address and byte, SETLN's L) in order. The cycles are the core's, not compared (the lift takes no time).
Instructions the lift does not translate (an indexed ,PC+ or ,-PC: lift.Unsupported) are counted apart.
"""
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent           # (arcade-decomp, the game's tools/core: the game's tools above it)
sys.path[:0] = [str(CORE), str(CORE.parent)]

from konami_cpu import interpreter, lift  # noqa: E402

SLOT = 8
FIRST_SLOT, LAST_SLOT = 0x0100, 0xFF00
BLOCKS = {0xB6, 0xCF, 0xD0}
REPORTED = 40


class Image:
    """The 64 KB as lift.Lifter reads the instruction's bytes"""

    def __init__(self, data):
        self.data = data

    def r8(self, address):
        return self.data[address & 0xFFFF]


def random_state(rng, opcode, pc):
    values = [pc, rng.randrange(256), rng.randrange(256), rng.randrange(256), rng.randrange(256),
              rng.randrange(0x10000), rng.randrange(0x10000), rng.randrange(0x10000), rng.randrange(0x10000),
              int(rng.random() < 0.25), int(rng.random() < 0.25)]
    if opcode in BLOCKS:
        values[7] = rng.randrange(48)
    return values


def batches(cases, rng, states_each):
    """Images of up to (LAST_SLOT - FIRST_SLOT) / SLOT instructions each: (image bytes, [(pc, state)])"""
    per_image = (LAST_SLOT - FIRST_SLOT) // SLOT
    for first in range(0, len(cases), per_image):
        data = bytearray(rng.randrange(256) for _ in range(0x10000))
        runs = []
        for index, code in enumerate(cases[first:first + per_image]):
            pc = FIRST_SLOT + SLOT * index
            data[pc:pc + len(code)] = code
            for _ in range(states_each):
                runs.append((pc, random_state(rng, code[0], pc)))
        yield bytes(data), runs


def all_cases(rng):
    return [bytes((opcode, second) + tuple(rng.randrange(256) for _ in range(SLOT - 2)))
            for opcode in range(256) for second in range(256)]


def program_cases(rom):
    """The bytes of every instruction the static analysis finds, each once"""
    import game_profile  # noqa: E402  (the game's)
    from konami_cpu import analysis  # noqa: E402
    image = game_profile.rom.build_program(game_profile.rom.load_set(Path(rom)))
    found = analysis.Analysis(image, analysis.load_symbols(game_profile.ROOT / "symbols.txt"))
    found.run()
    codes = {bytes(image[place:place + instruction.length]) + bytes(SLOT - instruction.length)
             for place, instruction in found.instructions.items()}
    return sorted(codes)


def interpret(image, state):
    pc, cc, a, b, dp, x, y, u, s, irq, firq = state
    registers = [0] * 8
    registers[lift.A], registers[lift.B], registers[lift.X], registers[lift.Y] = a, b, x, y
    registers[lift.U], registers[lift.S], registers[lift.DP] = u, s, dp
    flags = {name: cc >> position & 1 for name, position in lift.FLAG_BITS.items()}
    machine = interpreter.Machine(image, registers, flags, irq=bool(irq), firq=bool(firq))
    lifted = lift.lift(Image(image), pc)
    next_pc = machine.run(lifted)
    r = machine.regs
    text = (f"{next_pc:04X} {machine.cc():02X} {r[lift.A]:02X} {r[lift.B]:02X} {r[lift.DP]:02X} {r[lift.X]:04X} "
            f"{r[lift.Y]:04X} {r[lift.U]:04X} {r[lift.S]:04X}")
    accesses = " ".join(f"{kind}:{address:04X}:{value:02X}" for kind, address, value in machine.accesses)
    return text, accesses, lifted.mnemonic


def main():
    arguments = sys.argv[1:]

    def option(name, default):
        if name in arguments:
            position = arguments.index(name)
            value = arguments[position + 1]
            del arguments[position:position + 2]
            return value
        return default

    binaries = Path(option("--bin", str(CORE.parent.parent / "build" / "bin")))
    states_each = int(option("--states", "2"))
    rng = random.Random(int(option("--seed", "1")))
    program = option("--program", None)
    oracle = binaries / ("oracle_konami_step.exe" if (binaries / "oracle_konami_step.exe").exists()
                         else "oracle_konami_step")
    cases = all_cases(rng)
    if program:
        cases += program_cases(program)
    compared = unsupported = 0
    differences = []
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        for number, (image, runs) in enumerate(batches(cases, rng, states_each)):
            (folder / "image.bin").write_bytes(image)
            (folder / "states.txt").write_text("".join(
                " ".join(f"{value:X}" for value in state) + "\n" for _, state in runs))
            subprocess.run([str(oracle), str(folder / "image.bin"), str(folder / "states.txt"),
                            str(folder / "results.txt")], check=True)
            results = (folder / "results.txt").read_text().splitlines()
            for (pc, state), result in zip(runs, results):
                try:
                    text, accesses, mnemonic = interpret(image, state)
                except lift.Unsupported:
                    unsupported += 1
                    continue
                compared += 1
                head, _, theirs = result.partition("|")
                fields = head.split()
                mame = " ".join(fields[:9])
                if mame != text or theirs.split() != accesses.split():
                    differences.append((image[pc:pc + 5].hex(" "), mnemonic, state, mame, text, theirs.strip(),
                                        accesses))
            print(f"image {number + 1}: {compared} compared, {len(differences)} different", flush=True)
    for code, mnemonic, state, mame, ours, theirs, accesses in differences[:REPORTED]:
        print(f"\n{code} ({mnemonic}) from " + " ".join(f"{value:X}" for value in state))
        print(f"  MAME: {mame} | {theirs}")
        print(f"  lift: {ours} | {accesses}")
    by_opcode = {}
    for code, mnemonic, *_ in differences:
        by_opcode[code[:2]] = by_opcode.get(code[:2], 0) + 1
    print(f"\n{compared} states compared, {len(differences)} different, {unsupported} not lifted (,PC+ and ,-PC)")
    if by_opcode:
        print("different by opcode: " + ", ".join(f"{opcode} {count}" for opcode, count in sorted(by_opcode.items())))
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
