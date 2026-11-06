# license:BSD-3-Clause
# copyright-holders:Nathan Woods, Sebastian Toyos
"""
Name:        lift.py
Description: translates Konami CPU instructions (the 052001 and the 053248: a 6809 with Konami's opcodes) into IR
             statements that do what MAME's core does
Version:     0.1.0
Created:     06-11-2026
Modified:    06-11-2026
Author:      Sebastian Toyos

Every opcode of MAME's konami.lst (with the steps of base6x09.lst it shares with the 6809) becomes IR (v60/ir.py, the
core's IR), read from the instruction's bytes as the CPU reads them, not from the disassembler: its operands read and
written in the order the core makes its memory accesses, and every flag set the way the core's set_flags<T>(mask,
a, b, r) sets it (r the result as an unsigned 32-bit number: H bit 4 of a^b^r, N the result's top bit, Z the result
is 0, V the top bit of a^b^r^(r>>1), C the bit above the top). konami/reference/compare_lift_with_mame.py runs each
instruction on MAME's core (oracle_konami_step) and on this IR (konami_cpu/interpreter.py) from many states, and
compares what they leave and the accesses they make, one by one.

Registers: A 0 and B 1 (8 bits; D is A:B), X 2, Y 3, U 4, S 5 (16 bits), DP 6 (8 bits), and PC 7, which only the
effects that pull it write (rti). The condition code register is the flags e f h i s z ov cy (E F H I N Z V C,
bits 7-0).

Memory: every access is one byte, a Load or a Store of 8 bits at a 16-bit address, the data accesses the core makes
through read_memory and write_memory (the bank window 6000-7FFF is the board's: the bus answers it). The instruction's
own bytes (opcode, postbyte, immediates, offsets, addresses) are constants of the lift, as the core reads them through
the opcode space; so are its dummy reads of the instruction stream and of FFFF (dummy_read_opcode_arg, dummy_vma),
which reach the program's ROM. The data reads the core makes only to throw the value away are Loads bound to a
temporary nothing reads: CLR of memory reads it first. Konami's CPU runs base6x09.lst in the 6309's native mode
(konami.h: hd6309_native_mode() is true), so the 6809's other dummy reads are not made: PSHS and PSHU do not read the
stack's top first.

The indexed postbyte (konami.lst INDEXED), on the bits the CPU looks at (postbyte & F7; bit 3 indirect):
    07          an absolute address, 2 bytes
    r0 r1       ,R+ ,R++ (the address the register's, then the register up 1 or 2)
    r2 r3       ,-R ,--R
    r4 r5       a signed byte or word offset: the register's value before the offset's bytes (for PC, the address
                of the offset's first byte, not the instruction's end)
    r6          ,R
    C4          the direct page (DP:byte)
    rA0 rA1 rA7 (r with bit 7 set) A,R  B,R  D,R: the register plus A or B signed, or D
    any other   address 0 (MAME logs it)
  with r 2 X, 3 Y, 5 U, 6 S, 7 PC (bits 4-6). Indirect: the word at that address, read high byte first.
Konami's register addressing: an operand may be A, B or D (CLRA, NEGD, LSRDI #n...), the same steps on a register.

Effects (the runtime does them, runtime/include/konami/ops.h; the interpreter, here):
  set_lines VALUE       SETLN: the CPU's output lines (the board's bank and its other lines)
  rti                   RTI: CC pulled from S, then A B DP X Y U PC when its E bit is set, else the PC alone (into
                        the PC register, 7)
  bmove / bset / bsetw  the block instructions: while U is not 0, a byte from [Y+] to [X+] (bset: A to [X+]; bsetw:
                        A then B to [X+]), U down by one each time; MOVE is one step of BMOVE, lifted as IR
  illegal               an opcode the CPU does not know: MAME logs it and goes on to the next byte
Helpers (helper:NAME, runtime/include/konami/ops.h; here: helper()): the shifts by a count (lsrdi, rordi, asrdi, asldi,
roldi, each with _s _z _ov _cy for its flags: the count's low 4 bits steps, the flags as they were for none), daa
(and daa_cy), divx and divx_rem (DIV X,B: 0 and 0 when B is 0).
Control is the instruction's Control (lifter.control): how execution goes on after it. The interrupts (IRQ, FIRQ)
are taken between instructions, by the runtime and the interpreter (interpreter.Machine.interrupt), as the core's
MAIN takes them.
"""
from dataclasses import dataclass

from v60 import ir

A, B, X, Y, U, S, DP, PC = 0, 1, 2, 3, 4, 5, 6, 7
REGISTER_BITS = {A: 8, B: 8, X: 16, Y: 16, U: 16, S: 16, DP: 8, PC: 16}
REGISTER_NAMES = ("A", "B", "X", "Y", "U", "S", "DP", "PC")
FLAGS = ("e", "f", "h", "i", "s", "z", "ov", "cy")
FLAG_BITS = {"e": 7, "f": 6, "h": 5, "i": 4, "s": 3, "z": 2, "ov": 1, "cy": 0}
# set_flags' masks
HNZVC, NZVC, NZV, NZ, Z = ("h", "s", "z", "ov", "cy"), ("s", "z", "ov", "cy"), ("s", "z", "ov"), ("s", "z"), ("z",)
# the indexed postbyte's register (bits 4-6), as ireg() picks it; 0, 1 and 4 none
INDEX_REGISTERS = {2: X, 3: Y, 5: U, 6: S, 7: PC}
# EXG/TFR's register codes (read_exgtfr_register / write_exgtfr_register)
EXCHANGE_CODES = ("A", "D", "X", "Y", "DP", "U", "S", "PC")
# PSHS/PULS: the postbyte's bits, in the order they are pushed (pulled the other way round)
PUSH_ORDER = ((0x80, "PC"), (0x40, "other"), (0x20, Y), (0x10, X), (0x08, DP), (0x04, B), (0x02, A), (0x01, "CC"))
VECTOR_FIRQ, VECTOR_IRQ = 0xFFF6, 0xFFF8
ENTIRE_STATE, PARTIAL_STATE = 0xFF, 0x81
SHIFTS = ("lsrdi", "rordi", "asrdi", "asldi", "roldi")


