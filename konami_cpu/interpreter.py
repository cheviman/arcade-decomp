# license:BSD-3-Clause
# copyright-holders:Nathan Woods, Sebastian Toyos
"""
Name:        interpreter.py
Description: runs the IR of lifted Konami CPU instructions on a model of the CPU and its memory, access by access,
             the half of the lift's oracle that is not MAME's
Version:     0.1.0
Created:     06-11-2026
Modified:    06-11-2026
Author:      Sebastian Toyos

A Machine holds what the IR reads and writes: the registers as konami_cpu/lift.py numbers them, the flags, and the
64 KB the CPU sees: an image from address 0 and, past it, a pattern of the address (pattern(); the oracle
konami/reference/oracle_konami_step.cpp has the same). Every data access the IR makes is listed the way the oracle
lists MAME's: ('R' or 'W', the address, the byte), and SETLN's lines as ('L', 0, the value). The instruction's own
bytes are the lift's constants: the core reads them through its opcode space, which neither lists.

The helpers the lift uses (helper:NAME) and its effects are defined here as the core does them;
runtime/include/konami/ops.h has the same in C. An interrupt is taken after the instruction when its line is held
and the CC lets it through, as the core's MAIN takes it before the next one: FIRQ first (F clear), then IRQ (I
clear); the IRQ pushes the entire state (E set), the FIRQ the PC and CC (E clear), and the vector is read.
"""
from v60 import ir

from . import lift

MASK16 = 0xFFFF


def pattern(address):
    """What memory holds past the image where nothing wrote"""
    return ((address * 2654435761) & 0xFFFFFFFF) >> 13 & 0xFF


def mask(bits):
    return (1 << bits) - 1


def signed(value, bits):
    value &= mask(bits)
    return value - (1 << bits) if value >> (bits - 1) else value


# ---- the helpers ----

def shift_by(name, value, count, s, z, ov, cy):
    """(result, N, Z, V, C) of LSRDI, RORDI, ASRDI, ASLDI or ROLDI: the count's low 4 bits steps, each as konami.lst's
    loop body does it; the flags as they were when there is none"""
    for _ in range(count & 0x0F):
        if name in ("lsrdi", "asrdi"):
            cy = value & 1
            value = (value >> 1) | (value & 0x8000 if name == "asrdi" else 0)
            s, z = value >> 15 & 1, int(value == 0)
        elif name == "rordi":
            new_carry = value & 1
            value = value >> 1 | cy << 15
            cy = new_carry
            s, z = value >> 15 & 1, int(value == 0)
        elif name == "asldi":
            r = value << 1
            s, z = r >> 15 & 1, int(r & MASK16 == 0)
            ov = ((value ^ value ^ r ^ (r >> 1)) >> 15) & 1
            cy = r >> 16 & 1
            value = r & MASK16
        elif name == "roldi":
            new_carry = value >> 15 & 1
            value = (value << 1 | cy) & MASK16
            cy = new_carry
            s, z, ov = value >> 15 & 1, int(value == 0), 0
    return value, s, z, ov, cy


def daa(a, h, c):
    """(A, C) of DAA: m6809_base_device::daa; the carry is only ever set"""
    cf = 0
    msn, lsn = a & 0xF0, a & 0x0F
    if lsn > 0x09 or h:
        cf |= 0x06
    if msn > 0x80 and lsn > 0x09:
        cf |= 0x60
    if msn > 0x90 or c:
        cf |= 0x60
    t = a + cf
    return t & 0xFF, c | (t >> 8 & 1)


