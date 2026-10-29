# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        values.py
Description: what the Z80's registers can hold along the sound program, for its static analysis
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

A jump through a register (JP (HL), JP (IX), JP (IY)) goes where the register's value says, which the code
around it works out: a constant, an entry of a table of code addresses, or a word the program keeps in a
record and stored there before (a handler slot: the sound driver's channels each hold the routine that
runs them next, which the main loop jumps to). This walk follows the code the analysis has decoded, without
traces, and gives each 8-bit register the set of values some way into each instruction gives it (a way that
says nothing about a register adds nothing: what is wanted is every place the code can go). A value is one of
  ("b", n)            the constant byte n
  ("lo", W), ("hi", W)  the low or the high byte of the 16-bit value W:
      ("w", n)          the constant n (LD rr,nn, or a pair whose two bytes are constants)
      ("e", T)          an entry of the table of 16-bit words at T, by an index the walk does not know
      ("fw", R, d)      the word at offset d of the record R (IX or IY) points at
  ("f", R, d)         the byte at offset d of the record R points at
  ("plo", T, k), ("phi", T, k)  the two bytes of a place in the table at T: its address plus an index the walk
                      does not know (ADD HL,rr), k bytes on (INC rr); a byte read from it is the low (k = 0)
                      or the high (k = 1) byte of one of its entries
A call hands the callee what the registers hold, and the code after it starts with nothing known but the
stack. What the walk gives the analysis:
  - jumps: {address of a JP (rr): the words it can go to};
  - slot_words(R, d): the words the program stores at offset d of a record: LD (IX+d),r and LD (IX+d+1),r of
    the two bytes of one word, or LD (nn),rr, LD (nn),a at a record's address plus d (every constant the
    program loads into IX or IY is the address of a record); only words whose two bytes come from one value
    (constant bytes stored one by one are no word: a field can hold other things in other records);
  - arguments(entry): {pair: words}, what the registers hold where 'entry' starts, from every way into it;
  - tables: {T: instructions}: the tables of words the program reads an entry of, and where.