class Unsupported(Exception):
    """An instruction or operand the lifter does not translate (an indexed ,PC+ or ,-PC, which moves the PC inside the
    instruction: no program does it)"""


@dataclass
class Control:
    """How execution goes on after an instruction.
    next     on to the next instruction
    goto     to 'target' (BRA, LBRA, JMP to a fixed place)
    branch   to 'target' when 'condition' (1 bit) holds, else on (Bcc, LBcc, DBJNZ, DXJNZ)
    call     to 'target' (BSR, LBSR, JSR to a fixed place) or to the computed 'address', with the return address
             pushed by the statements
    jump     to the computed 'address' (JMP through memory or a register, EXG and TFR that write the PC)
    return   to the popped 'address' (RTS, RTI, PULS and PULU of the PC: the statements popped it)
    """
    kind: str = "next"
    target: int = None
    address: object = None
    condition: object = None


def const(value, bits=16):
    return ir.const(value, bits)


def lnot(value):
    return ir.op("eq", (value, ir.const(0, value.bits)), 1)


def land(a, b):
    return ir.op("and", (a, b), 1)


def lor(a, b):
    return ir.op("or", (a, b), 1)


def lxor(a, b):
    return ir.op("xor", (a, b), 1)


def flag(name):
    return ir.Flag(name)


def bit_of(value, number):
    """Bit 'number' of 'value', 1 bit"""
    return ir.trunc(ir.op("shr", (value, ir.const(number, value.bits)), value.bits), 1)


def condition_of(name):
    """The 1-bit condition of a branch ('hi', 'cc'...; 't' and 'f'), from the flags, as m6809.h's cond_*()"""
    z, s, ov, cy = flag("z"), flag("s"), flag("ov"), flag("cy")
    ge = lnot(lxor(s, ov))
    table = {
        "t": ir.const(1, 1), "f": ir.const(0, 1),
        "hi": lnot(lor(z, cy)), "ls": lor(z, cy), "cc": lnot(cy), "cs": cy, "ne": lnot(z), "eq": z,
        "vc": lnot(ov), "vs": ov, "pl": lnot(s), "mi": s, "ge": ge, "lt": lnot(ge),
        "gt": land(ge, lnot(z)), "le": lnot(land(ge, lnot(z))),
    }
    return table[name]


# the branches' conditions by the opcode's low 3 bits (60-67 and 68-6F; 70-7F the opposite ones)
CONDITIONS = ("t", "hi", "cc", "ne", "vc", "pl", "ge", "gt")
OPPOSITES = {"t": "f", "hi": "ls", "cc": "cs", "ne": "eq", "vc": "vs", "pl": "mi", "ge": "lt", "gt": "le"}


def cc_of():
    """The condition code register, E F H I N Z V C in bits 7-0, as an 8-bit value"""
    value = None
    for name in FLAGS:
        part = ir.op("shl", (ir.zext(flag(name), 8), const(FLAG_BITS[name], 8)), 8) if FLAG_BITS[name] \
            else ir.zext(flag(name), 8)
        value = part if value is None else ir.op("or", (value, part), 8)
    return value


class Operand:
    """Where an instruction's operand is: memory at 'address' (16 bits), the instruction's bytes ('immediate'), or a
    register: 'A', 'B' or 'D'"""

    def __init__(self, kind, address=None):
        self.kind = kind
        self.address = address


