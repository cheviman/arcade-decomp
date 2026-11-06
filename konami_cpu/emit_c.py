# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        emit_c.py
Description: writes the IR of lifted Konami CPU instructions (konami_cpu/lift.py) as C
Version:     0.1.0
Created:     06-11-2026
Modified:    06-11-2026
Author:      Sebastian Toyos

The core's printer (v60/emit_c.py) with the Konami CPU's names: the registers are cpu.a, cpu.b, cpu.x, cpu.y, cpu.u,
cpu.s, cpu.dp and cpu.pc, the flags cpu.e, cpu.f, cpu.h, cpu.i, cpu.n (N), cpu.z, cpu.v (V) and cpu.c (C) (the
runtime's konami/cpu.h), and the helpers and effects the runtime's konami/ops.h defines, as konami_cpu/interpreter.py
runs them. Memory goes through mem_read8 and mem_write8, the board's bus.
"""
from v60 import emit_c

from . import lift

REGISTERS = ("cpu.a", "cpu.b", "cpu.x", "cpu.y", "cpu.u", "cpu.s", "cpu.dp", "cpu.pc")
FLAGS = {"e": "cpu.e", "f": "cpu.f", "h": "cpu.h", "i": "cpu.i", "s": "cpu.n", "z": "cpu.z", "ov": "cpu.v",
         "cy": "cpu.c"}
PARTS = {"": "KONAMI_RESULT", "s": "KONAMI_N", "z": "KONAMI_Z", "ov": "KONAMI_V", "cy": "KONAMI_C"}


class Names(emit_c.Names):
    def register(self, index):
        return REGISTERS[index]

    def flag(self, name):
        return FLAGS[name]


class Printer(emit_c.Printer):
    def __init__(self, names=None):
        super().__init__(names or Names())

    def helper(self, name, args, bits):
        values = [self.expr(arg) for arg in args]
        base, _, part = name.partition("_")
        if base in lift.SHIFTS:
            return f"konami_shift(KONAMI_{base.upper()}, {PARTS[part]}, {', '.join(values)})"
        if name == "daa":
            return f"konami_daa({', '.join(values)})"
        if name == "daa_cy":
            return f"konami_daa_carry({', '.join(values)})"
        if name == "divx":
            return f"konami_divx({', '.join(values)})"
        if name == "divx_rem":
            return f"konami_divx_remainder({', '.join(values)})"
        raise ValueError(f"unknown helper {name}")

    def effect(self, node):
        values = [self.expr(arg) for arg in node.args]
        if node.name == "set_lines":
            return f"konami_set_lines({values[0]});"
        if node.name in ("rti", "bmove", "bset", "bsetw"):
            return f"konami_{node.name}();"
        if node.name == "illegal":
            return f"konami_illegal({values[0]});"
        raise ValueError(f"unknown effect {node.name}")