def divx(x, b):
    """(quotient, remainder) of DIV X,B: 0 and 0 for B 0"""
    if b == 0:
        return 0, 0
    return (x // b) & MASK16, (x % b) & 0xFF


def helper(name, args):
    base, _, part = name.partition("_")
    if base in lift.SHIFTS:
        result = shift_by(base, *args)
        return result[{"": 0, "s": 1, "z": 2, "ov": 3, "cy": 4}[part]]
    if base == "daa":
        return daa(*args)[{"": 0, "cy": 1}[part]]
    if base == "divx":
        return divx(*args)[{"": 0, "rem": 1}[part]]
    raise ValueError(f"unknown helper {name}")


class Machine:
    """The registers, the flags and the 64 KB the IR works on, and the data accesses it makes"""

    def __init__(self, image, registers, flags, irq=False, firq=False):
        self.image = image
        self.size = len(image)
        self.regs = list(registers)
        self.flags = dict(flags)
        self.irq = irq
        self.firq = firq
        self.written = {}
        self.accesses = []

    # ---- the bus ----

    def byte(self, address):
        address &= MASK16
        value = self.written.get(address)
        if value is not None:
            return value
        return self.image[address] if address < self.size else pattern(address)

    def load(self, address):
        address &= MASK16
        value = self.byte(address)
        self.accesses.append(("R", address, value))
        return value

    def store(self, address, value):
        address &= MASK16
        value &= 0xFF
        self.accesses.append(("W", address, value))
        self.written[address] = value

    # ---- the CC and the stack ----

    def cc(self):
        value = 0
        for name, position in lift.FLAG_BITS.items():
            value |= self.flags[name] << position
        return value

    def set_cc(self, value):
        for name, position in lift.FLAG_BITS.items():
            self.flags[name] = value >> position & 1

    def push(self, value, bits=8):
        """A byte, or a word low byte first, onto S"""
        if bits == 16:
            self.push(value & 0xFF)
            self.push(value >> 8)
            return
        self.regs[lift.S] = (self.regs[lift.S] - 1) & MASK16
        self.store(self.regs[lift.S], value)

    def pull(self):
        value = self.load(self.regs[lift.S])
        self.regs[lift.S] = (self.regs[lift.S] + 1) & MASK16
        return value

    def pull16(self):
        high = self.pull()
        return high << 8 | self.pull()

    def interrupt(self, pc):
        """The interrupt the lines hold, if the CC lets it through (FIRQ first): its handler's address, or None"""
        if self.firq and not self.flags["f"]:
            self.flags["e"] = 0
            self.push(pc, 16)
            self.push(self.cc())
            self.flags["i"] = self.flags["f"] = 1
            vector = lift.VECTOR_FIRQ
        elif self.irq and not self.flags["i"]:
            self.flags["e"] = 1
            self.push(pc, 16)
            self.push(self.regs[lift.U], 16)
            self.push(self.regs[lift.Y], 16)
            self.push(self.regs[lift.X], 16)
            self.push(self.regs[lift.DP])
            self.push(self.regs[lift.B])
            self.push(self.regs[lift.A])
            self.push(self.cc())
            self.flags["i"] = 1
            vector = lift.VECTOR_IRQ
        else:
            return None
        return self.load(vector) << 8 | self.load(vector + 1)

    # ---- the effects ----

    def effect(self, name, args):
        if name == "set_lines":
            self.accesses.append(("L", 0, args[0] & 0xFF))
        elif name == "rti":
            self.set_cc(self.pull())
            if self.flags["e"]:
                self.regs[lift.A] = self.pull()
                self.regs[lift.B] = self.pull()
                self.regs[lift.DP] = self.pull()
                self.regs[lift.X] = self.pull16()
                self.regs[lift.Y] = self.pull16()
                self.regs[lift.U] = self.pull16()
            self.regs[lift.PC] = self.pull16()
        elif name in ("bmove", "bset", "bsetw"):
            while self.regs[lift.U] != 0:
                if name == "bmove":
                    value = self.load(self.regs[lift.Y])
                    self.regs[lift.Y] = (self.regs[lift.Y] + 1) & MASK16
                    self.store(self.regs[lift.X], value)
                    self.regs[lift.X] = (self.regs[lift.X] + 1) & MASK16
                else:
                    self.store(self.regs[lift.X], self.regs[lift.A])
                    self.regs[lift.X] = (self.regs[lift.X] + 1) & MASK16
                    if name == "bsetw":
                        self.store(self.regs[lift.X], self.regs[lift.B])
                        self.regs[lift.X] = (self.regs[lift.X] + 1) & MASK16
                self.regs[lift.U] = (self.regs[lift.U] - 1) & MASK16
        elif name == "illegal":
            pass
        else:
            raise ValueError(f"unknown effect {name}")

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
            return self.load(self.evaluate(node.address, temps))
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
        if name in ("zext", "trunc"):
            return args[0] & full
        if name == "sext":
            return signed(args[0], args[1]) & full
        if name.startswith("helper:"):
            return helper(name[7:], args) & full
        raise ValueError(f"unknown operator {name}")

    def run(self, lifter):
        """Runs one lifted instruction, then the interrupt the lines hold if the CC lets it through; returns the
        address of the next instruction"""
        temps = {}
        for statement in lifter.statements:
            if isinstance(statement, ir.SetTemp):
                temps[statement.temp.name] = self.evaluate(statement.value, temps)
            elif isinstance(statement, ir.SetReg):
                self.regs[statement.index] = self.evaluate(statement.value, temps) & mask(lift.REGISTER_BITS[
                    statement.index])
            elif isinstance(statement, ir.SetFlag):
                self.flags[statement.name] = self.evaluate(statement.value, temps) & 1
            elif isinstance(statement, ir.Store):
                address = self.evaluate(statement.address, temps)
                self.store(address, self.evaluate(statement.value, temps))
            elif isinstance(statement, ir.Effect):
                self.effect(statement.name, [self.evaluate(arg, temps) for arg in statement.args])
            else:
                raise TypeError(f"not a statement: {statement!r}")
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
        next_pc &= MASK16
        handler = self.interrupt(next_pc)
        return handler if handler is not None else next_pc