class Lifter:
    """The IR statements of one instruction, and how execution goes on after it. 'mem' gives the instruction's bytes
    (r8 at a 16-bit address)."""

    def __init__(self, mem, address, prefix="t"):
        self.mem = mem
        self.address = address & 0xFFFF
        self.cursor = self.address
        self.statements = []
        self.prefix = prefix
        self.count = 0
        self.control = Control()
        self.opcode = None
        self.mnemonic = ""

    @property
    def length(self):
        return (self.cursor - self.address) & 0xFFFF

    @property
    def next_address(self):
        return self.cursor & 0xFFFF

    # ---- the instruction's bytes ----

    def fetch8(self):
        value = self.mem.r8(self.cursor & 0xFFFF)
        self.cursor = (self.cursor + 1) & 0xFFFF
        return value

    def fetch16(self):
        high = self.fetch8()
        return high << 8 | self.fetch8()

    # ---- statements ----

    def emit(self, statement):
        self.statements.append(statement)

    def temp(self, value, hint="value"):
        """Binds 'value' to a new temporary, so it is computed here, in order"""
        self.count += 1
        temp = ir.Temp(f"{self.prefix}{self.count}_{hint}", value.bits)
        self.emit(ir.SetTemp(temp, value))
        return temp

    def bound(self, value, hint="value"):
        """'value' as something that can be read twice: a constant, a temporary, or a new temporary"""
        if isinstance(value, (ir.Const, ir.Temp)):
            return value
        return self.temp(value, hint)

    def load(self, address, hint="byte"):
        """A data read of one byte, now"""
        return self.temp(ir.Load(ir.trunc(address, 16) if address.bits != 16 else address, 8), hint)

    def store(self, address, value):
        self.emit(ir.Store(address if address.bits == 16 else ir.trunc(address, 16), ir.trunc(value, 8)
                           if value.bits != 8 else value, 8))

    def reg(self, index):
        return ir.Reg(index, REGISTER_BITS[index])

    def set_reg(self, index, value):
        bits = REGISTER_BITS[index]
        if value.bits != bits:
            value = ir.trunc(value, bits) if value.bits > bits else ir.zext(value, bits)
        self.emit(ir.SetReg(index, value))

    def d_value(self):
        """D, A:B"""
        return ir.op("or", (ir.op("shl", (ir.zext(self.reg(A), 16), const(8)), 16), ir.zext(self.reg(B), 16)), 16)

    def set_d(self, value):
        value = self.bound(value, "d")
        self.set_reg(A, ir.trunc(ir.op("shr", (value, const(8)), 16), 8))
        self.set_reg(B, ir.trunc(value, 8))

    def set_flag(self, name, value):
        self.emit(ir.SetFlag(name, value))

    def add16(self, value, amount):
        return ir.op("add", (value, const(amount & 0xFFFF)), 16)

    # ---- set_flags ----

    def set_flags(self, mask, a, b, r, bits):
        """m6809_base_device::set_flags<T>(mask, a, b, r): 'a' and 'b' of 'bits' bits, 'r' the result as an unsigned
        32-bit number; the flags in 'mask' set from them; the result's low 'bits' bits"""
        a32, b32 = ir.zext(a, 32) if a.bits < 32 else a, ir.zext(b, 32) if b.bits < 32 else b
        r = self.bound(r if r.bits == 32 else ir.zext(r, 32), "r")
        top = bits - 1
        if "h" in mask:
            self.set_flag("h", bit_of(ir.op("xor", (ir.op("xor", (a32, b32), 32), r), 32), 4))
        if "s" in mask:
            self.set_flag("s", bit_of(r, top))
        if "z" in mask:
            self.set_flag("z", ir.op("eq", (ir.trunc(r, bits), ir.const(0, bits)), 1))
        if "ov" in mask:
            mixed = ir.op("xor", (ir.op("xor", (ir.op("xor", (a32, b32), 32), r), 32),
                                  ir.op("shr", (r, ir.const(1, 32)), 32)), 32)
            self.set_flag("ov", bit_of(mixed, top))
        if "cy" in mask:
            self.set_flag("cy", bit_of(r, bits))
        return ir.trunc(r, bits)

    def set_flags1(self, mask, r, bits):
        """set_flags<T>(mask, r): a 0, b and r the value at its width (V and C come out 0)"""
        r = self.bound(r, "r")
        return self.set_flags(mask, ir.const(0, bits), r, ir.zext(r, 32), bits)

    # ---- operands ----

    def indexed(self):
        """konami.lst INDEXED: the postbyte and its bytes; the operand at the address it gives"""
        postbyte = self.fetch8()
        mode = postbyte & 0xF7
        register = INDEX_REGISTERS.get(postbyte >> 4 & 7)
        if mode == 0x07:
            address = const(self.fetch16())
        elif mode in (0x20, 0x30, 0x50, 0x60, 0x70, 0x21, 0x31, 0x51, 0x61, 0x71,
                      0x22, 0x32, 0x52, 0x62, 0x72, 0x23, 0x33, 0x53, 0x63, 0x73):
            if register == PC:
                raise Unsupported(f"{postbyte:02X}: an auto increment or decrement of the PC")
            step = mode & 3
            current = self.temp(self.reg(register), "index")
            if step in (0, 1):
                address = current
                self.set_reg(register, self.add16(current, step + 1))
            else:
                address = self.temp(self.add16(current, -(step - 1)), "index")
                self.set_reg(register, address)
        elif mode in (0x24, 0x34, 0x54, 0x64, 0x74):
            base = const(self.cursor) if register == PC else self.temp(self.reg(register), "index")
            offset = self.fetch8()
            address = ir.op("add", (base, const(offset - 0x100 if offset & 0x80 else offset)), 16)
        elif mode in (0x25, 0x35, 0x55, 0x65, 0x75):
            base = const(self.cursor) if register == PC else self.temp(self.reg(register), "index")
            address = ir.op("add", (base, const(self.fetch16())), 16)
        elif mode in (0x26, 0x36, 0x56, 0x66, 0x76):
            address = const(self.cursor) if register == PC else self.reg(register)
        elif mode == 0xC4:
            address = ir.op("or", (ir.op("shl", (ir.zext(self.reg(DP), 16), const(8)), 16), const(self.fetch8())), 16)
        elif mode in (0xA0, 0xB0, 0xD0, 0xE0, 0xF0, 0xA1, 0xB1, 0xD1, 0xE1, 0xF1):
            base = const(self.cursor) if register == PC else self.reg(register)
            accumulator = self.reg(A if mode & 1 == 0 else B)
            address = ir.op("add", (base, ir.sext(accumulator, 16)), 16)
        elif mode in (0xA7, 0xB7, 0xD7, 0xE7, 0xF7):
            base = const(self.cursor) if register == PC else self.reg(register)
            address = ir.op("add", (base, self.d_value()), 16)
        else:
            address = const(0)
        address = self.bound(address, "ea")
        if postbyte & 0x08:
            high = self.load(address, "high")
            low = self.load(self.add16(address, 1), "low")
            address = self.temp(ir.op("or", (ir.op("shl", (ir.zext(high, 16), const(8)), 16), ir.zext(low, 16)), 16),
                                "ea")
        return Operand("ea", address)

    def read8(self, operand, hint="operand"):
        """read_operand(): a byte"""
        if operand.kind == "ea":
            return self.load(operand.address, hint)
        if operand.kind == "immediate":
            return const(self.fetch8(), 8)
        return self.temp(self.reg(A if operand.kind == "A" else B), hint)

    def read16(self, operand, hint="operand"):
        """read_operand(0), read_operand(1): a word, high byte first"""
        if operand.kind == "ea":
            high = self.load(operand.address, "high")
            low = self.load(self.add16(operand.address, 1), "low")
            return self.temp(ir.op("or", (ir.op("shl", (ir.zext(high, 16), const(8)), 16), ir.zext(low, 16)), 16), hint)
        if operand.kind == "immediate":
            return const(self.fetch16())
        return self.temp(self.d_value(), hint)

    def write8(self, operand, value):
        """write_operand(data)"""
        if operand.kind == "ea":
            self.store(operand.address, value)
        elif operand.kind in ("A", "B"):
            self.set_reg(A if operand.kind == "A" else B, value)

    def write16(self, operand, value):
        """write_operand(0, high), write_operand(1, low)"""
        value = self.bound(value, "word")
        if operand.kind == "ea":
            self.store(operand.address, ir.trunc(ir.op("shr", (value, const(8)), 16), 8))
            self.store(self.add16(operand.address, 1), ir.trunc(value, 8))
        elif operand.kind == "D":
            self.set_d(value)

    # ---- the stack ----

    def push(self, stack, mask):
        """PUSH_REGISTERS onto 'stack' (S or U): the postbyte's registers, PC first, CC last"""
        other = U if stack == S else S
        pointer = self.temp(self.reg(stack), "sp")
        for bit, name in PUSH_ORDER:
            if not mask & bit:
                continue
            if name in ("PC", "other") or name in (X, Y):
                value = const(self.next_address) if name == "PC" else self.reg(other if name == "other" else name)
                value = self.bound(value, "pushed")
                pointer = self.temp(self.add16(pointer, -1), "sp")
                self.store(pointer, ir.trunc(value, 8))
                pointer = self.temp(self.add16(pointer, -1), "sp")
                self.store(pointer, ir.trunc(ir.op("shr", (value, const(8)), 16), 8))
            else:
                value = cc_of() if name == "CC" else self.reg(name)
                pointer = self.temp(self.add16(pointer, -1), "sp")
                self.store(pointer, value)
        self.set_reg(stack, pointer)

    def pull(self, stack, mask):
        """PULL_REGISTERS from 'stack': CC first, the PC last; the PC pulled, or None"""
        other = U if stack == S else S
        pointer = self.temp(self.reg(stack), "sp")
        pc = None
        for bit, name in reversed(PUSH_ORDER):
            if not mask & bit:
                continue
            if name in ("PC", "other") or name in (X, Y):
                high = self.load(pointer, "high")
                pointer = self.temp(self.add16(pointer, 1), "sp")
                low = self.load(pointer, "low")
                pointer = self.temp(self.add16(pointer, 1), "sp")
                value = self.temp(ir.op("or", (ir.op("shl", (ir.zext(high, 16), const(8)), 16), ir.zext(low, 16)), 16),
                                  "pulled")
                if name == "PC":
                    pc = value
                else:
                    self.set_reg(other if name == "other" else name, value)
            else:
                value = self.load(pointer, "pulled")
                pointer = self.temp(self.add16(pointer, 1), "sp")
                if name == "CC":
                    self.set_cc(value)
                else:
                    self.set_reg(name, value)
        self.set_reg(stack, pointer)
        return pc

    def set_cc(self, value):
        value = self.bound(value, "cc")
        for name in FLAGS:
            self.set_flag(name, bit_of(value, FLAG_BITS[name]))

    def push_pc(self):
        """GOTO_SUBROUTINE: the return address on S, low byte first"""
        pointer = self.temp(self.add16(self.reg(S), -1), "sp")
        self.store(pointer, const(self.next_address & 0xFF, 8))
        pointer = self.temp(self.add16(pointer, -1), "sp")
        self.store(pointer, const(self.next_address >> 8, 8))
        self.set_reg(S, pointer)

    # ---- EXG and TFR ----

    def exchange_read(self, code):
        name = EXCHANGE_CODES[code & 7]
        if name == "A":
            return ir.op("or", (ir.zext(self.reg(A), 16), const(0x1000)), 16)
        if name == "D":
            return self.d_value()
        if name == "DP":
            return const(0)
        if name == "PC":
            return const(self.next_address)
        return self.reg({"X": X, "Y": Y, "U": U, "S": S}[name])

    def exchange_write(self, code, value):
        """write_exgtfr_register; the PC written, or None"""
        name = EXCHANGE_CODES[code & 7]
        if name == "A":
            self.set_reg(A, ir.trunc(value, 8))
        elif name == "D":
            self.set_reg(B, ir.trunc(value, 8))
        elif name == "PC":
            return value
        elif name != "DP":
            self.set_reg({"X": X, "Y": Y, "U": U, "S": S}[name], value)
        return None

    # ---- the instructions ----

    def lift(self):
        opcode = self.opcode = self.fetch8()
        handler = HANDLERS.get(opcode)
        if handler is None:
            self.mnemonic = "illegal"
            self.emit(ir.Effect("illegal", (const(opcode, 8),)))
            return self
        handler(self, opcode)
        return self


