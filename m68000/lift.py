# license:BSD-3-Clause
# copyright-holders:Olivier Galibert, Sebastian Toyos
"""
Name:        lift.py
Description: translates decoded 68000 instructions into IR statements that do what MAME's 68000 core does
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Every instruction the disassembler decodes as valid becomes IR (v60/ir.py, the core's IR): its operands
read and written in the order MAME's core (src/devices/cpu/m68000, Olivier Galibert's, built from the
68000's microcode) makes its bus cycles, with the same widths, and every flag set the way that core sets
it. konami/reference/compare_lift_with_mame.py runs each instruction the program has on MAME's core and
on this IR (m68000/interpreter.py) from many states, and compares what they leave and the bus cycles they
make, one by one.

Registers: D0-D7 are 0-7 and A0-A7 8-15, A7 the stack pointer of the mode the CPU is in (supervisor or
user); OTHER_SP (16) is the other one, the user stack pointer while the CPU is in supervisor mode and the
supervisor's while it is in user mode, which a change of mode swaps with A7; SR_SYSTEM (17) is the system
byte of the status register in its place, SR & 0xA700 (trace, supervisor, the interrupt mask). The flags
are the decompiler's names for N Z V C, s z ov cy, and x for X.

Memory: a Load or Store of 8 or 16 bits is one bus cycle (the 68000's bus is 16 bits wide: a byte is the
word at its even address with that half's mem_mask); of 32 bits, two, the word at the address and then
the one after it. Where the core makes the two halves of a long the other way round (a long written
through -(An), pushed on the stack...), the lift writes two 16-bit Stores, the second half first. A read
the core makes only to throw the value away (CLR and Scc read their destination before they write it)
is a Load bound to a temporary nothing reads.

Effects (the runtime does them, runtime/include/m68000/ops.h; the interpreter, here):
  write_sr VALUE              the whole status register: the flags, the mask, the mode (swapping the
                              stack pointers when it changes)
  exception_if COND VECTOR PC the exception VECTOR when COND (1 bit) is true: the frame pushed on the
                              supervisor's stack (PC, the status register), the CPU in supervisor mode, on
                              to the vector's handler; the rest of the instruction does not happen
  stop VALUE                  STOP: the status register, and the CPU waits for an interrupt
  reset                       RESET: the reset line pulsed for the devices
Control is the instruction's Control (lifter.control): how execution goes on after it.

Not modeled, because the program never takes them: the address error exception (a word or a long at an odd
address, or a jump to one) and the trace exception (the trace bit set); STOP's wait and RESET's pulse are the
runtime's.
"""
from dataclasses import dataclass

from v60 import ir

from . import disassembler as dis

D0, A0 = 0, 8
SP = 15
OTHER_SP = 16
SR_SYSTEM = 17
REGISTERS = 18
SR_MASK = 0xA700
SR_S = 0x2000
BITS = {"b": 8, "w": 16, "l": 32}
FLAGS = ("x", "s", "z", "ov", "cy")         # X N Z V C
FLAG_BITS = {"x": 4, "s": 3, "z": 2, "ov": 1, "cy": 0}

# Exception vectors
VECTOR_ILLEGAL = 4
VECTOR_ZERO_DIVIDE = 5
VECTOR_CHK = 6
VECTOR_TRAPV = 7
VECTOR_PRIVILEGE = 8
VECTOR_TRAP = 32

PRIVILEGED = {"rte", "stop", "reset"}


class Unsupported(Exception):
    """An instruction or operand the lifter does not translate."""


@dataclass
class Control:
    """How execution goes on after an instruction.
    next     on to the next instruction
    goto     to 'target' (BRA, JMP to a fixed place)
    branch   to 'target' when 'condition' (1 bit) holds, else on (Bcc, DBcc)
    call     to 'target' (BSR, JSR to a fixed place) or to the computed 'address', with the return address
             pushed by the statements
    jump     to the computed 'address' (JMP through a register)
    return   to the popped 'address' (RTS, RTR, RTE: the statements popped it)
    """
    kind: str = "next"
    target: int = None
    address: object = None
    condition: object = None


def register(name):
    """'D3' -> 3, 'A3' -> 11"""
    return int(name[1]) + (A0 if name[0] == "A" else D0)


def const(value, bits=32):
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


def condition_of(name):
    """The 1-bit condition of Bcc, DBcc and Scc ('hi', 'eq'...; 't' and 'f' too), from the flags."""
    z, s, ov, cy = flag("z"), flag("s"), flag("ov"), flag("cy")
    table = {
        "t": const(1, 1), "f": const(0, 1),
        "hi": lnot(lor(cy, z)), "ls": lor(cy, z), "cc": lnot(cy), "cs": cy, "ne": lnot(z), "eq": z,
        "vc": lnot(ov), "vs": ov, "pl": lnot(s), "mi": s, "ge": lnot(lxor(s, ov)), "lt": lxor(s, ov),
        "gt": lnot(lor(lxor(s, ov), z)), "le": lor(lxor(s, ov), z),
    }
    return table[name]


