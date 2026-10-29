# license:BSD-3-Clause
# copyright-holders:Olivier Galibert, Sebastian Toyos
"""
Name:        interpreter.py
Description: runs the IR of lifted 68000 instructions on a model of the CPU and its memory, bus cycle by bus
             cycle, the half of the lift's oracle that is not MAME's
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

A Machine holds what the IR reads and writes: the registers as m68000/lift.py numbers them, the flags, and
memory, which is the program image from address 0 and, past it, a pattern of the address (pattern(); the
oracle konami/reference/oracle_m68k_step.cpp has the same). Every bus cycle the IR makes is listed the way
the oracle lists MAME's: ('R' or 'W', the word's even address, mem_mask, the 16-bit word on the bus). A
byte is written on both halves of the bus, as the 68000 does.

The helpers the lift uses (helper:NAME) and the effects it emits are defined here as the core does them;
runtime/include/m68000/ops.h has the same in C. The exceptions and the interrupts push their frame in the
core's order: the PC's low word, the status register, the PC's high word, then the vector is read.
"""
from v60 import ir

from . import lift

MASK32 = 0xFFFFFFFF


class AddressError(Exception):
    """A word or long access at an odd address: the 68000 takes the address error exception, which the lift
    does not model (the program never makes one)."""


class Stopped(Exception):
    """STOP: the CPU waits for an interrupt."""


class Traced(Exception):
    """The instruction left the trace bit set (RTE, a write of the status register): the 68000 takes the trace
    exception after each instruction, which the lift does not model (the program never sets the bit)."""


def pattern(address):
    """What memory holds past the program image where nothing wrote"""
    return ((address * 2654435761) & MASK32) >> 13 & 0xFF


def mask(bits):
    return (1 << bits) - 1


def signed(value, bits):
    value &= mask(bits)
    return value - (1 << bits) if value >> (bits - 1) else value


# ---- the helpers of the core's ALU ----

def divu(dividend, divisor):
    """(result, N, Z, V) of DIVU.W: the remainder in the high word and the quotient in the low one; the
    dividend unchanged when the quotient does not fit (and by zero, where the exception follows)"""
    if divisor == 0:
        return dividend, dividend >> 31 & 1, int(dividend >> 16 == 0), 0
    quotient, remainder = divmod(dividend, divisor)
    if quotient > 0xFFFF:
        return dividend, 1, 0, 1
    return remainder << 16 | quotient, quotient >> 15 & 1, int(quotient == 0), 0


def divs(dividend, divisor):
    """(result, N, Z, V) of DIVS.W; by zero, the flags of the divisor (the core's microcode tests it first)"""
    if divisor == 0:
        return dividend, 0, 1, 0
    a, b = signed(dividend, 32), signed(divisor, 16)
    quotient = abs(a) // abs(b)
    if (a < 0) != (b < 0):
        quotient = -quotient
    remainder = a - quotient * b
    if not -0x8000 <= quotient <= 0x7FFF:
        return dividend, 1, 0, 1
    quotient &= 0xFFFF
    return (remainder & 0xFFFF) << 16 | quotient, quotient >> 15 & 1, int(quotient == 0), 0


def abcd(destination, source, extend):
    """(result, C, V) of the core's alu_abcd8 (X is C)"""
    low = ((destination & 0xF) + (source & 0xF) + extend) & 0xFF
    first = (destination + source + extend) & 0xFFFF
    result = (first + (6 if low > 9 else 0)) & 0xFFFF
    if result > 0x9F:
        result = (result + 0x60) & 0xFFFF
    carry = int(result & 0x300 != 0)
    overflow = int(bool(result & 0x80) and not first & 0x80)
    return result & 0xFF, carry, overflow