# ---- the opcodes, as konami.lst's switch ----

def accumulator_of(opcode):
    return "A" if opcode & 1 == 0 else "B"


def operand_8(lifter, opcode):
    """The operand of the 10-37 pairs: immediate when the opcode's bit 1 is clear (10 11: imm, 12 13: indexed)"""
    if opcode & 2:
        return lifter.indexed()
    return Operand("immediate")


def alu8(name):
    def handler(lifter, opcode):
        lifter.mnemonic = name
        register = A if opcode & 1 == 0 else B
        operand = operand_8(lifter, opcode)
        value = lifter.read8(operand)
        current = lifter.temp(lifter.reg(register), "reg")
        a32, v32 = ir.zext(current, 32), ir.zext(value, 32)
        carry = ir.zext(flag("cy"), 32)
        if name == "ld":
            lifter.set_reg(register, lifter.set_flags1(NZV, value, 8))
        elif name in ("add", "adc"):
            total = ir.op("add", (a32, v32), 32)
            if name == "adc":
                total = ir.op("add", (total, carry), 32)
            lifter.set_reg(register, lifter.set_flags(HNZVC, current, value, total, 8))
        elif name in ("sub", "sbc", "cmp"):
            total = ir.op("sub", (a32, v32), 32)
            if name == "sbc":
                total = ir.op("sub", (total, carry), 32)
            result = lifter.set_flags(NZVC, current, value, total, 8)
            if name != "cmp":
                lifter.set_reg(register, result)
        elif name in ("and", "bit", "eor", "or"):
            lifter.set_flag("ov", ir.const(0, 1))
            op = {"and": "and", "bit": "and", "eor": "xor", "or": "or"}[name]
            result = lifter.set_flags(NZ, ir.const(0, 8), current, ir.zext(ir.op(op, (current, value), 8), 32), 8)
            if name != "bit":
                lifter.set_reg(register, result)
    return handler