The analysis walks again as it finds more code (run()): what it found stays, and grows.
"""
from collections import defaultdict, deque

from . import disassembler as dis

MAX_VALUES = 256            # values a register can hold before it is no longer followed
TOO_MANY = frozenset({("too many",)})
PAIRS = {"bc": ("b", "c"), "de": ("d", "e"), "hl": ("h", "l"), "ix": ("ixh", "ixl"), "iy": ("iyh", "iyl"),
         "af": ("a", None)}
BYTES = {"a", "b", "c", "d", "e", "h", "l", "ixh", "ixl", "iyh", "iyl"}
STACK_DEPTH = 8
# what instructions with no value worth following write, by mnemonic (the operand they name besides these)
WRITES = {
    "rlca": {"a"}, "rrca": {"a"}, "rla": {"a"}, "rra": {"a"}, "cpl": {"a"}, "daa": {"a"}, "neg": {"a"},
    "rld": {"a"}, "rrd": {"a"}, "djnz": {"b"}, "exx": {"b", "c", "d", "e", "h", "l"},
    "ldi": {"b", "c", "d", "e", "h", "l"}, "ldd": {"b", "c", "d", "e", "h", "l"},
    "ldir": {"b", "c", "d", "e", "h", "l"}, "lddr": {"b", "c", "d", "e", "h", "l"},
    "cpi": {"b", "c", "h", "l"}, "cpd": {"b", "c", "h", "l"}, "cpir": {"b", "c", "h", "l"},
    "cpdr": {"b", "c", "h", "l"}, "ini": {"b", "h", "l"}, "ind": {"b", "h", "l"}, "inir": {"b", "h", "l"},
    "indr": {"b", "h", "l"}, "outi": {"b", "h", "l"}, "outd": {"b", "h", "l"}, "otir": {"b", "h", "l"},
    "otdr": {"b", "h", "l"},
}
ALU = {"add", "adc", "sub", "sbc", "and", "xor", "or"}
SHIFTS = {"rlc", "rrc", "rl", "rr", "sla", "sra", "sll", "srl", "res", "set"}


class State:
    """What the registers can hold at one place: {byte register: values}, and the stack's top pairs as far
    as they are known (each a pair of value sets, the high byte's and the low byte's)."""

    __slots__ = ("registers", "stack")

    def __init__(self, registers=None, stack=()):
        self.registers = registers or {}
        self.stack = stack

    def copy(self):
        return State(dict(self.registers), self.stack)

    def get(self, register):
        values = self.registers.get(register, frozenset())
        return frozenset() if values is TOO_MANY else values

    def set(self, register, values):
        if not values:
            self.registers.pop(register, None)
        else:
            self.registers[register] = TOO_MANY if len(values) > MAX_VALUES else frozenset(values)

    def forget(self, registers):
        for register in registers:
            self.registers.pop(register, None)

    def merge(self, other):
        """Adds what 'other' holds; True if anything is new"""
        changed = False
        for register, values in other.registers.items():
            old = self.registers.get(register)
            if old is TOO_MANY or (old is not None and values is not TOO_MANY and values <= old):
                continue
            union = values if old is None else (TOO_MANY if values is TOO_MANY else old | values)
            if union is not TOO_MANY and len(union) > MAX_VALUES:
                union = TOO_MANY
            if union != old:
                self.registers[register] = union
                changed = True
        if self.stack != other.stack:
            # what both ways know of the stack's top
            depth = min(len(self.stack), len(other.stack))
            stack = tuple((high | other_high, low | other_low) for (high, low), (other_high, other_low)
                          in zip(self.stack[len(self.stack) - depth:], other.stack[len(other.stack) - depth:]))
            if stack != self.stack:
                self.stack = stack
                changed = True
        return changed


def words(high, low, exact=False):
    """The 16-bit values a pair holds whose high byte is one of 'high' and low byte one of 'low'; with
    'exact', only the values whose two bytes come from one value (a constant's bytes loaded or stored one by
    one are not paired: in the bytes of a record's field, any constant of one place could go with any of
    the other)"""
    found = set()
    if exact:
        for value in low:
            if value[0] == "lo" and ("hi", value[1]) in high:
                found.add(value[1])
            elif value[0] == "plo" and ("phi",) + value[1:] in high:
                found.add(("p",) + value[1:])
            elif value[0] == "f" and ("f", value[1], value[2] + 1) in high:
                found.add(("fw", value[1], value[2]))
        return found
    high_constants = {value[1] for value in high if value[0] == "b"} \
        | {value[1][1] >> 8 for value in high if value[0] == "hi" and value[1][0] == "w"}
    high_bytes = [value[1] for value in high if value[0] == "b"]
    for value in low:
        kind = value[0]
        if kind == "lo":
            if ("hi", value[1]) in high:
                found.add(value[1])
            if value[1][0] == "w":
                # a constant whose high byte was loaded on its own (LD H,n after LD HL,nn)
                found.update(("w", (value[1][1] & 0xFF) | (byte << 8)) for byte in high_bytes)
        elif kind == "plo" and ("phi",) + value[1:] in high:
            found.add(("p",) + value[1:])
        elif kind == "f" and ("f", value[1], value[2] + 1) in high:
            found.add(("fw", value[1], value[2]))
        elif kind == "b":
            found.update(("w", (byte << 8) | value[1]) for byte in high_constants)
    return found


def halves(word_values):
    """The two bytes' values of a pair that holds 'word_values': (high, low)"""
    high, low = set(), set()
    for word in word_values:
        if word[0] == "p":
            high.add(("phi",) + word[1:])
            low.add(("plo",) + word[1:])
        else:
            high.add(("hi", word))
            low.add(("lo", word))
    return high, low


