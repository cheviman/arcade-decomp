# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        check_canonical_flags.py
Description: checks that the decompiler's lift of the Konami CPU (konami_cpu/function.Canonical: the flags as the
             decompiler's passes read them) does what konami_cpu/lift.py does, on every opcode
Version:     0.1.0
Created:     06-11-2026
Modified:    06-11-2026
Author:      Sebastian Toyos

Usage: python tools/core/konami/reference/check_canonical_flags.py [TRIALS]   (from a game's root, whose
       tools/game_profile.py the core reads)
Every opcode, with random operand bytes, registers and flags (the accumulators often at the flags' edges: 0, 1, 7F,
80, FE, FF), is run through lift.Lifter and through function.Canonical on konami_cpu/interpreter.py (with the
carry_add, carry_sub, overflow_add and overflow_sub operators Canonical writes): the registers, the flags, the
memory accesses and where the instruction goes must be the same. lift.py itself is checked against MAME's core
(compare_konami_lift_with_mame.py).
"""
import os
import random
import sys
from pathlib import Path

ROOT = Path.cwd()
sys.path[:0] = [str(ROOT / "tools" / "core"), str(ROOT / "tools")]
from konami_cpu import function as konami_function  # noqa: E402
from konami_cpu import interpreter, lift  # noqa: E402


class Machine(interpreter.Machine):
    def operator(self, node, temps):
        name = node.name
        if name in ("carry_add", "carry_sub", "overflow_add", "overflow_sub"):
            a, b, c = (self.evaluate(arg, temps) for arg in node.args)
            bits = node.args[0].bits
            full = interpreter.mask(bits)
            if name == "carry_add":
                return int(a + b + c > full)
            if name == "carry_sub":
                return int(a < b + c)
            top = 1 << (bits - 1)
            if name == "overflow_add":
                r = (a + b + c) & full
                return int(bool((a ^ r) & (b ^ r) & top))
            r = (a - b - c) & full
            return int(bool((a ^ b) & (a ^ r) & top))
        return super().operator(node, temps)


class Memory:
    def __init__(self, data):
        self.data = data

    def r8(self, address):
        return self.data[address & 0xFFFF]


def run(lifter_class, image, registers, flags, pc):
    lifter = lifter_class(Memory(image), pc).lift()
    machine = Machine(image, registers, flags)
    try:
        next_pc = machine.run(lifter)
    except Exception as problem:  # noqa: BLE001  (both lifts must fail alike)
        return ("error", type(problem).__name__)
    return (tuple(value & 0xFFFF for value in machine.regs), tuple(sorted(machine.flags.items())),
            tuple(machine.accesses), next_pc)


def main(arguments):
    trials = int(arguments[0]) if arguments else 400
    rng = random.Random(1)
    base = bytearray(os.urandom(0x10000))
    differences = checked = 0
    for opcode in range(256):
        for _ in range(trials):
            image = bytearray(base)
            pc = 0x8000
            image[pc] = opcode
            for offset in range(1, 4):
                image[pc + offset] = rng.getrandbits(8)
            registers = [rng.getrandbits(8), rng.getrandbits(8)] + [rng.getrandbits(16) for _ in range(4)] + \
                [rng.getrandbits(8), pc]
            for index in range(2):
                if rng.random() < 0.3:
                    registers[index] = rng.choice((0, 1, 0x7F, 0x80, 0xFF, 0xFE))
            flags = {name: rng.getrandbits(1) for name in lift.FLAGS}
            flags["i"] = flags["f"] = 1
            try:
                first = run(lift.Lifter, bytes(image), registers, flags, pc)
                second = run(konami_function.Canonical, bytes(image), registers, flags, pc)
            except lift.Unsupported:
                continue
            checked += 1
            if first != second:
                differences += 1
                if differences <= 10:
                    print(f"opcode {opcode:02X} bytes {image[pc + 1]:02X} {image[pc + 2]:02X}: {first} / {second}")
    print(f"{checked} runs, {differences} differ")
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