def alu16(name, register):
    def handler(lifter, opcode):
        lifter.mnemonic = f"{name}{'d' if register == 'D' else REGISTER_NAMES[register].lower()}"
        operand = Operand("immediate") if opcode in IMMEDIATE_16 else lifter.indexed()
        value = lifter.read16(operand)
        if name == "ld":
            result = lifter.set_flags1(NZV, value, 16)
        else:
            current = lifter.temp(lifter.d_value() if register == "D" else lifter.reg(register), "reg")
            op = "add" if name == "add" else "sub"
            result = lifter.set_flags(NZVC, current, value, ir.op(op, (ir.zext(current, 32), ir.zext(value, 32)), 32),
                                      16)
            if name == "cmp":
                return
        if register == "D":
            lifter.set_d(result)
        else:
            lifter.set_reg(register, result)
    return handler


def store16(register):
    def handler(lifter, opcode):
        lifter.mnemonic = f"st{'d' if register == 'D' else REGISTER_NAMES[register].lower()}"
        operand = lifter.indexed()
        value = lifter.temp(lifter.d_value() if register == "D" else lifter.reg(register), "reg")
        lifter.write16(operand, value)
        lifter.set_flags1(NZV, value, 16)
    return handler


def lea(register):
    def handler(lifter, opcode):
        lifter.mnemonic = f"lea{REGISTER_NAMES[register].lower()}"
        operand = lifter.indexed()
        address = lifter.bound(operand.address, "ea")
        if register in (X, Y):
            lifter.set_flags1(Z, address, 16)
        lifter.set_reg(register, address)
    return handler


def push_pull(stack, pull):
    def handler(lifter, opcode):
        lifter.mnemonic = ("pull" if pull else "push") + ("s" if stack == S else "u")
        mask = lifter.fetch8()
        if pull:
            pc = lifter.pull(stack, mask)
            if pc is not None:
                lifter.control = Control("return", address=pc)
        else:
            lifter.push(stack, mask)
    return handler


def setln(lifter, opcode):
    lifter.mnemonic = "setln"
    operand = Operand("immediate") if opcode == 0x38 else lifter.indexed()
    lifter.emit(ir.Effect("set_lines", (lifter.read8(operand),)))


def store8(lifter, opcode):
    lifter.mnemonic = f"st{accumulator_of(opcode).lower()}"
    operand = lifter.indexed()
    value = lifter.temp(lifter.reg(A if opcode & 1 == 0 else B), "reg")
    lifter.store(operand.address, lifter.set_flags1(NZV, value, 8))


def cc_op(lifter, opcode):
    lifter.mnemonic = "andcc" if opcode == 0x3C else "orcc"
    value = const(lifter.fetch8(), 8)
    op = "and" if opcode == 0x3C else "or"
    lifter.set_cc(ir.op(op, (cc_of(), value), 8))


def exgtfr(lifter, opcode):
    param = lifter.fetch8()
    first = lifter.bound(lifter.exchange_read(param), "first")
    pc = None
    if not param & 0x80:
        lifter.mnemonic = "exg"
        second = lifter.bound(lifter.exchange_read(param >> 4), "second")
        pc = lifter.exchange_write(param, second)
    else:
        lifter.mnemonic = "tfr"
    written = lifter.exchange_write(param >> 4, first)
    pc = written if written is not None else pc
    if pc is not None:
        lifter.control = Control("jump", address=lifter.bound(pc, "pc"))


def branch(lifter, opcode):
    long = opcode & 0x08
    name = CONDITIONS[opcode & 7]
    if opcode & 0x10:
        name = OPPOSITES[name]
    lifter.mnemonic = ("lb" if long else "b") + ("ra" if name == "t" else "rn" if name == "f" else name)
    offset = lifter.fetch16() if long else lifter.fetch8()
    if long:
        offset = offset - 0x10000 if offset & 0x8000 else offset
    else:
        offset = offset - 0x100 if offset & 0x80 else offset
    target = (lifter.next_address + offset) & 0xFFFF
    if name == "t":
        lifter.control = Control("goto", target=target)
    elif name != "f":
        lifter.control = Control("branch", target=target, condition=condition_of(name))