def sbcd(destination, source, extend):
    """(result, C, V) of the core's alu_sbcd8 (X is C)"""
    low = ((destination & 0xF) - (source & 0xF) - extend) & 0xFF
    first = (destination - source - extend) & 0xFFFF
    result = (first - (6 if low & 0x10 else 0)) & 0xFFFF
    if first & 0x100:
        result = (result - 0x60) & 0xFFFF
    carry = int(result & 0x300 != 0)
    overflow = int(not result & 0x80 and bool(first & 0x80))
    return result & 0xFF, carry, overflow


def shift(name, value, count, extend, bits):
    """(result, C, V) of a shift or rotate by 'count' (0-63), one bit at a time as the core does them;
    C is the last bit out (0 for no shift, but X for ROXL and ROXR), V for ASL any change of the sign bit"""
    top = 1 << (bits - 1)
    full = mask(bits)
    carry = extend if name in ("roxl", "roxr") else 0
    overflow = 0
    for _ in range(count):
        if name in ("asl", "lsl", "rol", "roxl"):
            out = 1 if value & top else 0
            shifted = (value << 1) & full
            if name == "rol":
                shifted |= out
            elif name == "roxl":
                shifted |= extend
                extend = out
            if name == "asl" and (shifted ^ value) & top:
                overflow = 1
        else:
            out = value & 1
            shifted = value >> 1
            if name == "asr":
                shifted |= value & top
            elif name == "ror":
                shifted |= out << (bits - 1)
            elif name == "roxr":
                shifted |= extend << (bits - 1)
                extend = out
        value = shifted
        carry = out
    return value, carry, overflow


def chk(value, bound):
    """(N, Z, V, C) of CHK.W, the 16-bit register value against the bound"""
    a, b = signed(value, 16), signed(bound, 16)
    negative = 1 if a < 0 else 0 if a > b else 0
    return negative, int(value & 0xFFFF == 0), 0, 0


def helper(name, args, widths):
    """The value of helper:NAME on 'args', whose widths in bits are 'widths'"""
    if name in ("divu", "divu_s", "divu_z", "divu_ov", "divs", "divs_s", "divs_z", "divs_ov"):
        base, _, part = name.partition("_")
        result = (divu if base == "divu" else divs)(args[0], args[1])
        return result[{"": 0, "s": 1, "z": 2, "ov": 3}[part]]
    if name in ("abcd", "abcd_cy", "abcd_ov", "sbcd", "sbcd_cy", "sbcd_ov"):
        base, _, part = name.partition("_")
        result = (abcd if base == "abcd" else sbcd)(*args)
        return result[{"": 0, "cy": 1, "ov": 2}[part]]
    if name in ("chk_s", "chk_z", "chk_ov", "chk_cy"):
        return chk(*args)[{"chk_s": 0, "chk_z": 1, "chk_ov": 2, "chk_cy": 3}[name]]
    base, _, part = name.partition("_")
    if base in ("asl", "asr", "lsl", "lsr", "rol", "ror", "roxl", "roxr"):
        value, count, extend = args
        result = shift(base, value, count, extend, widths[0])
        return result[{"": 0, "cy": 1, "ov": 2}[part]]
    raise ValueError(f"unknown helper {name}")