class Walk:
    """The walk over the instructions the analysis decoded ({address: Instruction}), from 'entries'.
    'targets' ({address of a JP (rr): targets}), 'slot_jumps' ({address of a JP (rr): the slots it goes
    through}) and 'slot_targets' ({(register, offset): targets}) are the places the analysis already knows
    those jumps go; 'code_end' is where the program's ROM ends (a constant address below it reads a known
    byte)."""

    def __init__(self, instructions, entries, image, code_end, targets=None, slot_jumps=None, slot_targets=None):
        self.instructions = instructions
        self.image = image
        self.code_end = code_end
        # (the analysis's own dictionaries, which it adds to between runs)
        self.targets = {} if targets is None else targets
        self.slot_jumps = {} if slot_jumps is None else slot_jumps
        self.slot_targets = {} if slot_targets is None else slot_targets
        self.states = {}
        self.jumps = defaultdict(set)
        self.field_bytes = defaultdict(set)     # (register, offset) -> byte values stored there
        self.absolute_bytes = defaultdict(set)  # address -> byte values stored there
        self.absolute_words = defaultdict(set)  # address -> words stored there (LD (nn),rr)
        self.tables = defaultdict(set)          # table -> instructions that read one of its entries
        self.bases = {"ix": set(), "iy": set()}
        self.run(entries)

    # ---- the walk ----

    def run(self, entries, again=()):
        """Walks on from 'entries' (a new one starts with nothing known) and the instructions 'again' (the
        jumps through a register that go somewhere new): the instructions, the jumps' targets and the slots'
        can have grown since the last run, never shrunk"""
        for instruction in self.instructions.values():
            operands = instruction.operands
            if instruction.mnemonic == "ld" and len(operands) == 2 and operands[0].mode == "reg" \
                    and operands[0].reg in self.bases and operands[1].mode == "immediate":
                self.bases[operands[0].reg].add(operands[1].value)
        work = deque(address for address in again if address in self.states)
        for entry in entries:
            if entry in self.instructions and entry not in self.states:
                self.states[entry] = State()
                work.append(entry)
        queued = set(work)
        while work:
            address = work.popleft()
            queued.discard(address)
            instruction = self.instructions[address]
            for successor, state in self.step(instruction, self.states[address].copy()):
                if successor not in self.instructions:
                    continue
                old = self.states.get(successor)
                if old is None:
                    self.states[successor] = state.copy()    # (the ways out of a branch share one state)
                elif not old.merge(state):
                    continue
                if successor not in queued:
                    queued.add(successor)
                    work.append(successor)

    def step(self, instruction, state):
        """[(successor, state there)] after one instruction"""
        address = instruction.address
        after = (address + instruction.length) & 0xFFFF
        mnemonic = instruction.mnemonic
        flow = instruction.flow
        if flow == dis.FLOW_JUMP and instruction.target is None:
            pair = instruction.operands[0].reg
            found = self.pair_words(state, pair)
            self.jumps[address] |= found
            ways = [(target, state) for target in self.targets.get(address, ())]
            ways += [(target, state) for slot in self.slot_jumps.get(address, ())
                     for target in self.slot_targets.get(slot, ())]
            return ways
        if flow == dis.FLOW_CALL:
            # the code after the call knows nothing of what the callee leaves in the registers
            return [(instruction.target, state), (after, State({}, state.stack))]
        if flow == dis.FLOW_RETURN:
            return []
        if mnemonic == "ret":
            return [(after, state)]
        if flow in (dis.FLOW_JUMP, dis.FLOW_BRANCH):
            if mnemonic == "djnz":
                state.forget({"b"})
            ways = [(instruction.target, state)]
            if flow == dis.FLOW_BRANCH:
                ways.append((after, state))
            return ways
        self.transfer(instruction, state)
        return [(after, state)]

    def arguments(self, entry):
        """{pair: words} the registers hold where 'entry' starts, from every way into it"""
        state = self.states.get(entry)
        if state is None:
            return {}
        found = {pair: self.pair_words(state, pair) for pair in ("bc", "de", "hl", "ix", "iy")}
        return {pair: values for pair, values in found.items() if values}

    # ---- reading and writing the registers ----

    def pair_words(self, state, pair):
        high, low = PAIRS[pair]
        return words(state.get(high), state.get(low))

    def set_pair(self, state, pair, word_values):
        high, low = PAIRS[pair]
        high_values, low_values = halves(word_values)
        state.set(high, high_values)
        if low is not None:
            state.set(low, low_values)

    def forget_pair(self, state, pair):
        state.forget(name for name in PAIRS[pair] if name)

    def read_byte(self, state, pair, instruction):
        """What a byte read through the pair can be"""
        found = set()
        for word in self.pair_words(state, pair):
            if word[0] == "w" and word[1] < self.code_end:
                found.add(("b", self.image[word[1]]))
            elif word[0] == "p" and word[2] < 2:
                found.add(("lo" if word[2] == 0 else "hi", ("e", word[1])))
                self.tables[word[1]].add(instruction.address)
        return found

    def store(self, state, operand, values, instruction):
        """A byte stored at the place 'operand' names"""
        if not values:
            return
        if operand.mode == "indexed":
            self.field_bytes[(operand.reg, operand.disp)] |= values
        elif operand.mode == "direct":
            self.absolute_bytes[operand.value] |= values
        elif operand.mode == "reg_indirect":
            for word in self.pair_words(state, operand.reg):
                if word[0] == "w":
                    self.absolute_bytes[word[1]] |= values

    def source(self, state, operand, instruction):
        """The byte values an 8-bit source operand gives"""
        mode = operand.mode
        if mode == "reg" and operand.reg in BYTES:
            return state.get(operand.reg)
        if mode == "immediate":
            return {("b", operand.value)}
        if mode == "indexed":
            return {("f", operand.reg, operand.disp)}
        if mode == "reg_indirect" and operand.reg in ("hl", "bc", "de"):
            return self.read_byte(state, operand.reg, instruction)
        if mode == "direct" and operand.value < self.code_end:
            return {("b", self.image[operand.value])}
        return set()

    def transfer(self, instruction, state):
        """What a non-control instruction does to the registers"""
        mnemonic = instruction.mnemonic
        operands = instruction.operands
        first = operands[0] if operands else None
        if instruction.copy_to:
            state.forget({instruction.copy_to})
        if mnemonic == "ld" and len(operands) == 2:
            self.load(instruction, state, operands[0], operands[1])
            return
        if mnemonic == "push":
            pair = first.reg
            entry = (state.get(PAIRS[pair][0]), state.get(PAIRS[pair][1]) if PAIRS[pair][1] else frozenset())
            state.stack = (state.stack + (entry,))[-STACK_DEPTH:]
            return
        if mnemonic == "pop":
            pair = first.reg
            high, low = PAIRS[pair]
            if state.stack:
                entry = state.stack[-1]
                state.stack = state.stack[:-1]
                state.set(high, entry[0])
                if low is not None:
                    state.set(low, entry[1])
            else:
                state.forget({high, low} - {None})
            return
        if mnemonic == "ex":
            if first.reg == "de":
                hl = (state.get("h"), state.get("l"))
                state.set("h", state.get("d"))
                state.set("l", state.get("e"))
                state.set("d", hl[0])
                state.set("e", hl[1])
            elif first.reg == "sp":
                pair = operands[1].reg
                high, low = PAIRS[pair]
                held = (state.get(high), state.get(low))
                if state.stack:
                    top = state.stack[-1]
                    state.set(high, top[0])
                    state.set(low, top[1])
                    state.stack = state.stack[:-1] + (held,)
                else:
                    state.forget({high, low})
            else:
                state.forget({"a"})
            return
        if mnemonic in ("inc", "dec") and first.mode == "reg":
            step = 1 if mnemonic == "inc" else -1
            if first.reg in PAIRS:
                found = set()
                for word in self.pair_words(state, first.reg):
                    if word[0] == "w":
                        found.add(("w", (word[1] + step) & 0xFFFF))
                    elif word[0] == "p" and step == 1:
                        found.add(("p", word[1], word[2] + 1))
                self.set_pair(state, first.reg, found)
            elif first.reg in BYTES:
                state.set(first.reg, {("b", (value[1] + step) & 0xFF) for value in state.get(first.reg)
                                      if value[0] == "b"})
            return
        if mnemonic in ("add", "adc", "sbc") and first.mode == "reg" and first.reg in ("hl", "ix", "iy"):
            found = set()
            if mnemonic == "add":
                other = operands[1].reg
                added = self.pair_words(state, other) if other in PAIRS else set()
                constants = {word[1] for word in added if word[0] == "w"}
                for word in self.pair_words(state, first.reg):
                    if word[0] == "w" and other == first.reg:
                        found.add(("w", (word[1] * 2) & 0xFFFF))
                    elif word[0] == "w" and constants and len(constants) == len(added):
                        found.update(("w", (word[1] + constant) & 0xFFFF) for constant in constants)
                    elif word[0] in ("w", "p"):
                        found.add(("p", word[1], 0))
            self.set_pair(state, first.reg, found)
            return
        if mnemonic in ("xor", "sub") and first.mode == "reg" and first.reg == "a" and len(operands) == 1:
            state.set("a", {("b", 0)})
            return
        if mnemonic in ("and", "or") and first.mode == "reg" and first.reg == "a" and len(operands) == 1:
            return
        if mnemonic == "cp" or mnemonic == "bit":
            return
        if mnemonic in ALU:
            state.forget({"a"})
            return
        if mnemonic in SHIFTS or mnemonic in ("inc", "dec"):
            if first is not None and first.mode == "reg":
                state.forget({first.reg} if first.reg in BYTES else set(PAIRS.get(first.reg, ())) - {None})
            elif len(operands) == 2 and operands[1].mode == "reg":
                state.forget({operands[1].reg})
            return
        if mnemonic == "in":
            if first.mode == "reg":
                state.forget({first.reg})
            return
        if mnemonic in WRITES:
            state.forget(WRITES[mnemonic])

    def load(self, instruction, state, destination, source):
        mode = destination.mode
        if mode == "reg" and destination.reg in BYTES:
            state.set(destination.reg, self.source(state, source, instruction))
            return
        if mode == "reg" and destination.reg in ("bc", "de", "hl", "ix", "iy"):
            if source.mode == "immediate":
                self.set_pair(state, destination.reg, {("w", source.value)})
            elif source.mode == "direct" and source.value + 1 < self.code_end:
                value = self.image[source.value] | (self.image[source.value + 1] << 8)
                self.set_pair(state, destination.reg, {("w", value)})
            else:
                self.forget_pair(state, destination.reg)
            return
        if mode == "reg" and destination.reg == "sp":
            state.stack = ()
            return
        if mode == "reg":
            # i, r: nothing followed; LD A,I and LD A,R write A
            state.forget({destination.reg})
            return
        if source.mode == "reg" and source.reg in ("bc", "de", "hl", "ix", "iy", "sp"):
            # LD (nn),rr
            if source.reg != "sp":
                found = self.pair_words(state, source.reg)
                if found:
                    self.absolute_words[destination.value] |= found
            return
        self.store(state, destination, self.source(state, source, instruction), instruction)

    # ---- what the program keeps in its records ----

    def slot_words(self, register, offset):
        """The words the program stores at offset 'offset' of the records 'register' points at"""
        found = words(self.field_bytes.get((register, offset + 1), set()),
                      self.field_bytes.get((register, offset), set()), exact=True)
        for base in self.bases[register]:
            place = (base + offset) & 0xFFFF
            found |= self.absolute_words.get(place, set())
            found |= words(self.absolute_bytes.get((place + 1) & 0xFFFF, set()), self.absolute_bytes.get(place, set()),
                           exact=True)
        return found