def unary8(name):
    def handler(lifter, opcode):
        lifter.mnemonic = name
        if opcode in (0x82, 0x85, 0x88, 0x8B, 0x8E, 0x92, 0x95, 0x98, 0x9B, 0x9E, 0xA2):
            operand = lifter.indexed()
        else:
            operand = Operand("A" if opcode in (0x80, 0x83, 0x86, 0x89, 0x8C, 0x90, 0x93, 0x96, 0x99, 0x9C, 0xA0)
                              else "B")
        value = lifter.read8(operand)
        v32 = ir.zext(value, 32)
        if name == "clr":
            for flag_name, set_value in (("s", 0), ("z", 1), ("ov", 0), ("cy", 0)):
                lifter.set_flag(flag_name, ir.const(set_value, 1))
            lifter.write8(operand, const(0, 8))
            return
        if name == "tst":
            lifter.set_flags1(NZV, value, 8)
            return
        if name == "com":
            lifter.set_flag("ov", ir.const(0, 1))
            lifter.set_flag("cy", ir.const(1, 1))
            result = lifter.set_flags1(NZ, ir.op("not", (value,), 8), 8)
        elif name == "neg":
            result = lifter.set_flags(NZVC, ir.const(0, 8), value, ir.op("neg", (v32,), 32), 8)
        elif name == "inc":
            result = lifter.set_flags(NZV, value, ir.const(1, 8), ir.op("add", (v32, ir.const(1, 32)), 32), 8)
        elif name == "dec":
            result = lifter.set_flags(NZV, value, ir.const(1, 8), ir.op("sub", (v32, ir.const(1, 32)), 32), 8)
        elif name in ("lsr", "asr"):
            lifter.set_flag("cy", bit_of(value, 0))
            op = "shr" if name == "lsr" else "sar"
            result = lifter.set_flags1(NZ, ir.op(op, (value, ir.const(1, 8)), 8), 8)
        elif name == "ror":
            old = ir.zext(flag("cy"), 8)
            rotated = lifter.bound(ir.op("or", (ir.op("shr", (value, ir.const(1, 8)), 8),
                                                ir.op("shl", (old, ir.const(7, 8)), 8)), 8), "rotated")
            lifter.set_flag("cy", bit_of(value, 0))
            result = lifter.set_flags1(NZ, rotated, 8)
        elif name == "asl":
            result = lifter.set_flags(NZVC, value, value, ir.op("shl", (v32, ir.const(1, 32)), 32), 8)
        elif name == "rol":
            # rotate_left: the 9-bit value (the old carry in bit 0), C its bit 8, then set_flags with it as r
            rotated = lifter.bound(ir.op("or", (ir.op("shl", (v32, ir.const(1, 32)), 32), ir.zext(flag("cy"), 32)), 32),
                                   "rotated")
            lifter.set_flag("cy", bit_of(value, 7))
            result = lifter.set_flags(NZV, value, value, rotated, 8)
        else:
            raise ValueError(name)
        lifter.write8(operand, result)
    return handler


def unary16(name):
    def handler(lifter, opcode):
        lifter.mnemonic = name + ("d" if opcode in REGISTER_D else "w")
        operand = Operand("D") if opcode in REGISTER_D else lifter.indexed()
        if name == "clr":
            for flag_name, set_value in (("s", 0), ("z", 1), ("ov", 0), ("cy", 0)):
                lifter.set_flag(flag_name, ir.const(set_value, 1))
            lifter.write16(operand, const(0))
            return
        value = lifter.read16(operand)
        v32 = ir.zext(value, 32)
        if name == "tst":
            lifter.set_flags1(NZV, value, 16)
            return
        if name == "neg":
            result = lifter.set_flags(NZVC, const(0), value, ir.op("neg", (v32,), 32), 16)
        elif name == "inc":
            result = lifter.set_flags(NZVC, value, const(1), ir.op("add", (v32, ir.const(1, 32)), 32), 16)
        elif name == "dec":
            result = lifter.set_flags(NZVC, value, const(1), ir.op("sub", (v32, ir.const(1, 32)), 32), 16)
        elif name in ("lsr", "asr"):
            lifter.set_flag("cy", bit_of(value, 0))
            result = lifter.set_flags1(NZ, ir.op("shr" if name == "lsr" else "sar", (value, const(1)), 16), 16)
        elif name == "ror":
            old = ir.zext(flag("cy"), 16)
            rotated = lifter.bound(ir.op("or", (ir.op("shr", (value, const(1)), 16),
                                                ir.op("shl", (old, const(15)), 16)), 16), "rotated")
            lifter.set_flag("cy", bit_of(value, 0))
            result = lifter.set_flags1(NZ, rotated, 16)
        elif name == "asl":
            result = lifter.set_flags(NZVC, value, value, ir.op("shl", (v32, ir.const(1, 32)), 32), 16)
        elif name == "rol":
            # ROL16 sets its flags from the 16 bits alone (set_flags<uint16_t>(CC_NZV, rotate_left(...))): V is 0
            rotated = lifter.bound(ir.op("or", (ir.op("shl", (value, const(1)), 16), ir.zext(flag("cy"), 16)), 16),
                                   "rotated")
            lifter.set_flag("cy", bit_of(value, 15))
            result = lifter.set_flags1(NZV, rotated, 16)
        else:
            raise ValueError(name)
        lifter.write16(operand, result)
    return handler