class Machine:
    """The registers, the flags and the memory the IR works on, and the bus cycles it makes"""

    def __init__(self, image, registers, flags, irq=0):
        self.image = image
        self.size = len(image)
        self.regs = list(registers)
        self.flags = dict(flags)
        self.irq = irq
        self.written = {}
        self.accesses = []

    # ---- the bus ----

    def byte(self, address):
        address &= 0xFFFFFF
        value = self.written.get(address)
        if value is not None:
            return value
        return self.image[address] if address < self.size else pattern(address)

    def read16(self, address, mem_mask, program=False):
        """A word of the bus; 'program' for a read in the program space, which the oracle does not list"""
        address &= 0xFFFFFE
        word = self.byte(address) << 8 | self.byte(address + 1)
        if not program:
            self.accesses.append(("R", address, mem_mask, word))
        return word

    def write16(self, address, data, mem_mask):
        address &= 0xFFFFFE
        self.accesses.append(("W", address, mem_mask, data))
        if mem_mask & 0xFF00:
            self.written[address] = data >> 8
        if mem_mask & 0xFF:
            self.written[address + 1] = data & 0xFF

    def load(self, address, bits, program=False):
        address &= 0xFFFFFF
        if bits == 8:
            word = self.read16(address, 0x00FF if address & 1 else 0xFF00, program)
            return word & 0xFF if address & 1 else word >> 8
        if address & 1:
            raise AddressError(address)
        if bits == 16:
            return self.read16(address, 0xFFFF, program)
        return self.read16(address, 0xFFFF, program) << 16 | self.read16(address + 2, 0xFFFF, program)

    def store(self, address, value, bits):
        address &= 0xFFFFFF
        if bits == 8:
            value &= 0xFF
            self.write16(address, value << 8 | value, 0x00FF if address & 1 else 0xFF00)
            return
        if address & 1:
            raise AddressError(address)
        if bits == 16:
            self.write16(address, value & 0xFFFF, 0xFFFF)
            return
        self.write16(address, value >> 16 & 0xFFFF, 0xFFFF)
        self.write16(address + 2, value & 0xFFFF, 0xFFFF)

    # ---- the status register ----

    def sr(self):
        value = self.regs[lift.SR_SYSTEM] & lift.SR_MASK
        for name, position in lift.FLAG_BITS.items():
            value |= self.flags[name] << position
        return value

    def write_sr(self, value):
        old = self.regs[lift.SR_SYSTEM]
        new = value & lift.SR_MASK
        if (old ^ new) & lift.SR_S:
            self.regs[lift.SP], self.regs[lift.OTHER_SP] = self.regs[lift.OTHER_SP], self.regs[lift.SP]
        self.regs[lift.SR_SYSTEM] = new
        for name, position in lift.FLAG_BITS.items():
            self.flags[name] = value >> position & 1

    def exception(self, vector, pc, level=None):
        """The exception (or, with a level, the interrupt) VECTOR, taken with PC to come back to: the handler's
        address"""
        old = self.sr()
        new = (old & ~0x8000 & 0xFFFF) | lift.SR_S
        if level is not None:
            new = (new & ~0x0700) | level << 8
        self.write_sr(new)
        sp = self.regs[lift.SP]
        self.store(sp - 2, pc & 0xFFFF, 16)
        self.store(sp - 6, old, 16)
        self.store(sp - 4, pc >> 16 & 0xFFFF, 16)
        self.regs[lift.SP] = (sp - 6) & MASK32
        return self.load(vector * 4, 32)

    # ---- the IR ----

    def evaluate(self, node, temps):
        if isinstance(node, ir.Const):
            return node.value
        if isinstance(node, ir.Reg):
            return self.regs[node.index] & mask(node.bits)
        if isinstance(node, ir.Flag):
            return self.flags[node.name]
        if isinstance(node, ir.Temp):
            return temps[node.name]
        if isinstance(node, ir.Load):
            return self.load(self.evaluate(node.address, temps), node.bits)
        if isinstance(node, ir.Op):
            return self.operator(node, temps)
        raise TypeError(f"not an expression: {node!r}")

    def operator(self, node, temps):
        name, bits = node.name, node.bits
        if name == "select":
            condition = self.evaluate(node.args[0], temps)
            return self.evaluate(node.args[1] if condition else node.args[2], temps)
        args = [self.evaluate(arg, temps) for arg in node.args]
        full = mask(bits)
        if name == "add":
            return (args[0] + args[1]) & full
        if name == "sub":
            return (args[0] - args[1]) & full
        if name == "mul":
            return (args[0] * args[1]) & full
        if name == "and":
            return args[0] & args[1] & full
        if name == "or":
            return (args[0] | args[1]) & full
        if name == "xor":
            return (args[0] ^ args[1]) & full
        if name == "not":
            return ~args[0] & full
        if name == "neg":
            return -args[0] & full
        if name in ("shl", "shr", "sar"):
            count = args[1] & (63 if bits == 64 else 31)
            if name == "shl":
                return (args[0] << count) & full
            if name == "shr":
                return (args[0] & full) >> count
            return (signed(args[0], bits) >> count) & full
        if name in ("eq", "ne", "ult", "ule", "lt", "le"):
            width = node.args[0].bits
            a, b = args[0] & mask(width), args[1] & mask(width)
            if name in ("lt", "le"):
                a, b = signed(a, width), signed(b, width)
            return int({"eq": a == b, "ne": a != b, "ult": a < b, "ule": a <= b, "lt": a < b, "le": a <= b}[name])
        if name == "zext":
            return args[0] & full
        if name == "trunc":
            return args[0] & full
        if name == "sext":
            return signed(args[0], args[1]) & full
        if name in ("carry_add", "overflow_add", "carry_sub", "overflow_sub"):
            width = node.args[0].bits
            a, b, carry = args[0], args[1], args[2]
            top = 1 << (width - 1)
            if name.endswith("add"):
                total = a + b + carry
                result = total & mask(width)
                if name == "carry_add":
                    return int(total >> width != 0)
                return int(((a ^ result) & (b ^ result) & top) != 0)
            result = (a - b - carry) & mask(width)
            if name == "carry_sub":
                return int(a < b + carry)
            return int(((a ^ b) & (a ^ result) & top) != 0)
        if name.startswith("helper:"):
            return helper(name[7:], args, [arg.bits for arg in node.args]) & full
        raise ValueError(f"unknown operator {name}")

    def run(self, lifter):
        """Runs one lifted instruction, then the interrupt the lines hold if the mask lets it through; returns
        the address of the next instruction"""
        temps = {}
        next_pc = None
        for statement in lifter.statements:
            if isinstance(statement, ir.SetTemp):
                value = statement.value
                if statement.temp.name in lifter.program_reads:
                    # (a PC-relative operand, read in the program space)
                    address = self.evaluate(value.address, temps)
                    temps[statement.temp.name] = self.load(address, value.bits, program=True)
                else:
                    temps[statement.temp.name] = self.evaluate(value, temps)
            elif isinstance(statement, ir.SetReg):
                self.regs[statement.index] = self.evaluate(statement.value, temps) & MASK32
            elif isinstance(statement, ir.SetFlag):
                self.flags[statement.name] = self.evaluate(statement.value, temps) & 1
            elif isinstance(statement, ir.Store):
                address = self.evaluate(statement.address, temps)
                self.store(address, self.evaluate(statement.value, temps), statement.bits)
            elif isinstance(statement, ir.Effect):
                args = [self.evaluate(arg, temps) for arg in statement.args]
                if statement.name == "write_sr":
                    self.write_sr(args[0])
                elif statement.name == "exception_if":
                    if args[0]:
                        next_pc = self.exception(args[1], args[2])
                        break
                elif statement.name == "stop":
                    self.write_sr(args[0])
                    raise Stopped()
                elif statement.name == "reset":
                    pass
                else:
                    raise ValueError(f"unknown effect {statement.name}")
            else:
                raise TypeError(f"not a statement: {statement!r}")
        if next_pc is None:
            control = lifter.control
            if control.kind == "next":
                next_pc = lifter.next_address
            elif control.kind == "goto":
                next_pc = control.target
            elif control.kind == "branch":
                next_pc = control.target if self.evaluate(control.condition, temps) else lifter.next_address
            elif control.kind == "call":
                next_pc = control.target if control.target is not None else self.evaluate(control.address, temps)
            else:
                next_pc = self.evaluate(control.address, temps)
        next_pc &= MASK32
        if next_pc & 1:
            raise AddressError(next_pc)
        if self.regs[lift.SR_SYSTEM] & 0x8000:
            raise Traced()
        # (level 7 cannot be masked)
        if self.irq and (self.irq == 7 or self.irq > (self.regs[lift.SR_SYSTEM] >> 8 & 7)):
            next_pc = self.exception(24 + self.irq, next_pc, self.irq)
        return next_pc