def ccr_of():
    """The condition code register, X N Z V C in bits 4-0, as an 8-bit value."""
    value = None
    for name in FLAGS:
        part = ir.op("shl", (ir.zext(flag(name), 8), const(FLAG_BITS[name], 8)), 8) if FLAG_BITS[name] \
            else ir.zext(flag(name), 8)
        value = part if value is None else ir.op("or", (value, part), 8)
    return value


def sr_of():
    """The whole status register, 16 bits."""
    return ir.op("or", (ir.trunc(ir.Reg(SR_SYSTEM), 16), ir.zext(ccr_of(), 16)), 16)


class Location:
    """Where an operand is: a register (its index) or memory (an address expression); 'program' for the
    PC-relative ones, which the 68000 reads in its program space"""

    def __init__(self, register=None, address=None, program=False):
        self.register = register
        self.address = address
        self.program = program


class Lifter:
    """The IR statements of one instruction, and how execution goes on after it."""

    def __init__(self, instruction, prefix="t"):
        self.instruction = instruction
        self.statements = []
        self.prefix = prefix
        self.count = 0
        self.next_address = (instruction.address + instruction.length) & 0xFFFFFFFF
        self.control = Control()
        # the temporaries that read a PC-relative operand: the core reads those through its program space, as
        # it fetches its instructions (the board answers both alike)
        self.program_reads = set()

    def emit(self, statement):
        self.statements.append(statement)

    def temp(self, value, hint="value"):
        """Binds 'value' to a new temporary, so it is computed here, in order."""
        self.count += 1
        temp = ir.Temp(f"{self.prefix}{self.count}_{hint}", value.bits)
        self.emit(ir.SetTemp(temp, value))
        return temp

    def load(self, address, bits, hint="value", program=False):
        value = self.temp(ir.Load(address, bits), hint)
        if program:
            self.program_reads.add(value.name)
        return value

    def bound(self, value, hint="value"):
        """'value' as something that can be read twice: a constant, a temporary, or a new temporary."""
        if isinstance(value, (ir.Const, ir.Temp)):
            return value
        return self.temp(value, hint)

    # ---- registers ----

    def reg_value(self, index, bits, hint="value"):
        """The low 'bits' of a register, read now."""
        return self.temp(ir.trunc(ir.Reg(index), bits), hint)

    def set_reg(self, index, value, bits):
        """Writes the low 'bits' of a register (a data register keeps the bits above them)."""
        value = ir.trunc(value, bits) if value.bits > bits else ir.zext(value, bits)
        if bits == 32:
            self.emit(ir.SetReg(index, value))
            return
        keep = ir.op("and", (ir.Reg(index), const(~ir.MASKS[bits])), 32)
        self.emit(ir.SetReg(index, ir.op("or", (keep, ir.zext(value, 32)), 32)))

    def add_reg(self, index, amount):
        self.emit(ir.SetReg(index, ir.op("add", (ir.Reg(index), const(amount)), 32)))

    # ---- effective addresses ----

    def index_value(self, operand):
        """The index register of (d8,An,Xn) and (d8,PC,Xn), as 32 bits"""
        index = ir.Reg(register(operand.index))
        return ir.sext(ir.trunc(index, 16), 32) if operand.index_size == "w" else index

    def step(self, operand, bits):
        """How far (An)+ and -(An) move: a byte on A7 moves it by 2, to keep the stack aligned"""
        if bits == 8 and register(operand.reg) == SP:
            return 2
        return bits // 8

    def location(self, operand, bits, hint="address"):
        """Where an operand is. (An)+ and -(An) move their register here, -(An) before, (An)+ after the
        address is taken: the address comes back bound to a temporary."""
        mode = operand.mode
        if mode in ("dreg", "areg"):
            return Location(register=register(operand.reg))
        if mode == "indirect":
            return Location(address=self.temp(ir.Reg(register(operand.reg)), hint))
        if mode == "postinc":
            index = register(operand.reg)
            address = self.temp(ir.Reg(index), hint)
            self.add_reg(index, self.step(operand, bits))
            return Location(address=address)
        if mode == "predec":
            index = register(operand.reg)
            self.add_reg(index, -self.step(operand, bits))
            return Location(address=self.temp(ir.Reg(index), hint))
        if mode == "disp":
            base = ir.Reg(register(operand.reg))
            return Location(address=self.temp(ir.op("add", (base, const(operand.disp)), 32), hint))
        if mode == "index":
            base = ir.op("add", (ir.Reg(register(operand.reg)), const(operand.disp)), 32)
            return Location(address=self.temp(ir.op("add", (base, self.index_value(operand)), 32), hint))
        if mode in ("abs_short", "abs_long"):
            return Location(address=const(operand.value))
        if mode == "pc_disp":
            return Location(address=const(operand.value), program=True)
        if mode == "pc_index":
            base = const(operand.base + operand.disp)
            return Location(address=self.temp(ir.op("add", (base, self.index_value(operand)), 32), hint), program=True)
        raise Unsupported(f"operand {operand.text} ({mode})")

    def address_only(self, operand, hint="address"):
        """The address LEA, PEA, JMP and JSR take (no memory read)."""
        if operand.mode not in ("indirect", "disp", "index", "abs_short", "abs_long", "pc_disp", "pc_index"):
            raise Unsupported(f"no address for {operand.text}")
        return self.location(operand, 32, hint).address

    def read(self, location, bits, hint="value"):
        if location.register is not None:
            return self.reg_value(location.register, bits, hint)
        return self.load(location.address, bits, hint, location.program)

    def write(self, location, bits, value, low_first=False):
        """'low_first': a long goes to memory its low word first (the core's order for the instructions
        that read their destination before they write it)"""
        if location.register is not None:
            self.set_reg(location.register, value, bits)
            return
        value = ir.trunc(value, bits) if value.bits > bits else ir.zext(value, bits)
        if bits == 32 and low_first:
            self.write_long_low_first(location.address, value)
        else:
            self.emit(ir.Store(location.address, value, bits))

    def write_long_low_first(self, address, value):
        """A long written its low word first, then its high word (the core's order for -(An))"""
        value = self.bound(value)
        address = self.bound(address, "address")
        self.emit(ir.Store(ir.op("add", (address, const(2)), 32), ir.trunc(value, 16), 16))
        self.emit(ir.Store(address, ir.trunc(ir.op("shr", (value, const(16, 8)), 32), 16), 16))

    def source(self, operand, bits, hint="source"):
        """The value of a source operand"""
        if operand.mode in ("immediate", "quick"):
            return const(operand.value, bits)
        return self.read(self.location(operand, bits), bits, hint)

    def destination_write(self, operand, bits, value):
        """A plain write (MOVE's destination): -(An) writes a long its low word first"""
        location = self.location(operand, bits)
        if bits == 32 and operand.mode == "predec":
            self.write_long_low_first(location.address, value)
        else:
            self.write(location, bits, value)

    # ---- flags ----

    def set_flag(self, name, value):
        self.emit(ir.SetFlag(name, value))

    def set_nz(self, result):
        bits = result.bits
        self.set_flag("s", ir.bit(result, bits - 1))
        self.set_flag("z", ir.op("eq", (result, const(0, bits)), 1))

    def logic_flags(self, result):
        self.set_nz(result)
        self.set_flag("ov", const(0, 1))
        self.set_flag("cy", const(0, 1))

    def add_flags(self, a, b, carry, result, extend=True, keep_zero=False):
        self.set_flag("cy", ir.op("carry_add", (a, b, carry), 1))
        self.set_flag("ov", ir.op("overflow_add", (a, b, carry), 1))
        if extend:
            self.set_flag("x", flag("cy"))
        self.result_flags(result, keep_zero)

    def sub_flags(self, a, b, borrow, result, extend=True, keep_zero=False):
        self.set_flag("cy", ir.op("carry_sub", (a, b, borrow), 1))
        self.set_flag("ov", ir.op("overflow_sub", (a, b, borrow), 1))
        if extend:
            self.set_flag("x", flag("cy"))
        self.result_flags(result, keep_zero)

    def result_flags(self, result, keep_zero):
        bits = result.bits
        self.set_flag("s", ir.bit(result, bits - 1))
        zero = ir.op("eq", (result, const(0, bits)), 1)
        # ADDX, SUBX, NEGX, ABCD, SBCD, NBCD: Z is cleared by a result that is not zero, else kept
        self.set_flag("z", land(flag("z"), zero) if keep_zero else zero)

    # ---- exceptions ----

    def exception_if(self, condition, vector, pc):
        self.emit(ir.Effect("exception_if", (condition, const(vector), const(pc))))

    def privileged(self):
        """In user mode a privileged instruction takes the privilege violation exception instead"""
        user = ir.op("eq", (ir.op("and", (ir.Reg(SR_SYSTEM), const(SR_S)), 32), const(0)), 1)
        self.exception_if(user, VECTOR_PRIVILEGE, self.instruction.address)

    # ---- the stack ----

    def push_long(self, value):
        """A long pushed on the stack (BSR, JSR, PEA, LINK): A7 moves first, then the long is written, its high
        word first"""
        value = self.bound(value)
        self.add_reg(SP, -4)
        self.emit(ir.Store(ir.Reg(SP), value, 32))

    def pop(self, bits, hint="value"):
        value = self.load(ir.Reg(SP), bits, hint)
        self.add_reg(SP, 2 if bits == 8 else bits // 8)
        return value


# ---- the instructions ----

def size_bits(instruction):
    return BITS[instruction.size]


def lift_instruction(lifter, instruction):
    """Lifts 'instruction' into lifter.statements and lifter.control; raises Unsupported if it cannot"""
    mnemonic = instruction.mnemonic
    handler = HANDLERS.get(mnemonic)
    if handler is None:
        raise Unsupported(f"{instruction.address:06X}: {instruction.text}")
    if mnemonic in PRIVILEGED:
        lifter.privileged()
    handler(lifter, instruction)


def lift(instruction, prefix="t"):
    """A Lifter with the IR of one instruction"""
    lifter = Lifter(instruction, prefix)
    lift_instruction(lifter, instruction)
    return lifter


# ---- moves ----

def do_move(lifter, instruction):
    source, destination = instruction.operands
    if source.mode == "sr" or destination.mode in ("sr", "ccr") or "usp" in (source.mode, destination.mode):
        return do_move_special(lifter, instruction)
    bits = size_bits(instruction)
    value = lifter.source(source, bits)
    location = lifter.location(destination, bits)
    lifter.logic_flags(value)
    if destination.mode == "predec" and bits == 32:
        lifter.write_long_low_first(location.address, value)
    else:
        lifter.write(location, bits, value)


def do_move_special(lifter, instruction):
    source, destination = instruction.operands
    if destination.mode == "ccr":
        value = lifter.source(source, 16)
        for name in FLAGS:
            lifter.set_flag(name, ir.bit(value, FLAG_BITS[name]))
        return
    if destination.mode == "sr":
        lifter.privileged()
        value = lifter.source(source, 16)
        lifter.emit(ir.Effect("write_sr", (value,)))
        return
    if source.mode == "sr":
        location = lifter.location(destination, 16)
        if location.address is not None:
            lifter.load(location.address, 16, "unused")    # (the core reads the destination first)
        lifter.write(location, 16, lifter.temp(sr_of(), "sr"))
        return
    lifter.privileged()
    if source.mode == "usp":
        lifter.set_reg(register(destination.reg), ir.Reg(OTHER_SP), 32)
    else:
        lifter.set_reg(OTHER_SP, lifter.reg_value(register(source.reg), 32), 32)


def do_movea(lifter, instruction):
    source, destination = instruction.operands
    bits = size_bits(instruction)
    value = lifter.source(source, bits)
    lifter.set_reg(register(destination.reg), ir.sext(value, 32), 32)


def do_moveq(lifter, instruction):
    source, destination = instruction.operands
    value = const(source.value, 32)
    lifter.logic_flags(value)
    lifter.set_reg(register(destination.reg), value, 32)


def do_lea(lifter, instruction):
    source, destination = instruction.operands
    address = lifter.address_only(source)
    lifter.set_reg(register(destination.reg), address, 32)


def do_pea(lifter, instruction):
    address = lifter.bound(lifter.address_only(instruction.operands[0]), "address")
    lifter.push_long(address)


def do_clr(lifter, instruction):
    bits = size_bits(instruction)
    location = lifter.location(instruction.operands[0], bits)
    if location.address is not None:
        lifter.load(location.address, bits, "unused")    # (the core reads the destination first)
    zero = const(0, bits)
    lifter.set_flag("s", const(0, 1))
    lifter.set_flag("z", const(1, 1))
    lifter.set_flag("ov", const(0, 1))
    lifter.set_flag("cy", const(0, 1))
    lifter.write(location, bits, zero, low_first=True)


def do_exg(lifter, instruction):
    first, second = (register(operand.reg) for operand in instruction.operands)
    a = lifter.reg_value(first, 32, "first")
    b = lifter.reg_value(second, 32, "second")
    lifter.set_reg(first, b, 32)
    lifter.set_reg(second, a, 32)


def do_swap(lifter, instruction):
    index = register(instruction.operands[0].reg)
    value = lifter.reg_value(index, 32)
    swapped = ir.op("or", (ir.op("shl", (value, const(16, 8)), 32), ir.op("shr", (value, const(16, 8)), 32)), 32)
    result = lifter.temp(swapped, "result")
    lifter.logic_flags(result)
    lifter.set_reg(index, result, 32)


def do_ext(lifter, instruction):
    index = register(instruction.operands[0].reg)
    bits = size_bits(instruction)
    value = lifter.reg_value(index, bits // 2)
    result = lifter.temp(ir.sext(value, bits), "result")
    lifter.logic_flags(result)
    lifter.set_reg(index, result, bits)


def do_link(lifter, instruction):
    areg, displacement = instruction.operands
    index = register(areg.reg)
    value = lifter.reg_value(index, 32)
    lifter.push_long(value)
    lifter.set_reg(index, ir.Reg(SP), 32)
    lifter.add_reg(SP, displacement.value)


def do_unlk(lifter, instruction):
    index = register(instruction.operands[0].reg)
    lifter.set_reg(SP, ir.Reg(index), 32)
    value = lifter.pop(32)
    lifter.set_reg(index, value, 32)


def do_movem(lifter, instruction):
    first, second = instruction.operands
    bits = size_bits(instruction)
    size = bits // 8
    if first.mode == "reglist":
        registers = [register(name) for name in first.registers]
        if second.mode == "predec":
            base = register(second.reg)
            address = lifter.temp(ir.Reg(base), "address")
            for index in reversed(registers):
                address = lifter.temp(ir.op("sub", (address, const(size)), 32), "address")
                value = lifter.reg_value(index, bits)
                if bits == 32:
                    lifter.write_long_low_first(address, value)
                else:
                    lifter.emit(ir.Store(address, value, 16))
            lifter.set_reg(base, address, 32)
            return
        address = lifter.location(second, bits).address
        for position, index in enumerate(registers):
            place = ir.op("add", (address, const(position * size)), 32) if position else address
            lifter.emit(ir.Store(place, lifter.reg_value(index, bits), bits))
        return
    registers = [register(name) for name in second.registers]
    program = first.mode in ("pc_disp", "pc_index")
    if first.mode == "postinc":
        base = register(first.reg)
        address = lifter.temp(ir.Reg(base), "address")
    else:
        address = lifter.location(first, bits).address
    for position, index in enumerate(registers):
        place = ir.op("add", (address, const(position * size)), 32) if position else address
        value = lifter.load(place, bits, program=program)
        lifter.set_reg(index, ir.sext(value, 32), 32)
    # the core reads one word past the last register
    lifter.load(ir.op("add", (address, const(len(registers) * size)), 32), 16, "unused", program)
    if first.mode == "postinc":
        lifter.set_reg(base, ir.op("add", (address, const(len(registers) * size)), 32), 32)


def do_movep(lifter, instruction):
    first, second = instruction.operands
    bits = size_bits(instruction)
    count = bits // 8
    if first.mode == "dreg":
        value = lifter.reg_value(register(first.reg), 32)
        base = lifter.temp(ir.op("add", (ir.Reg(register(second.reg)), const(second.disp)), 32), "address")
        for position in range(count):
            shift = 8 * (count - 1 - position)
            byte = ir.trunc(ir.op("shr", (value, const(shift, 8)), 32), 8) if shift else ir.trunc(value, 8)
            lifter.emit(ir.Store(ir.op("add", (base, const(2 * position)), 32) if position else base, byte, 8))
        return
    base = lifter.temp(ir.op("add", (ir.Reg(register(first.reg)), const(first.disp)), 32), "address")
    value = None
    for position in range(count):
        byte = lifter.load(ir.op("add", (base, const(2 * position)), 32) if position else base, 8, "byte")
        part = ir.op("shl", (ir.zext(byte, bits), const(8 * (count - 1 - position), 8)), bits)
        value = part if value is None else ir.op("or", (value, part), bits)
    lifter.set_reg(register(second.reg), value, bits)


# ---- arithmetic ----

def operation_operands(lifter, instruction, bits):
    """(source value, destination location, destination value) of a two-operand instruction"""
    source, destination = instruction.operands
    value = lifter.source(source, bits)
    location = lifter.location(destination, bits, "address")
    current = lifter.read(location, bits, "operand")
    return value, location, current


def do_add_sub(lifter, instruction):
    mnemonic = instruction.mnemonic
    bits = size_bits(instruction)
    destination = instruction.operands[1]
    if destination.mode == "areg":
        # ADDQ, SUBQ to an address register: all 32 bits, no flags
        value = lifter.source(instruction.operands[0], bits)
        index = register(destination.reg)
        operation = "add" if mnemonic.startswith("add") else "sub"
        lifter.set_reg(index, ir.op(operation, (ir.Reg(index), const(value.value)), 32), 32)
        return
    value, location, current = operation_operands(lifter, instruction, bits)
    zero = const(0, 1)
    if mnemonic.startswith("add"):
        result = lifter.temp(ir.op("add", (current, value), bits), "result")
        lifter.add_flags(current, value, zero, result)
    else:
        result = lifter.temp(ir.op("sub", (current, value), bits), "result")
        lifter.sub_flags(current, value, zero, result)
    lifter.write(location, bits, result, low_first=True)


def do_adda_suba(lifter, instruction):
    source, destination = instruction.operands
    bits = size_bits(instruction)
    value = ir.sext(lifter.source(source, bits), 32)
    index = register(destination.reg)
    operation = "add" if instruction.mnemonic == "adda" else "sub"
    lifter.set_reg(index, ir.op(operation, (ir.Reg(index), value), 32), 32)


def do_addx_subx(lifter, instruction):
    bits = size_bits(instruction)
    source, destination = instruction.operands

    def read_operand(operand, hint):
        location = lifter.location(operand, bits)
        if location.register is not None or bits != 32:
            return location, lifter.read(location, bits, hint)
        # -(An) with longs: the core reads the low word first
        low = lifter.load(ir.op("add", (location.address, const(2)), 32), 16, "low")
        high = lifter.load(location.address, 16, "high")
        whole = ir.op("or", (ir.op("shl", (ir.zext(high, 32), const(16, 8)), 32), ir.zext(low, 32)), 32)
        return location, lifter.temp(whole, hint)

    _, value = read_operand(source, "source")
    location, current = read_operand(destination, "operand")
    extend = lifter.temp(flag("x"), "extend")
    if instruction.mnemonic == "addx":
        total = ir.op("add", (ir.op("add", (current, value), bits), ir.zext(extend, bits)), bits)
        result = lifter.temp(total, "result")
        lifter.add_flags(current, value, extend, result, keep_zero=True)
    else:
        total = ir.op("sub", (ir.op("sub", (current, value), bits), ir.zext(extend, bits)), bits)
        result = lifter.temp(total, "result")
        lifter.sub_flags(current, value, extend, result, keep_zero=True)
    lifter.write(location, bits, result, low_first=True)


def do_neg(lifter, instruction):
    bits = size_bits(instruction)
    location = lifter.location(instruction.operands[0], bits)
    value = lifter.read(location, bits, "operand")
    zero = const(0, bits)
    if instruction.mnemonic == "neg":
        result = lifter.temp(ir.op("neg", (value,), bits), "result")
        lifter.sub_flags(zero, value, const(0, 1), result)
    else:
        extend = lifter.temp(flag("x"), "extend")
        total = ir.op("sub", (ir.op("neg", (value,), bits), ir.zext(extend, bits)), bits)
        result = lifter.temp(total, "result")
        lifter.sub_flags(zero, value, extend, result, keep_zero=True)
    lifter.write(location, bits, result, low_first=True)


def do_cmp(lifter, instruction):
    bits = size_bits(instruction)
    source, destination = instruction.operands
    value = lifter.source(source, bits)
    if instruction.mnemonic == "cmpa":
        value = lifter.bound(ir.sext(value, 32), "source")
        current = lifter.reg_value(register(destination.reg), 32, "operand")
        bits = 32
    else:
        current = lifter.source(destination, bits, "operand")
    difference = lifter.temp(ir.op("sub", (current, value), bits), "difference")
    lifter.sub_flags(current, value, const(0, 1), difference, extend=False)


def do_tst(lifter, instruction):
    bits = size_bits(instruction)
    value = lifter.source(instruction.operands[0], bits)
    lifter.logic_flags(value)


def do_mul(lifter, instruction):
    source, destination = instruction.operands
    value = lifter.source(source, 16)
    index = register(destination.reg)
    current = lifter.reg_value(index, 16, "operand")
    extend = ir.sext if instruction.mnemonic == "muls" else ir.zext
    result = lifter.temp(ir.op("mul", (extend(current, 32), extend(value, 32)), 32), "result")
    lifter.logic_flags(result)
    lifter.set_reg(index, result, 32)


def do_div(lifter, instruction):
    source, destination = instruction.operands
    divisor = lifter.source(source, 16, "divisor")
    index = register(destination.reg)
    dividend = lifter.reg_value(index, 32, "dividend")
    name = instruction.mnemonic
    args = (dividend, divisor)
    lifter.set_flag("s", ir.op(f"helper:{name}_s", args, 1))
    lifter.set_flag("z", ir.op(f"helper:{name}_z", args, 1))
    lifter.set_flag("ov", ir.op(f"helper:{name}_ov", args, 1))
    lifter.set_flag("cy", const(0, 1))
    lifter.exception_if(ir.op("eq", (divisor, const(0, 16)), 1), VECTOR_ZERO_DIVIDE, lifter.next_address)
    lifter.set_reg(index, ir.op(f"helper:{name}", args, 32), 32)


def do_bcd(lifter, instruction):
    """ABCD, SBCD (Dy, Dx or -(Ay), -(Ax)) and NBCD <ea>: helpers of the core's ALU (alu_abcd8, alu_sbcd8)"""
    name = instruction.mnemonic
    if name == "nbcd":
        location = lifter.location(instruction.operands[0], 8)
        value = lifter.read(location, 8, "operand")
        current = const(0, 8)
        operation = "sbcd"
    else:
        source, destination = instruction.operands
        value = lifter.source(source, 8)
        location = lifter.location(destination, 8)
        current = lifter.read(location, 8, "operand")
        operation = name
    extend = lifter.temp(flag("x"), "extend")
    args = (current, value, extend)
    result = lifter.temp(ir.op(f"helper:{operation}", args, 8), "result")
    lifter.set_flag("cy", ir.op(f"helper:{operation}_cy", args, 1))
    lifter.set_flag("ov", ir.op(f"helper:{operation}_ov", args, 1))
    lifter.set_flag("x", flag("cy"))
    lifter.result_flags(result, keep_zero=True)
    lifter.write(location, 8, result)


def do_chk(lifter, instruction):
    source, destination = instruction.operands
    bound = lifter.source(source, 16, "bound")
    value = lifter.reg_value(register(destination.reg), 16, "operand")
    args = (value, bound)
    lifter.set_flag("s", ir.op("helper:chk_s", args, 1))
    lifter.set_flag("z", ir.op("helper:chk_z", args, 1))
    lifter.set_flag("ov", ir.op("helper:chk_ov", args, 1))
    lifter.set_flag("cy", ir.op("helper:chk_cy", args, 1))
    outside = lor(ir.op("lt", (value, const(0, 16)), 1), ir.op("lt", (bound, value), 1))
    lifter.exception_if(outside, VECTOR_CHK, lifter.next_address)


# ---- logic ----

def do_logic(lifter, instruction):
    destination = instruction.operands[1]
    if destination.mode in ("ccr", "sr"):
        return do_logic_status(lifter, instruction)
    bits = size_bits(instruction)
    operation = {"and": "and", "andi": "and", "or": "or", "ori": "or", "eor": "xor", "eori": "xor"}[instruction.mnemonic]
    value, location, current = operation_operands(lifter, instruction, bits)
    result = lifter.temp(ir.op(operation, (current, value), bits), "result")
    lifter.logic_flags(result)
    lifter.write(location, bits, result, low_first=True)


def do_logic_status(lifter, instruction):
    source, destination = instruction.operands
    operation = {"andi": "and", "ori": "or", "eori": "xor"}[instruction.mnemonic]
    if destination.mode == "ccr":
        value = ir.op(operation, (ccr_of(), const(source.value & 0xFF, 8)), 8)
        result = lifter.temp(value, "ccr")
        for name in FLAGS:
            lifter.set_flag(name, ir.bit(result, FLAG_BITS[name]))
        return
    lifter.privileged()
    value = lifter.temp(ir.op(operation, (sr_of(), const(source.value & 0xFFFF, 16)), 16), "sr")
    lifter.emit(ir.Effect("write_sr", (value,)))


def do_not(lifter, instruction):
    bits = size_bits(instruction)
    location = lifter.location(instruction.operands[0], bits)
    value = lifter.read(location, bits, "operand")
    result = lifter.temp(ir.op("not", (value,), bits), "result")
    lifter.logic_flags(result)
    lifter.write(location, bits, result, low_first=True)


# ---- shifts and rotates ----

def do_shift(lifter, instruction):
    name = instruction.mnemonic
    bits = size_bits(instruction)
    if len(instruction.operands) == 1:
        # the memory forms: a word, by one
        location = lifter.location(instruction.operands[0], 16)
        count = const(1, 8)
        bits = 16
    else:
        counter, target = instruction.operands
        location = Location(register=register(target.reg))
        if counter.mode == "quick":
            count = const(counter.value, 8)
        else:
            count = lifter.temp(ir.op("and", (ir.trunc(ir.Reg(register(counter.reg)), 8), const(63, 8)), 8), "count")
    value = lifter.read(location, bits, "operand")
    extend = lifter.temp(flag("x"), "extend")
    args = (value, count, extend)
    result = lifter.temp(ir.op(f"helper:{name}", args, bits), "result")
    carry = ir.op(f"helper:{name}_cy", args, 1)
    lifter.set_flag("cy", carry)
    if name in ("rol", "ror"):
        pass
    elif isinstance(count, ir.Const) and count.value:
        lifter.set_flag("x", flag("cy"))
    else:
        lifter.set_flag("x", ir.op("select", (ir.op("ne", (count, const(0, 8)), 1), flag("cy"), extend), 1))
    lifter.set_flag("ov", ir.op("helper:asl_ov", args, 1) if name == "asl" else const(0, 1))
    lifter.set_nz(result)
    lifter.write(location, bits, result)


# ---- bits ----

def do_bit(lifter, instruction):
    name = instruction.mnemonic
    number, target = instruction.operands
    bits = 32 if target.mode == "dreg" else 8
    if number.mode == "immediate":
        count = const(number.value & (bits - 1), 8)
    else:
        count = lifter.temp(ir.op("and", (ir.trunc(ir.Reg(register(number.reg)), 8), const(bits - 1, 8)), 8), "bit")
    if target.mode == "immediate":
        # (BTST Dn, #data: a bit of the immediate byte)
        location, value = None, const(target.value & 0xFF, 8)
    else:
        location = lifter.location(target, bits)
        value = lifter.read(location, bits, "operand")
    mask = const(1 << count.value, bits) if isinstance(count, ir.Const) \
        else lifter.temp(ir.op("shl", (const(1, bits), count), bits), "mask")
    lifter.set_flag("z", ir.op("eq", (ir.op("and", (value, mask), bits), const(0, bits)), 1))
    if name == "btst":
        return
    if name == "bchg":
        result = ir.op("xor", (value, mask), bits)
    elif name == "bclr":
        result = ir.op("and", (value, ir.op("not", (mask,), bits)), bits)
    else:
        result = ir.op("or", (value, mask), bits)
    lifter.write(location, bits, lifter.temp(result, "result"))


def do_scc(lifter, instruction):
    location = lifter.location(instruction.operands[0], 8)
    if location.address is not None:
        lifter.load(location.address, 8, "unused")    # (the core reads the destination first)
    value = ir.op("select", (condition_of(instruction.condition), const(0xFF, 8), const(0, 8)), 8)
    lifter.write(location, 8, lifter.temp(value, "value"))


def do_tas(lifter, instruction):
    location = lifter.location(instruction.operands[0], 8)
    value = lifter.read(location, 8, "operand")
    lifter.logic_flags(value)
    lifter.write(location, 8, lifter.temp(ir.op("or", (value, const(0x80, 8)), 8), "result"))


# ---- control ----

def do_bra(lifter, instruction):
    lifter.control = Control("goto", target=instruction.target & 0xFFFFFFFF)


def do_bcc(lifter, instruction):
    lifter.control = Control("branch", target=instruction.target & 0xFFFFFFFF,
                             condition=condition_of(instruction.condition))


def do_dbcc(lifter, instruction):
    counter = register(instruction.operands[0].reg)
    condition = lifter.temp(condition_of(instruction.condition), "condition")
    low = ir.trunc(ir.Reg(counter), 16)
    decremented = lifter.temp(ir.op("sub", (low, const(1, 16)), 16), "counter")
    kept = ir.op("and", (ir.Reg(counter), const(0xFFFF0000)), 32)
    new = ir.op("select", (condition, ir.Reg(counter), ir.op("or", (kept, ir.zext(decremented, 32)), 32)), 32)
    lifter.emit(ir.SetReg(counter, new))
    loops = land(lnot(condition), ir.op("ne", (decremented, const(0xFFFF, 16)), 1))
    lifter.control = Control("branch", target=instruction.target & 0xFFFFFFFF, condition=loops)


def do_bsr(lifter, instruction):
    lifter.push_long(const(lifter.next_address))
    lifter.control = Control("call", target=instruction.target & 0xFFFFFFFF, address=const(instruction.target))


def do_jsr(lifter, instruction):
    address = lifter.bound(lifter.address_only(instruction.operands[0], "target"), "target")
    lifter.push_long(const(lifter.next_address))
    target = instruction.target & 0xFFFFFFFF if instruction.target is not None else None
    lifter.control = Control("call", target=target, address=address)


def do_jmp(lifter, instruction):
    address = lifter.address_only(instruction.operands[0], "target")
    if instruction.target is not None:
        lifter.control = Control("goto", target=instruction.target & 0xFFFFFFFF)
    else:
        lifter.control = Control("jump", address=address)


def do_rts(lifter, instruction):
    address = lifter.pop(32, "return")
    lifter.control = Control("return", address=address)


def do_rtr(lifter, instruction):
    ccr = lifter.pop(16, "ccr")
    address = lifter.pop(32, "return")
    for name in FLAGS:
        lifter.set_flag(name, ir.bit(ccr, FLAG_BITS[name]))
    lifter.control = Control("return", address=address)


def do_rte(lifter, instruction):
    sr = lifter.pop(16, "sr")
    address = lifter.pop(32, "return")
    lifter.emit(ir.Effect("write_sr", (sr,)))
    lifter.control = Control("return", address=address)


def do_trap(lifter, instruction):
    lifter.exception_if(const(1, 1), VECTOR_TRAP + instruction.operands[0].value, lifter.next_address)


def do_trapv(lifter, instruction):
    lifter.exception_if(flag("ov"), VECTOR_TRAPV, lifter.next_address)


def do_stop(lifter, instruction):
    lifter.emit(ir.Effect("stop", (const(instruction.operands[0].value & 0xFFFF, 16),)))


def do_reset(lifter, instruction):
    lifter.emit(ir.Effect("reset", ()))


def do_nop(lifter, instruction):
    pass


HANDLERS = {
    "move": do_move, "movea": do_movea, "moveq": do_moveq, "lea": do_lea, "pea": do_pea, "clr": do_clr,
    "exg": do_exg, "swap": do_swap, "ext": do_ext, "link": do_link, "unlk": do_unlk, "movem": do_movem,
    "movep": do_movep,
    "add": do_add_sub, "addi": do_add_sub, "addq": do_add_sub, "sub": do_add_sub, "subi": do_add_sub,
    "subq": do_add_sub, "adda": do_adda_suba, "suba": do_adda_suba, "addx": do_addx_subx, "subx": do_addx_subx,
    "neg": do_neg, "negx": do_neg, "cmp": do_cmp, "cmpa": do_cmp, "cmpi": do_cmp, "cmpm": do_cmp, "tst": do_tst,
    "mulu": do_mul, "muls": do_mul, "divu": do_div, "divs": do_div, "abcd": do_bcd, "sbcd": do_bcd,
    "nbcd": do_bcd, "chk": do_chk,
    "and": do_logic, "andi": do_logic, "or": do_logic, "ori": do_logic, "eor": do_logic, "eori": do_logic,
    "not": do_not,
    "asl": do_shift, "asr": do_shift, "lsl": do_shift, "lsr": do_shift, "rol": do_shift, "ror": do_shift,
    "roxl": do_shift, "roxr": do_shift,
    "btst": do_bit, "bchg": do_bit, "bclr": do_bit, "bset": do_bit, "scc": do_scc, "tas": do_tas,
    "bra": do_bra, "bcc": do_bcc, "dbcc": do_dbcc, "bsr": do_bsr, "jsr": do_jsr, "jmp": do_jmp, "rts": do_rts,
    "rtr": do_rtr, "rte": do_rte, "trap": do_trap, "trapv": do_trapv, "stop": do_stop, "reset": do_reset,
    "nop": do_nop,
}