def abs_op(lifter, opcode):
    lifter.mnemonic = {0xCC: "absa", 0xCD: "absb", 0xCE: "absd"}[opcode]
    if opcode == 0xCE:
        operand = Operand("D")
        value = lifter.read16(operand)
        bits = 16
    else:
        operand = Operand("A" if opcode == 0xCC else "B")
        value = lifter.read8(operand)
        bits = 8
    v32 = ir.zext(value, 32)
    negative = bit_of(value, bits - 1)
    r = ir.op("select", (negative, ir.op("neg", (v32,), 32), v32), 32)
    result = lifter.set_flags(NZVC, ir.const(0, bits), value, r, bits)
    if bits == 16:
        lifter.write16(operand, result)
    else:
        lifter.write8(operand, result)


def shift_by(name):
    def handler(lifter, opcode):
        lifter.mnemonic = name
        if opcode & 1 == 0:
            count = const(lifter.fetch8(), 8)
            operand = Operand("D")
        else:
            operand = lifter.indexed()
            count = lifter.temp(lifter.reg(A), "count")
        value = lifter.read16(operand)
        args = (value, count, flag("s"), flag("z"), flag("ov"), flag("cy"))
        result = lifter.temp(ir.op(f"helper:{name}", args, 16), "shifted")
        flags = [(part, lifter.temp(ir.op(f"helper:{name}_{part}", args, 1), part)) for part in ("s", "z", "ov", "cy")]
        for part, value_of in flags:
            lifter.set_flag(part, value_of)
        lifter.write16(operand, result)
    return handler


def rts(lifter, opcode):
    lifter.mnemonic = "rts"
    lifter.control = Control("return", address=lifter.pull(S, 0x80))


def rti(lifter, opcode):
    lifter.mnemonic = "rti"
    lifter.emit(ir.Effect("rti"))
    lifter.control = Control("return", address=ir.Reg(PC, 16))


def jmp_jsr(lifter, opcode):
    lifter.mnemonic = "jmp" if opcode == 0xA8 else "jsr"
    operand = lifter.indexed()
    address = operand.address
    fixed = address.value if isinstance(address, ir.Const) else None
    if opcode == 0xA8:
        lifter.control = Control("goto", target=fixed) if fixed is not None else Control("jump", address=address)
        return
    lifter.push_pc()
    lifter.control = Control("call", target=fixed) if fixed is not None else Control("call", address=address)


def bsr(lifter, opcode):
    long = opcode == 0xAB
    lifter.mnemonic = "lbsr" if long else "bsr"
    offset = lifter.fetch16() if long else lifter.fetch8()
    if long:
        offset = offset - 0x10000 if offset & 0x8000 else offset
    else:
        offset = offset - 0x100 if offset & 0x80 else offset
    target = (lifter.next_address + offset) & 0xFFFF
    lifter.push_pc()
    lifter.control = Control("call", target=target)


def count_down(lifter, opcode):
    lifter.mnemonic = "dbjnz" if opcode == 0xAC else "dxjnz"
    if opcode == 0xAC:
        value = lifter.temp(lifter.reg(B), "count")
        result = lifter.set_flags(NZV, value, ir.const(1, 8),
                                  ir.op("sub", (ir.zext(value, 32), ir.const(1, 32)), 32), 8)
        lifter.set_reg(B, result)
    else:
        value = lifter.temp(lifter.reg(X), "count")
        result = lifter.set_flags(NZV, value, const(1), ir.op("sub", (ir.zext(value, 32), ir.const(1, 32)), 32), 16)
        lifter.set_reg(X, result)
    offset = lifter.fetch8()
    target = (lifter.next_address + (offset - 0x100 if offset & 0x80 else offset)) & 0xFFFF
    lifter.control = Control("branch", target=target, condition=lnot(flag("z")))


def nop(lifter, opcode):
    lifter.mnemonic = "nop"


def abx(lifter, opcode):
    lifter.mnemonic = "abx"
    lifter.set_reg(X, ir.op("add", (lifter.reg(X), ir.zext(lifter.reg(B), 16)), 16))


def daa(lifter, opcode):
    lifter.mnemonic = "daa"
    args = (lifter.reg(A), flag("h"), flag("cy"))
    value = lifter.temp(ir.op("helper:daa", args, 8), "adjusted")
    carry = lifter.temp(ir.op("helper:daa_cy", args, 1), "carry")
    lifter.set_flag("ov", ir.const(0, 1))
    lifter.set_flag("cy", carry)
    lifter.set_reg(A, lifter.set_flags1(NZ, value, 8))


def sex(lifter, opcode):
    lifter.mnemonic = "sex"
    lifter.set_d(lifter.set_flags1(NZ, ir.sext(lifter.reg(B), 16), 16))


def mul(lifter, opcode):
    lifter.mnemonic = "mul"
    product = lifter.temp(ir.op("mul", (ir.zext(lifter.reg(A), 16), ir.zext(lifter.reg(B), 16)), 16), "product")
    lifter.set_d(lifter.set_flags1(Z, product, 16))
    lifter.set_flag("cy", bit_of(product, 7))


def lmul(lifter, opcode):
    lifter.mnemonic = "lmul"
    product = lifter.temp(ir.op("mul", (ir.zext(lifter.reg(X), 32), ir.zext(lifter.reg(Y), 32)), 32), "product")
    lifter.set_reg(X, ir.trunc(ir.op("shr", (product, ir.const(16, 32)), 32), 16))
    lifter.set_reg(Y, ir.trunc(product, 16))
    lifter.set_flag("z", ir.op("eq", (product, ir.const(0, 32)), 1))
    lifter.set_flag("cy", bit_of(product, 15))


