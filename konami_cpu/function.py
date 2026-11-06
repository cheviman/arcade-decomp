# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        function.py
Description: a Konami CPU program (the 053248's, banked) split into functions, and each one as a control-flow graph
             of IR blocks for the decompiler
Version:     0.1.0
Created:     06-11-2026
Modified:    06-11-2026
Author:      Sebastian Toyos

Program takes what the static analysis finds (konami_cpu/analysis.py, symbols.txt, no traces) and splits it into
functions as the translation scaffold does (konami_cpu/translate.py): a function starts at each vector's handler, at
each target of a call, at each place a table of code leads to, at each func or code line of symbols.txt; code
reached from none of them starts one of its own; code two functions run into, long enough, is a function of its own.
A place is the program region's offset of the code (a bank's code at its bank x 2000, the fixed program's from
078000): a bank's code stays in its bank (no bank's code changes the bank: every SETLN is the fixed program's), and
the analysis says, for each call, branch or jump, which bank's code it reaches (Found.reached).

Builder lifts every instruction (konami_cpu/lift.py, through Canonical for the flags) into the decompiler's blocks
(decompiler/function.py), with the CPU's ways of calling and returning said as the C has them:
  - BSR, LBSR and JSR are calls (the return address they push is the call itself); RTS, RTI and PULLS of the PC are
    returns;
  - a jump or call through memory or a register goes where the analysis finds it can: a switch on what picks the
    entry of the table it reads (the accumulator added to the table's address, or the entry's own address), each
    case a tail call or a call of the code that entry holds, in the bank the CPU has there (a table of banks read
    with the same index gives each entry's);
  - the direct page is the constant the profile's DIRECT_PAGE says (the boot sets it once, at DIRECT_PAGE_SET, and
    nothing changes it after): <$xx is the work RAM at DIRECT_PAGE x 100 + xx;
  - D is A:B, a register of the IR (8) whose bytes A and B are (z80/pairs.py splits a function's bytes where it
    uses them apart, as the Z80's BC);
  - a word the CPU reads or writes a byte at a time, high byte first, is one access of 16 bits (big endian), and so
    is a pushed or pulled word: the stack's slots are words where the code keeps them so (decompiler/stack.py); a
    function whose stack is read across what it pushed (PUSHS Y,X; LDD #$01,S) keeps it a byte at a time;
  - the interrupt mask (the CC's E, F and I) is the runtime's: ORCC and ANDCC change it (and_mask, or_mask), a
    PULLS of the CC puts it back but where it pulls what a PUSHS of the same block pushed (the mask then is as it
    was: only the flags come back); the flags are the C's;
  - BMOVE, BSET and BSETW are loops: while U is not 0, a byte (or two) to X on;
  - the profile's FRAME_WAITS, where the program finds nothing to do until its next frame, run the frame before the
    instruction (frame_wait) and take the interrupts after it (take_interrupts), as the scaffold does; so does the
    FIRQ_POINT (firq_point before), and ANDCC and an unmatched PULLS of the CC after them.
Canonical lifts the flags as the decompiler's other CPUs do (conditions.py reads them): Z the result is 0, N its top
bit, C and V the carry_add/carry_sub and overflow_add/overflow_sub of the operands, H bit 4 of a ^ b ^ result; the
same values MAME's set_flags gives (tools/core/konami/reference/check_canonical_flags.py runs both on every
opcode).
"""
import dataclasses

from decompiler import conditions
from decompiler import cpu
from decompiler.function import Block, Function
from v60 import ir

import game_profile
import recompile

from . import analysis
from . import disassembler as dis
from . import lift

A, B, X, Y, U, S, DP, PC = range(8)
D = 8                       # A:B, the pair the decompiler keeps (A the high byte)
LEFT_CALLER = 33            # decompiler/stack.py's: set where a function returns past its caller (PULLS D,PC)
BITS = {A: 8, B: 8, X: 16, Y: 16, U: 16, S: 16, D: 16}

KONAMI = cpu.Profile(
    name="konami",
    registers=(A, B, X, Y, U, D, LEFT_CALLER),
    sp=S,
    address_bits=16,
    flags=("z", "s", "ov", "cy", "h"),
    return_address_bytes=2,
    interrupt_effects=("frame_wait", "take_interrupts"),
    result_register=None,
    register_bits={A: 8, B: 8, X: 16, Y: 16, U: 16, D: 16, LEFT_CALLER: 32},
    register_names={A: "a", B: "b", X: "x", Y: "y", U: "u", S: "s", D: "d", LEFT_CALLER: "left_caller"},
    flag_names={"z": "zero", "s": "negative", "ov": "overflow", "cy": "carry", "h": "half_carry"},
    variable_names={A: "a", B: "b", X: "x", Y: "y", U: "u", D: "d", LEFT_CALLER: "left_caller"},
    invalid_jump="invalid_jump",
    byte_order="big",
)

# the vectors: FIRQ, IRQ, SWI, NMI, RESET
VECTORS = translate_vectors = (0xFFF6, 0xFFF8, 0xFFFA, 0xFFFC, 0xFFFE)
INTERRUPT_VECTORS = (0xFFF6, 0xFFF8)
FRAME_WAITS = set(getattr(game_profile, "FRAME_WAITS", ()))
FIRQ_POINT = getattr(game_profile, "FIRQ_POINT", None)
DIRECT_PAGE = getattr(game_profile, "DIRECT_PAGE", None)
DIRECT_PAGE_SET = getattr(game_profile, "DIRECT_PAGE_SET", None)
ANDCC, ORCC, PUSHS, PULLS, PUSHU, PULLU = 0x3C, 0x3D, 0x0C, 0x0E, 0x0D, 0x0F
BLOCK_MOVES = (0xB6, 0xCF, 0xD0)     # BMOVE, BSET, BSETW
# the 6809's branch conditions as the decompiler names them (conditions.flag_expression: the V60's)
CONDITION_NAMES = {"hi": "h", "ls": "nh", "cc": "nl", "cs": "l", "ne": "ne", "eq": "e", "vc": "nv", "vs": "v",
                   "pl": "p", "mi": "n", "ge": "ge", "lt": "lt", "gt": "gt", "le": "le"}
ARITHMETIC_FLAGS = ("h", "s", "z", "ov", "cy")
MASK_BITS = 0xD0                     # the CC's E, F and I: the runtime's interrupt mask


class Unsupported(Exception):
    """Code the decompiler's frontend does not lift"""


# ---- the flags as the decompiler reads them ----

def wide(value):
    return ir.zext(value, 32) if value.bits < 32 else value


def classify(a, b, r):
    """('add' or 'sub' or 'shl', x, y, carry) for a set_flags result 'r' worked out from the operands 'a' and 'b'
    (zero-extended to 32 bits, with the carry or the borrow in), or None"""
    a32, b32 = wide(a), wide(b)
    if not isinstance(r, ir.Op) or r.bits != 32:
        return None
    if r.name in ("add", "sub"):
        left, right = r.args
        carry = None
        if isinstance(left, ir.Op) and left.name == r.name and right == ir.zext(ir.Flag("cy"), 32):
            carry = ir.Flag("cy")
            left, right = left.args
        if (left, right) == (a32, b32):
            return r.name, a, b, carry
    if r.name == "neg" and r.args == (b32,) and isinstance(a, ir.Const) and a.value == 0:
        return "sub", a, b, None
    if r.name == "shl" and r.args == (a32, ir.const(1, 32)) and a == b:
        return "shl", a, a, None
    return None


class Canonical(lift.Lifter):
    """lift.Lifter with the flags written as the decompiler's passes read them: the same values"""

    def set_flags(self, mask, a, b, r, bits):
        found = classify(a, b, r)
        if found is None:
            zero = isinstance(a, ir.Const) and a.value == 0 and isinstance(r, ir.Op) and r.name == "zext"
            if zero and r.args[0].bits == bits and set(mask) <= {"s", "z"}:
                # (AND, OR, EOR and BIT: the flags of the result alone)
                return self.set_flags1(mask, r.args[0], bits)
            return super().set_flags(mask, a, b, r, bits)
        kind, x, y, carry = found
        if kind == "shl":
            value = ir.op("shl", (x, ir.const(1, 8)), bits)
        else:
            value = ir.op(kind, (x, y), bits)
            if carry is not None:
                value = ir.op(kind, (value, ir.zext(carry, bits)), bits)
        result = self.temp(value, "result")
        carry_in = carry if carry is not None else ir.const(0, 1)
        operation = "sub" if kind == "sub" else "add"
        if "h" in mask:
            self.set_flag("h", ir.bit(ir.op("xor", (ir.op("xor", (x, y), bits), result), bits), 4))
        if "s" in mask:
            self.set_flag("s", ir.bit(result, bits - 1))
        if "z" in mask:
            self.set_flag("z", ir.op("eq", (result, ir.const(0, bits)), 1))
        if "ov" in mask:
            self.set_flag("ov", ir.op(f"overflow_{operation}", (x, y, carry_in), 1))
        if "cy" in mask:
            self.set_flag("cy", ir.op(f"carry_{operation}", (x, y, carry_in), 1))
        return result

    def set_flags1(self, mask, r, bits):
        r = self.bound(r if r.bits == bits else ir.trunc(r, bits), "r")
        if "h" in mask:
            self.set_flag("h", ir.const(0, 1))
        if "s" in mask:
            self.set_flag("s", ir.bit(r, bits - 1))
        if "z" in mask:
            self.set_flag("z", ir.op("eq", (r, ir.const(0, bits)), 1))
        if "ov" in mask:
            self.set_flag("ov", ir.const(0, 1))
        if "cy" in mask:
            self.set_flag("cy", ir.const(0, 1))
        return r

    def lift(self):
        opcode = self.mem.r8(self.address)
        count = self.mem.r8((self.address + 1) & 0xFFFF) & 0x0F
        if opcode in (0xB8, 0xBC, 0xBE) and count:
            # LSRDI, ASRDI, ASLDI by a constant: D shifted, N and Z of the result, C the last bit out (ASLDI's V as
            # the CPU's steps leave it)
            self.opcode = self.fetch8()
            self.fetch8()
            name = {0xB8: "lsrdi", 0xBC: "asrdi", 0xBE: "asldi"}[opcode]
            self.mnemonic = name
            value = self.temp(self.d_value(), "value")
            operation = {"lsrdi": "shr", "asrdi": "sar", "asldi": "shl"}[name]
            result = self.temp(ir.op(operation, (value, ir.const(count, 8)), 16), "shifted")
            if name == "asldi":
                args = (value, ir.const(count, 8), ir.Flag("s"), ir.Flag("z"), ir.Flag("ov"), ir.Flag("cy"))
                self.set_flag("ov", self.temp(ir.op("helper:asldi_ov", args, 1), "ov"))
            self.set_flag("s", ir.bit(result, 15))
            self.set_flag("z", ir.op("eq", (result, ir.const(0, 16)), 1))
            self.set_flag("cy", ir.bit(value, 16 - count if name == "asldi" else count - 1))
            self.set_d(result)
            return self
        return super().lift()


def canonical(mem, address, prefix="t"):
    return Canonical(mem, address, prefix).lift()


# ---- the IR of one instruction, rewritten for the decompiler ----

def word_of(high, low):
    """The 16-bit value the lift puts together from a high and a low byte"""
    return ir.op("or", (ir.op("shl", (ir.zext(high, 16), lift.const(8)), 16), ir.zext(low, 16)), 16)


def next_byte(first, second):
    """True if address 'second' is the one after 'first' (16 bits)"""
    if isinstance(first, ir.Const) and isinstance(second, ir.Const):
        return second.value == (first.value + 1) & 0xFFFF
    return second == ir.op("add", (first, lift.const(1)), 16)


def joined_words(statements):
    """The lift's words read or written a byte at a time, high byte first, as one access of 16 bits"""
    out = []
    index = 0
    while index < len(statements):
        first = statements[index]
        if index + 2 < len(statements) and isinstance(first, ir.SetTemp) and isinstance(first.value, ir.Load) \
                and first.value.bits == 8:
            second, third = statements[index + 1], statements[index + 2]
            if isinstance(second, ir.SetTemp) and isinstance(second.value, ir.Load) and second.value.bits == 8 \
                    and next_byte(first.value.address, second.value.address) and isinstance(third, ir.SetTemp) \
                    and third.value == word_of(first.temp, second.temp):
                out.append(ir.SetTemp(third.temp, ir.Load(first.value.address, 16)))
                index += 3
                continue
        if index + 1 < len(statements) and isinstance(first, ir.Store) and first.bits == 8:
            second = statements[index + 1]
            value = first.value
            if isinstance(second, ir.Store) and second.bits == 8 and next_byte(first.address, second.address) \
                    and isinstance(value, ir.Op) and value.name == "trunc" and isinstance(value.args[0], ir.Op) \
                    and value.args[0].name == "shr" and value.args[0].args[1] == lift.const(8) \
                    and second.value == ir.trunc(value.args[0].args[0], 8):
                out.append(ir.Store(first.address, value.args[0].args[0], 16))
                index += 2
                continue
        out.append(first)
        index += 1
    return out


def direct_page_address(node):
    """The constant address of a direct page operand (DP:byte, with DP the profile's DIRECT_PAGE), or None"""
    if isinstance(node, ir.Op) and node.name == "or" and isinstance(node.args[1], ir.Const):
        shifted = node.args[0]
        if isinstance(shifted, ir.Op) and shifted.name == "shl" and shifted.args[0] == ir.zext(ir.Reg(DP, 8), 16):
            return ir.const(DIRECT_PAGE << 8 | node.args[1].value, 16)
    return None


def pair_byte(index):
    """A or B read from D"""
    whole = ir.Reg(D, 16)
    if index == B:
        return ir.trunc(whole, 8)
    return ir.trunc(ir.op("shr", (whole, ir.const(8, 8)), 16), 8)


def rewritten(node):
    """An expression with the direct page a constant, D whole where the lift puts A and B together, and A and B the
    bytes of D"""
    if node == word_of(ir.Reg(A, 8), ir.Reg(B, 8)):
        return ir.Reg(D, 16)
    page = direct_page_address(node)
    if page is not None:
        return page
    if isinstance(node, ir.Reg):
        if node.index in (A, B):
            return pair_byte(node.index)
        if node.index == DP:
            return ir.const(DIRECT_PAGE, 8)
        return node
    if isinstance(node, ir.Load):
        address = rewritten(node.address)
        return node if address is node.address else ir.Load(address, node.bits)
    if isinstance(node, ir.Op):
        args = tuple(rewritten(arg) for arg in node.args)
        return node if all(new is old for new, old in zip(args, node.args)) else ir.Op(node.name, args, node.bits)
    return node


def set_pair_byte(index, value):
    whole = ir.Reg(D, 16)
    wide_value = ir.zext(value, 16)
    if index == B:
        return ir.SetReg(D, ir.op("or", (ir.op("and", (whole, ir.const(0xFF00, 16)), 16), wide_value), 16))
    return ir.SetReg(D, ir.op("or", (ir.op("and", (whole, ir.const(0x00FF, 16)), 16),
                                     ir.op("shl", (wide_value, ir.const(8, 8)), 16)), 16))


def rewrite_statements(statements, place, join=True):
    """The statements of one instruction for the decompiler: words joined, the direct page a constant, A and B the
    bytes of D (a write of both, as LDD does, a write of D)"""
    if join:
        statements = joined_words(statements)
    out = []
    index = 0
    while index < len(statements):
        statement = statements[index]
        if isinstance(statement, ir.SetReg) and statement.index == DP:
            if place != DIRECT_PAGE_SET:
                raise Unsupported(f"{place:06X}: the direct page is set here, not only at {DIRECT_PAGE_SET:06X}")
            index += 1
            continue
        if isinstance(statement, ir.SetFlag) and statement.name in ("e", "f", "i"):
            raise Unsupported(f"{place:06X}: the interrupt mask is written as a flag")
        if isinstance(statement, ir.SetReg) and statement.index == A and index + 1 < len(statements):
            following = statements[index + 1]
            value = statement.value
            if isinstance(following, ir.SetReg) and following.index == B and isinstance(value, ir.Op) \
                    and value.name == "trunc" and isinstance(value.args[0], ir.Op) and value.args[0].name == "shr" \
                    and value.args[0].args[1] == lift.const(8) and following.value == ir.trunc(value.args[0].args[0], 8):
                out.append(ir.SetReg(D, rewritten(value.args[0].args[0])))
                index += 2
                continue
        if isinstance(statement, (ir.SetTemp, ir.SetReg, ir.SetFlag)):
            statement.value = rewritten(statement.value)
        elif isinstance(statement, ir.Store):
            statement.address = rewritten(statement.address)
            statement.value = rewritten(statement.value)
        elif isinstance(statement, ir.Effect):
            statement.args = tuple(rewritten(arg) for arg in statement.args)
        if isinstance(statement, ir.SetReg) and statement.index in (A, B):
            statement = set_pair_byte(statement.index, statement.value)
        out.append(statement)
        index += 1
    return out


def register_value(name):
    """The IR of a register PUSHS and PULLS name (lift.PUSH_ORDER's)"""
    return ir.Reg(name, BITS[name])


# ---- the program ----

class Found(analysis.Analysis):
    """The static analysis, keeping where each call, branch and jump goes (in which bank) and the state where each
    jump or call through memory is"""

    def reset(self):
        super().reset()
        self.reached = {}           # {instruction place: {places}}
        self.computed_states = {}   # {instruction place: what the registers and the bank hold there}
        self._at = None

    def named_places(self, at, instruction, state):
        self._at = at
        super().named_places(at, instruction, state)

    def computed(self, place, instruction, state):
        self.computed_states[place] = dict(state)
        super().computed(place, instruction, state)

    def enter(self, place, state=None, kind=None, source="flow"):
        if self._at is not None and kind in ("func", "loc") and place is not None:
            self.reached.setdefault(self._at, set()).add(place)
        super().enter(place, state, kind, source)


class Placed:
    """An instruction at its place"""

    def __init__(self, place, instruction):
        self.place = place
        self.instruction = instruction
        self.address = place
        self.flow = instruction.flow
        self.length = instruction.length
        self.text = instruction.text
        self.mnemonic = instruction.mnemonic

    @property
    def bank(self):
        return analysis.bank_of(self.place)

    @property
    def cpu(self):
        return self.instruction.address & 0xFFFF

    @property
    def next_cpu(self):
        return (self.instruction.address + self.instruction.length) & 0xFFFF


class Program(recompile.Program):
    """The analysed program, split into functions for the decompiler (translate.Program's split, without the
    board's code in place of the game's: the decompiled game puts its own in place of a whole function)"""

    def __init__(self, image, symbols):
        self.image = image
        self.symbols = symbols
        self.analysis = Found(image, symbols)
        self.analysis.run()
        found = self.analysis
        # (what the decompiler's data flow asks of the V60's analysis: no stores of code addresses into slots it
        # follows, no tables of calls; the calls through tables carry their choices)
        found.pointer_writes = []
        found.table_writes = []
        found.pointer_tables = {}
        found.table_targets = {}
        self.placed = {place: Placed(place, instruction) for place, instruction in found.instructions.items()}
        self.weak = set()           # entries walked through (weaken)
        self.byte_stack = set()     # functions whose stack's slots stay bytes (a word read across two pushes)
        self.table_inner = set()    # and through tables
        fixed = found.view(None)
        self.handlers = [analysis.place_of(None, fixed.r16(vector)) for vector in VECTORS]
        entries = set(found.functions) | set(found.from_table) | set(self.handlers)
        entries |= {address for address, symbol in symbols.items() if symbol.kind in ("func", "code")}
        self.entries = {place for place in entries if place in self.placed}
        self.bodies = {entry: self.walk(entry) for entry in sorted(self.entries)}
        covered = set().union(*self.bodies.values())
        self.orphans = sorted(place for place in self.placed if place not in covered)
        while True:
            covered = set().union(*self.bodies.values())
            starts = [place for place in self.orphans if place not in covered]
            if not starts:
                break
            self.entries.add(starts[0])
            self.bodies[starts[0]] = self.walk(starts[0])
        self.base_entries = set(self.entries)
        self.split_rejected = set()
        self.weak = set()
        self.split_shared_code()
        self.names = self.choose_names()
        self.unreached = {address for address, symbol in symbols.items()
                          if symbol.kind == "func" and symbol.name.startswith(("unreached_", "unused_"))
                          and address in self.entries}

    def split_shared_code(self):
        """recompile.Program's split of code functions share, but for the starts rejected before (a shared stretch
        that runs on a stack its functions pushed: drop_split)"""
        original = set(self.entries)
        rejected = set(self.split_rejected)
        while True:
            owners = {}
            for entry, body in self.bodies.items():
                for address in body:
                    owners.setdefault(address, set()).add(entry)
            starts = set()
            for entry, body in self.bodies.items():
                for address, instruction in body.items():
                    for successor in self.successors(instruction):
                        if successor in body and len(owners[successor]) > 1 \
                                and owners[successor] != owners[address]:
                            starts.add(successor)
            starts -= rejected | self.weak
            small = {start for start in starts if len(self.walk(start)) < recompile.SHARED_MINIMUM}
            rejected |= small
            starts -= small
            if not starts:
                return
            self.entries |= starts
            self.bodies = {entry: self.walk(entry) for entry in sorted(self.entries)}
            looping = {entry for component in self.tail_call_cycles() for entry in component} - original
            if looping:
                rejected |= looping
                self.entries -= looping
                self.bodies = {entry: self.walk(entry) for entry in sorted(self.entries)}

    def weaken(self, places, tables=True, untabled=()):
        """The entries at 'places' run on a stack the code that runs or jumps into them pushed (a table's code
        that pulls what its dispatcher pushed, a stretch of a routine a table names by mistake): the functions that
        run or branch into them have their code as their own, and so do those that jump into them through a table
        ('tables'; but for 'untabled', where that does not hold the stack together either: a table's entry the
        analysis takes too far); they stay functions of their own for their calls and the other jumps"""
        self.weak |= set(places)
        if tables:
            self.table_inner |= set(places)
        self.table_inner -= set(untabled)
        self.entries = set(self.base_entries)
        self.bodies = {entry: self.walk(entry) for entry in sorted(self.entries)}
        self.split_shared_code()
        self.names = self.choose_names()

    def handlers_of_vectors(self):
        """The places of the reset's, the IRQ's and the FIRQ's handlers"""
        fixed = self.analysis.view(None)
        return tuple(analysis.place_of(None, fixed.r16(vector)) for vector in (0xFFFE, 0xFFF8, 0xFFF6))

    def drop_split(self, starts):
        """The functions at 'starts', which the split of shared code made, go back into the functions that run into
        them (each its own copy): they run on a stack those functions pushed. False if one is no such function"""
        if not set(starts) <= self.entries - self.base_entries:
            return False
        self.split_rejected |= set(starts)
        self.entries = set(self.base_entries)
        self.bodies = {entry: self.walk(entry) for entry in sorted(self.entries)}
        self.split_shared_code()
        self.names = self.choose_names()
        return True

    def place_of(self, placed, cpu, reached=True):
        """The place of a CPU address an instruction goes to: in its own bank in the window, the fixed program's, or,
        from the fixed program into the window, the one the analysis found it reaches there (one bank only: None for
        none or several)"""
        cpu &= 0xFFFF
        if cpu >= analysis.FIXED_CPU:
            return analysis.place_of(None, cpu)
        if cpu >= analysis.WINDOW and placed.bank is not None:
            return analysis.place_of(placed.bank, cpu)
        if cpu >= analysis.WINDOW and reached:
            found = {place for place in self.analysis.reached.get(placed.place, ())
                     if analysis.cpu_of(place) == cpu and place < analysis.FIXED_START}
            if len(found) == 1:
                return found.pop()
        return None

    def successors(self, placed):
        flow = placed.flow
        following = []
        if flow in (dis.FLOW_NORMAL, dis.FLOW_BRANCH, dis.FLOW_CALL):
            following.append(placed.place + placed.length)
        if flow in (dis.FLOW_BRANCH, dis.FLOW_JUMP) and placed.instruction.target is not None:
            target = self.place_of(placed, placed.instruction.target)
            if target is not None:
                following.append(target)
        if flow == dis.FLOW_JUMP and placed.instruction.target is None:
            # (through a table: the weak entries it goes to are the function's own code)
            following += sorted(place for place in self.analysis.reached.get(placed.place, ())
                                if place in self.table_inner)
        return following

    def walk(self, entry):
        body = {}
        stack = [entry]
        while stack:
            place = stack.pop()
            if place in body or (place != entry and place in self.entries and place not in self.weak):
                continue
            placed = self.placed.get(place)
            if placed is None:
                raise SystemExit(f"No decoded instruction at {place:06X} (function {entry:06X})")
            body[place] = placed
            stack += self.successors(placed)
        return body

    def choose_names(self):
        named = {address: symbol.name for address, symbol in self.symbols.items() if symbol.kind in ("func", "code")}
        names, used = {}, set()
        for entry in sorted(self.entries):
            name = named.get(entry, f"sub_{entry:06X}")
            if name in used:
                name = f"{name}_{entry:06X}"
            used.add(name)
            names[entry] = name
        return names


# ---- jumps and calls through memory or a register ----

def signed_range(scale, wide_index):
    """The entry numbers an index register times 'scale' can pick, as the CPU adds it signed"""
    if wide_index:
        return range(-0x8000 // scale, 0x8000 // scale)
    return range(-0x80 // scale, 0x80 // scale + (1 if 0x80 % scale else 0))


class Cases:
    """Where a jump or call through memory or a register goes: 'subject', the IR of the value that picks the case,
    'signed' whether its cases are written as signed numbers, and {case value: target place}"""

    def __init__(self, subject, signed, targets):
        self.subject = subject
        self.signed = signed
        self.targets = targets


def case_key(subject, signed):
    """The value a switch of a jump or call through a table is on (konami_cpu/emit.py writes it): what picks the
    case, its cases signed numbers or not"""
    return ir.op("case_key", (subject, ir.const(1 if signed else 0, 1)), 16)


def narrowed_key(key, cases):
    """For a switch on the address an entry is read at, now that the decompiler knows the table's: (the switch on
    the offset the accumulator adds, its cases), or None. A case the offset cannot reach is another table's (code
    that a dispatcher shares with others, each with its own table's address)"""
    subject = key.args[0]
    if key.args[1].value or not (isinstance(subject, ir.Op) and subject.name == "add" and subject.bits == 16):
        return None
    left, right = subject.args
    base, rest = (left, right) if isinstance(left, ir.Const) else (right, left)
    if not isinstance(base, ir.Const) or isinstance(rest, ir.Const):
        return None
    if isinstance(rest, ir.Op) and rest.name == "sext" and rest.args[0].bits == 8:
        byte = rest.args[0]
        found = {}
        for value, target in cases.items():
            offset = (value - base.value) & 0xFFFF
            if offset < 0x80 or offset >= 0xFF80:
                found[offset & 0xFF] = target
        return case_key(byte, True), found
    found = {(value - base.value) & 0xFFFF: target for value, target in cases.items()}
    return case_key(rest, True), found


def narrow_cases(function):
    """The switches on a table's entries' addresses whose table the decompiler knows on the offset into it
    (narrowed_key); the blocks no case reaches any more go. True if something changed"""
    changed = False
    for block in function.blocks.values():
        end = block.end
        if isinstance(end, ir.Switch) and isinstance(end.address, ir.Op) and end.address.name == "case_key":
            found = narrowed_key(end.address, end.cases)
            if found is not None:
                end.address, end.cases = found
                changed = True
        for statement in block.statements:
            if isinstance(statement, ir.Call) and isinstance(statement.address, ir.Op) \
                    and statement.address.name == "case_key":
                found = narrowed_key(statement.address, getattr(statement, "cases", {}))
                if found is not None:
                    statement.address, statement.cases = found
                    statement.choices = tuple(sorted(set(found[1].values())))
                    changed = True
    if changed:
        reached, work = set(), [function.entry]
        while work:
            label = work.pop()
            if label not in reached:
                reached.add(label)
                work += function.blocks[label].successors()
        function.blocks = {label: block for label, block in function.blocks.items() if label in reached}
    return changed


def computed_cases(program, placed, lifter):
    """The Cases of a jump or call through memory or a register at 'placed' (lifted by 'lifter'), from what the
    analysis found there; None if the analysis found nothing there (code nothing reaches).

    Through memory ([A,X], [,X], [#n,U], [$xxxx]...): the case is the address the word is read at (the effective
    address before the read, the IR of the lift's), for each place the analysis' state lets the code read it at:
    each base an index register can hold plus each offset its accumulator can add (signed, as the CPU adds it), or
    each entry of the table a cursor runs through; the word there, in the bank the table is read in, is the target,
    in the bank the CPU has (a table of banks read with the same entry number gives each entry's). The decompiler
    turns a switch on base + offset into one on the offset once it knows the base (narrow_cases). To what a register
    holds (JMP ,X): a case for each code address it can hold."""
    found = program.analysis
    reached = {place for place in found.reached.get(placed.place, ()) if place in program.entries}
    if not reached:
        return None
    state = found.computed_states.get(placed.place, {})
    operand = placed.instruction.operands[0]
    bank = placed.bank
    banks = analysis.banks_in(state)
    where = f"{placed.place:06X}: {placed.text}"

    if not (operand.mode == "indexed" and operand.indirect):
        targets = {analysis.cpu_of(place): place for place in reached}
        if len(targets) != len(reached):
            raise Unsupported(f"{where} reaches code of several banks at one address")
        return Cases(lifter.control.address, False, targets)

    # the tables of banks the code read the bank from (each the same whatever the CPU's bank where it is fixed)
    tables_of_banks = {}
    for kind, value in analysis.known(state.get("bank")):
        if kind == analysis.BYTE:
            table, length, offset, table_bank = value
            key = (table, length, offset, table_bank if table < analysis.FIXED_CPU else None)
            tables_of_banks[key] = value
    if len(tables_of_banks) > 1:
        raise Unsupported(f"{where} has several tables of banks")
    banks_from = next(iter(tables_of_banks.values()), None)

    def table_bank(address):
        """The bank a word at CPU address 'address' is read in: none for the fixed program's"""
        if address >= analysis.FIXED_CPU:
            return None
        if bank is not None:
            return bank
        if len(banks) == 1 and banks[0] is not None:
            return banks[0]
        raise Unsupported(f"{where} reads its table in several banks")

    def entry_bank(number):
        """The bank the CPU has where it goes to entry 'number'"""
        if banks_from is not None:
            banks_table, banks_length, banks_offset, banks_bank = banks_from
            held = found.byte_at((banks_table + banks_length * number + banks_offset) & 0xFFFF, banks_bank)
            return None if held is None else held & 0x3F
        if bank is not None:
            return bank
        if len(banks) == 1:
            return banks[0]
        # (several banks the CPU may have, of which the analysis finds code in one only: that one's)
        window = {analysis.bank_of(place) for place in reached if place < analysis.FIXED_START}
        return window.pop() if len(window) == 1 else None

    targets = {}

    def add(address, number):
        address &= 0xFFFF
        word = found.word_at(address, table_bank(address))
        if word is None:
            return
        target = analysis.place_of(entry_bank(number), word) if word < analysis.FIXED_CPU \
            else analysis.place_of(None, word)
        if target not in reached:
            return
        if targets.get(address, target) != target:
            raise Unsupported(f"{where}: the word at {address:04X} leads to two places")
        targets[address] = target

    if operand.kind == "extended":
        add(operand.value, 0)
    else:
        held = analysis.known(state.get(operand.reg)) if operand.reg in analysis.REGISTERS else ()
        for kind, value in held:
            if operand.kind == "accumulator" and kind == analysis.CONSTANT:
                if operand.accumulator == "D":
                    for number in range(-0x40, 0x40):
                        add(value + 2 * number, number)
                    continue
                for option, amount in found.index_options(operand.accumulator, state):
                    if option == "offset":
                        add(value + amount, 0)
                    else:
                        for number in signed_range(amount, False):
                            add(value + amount * number, number)
            elif operand.kind in ("offset", "register") and kind == analysis.CONSTANT:
                add(value + (operand.disp if operand.kind == "offset" else 0), 0)
            elif operand.kind in ("offset", "register") and kind == analysis.CURSOR:
                table, length, offset, _ = value
                disp = operand.disp if operand.kind == "offset" else 0
                for number in range(-0x80, 0x80):
                    add(table + length * number + offset + disp, number)
    cases = Cases(lifter_address(lifter) if operand.kind != "extended" else ir.const(operand.value, 16), False,
                  targets)
    if set(cases.targets.values()) != reached:
        missing = sorted(reached - set(cases.targets.values()))
        raise Unsupported(f"{where}: no case for {', '.join(f'{place:06X}' for place in missing)}")
    return cases


def lifter_address(lifter):
    """The address the indirect operand's word is read at: the lift's bound effective address before the read"""
    for statement in lifter.statements:
        if isinstance(statement, ir.SetTemp) and statement.temp.name.endswith("_ea"):
            return statement.value
    raise Unsupported("no effective address")


# ---- the blocks ----

class Builder:
    """Lifts the functions of a Program"""

    def __init__(self, program):
        self.program = program
        self.analysis = program.analysis
        self.entries = program.entries
        self.byte_stack = program.byte_stack    # functions whose stack stays a byte at a time

    def build(self, entry):
        body = self.program.bodies[entry]
        function = Function(entry, self.program.names[entry])
        self.extra_blocks = []
        self.function = function
        self.bytes = entry in self.byte_stack
        leaders = self.leaders(entry, body)
        order = sorted(body)
        self.matched_cc = self.cc_pairs(order, body, leaders)
        current = None
        for position, place in enumerate(order):
            placed = body[place]
            if current is not None and place in leaders:
                if current.end is None:
                    current.end = self.transfer(entry, place)
                current = None
            if current is None:
                current = Block(place)
                function.blocks[place] = current
            current.addresses.append(place)
            before, extra = len(current.statements), len(self.extra_blocks)
            self.lift(function, current, placed)
            for statement in current.statements[before:]:
                statement.origin = place
            for block in self.extra_blocks[extra:]:
                for statement in block.statements:
                    statement.origin = place
                if block.end is not None and not hasattr(block.end, "origin"):
                    block.end.origin = place
            if current.end is not None and not hasattr(current.end, "origin"):
                current.end.origin = place
            following = place + placed.length
            if current.end is None and (position + 1 >= len(order) or order[position + 1] != following):
                current.end = self.transfer(entry, following)
                current.end.origin = place
            if current.end is not None:
                current = None
        for block in self.extra_blocks:
            function.blocks[block.label] = block
        stack_pointers(function)
        self.close_transfers(function)
        reached, work = set(), [entry]
        while work:
            label = work.pop()
            if label not in reached:
                reached.add(label)
                work += function.blocks[label].successors()
        function.blocks = {entry: function.blocks[entry],
                           **{label: block for label, block in function.blocks.items()
                              if label != entry and label in reached}}
        return function

    def leaders(self, entry, body):
        leaders = {entry}
        for place, placed in body.items():
            following = place + placed.length
            for target in self.program.successors(placed):
                if target != following:
                    leaders.add(target)
            if placed.flow in (dis.FLOW_BRANCH, dis.FLOW_JUMP, dis.FLOW_RETURN, dis.FLOW_CALL):
                leaders.add(following)
            opcode = self.analysis.view(placed.bank).r8(placed.cpu)
            if opcode in BLOCK_MOVES:
                leaders.add(following)
        return leaders & set(body)

    def cc_pairs(self, order, body, leaders):
        """The PUSHS and PULLS of the CC that go together: in one block, the PULLS pulling the byte the PUSHS pushed,
        with no call, nothing that changes the mask and no other way of moving S between them. {place of either}"""
        matched = set()
        pending = {}
        depth = 0
        for place in order:
            placed = body[place]
            if place in leaders:
                pending, depth = {}, 0
            opcode = self.analysis.view(placed.bank).r8(placed.cpu)
            operands = placed.instruction.operands
            if opcode in (PUSHS, PULLS):
                registers = operands[0].registers
                size = sum(2 if name in ("PC", "U", "S", "X", "Y", "D") else 1 for name in registers)
                if opcode == PUSHS:
                    depth -= size
                    if "CC" in registers:
                        pending[depth] = place
                else:
                    if "CC" in registers and depth in pending:
                        matched |= {pending.pop(depth), place}
                    depth += size
                    if "PC" in registers:
                        pending = {}
            elif opcode == 0x0B and operands[0].kind in ("offset", "register") and operands[0].reg == "S" \
                    and not operands[0].indirect:
                depth += operands[0].disp
            elif placed.flow != dis.FLOW_NORMAL or opcode in (ANDCC, ORCC) \
                    or any(op.mode == "registers" and "S" in (op.source, op.destination) for op in operands) \
                    or placed.mnemonic in ("lds", "sts"):
                pending = {}
        return matched

    def transfer(self, entry, target):
        """How execution gets to 'target' (a place): a jump in this function, or into another one"""
        if target != entry and target in self.entries and target not in self.program.weak:
            return ir.TailCall(target)
        return ir.Goto(target)

    def close_transfers(self, function):
        """Branch and switch targets in other functions get a block of their own with the tail call"""
        def resolve(target, origin):
            if target in function.blocks:
                return target
            if target in self.entries:
                label = ("tail", target)
                if label not in function.blocks:
                    function.blocks[label] = Block(label, end=ir.TailCall(target))
                    function.blocks[label].end.origin = origin
                return label
            raise Unsupported(f"function {function.entry:06X}: no code at {target!r}")

        for block in list(function.blocks.values()):
            end = block.end
            origin = block.addresses[-1] if block.addresses else getattr(end, "origin", None)
            if isinstance(end, ir.Goto):
                end.target = resolve(end.target, origin)
            elif isinstance(end, ir.Branch):
                end.target = resolve(end.target, origin)
                end.otherwise = resolve(end.otherwise, origin)
            elif isinstance(end, ir.Switch):
                end.cases = {case: resolve(target, origin) for case, target in end.cases.items()}

    # ---- one instruction ----

    def target(self, placed, cpu):
        place = self.program.place_of(placed, cpu)
        if place is None:
            raise Unsupported(f"{placed.place:06X}: {placed.text} goes to {cpu:04X} in no single bank")
        return place

    def lift(self, function, block, placed):
        place = placed.place
        statements = block.statements
        view = self.analysis.view(placed.bank)
        opcode = view.r8(placed.cpu)
        following = place + placed.length
        if place in FRAME_WAITS:
            statements.append(ir.Effect("frame_wait", (ir.const(placed.cpu, 16),)))
        if place == FIRQ_POINT:
            statements.append(ir.Effect("firq_point", ()))
        interrupts = place in FRAME_WAITS or place == FIRQ_POINT
        try:
            lifter = canonical(view, placed.cpu, prefix=f"t{place:06X}_")
        except lift.Unsupported as problem:
            raise Unsupported(f"{place:06X}: {problem}") from problem
        if lifter.length != placed.length:
            raise Unsupported(f"{place:06X}: the lift reads {lifter.length} bytes, the decoder {placed.length}")
        control = lifter.control

        if opcode in (PUSHS, PULLS):
            ended = self.stack_registers(block, placed, opcode, lifter)
            if ended:
                return
        elif opcode in (ANDCC, ORCC):
            self.condition_codes(statements, opcode, view.r8((placed.cpu + 1) & 0xFFFF))
            if opcode == ANDCC:
                statements.append(ir.Effect("take_interrupts", ()))
            return
        elif opcode in BLOCK_MOVES:
            self.block_move(block, placed, opcode)
            return
        elif opcode == 0x9F:    # RTI
            block.end = ir.Return()
            return
        elif control.kind == "call":
            lifted = [statement for statement in lifter.statements if not mentions_stack(statement)]
            statements += rewrite_statements(lifted, place, self.joins(placed))
            if control.target is not None:
                statements.append(ir.Call(target=self.target(placed, control.target), return_address=following))
            else:
                cases = computed_cases(self.program, placed, lifter)
                if cases is None:
                    raise Unsupported(f"{place:06X}: {placed.text} reaches no code")
                call = ir.Call(address=case_key(rewritten(cases.subject), cases.signed),
                               choices=tuple(sorted(set(cases.targets.values()))), return_address=following)
                call.cases = dict(cases.targets)
                call.signed_cases = cases.signed
                statements.append(call)
            block.end = ir.Goto(following)
            return
        elif control.kind == "return":
            # RTS: the pull of the PC is the return (PULLS of the PC is stack_registers')
            block.end = ir.Return()
            return
        else:
            statements += rewrite_statements(list(lifter.statements), place, self.joins(placed))

        if control.kind == "goto":
            if interrupts:
                statements.append(ir.Effect("take_interrupts", ()))
            block.end = self.transfer(function.entry, self.target(placed, control.target))
        elif control.kind == "branch":
            if interrupts:
                statements.append(ir.Effect("take_interrupts", ()))
            block.end = ir.Branch(self.condition(placed, control), self.target(placed, control.target), following)
        elif control.kind == "jump":
            cases = computed_cases(self.program, placed, lifter)
            if cases is None:
                # (a jump through a table that holds no code addresses, in code nothing reaches: symbols.txt's
                # calls= of a count=0 table): the program cannot go on there
                statements.append(ir.Effect("invalid_jump", (rewritten(control.address),)))
                block.end = ir.Return()
            elif len(set(cases.targets.values())) == 1 and not cases.signed and isinstance(cases.subject, ir.Const):
                block.end = ir.TailCall(next(iter(cases.targets.values())))
            else:
                block.end = ir.Switch(case_key(rewritten(cases.subject), cases.signed), dict(cases.targets))
                block.end.signed_cases = cases.signed
        elif interrupts:
            statements.append(ir.Effect("take_interrupts", ()))

    def joins(self, placed):
        """Whether the words an instruction reads or writes a byte at a time are one access: not where it reaches the
        stack in a function whose stack stays a byte at a time"""
        return not self.bytes or not any(operand.mode == "indexed" and operand.reg == "S"
                                         for operand in placed.instruction.operands)

    def condition(self, placed, control):
        """A branch's condition as the decompiler names it (conditions.flag_expression)"""
        mnemonic = placed.mnemonic
        if mnemonic in ("dbjnz", "dxjnz"):
            return conditions.flag_expression("ne")
        name = placed.instruction.condition
        if name not in CONDITION_NAMES:
            raise Unsupported(f"{placed.place:06X}: {placed.text}: condition {name}")
        return conditions.flag_expression(CONDITION_NAMES[name])

    def condition_codes(self, statements, opcode, value):
        """ANDCC and ORCC: the flags they clear or set, and the interrupt mask's bits (the runtime's)"""
        for name in ARITHMETIC_FLAGS:
            bit = value >> lift.FLAG_BITS[name] & 1
            if opcode == ANDCC and not bit:
                statements.append(ir.SetFlag(name, ir.const(0, 1)))
            elif opcode == ORCC and bit:
                statements.append(ir.SetFlag(name, ir.const(1, 1)))
        if opcode == ANDCC and (~value & MASK_BITS):
            statements.append(ir.Effect("and_mask", (ir.const(value | ~MASK_BITS & 0xFF, 8),)))
        elif opcode == ORCC and value & MASK_BITS:
            statements.append(ir.Effect("or_mask", (ir.const(value & MASK_BITS, 8),)))

    def stack_registers(self, block, placed, opcode, lifter):
        """PUSHS and PULLS: the registers to and from the stack's slots, a word for a 16-bit register (and for D, A
        and B pushed together), a byte for the others (the function's stack a byte at a time: each byte). True if
        the instruction ended the block (a PULLS of the PC returns)"""
        statements = block.statements
        place = placed.place
        mask = self.analysis.view(placed.bank).r8((placed.cpu + 1) & 0xFFFF)
        names = [name for bit, name in lift.PUSH_ORDER if mask & bit]
        items = []    # (name, bytes) in the order they are pushed
        for name in names:
            if name == lift.B and lift.A in names and not self.bytes:
                items.append(("D", 2))
            elif name == lift.A and lift.B in names and not self.bytes:
                continue
            else:
                items.append((name, 2 if name in ("PC", "other", lift.X, lift.Y) else 1))
        total = sum(size for _, size in items)
        sp = ir.Reg(S, 16)
        matched = place in self.matched_cc
        if opcode == PUSHS:
            down = 0
            for name, size in items:
                down += size
                if name == "PC":
                    value = ir.const(placed.next_cpu, 16)
                elif name == "other":
                    value = ir.Reg(U, 16)
                elif name == "D":
                    value = ir.Reg(D, 16)
                elif name == lift.DP:
                    value = ir.const(DIRECT_PAGE, 8)
                elif name == "CC":
                    value = self.cc_value(matched)
                else:
                    value = rewritten(ir.Reg(name, lift.REGISTER_BITS[name]))
                address = ir.op("add", (sp, ir.const(-down & 0xFFFF, 16)), 16)
                for offset, part, bits in self.parts(value, size):
                    statements.append(ir.Store(ir.op("add", (sp, ir.const(-down + offset & 0xFFFF, 16)), 16)
                                               if offset else address, part, bits))
            statements.append(ir.SetReg(S, ir.op("add", (sp, ir.const(-total & 0xFFFF, 16)), 16)))
            return False
        up = 0
        returned = False
        for name, size in reversed(items):
            address = ir.op("add", (sp, ir.const(up, 16)), 16) if up else sp
            if name == "PC":
                returned = True    # (the return: the slots stop at the return address, decompiler/stack.py's)
                break
            elif name == lift.DP:
                pass
            else:
                loaded = self.load_parts(statements, placed, address, size, sp, up)
                if name == "CC":
                    self.restore_cc(statements, loaded, matched)
                elif name == "D":
                    statements.append(ir.SetReg(D, loaded))
                elif name == "other":
                    statements.append(ir.SetReg(U, loaded))
                elif name in (lift.A, lift.B):
                    statements.append(set_pair_byte(name, loaded))
                else:
                    statements.append(ir.SetReg(name, loaded))
            up += size
        statements.append(ir.SetReg(S, ir.op("add", (sp, ir.const(up, 16)), 16)))
        if returned:
            block.end = ir.Return()
            return True
        if "CC" in names and not matched:
            statements.append(ir.Effect("take_interrupts", ()))
        return False

    def parts(self, value, size):
        """[(offset, value, bits)] of a value pushed: one word, or its bytes (a byte-at-a-time stack)"""
        if size == 1 or not self.bytes:
            return [(0, value, 8 * size)]
        return [(0, ir.trunc(ir.op("shr", (value, lift.const(8)), 16), 8), 8), (1, ir.trunc(value, 8), 8)]

    def load_parts(self, statements, placed, address, size, sp, up):
        """A pulled value: bound to a temporary (a word, or its two bytes put together)"""
        name = f"t{placed.place:06X}_pull{up}"
        if size == 1 or not self.bytes:
            temp = ir.Temp(name, 8 * size)
            statements.append(ir.SetTemp(temp, ir.Load(address, 8 * size)))
            return temp
        high = ir.Temp(name + "_high", 8)
        low = ir.Temp(name + "_low", 8)
        statements.append(ir.SetTemp(high, ir.Load(address, 8)))
        statements.append(ir.SetTemp(low, ir.Load(ir.op("add", (sp, ir.const(up + 1, 16)), 16), 8)))
        return word_of(high, low)

    def cc_value(self, matched):
        """The CC byte a PUSHS pushes: the flags, and the interrupt mask but where its PULLS leaves the mask alone"""
        value = None
        for name in ARITHMETIC_FLAGS:
            part = ir.op("shl", (ir.zext(ir.Flag(name), 8), ir.const(lift.FLAG_BITS[name], 8)), 8) \
                if lift.FLAG_BITS[name] else ir.zext(ir.Flag(name), 8)
            value = part if value is None else ir.op("or", (value, part), 8)
        if not matched:
            value = ir.op("or", (value, ir.op("helper:interrupt_mask", (), 8)), 8)
        return value

    def restore_cc(self, statements, loaded, matched):
        for name in ARITHMETIC_FLAGS:
            statements.append(ir.SetFlag(name, ir.bit(loaded, lift.FLAG_BITS[name])))
        if not matched:
            statements.append(ir.Effect("restore_mask", (loaded,)))

    def block_move(self, block, placed, opcode):
        """BMOVE (a byte from Y on to X on), BSET (A to X on) and BSETW (A and B to X on): while U is not 0, one step,
        U down by one"""
        place = placed.place
        following = place + placed.length
        check, step = ("move", place), ("step", place)
        statements = []
        x, y, u = ir.Reg(X, 16), ir.Reg(Y, 16), ir.Reg(U, 16)
        one = lift.const(1)
        if opcode == 0xB6:
            moved = ir.Temp(f"t{place:06X}_moved", 8)
            statements += [ir.SetTemp(moved, ir.Load(y, 8)), ir.Store(x, moved, 8),
                           ir.SetReg(Y, ir.op("add", (y, one), 16))]
        else:
            statements.append(ir.Store(x, pair_byte(A), 8))
            if opcode == 0xD0:
                statements.append(ir.Store(ir.op("add", (x, one), 16), pair_byte(B), 8))
        statements.append(ir.SetReg(X, ir.op("add", (x, lift.const(2 if opcode == 0xD0 else 1)), 16)))
        statements.append(ir.SetReg(U, ir.op("sub", (u, one), 16)))
        self.extra_blocks.append(Block(check, [], ir.Branch(ir.op("ne", (u, lift.const(0)), 1), step, following)))
        self.extra_blocks.append(Block(step, statements, ir.Goto(check)))
        block.end = ir.Goto(check)


def mentions_stack(statement):
    """True for a statement of the push of a call's return address (what reads or writes S)"""
    return any(isinstance(node, ir.Reg) and node.index == S
               for expression in ir.statement_expressions(statement) for node in ir.walk(expression)) \
        or (isinstance(statement, ir.SetReg) and statement.index == S) \
        or (isinstance(statement, ir.SetTemp) and statement.temp.name.endswith("_sp")) \
        or (isinstance(statement, ir.Store) and isinstance(statement.address, ir.Temp)
            and statement.address.name.endswith("_sp"))


def stack_pointers(function):
    """The stack's places as decompiler/stack.py follows them: S plus a constant where an access reads or writes it.
    A temporary that holds a place of the stack (",S": the lift binds S's value to one, then reads through it) is
    that place where the instruction reads or writes through it; an index register a block sets to a place of the
    stack (LEAU #$00,S) is that place where the block reads it later (LDB #$04,U: the byte a PUSHS pushed), however S
    moved in between. The register itself keeps the place only as a value nothing should read (stack.py makes it a
    stack_address, which the C writer refuses)"""
    for block in function.blocks.values():
        delta = 0           # where S is, from where it was when the block started
        places = {}         # temporary -> the place of the stack it holds, from S where the block started
        holding = {}        # register -> the place of the stack it holds
        known = True

        def offset_of(node):
            if isinstance(node, ir.Reg) and node.index == S:
                return delta
            if isinstance(node, ir.Temp):
                return places.get(node)
            if isinstance(node, ir.Op) and node.name in ("add", "sub") and node.bits == 16 \
                    and isinstance(node.args[1], ir.Const):
                inner = offset_of(node.args[0])
                if inner is None:
                    return None
                amount = node.args[1].value
                amount = amount - 0x10000 if amount >= 0x8000 else amount
                return inner + amount if node.name == "add" else inner - amount
            return None

        def place(offset):
            return ir.op("add", (ir.Reg(S, 16), ir.const(offset - delta & 0xFFFF, 16)), 16) if offset != delta \
                else ir.Reg(S, 16)

        def change(node):
            if isinstance(node, ir.Reg) and node.index in holding:
                return place(holding[node.index])
            if isinstance(node, ir.Load) and not isinstance(node.address, ir.Const):
                offset = offset_of(node.address)
                if offset is not None:
                    return ir.Load(place(offset), node.bits)
            return None

        for statement in block.statements:
            if not known:
                break
            ir.map_statement(statement, change)
            if isinstance(statement, ir.Store) and not isinstance(statement.address, ir.Const):
                offset = offset_of(statement.address)
                if offset is not None:
                    statement.address = place(offset)
            if isinstance(statement, ir.SetTemp):
                offset = offset_of(statement.value)
                if offset is not None:
                    places[statement.temp] = offset
            elif isinstance(statement, ir.Call):
                holding = {}
            elif isinstance(statement, ir.SetReg) and statement.index == S:
                offset = offset_of(statement.value)
                if offset is None:
                    known = False    # (S set to a place of its own: the boot's)
                else:
                    delta = offset
            elif isinstance(statement, ir.SetReg) and statement.index in (X, Y, U):
                offset = offset_of(statement.value)
                if offset is not None:
                    holding[statement.index] = offset
                else:
                    holding.pop(statement.index, None)
