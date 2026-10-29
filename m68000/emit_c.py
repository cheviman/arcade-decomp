# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        emit_c.py
Description: writes the IR of lifted 68000 instructions (m68000/lift.py) as C
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

The core's printer (v60/emit_c.py) with the 68000's names: the registers are D0-D7, A0-A7, OTHER_SP and
SR_SYSTEM (macros over cpu.r[], m68000/registers.h of the game's runtime), the flags cpu.x, cpu.s (N),
cpu.z, cpu.ov (V) and cpu.cy (C), and the helpers and effects the runtime's m68000/ops.h defines, as
m68000/interpreter.py runs them. Memory goes through mem_read8/16/32 and mem_write8/16/32, the board's bus
in the 68000's bus cycles (a long is its high word, then its low one).

The exception_if effect is control flow, which the translator writes (m68000/translate.py), not this.
"""
from v60 import emit_c

REGISTER_NAMES = tuple(f"D{index}" for index in range(8)) + tuple(f"A{index}" for index in range(8)) \
    + ("OTHER_SP", "SR_SYSTEM")
SHIFTS = ("asl", "asr", "lsl", "lsr", "rol", "ror", "roxl", "roxr")


class Names(emit_c.Names):
    def register(self, index):
        return REGISTER_NAMES[index]


class Printer(emit_c.Printer):
    def __init__(self, names=None):
        super().__init__(names or Names())

    def operator(self, node):
        if node.name in ("carry_add", "overflow_add", "carry_sub", "overflow_sub"):
            width = node.args[0].bits
            a, b, c = (self.expr(arg) for arg in node.args)
            return f"m68k_{node.name}{width}({a}, {b}, {c})"
        return super().operator(node)

    def helper(self, name, args, bits):
        values = [self.expr(arg) for arg in args]
        base, _, part = name.partition("_")
        if base in SHIFTS:
            function = {"": "m68k_shift", "cy": "m68k_shift_carry", "ov": "m68k_shift_overflow"}[part]
            return f"{function}(M68K_{base.upper()}, {values[0]}, {values[1]}, {values[2]}, {args[0].bits})"
        if base in ("divu", "divs", "abcd", "sbcd", "chk"):
            return f"m68k_{name}({', '.join(values)})"
        raise ValueError(f"unknown helper {name}")

    def effect(self, node):
        values = [self.expr(arg) for arg in node.args]
        if node.name == "write_sr":
            return f"m68k_write_sr({values[0]});"
        if node.name == "stop":
            return f"m68k_stop({values[0]});"
        if node.name == "reset":
            return "m68k_reset_devices();"
        raise ValueError(f"unknown effect {node.name}")