def divx(lifter, opcode):
    lifter.mnemonic = "div"
    args = (lifter.reg(X), lifter.reg(B))
    quotient = lifter.temp(ir.op("helper:divx", args, 16), "quotient")
    remainder = lifter.temp(ir.op("helper:divx_rem", args, 8), "remainder")
    lifter.set_reg(X, lifter.set_flags1(Z, quotient, 16))
    lifter.set_reg(B, remainder)
    lifter.set_flag("cy", bit_of(quotient, 7))


def block(lifter, opcode):
    lifter.mnemonic = {0xB6: "bmove", 0xCF: "bset", 0xD0: "bsetw"}[opcode]
    lifter.emit(ir.Effect(lifter.mnemonic))


def move(lifter, opcode):
    lifter.mnemonic = "move"
    source = lifter.temp(lifter.reg(Y), "source")
    value = lifter.load(source, "moved")
    lifter.set_reg(Y, lifter.add16(source, 1))
    destination = lifter.temp(lifter.reg(X), "destination")
    lifter.store(destination, value)
    lifter.set_reg(X, lifter.add16(destination, 1))
    lifter.set_reg(U, lifter.add16(lifter.reg(U), -1))


IMMEDIATE_16 = {0x40, 0x42, 0x44, 0x46, 0x48, 0x4A, 0x4C, 0x4E, 0x50, 0x52, 0x54, 0x56}
REGISTER_D = {0xC2, 0xC4, 0xC6, 0xC8, 0xCA}

HANDLERS = {
    0x08: lea(X), 0x09: lea(Y), 0x0A: lea(U), 0x0B: lea(S),
    0x0C: push_pull(S, False), 0x0D: push_pull(U, False), 0x0E: push_pull(S, True), 0x0F: push_pull(U, True),
    0x38: setln, 0x39: setln, 0x3A: store8, 0x3B: store8, 0x3C: cc_op, 0x3D: cc_op, 0x3E: exgtfr, 0x3F: exgtfr,
    0x40: alu16("ld", "D"), 0x41: alu16("ld", "D"), 0x42: alu16("ld", X), 0x43: alu16("ld", X),
    0x44: alu16("ld", Y), 0x45: alu16("ld", Y), 0x46: alu16("ld", U), 0x47: alu16("ld", U),
    0x48: alu16("ld", S), 0x49: alu16("ld", S), 0x4A: alu16("cmp", "D"), 0x4B: alu16("cmp", "D"),
    0x4C: alu16("cmp", X), 0x4D: alu16("cmp", X), 0x4E: alu16("cmp", Y), 0x4F: alu16("cmp", Y),
    0x50: alu16("cmp", U), 0x51: alu16("cmp", U), 0x52: alu16("cmp", S), 0x53: alu16("cmp", S),
    0x54: alu16("add", "D"), 0x55: alu16("add", "D"), 0x56: alu16("sub", "D"), 0x57: alu16("sub", "D"),
    0x58: store16("D"), 0x59: store16(X), 0x5A: store16(Y), 0x5B: store16(U), 0x5C: store16(S),
    0x8F: rts, 0x9F: rti, 0xA8: jmp_jsr, 0xA9: jmp_jsr, 0xAA: bsr, 0xAB: bsr, 0xAC: count_down, 0xAD: count_down,
    0xAE: nop, 0xB0: abx, 0xB1: daa, 0xB2: sex, 0xB3: mul, 0xB4: lmul, 0xB5: divx, 0xB6: block, 0xB7: move,
    0xCC: abs_op, 0xCD: abs_op, 0xCE: abs_op, 0xCF: block, 0xD0: block,
}
for _opcode, _name in zip(range(0x10, 0x38, 4), ("ld", "add", "adc", "sub", "sbc", "and", "bit", "eor", "or", "cmp")):
    for _offset in range(4):
        HANDLERS[_opcode + _offset] = alu8(_name)
for _opcode in range(0x60, 0x80):
    HANDLERS[_opcode] = branch
for _base, _name in ((0x80, "clr"), (0x83, "com"), (0x86, "neg"), (0x89, "inc"), (0x8C, "dec"), (0x90, "tst"),
                     (0x93, "lsr"), (0x96, "ror"), (0x99, "asr"), (0x9C, "asl"), (0xA0, "rol")):
    for _offset in range(3):
        HANDLERS[_base + _offset] = unary8(_name)
for _opcode, _name in ((0xA3, "lsr"), (0xA4, "ror"), (0xA5, "asr"), (0xA6, "asl"), (0xA7, "rol"), (0xC2, "clr"),
                       (0xC3, "clr"), (0xC4, "neg"), (0xC5, "neg"), (0xC6, "inc"), (0xC7, "inc"), (0xC8, "dec"),
                       (0xC9, "dec"), (0xCA, "tst"), (0xCB, "tst")):
    HANDLERS[_opcode] = unary16(_name)
for _opcode, _name in ((0xB8, "lsrdi"), (0xBA, "rordi"), (0xBC, "asrdi"), (0xBE, "asldi"), (0xC0, "roldi")):
    HANDLERS[_opcode] = HANDLERS[_opcode + 1] = shift_by(_name)


def lift(mem, address, prefix="t"):
    """The Lifter of the instruction at 'address' (its statements, control, length and mnemonic)"""
    return Lifter(mem, address, prefix).lift()
