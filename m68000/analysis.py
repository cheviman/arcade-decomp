# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        analysis.py
Description: recursive-descent disassembly of a 68000 program without traces, into a listing and a report
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Usage: python tools/disassemble.py [rom folder or .zip] [--trace executed.txt ...]   (the game's shim; default
       ROM: rom/). Writes build/listing/BINARY.s and build/listing/report.txt. --trace adds the addresses a
       real run executed (konami/reference/kref --trace) to check the analysis against them: how many it finds,
       and executed addresses that fall inside an instruction it decoded (one of the two readings is wrong).

Starts at the exception vectors (the reset's PC, the exceptions', the autovectors', the traps') and at the
entry points symbols.txt gives, and follows every branch, jump and call. Where a jump or call goes through
a register, it follows what the address registers can hold along the way, block to block and from a caller
into the routine it calls or jumps to: each register holds the set of values some way into the block gives
it (a way that says nothing about a register adds nothing: what is wanted is every place the code can go),
each one of these:
  - an address: from lea, movea.l #, a long read from the program (movea.l (An), (d16,An), abs), or one
    moved or added to;
  - an entry of a table: movea.l (An,Xn) with An holding the table's address. A jump or call through it
    makes the table one of code addresses;
  - an entry of an entry of a table: the same through a register that held an entry: a table of tables of
    code addresses (the object handlers by kind, then by state);
  - a place in a table, its address and an index the analysis does not know (lea (d,An,Xn), adda Dn, or a
    register (An)+ moves on): a long read from it is an entry of the table, and a long read with (An)+ from
    a table's address too (a list the code walks); three tables deep at most;
  - a field of an entry of a table: movea.l (d8,An,Xn) with a displacement, a field of a table of records.
    How long the records are the code does not say: symbols.txt does (record=, code=, count=), and the
    report lists the ones it does not describe.
A table's length is what symbols.txt says (a data line with record=4 code=0 count=N) or, if it says
nothing, its entries up to the first that is not a code address a trial decode accepts, or that is code or
another table already, or a place the code names as data (an absolute or PC-relative operand, a long it
stores: what a table read on into holds can decode, so the walk starts again with the table stopping there
once the code that names it is found); a zero entry before one that is not (and first, or after one that is not) is an
unused one (a state with no handler). A jump or call through a field of a structure, movea.l (d16,An), Am
then jsr (Am), makes that offset a handler slot: an address stored in such a slot (move.l #code, or a
register holding it) is code. symbols.txt keeps the V60's format (disassemble.load_symbols): func, code and
data lines.
"""
import sys
from bisect import bisect_left, bisect_right
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import game_profile  # noqa: E402  (the game's: its root, its ROM set)
import disassemble  # noqa: E402  (symbols.txt and traces, in the format all the games share)
from m68000 import disassembler as dis  # noqa: E402

ROOT = game_profile.ROOT
CODE_START = getattr(game_profile, "CODE_START", 0x1000)    # the vectors and what follows them are no code
CODE_END = game_profile.rom.PROGRAM_SIZE
VECTORS = range(1, 48)          # the reset's PC, the exceptions', the autovectors' and the traps'
VECTOR_TABLE = 0x400            # the 256 vectors
TRIAL_INSTRUCTIONS = 12         # a trial decode: this many valid instructions, or fewer up to a jump or return
MAX_TABLE = 1024
MAX_VALUES = 1024               # values a register can hold before it is no longer followed (the objects' state
                                # dispatcher at TMNT's 048236 gets a table from each of some 170 places)

load_symbols = disassemble.load_symbols
load_trace = disassemble.load_trace

# What an address register can hold: a set of (CONSTANT, address), (ENTRY, table), (ENTRY2, table), (ENTRY3,
# table), (FIELD, (table, offset)) and (CURSOR, table): a place in a table, its address and an index the
# analysis does not know (lea (d,An,Xn), adda Dn, a register a (An)+ moves on); TOO_MANY once it could hold more
# than MAX_VALUES; nothing is known of a register that is absent
CONSTANT, ENTRY, ENTRY2, ENTRY3, FIELD, CURSOR = ("address", "entry", "entry of an entry", "entry of an entry of an entry",
                                                  "field of an entry", "place in a table")
FIELD2 = "field of what an entry points at"     # (FIELD2, (table, offset))
# the register that holds the object a handler runs on (TMNT's A0), if the game's profile says: a long the
# code stores in one of its fields is what a long read from that field gives, wherever the object is
OBJECT_REGISTER = getattr(game_profile, "OBJECT_REGISTER", None)
TABLE_BASES = (CONSTANT, CURSOR)    # what a table is read from: its address, or a place in it
ADDRESS_REGISTERS = frozenset(f"A{number}" for number in range(8))
NO_WRITE = {"cmp", "cmpa", "cmpi", "cmpm", "tst", "btst", "chk"}   # an address register last, and not written


def writes(instruction):
    """The address registers an instruction writes"""
    operands = instruction.operands
    found = {operand.reg for operand in operands if operand.mode in ("postinc", "predec")}
    if instruction.mnemonic == "movem" and operands and operands[-1].mode == "reglist":
        found.update(operands[-1].registers)
    elif instruction.mnemonic in ("exg", "link", "unlk"):
        found.update(operand.reg for operand in operands if operand.mode == "areg")
    elif operands and operands[-1].mode == "areg" and instruction.mnemonic not in NO_WRITE:
        found.add(operands[-1].reg)
    return found & ADDRESS_REGISTERS
TOO_MANY = frozenset({("too many", 0)})


def merge(first, second):
    """What two ways into a block can give each register"""
    merged = dict(first)
    for register, values in second.items():
        union = merged.get(register, frozenset()) | values
        merged[register] = TOO_MANY if TOO_MANY <= union or len(union) > MAX_VALUES else union
    return merged


def known(values):
    """The values a register can hold, as far as they are followed"""
    return () if values is None or values == TOO_MANY else values


class Analysis:
    """The program's code, found from its entry points. Attributes: instructions ({address: Instruction}),
    functions (call targets), labels (branch and jump targets), code_tables ({address: entries}),
    table_tables ({address: entries}), slots (handler slots' offsets), mem."""

    def __init__(self, image, symbols, executed=None):
        self.image = image
        self.mem = dis.FlatMemory(image)
        self.symbols = symbols
        self.executed = executed    # (the coverage passes a trace: the analysis does not use it)
        self.data_starts = set()    # places inside a table read the code names as data: the table stops there
        self.preserved = {}         # {function: address registers a call to it leaves alone} (leaves_alone())
        self.object_fields = {}     # {offset: what the code stores in that field of the object} (OBJECT_REGISTER)
        self.reset()

    def reset(self):
        """Everything a walk finds (run() walks again when a table turns out to stop sooner)"""
        self.instructions = {}
        self.functions = set()
        self.labels = set()
        self.code_tables = {}
        self.table_tables = {}
        self.table_ends = {}        # {table: where it stops} of the tables read without symbols.txt
        # what the coverage reads (made by finish()): every byte of code -> its instruction; the code addresses
        # the tables hold, by where they hold them; (first, stop) of the vectors and the tables; the data
        # addresses the described tables' data= fields hold, by where they hold them
        self.owner = {}
        self.data_words = {}
        self.described = []
        self.data_fields = {}
        self.fill = []              # (first, stop) of the bytes that are only there to fill the program
        self.slots = set()
        self.field_tables = {}      # {(table, offset): entries} of the tables of records symbols.txt describes
        self.undescribed = set()    # (table, offset) of the ones it does not
        self.stores = {}            # handler slot candidates: {offset: {addresses stored there}}
        self.fields_found = {}      # {offset: what this walk saw stored in that field of the object}
        self.entry_states = {}      # what the address registers can hold where a block starts
        self.work = deque()

    # ---- what can be code ----

    def plausible(self, address):
        return CODE_START <= address < CODE_END and address % 2 == 0

    def clean(self, address):
        """A trial decode: valid instructions up to a jump or a return, or TRIAL_INSTRUCTIONS of them"""
        for _ in range(TRIAL_INSTRUCTIONS):
            if not self.plausible(address):
                return False
            instruction = dis.decode(self.mem, address)
            if instruction.flow == dis.FLOW_INVALID:
                return False
            if instruction.flow in (dis.FLOW_JUMP, dis.FLOW_RETURN):
                return True
            address += instruction.length
        return True

    def enter(self, address, state=None, kind=None):
        """A block starts at 'address' with the address registers in 'state'"""
        address &= 0xFFFFFF
        if not self.plausible(address):
            return
        if kind == "func":
            self.functions.add(address)
        elif kind == "loc":
            self.labels.add(address)
        state = dict(state or {})
        before = self.entry_states.get(address)
        if before is not None:
            state = merge(before, state)
            if state == before:
                return
        self.entry_states[address] = state
        self.work.append(address)

    # ---- tables ----

    def layout_of(self, table):
        """The entries symbols.txt gives a table: (count, record, code offset), or None"""
        symbol = self.symbols.get(table)
        if symbol is None or symbol.kind != "data" or symbol.layout is None:
            return None
        layout = symbol.layout
        if layout.count is None or len(layout.code) != 1 or layout.code[0][1] is not None:
            raise SystemExit(f"{symbol.name}: only record=, count= and one 32-bit code= field are read for the 68000")
        return layout.count, layout.record, layout.code[0][0]

    def ends_table(self, address, table):
        """Where a table without a described length stops: code, another table, or a place the code names as data"""
        return address != table and (address in self.instructions or address in self.code_tables
                                     or address in self.table_tables or address in self.data_starts)

    def unused(self, where, first):
        """A zero entry before one that is not, and first or after one that is not: a state with no handler"""
        return self.mem.r32(where) == 0 and self.mem.r32(where + 4) != 0 and (first or self.mem.r32(where - 4) != 0)

    def read_code_table(self, table):
        if table in self.code_tables:
            return
        entries = []
        described = self.layout_of(table)
        if described is not None:
            count, record, offset = described
            entries = [self.mem.r32(table + record * index + offset) & 0xFFFFFF for index in range(count)]
        else:
            for index in range(MAX_TABLE):
                where = table + 4 * index
                value = self.mem.r32(where) & 0xFFFFFF
                if self.unused(where, index == 0):
                    continue
                if self.ends_table(where, table) or not self.plausible(value) or not self.clean(value):
                    break
                entries.append(value)
                self.table_ends[table] = where + 4
        self.code_tables[table] = entries
        for value in entries:
            self.enter(value, kind="loc")

    def read_field_table(self, table, offset):
        """A code address in a field of a table of records: as long as symbols.txt describes the records"""
        if (table, offset) in self.field_tables:
            return
        described = self.layout_of(table)
        if described is None or described[2] != offset:
            self.undescribed.add((table, offset))
            return
        count, record, _ = described
        entries = [self.mem.r32(table + record * index + offset) & 0xFFFFFF for index in range(count)]
        self.field_tables[(table, offset)] = entries
        for value in entries:
            self.enter(value, kind="loc")

    def read_table_table(self, table):
        if table in self.table_tables:
            return
        entries = []
        described = self.layout_of(table)
        if described is not None:
            count, record, offset = described
            entries = [self.mem.r32(table + record * index + offset) & 0xFFFFFF for index in range(count)]
        else:
            for index in range(MAX_TABLE):
                where = table + 4 * index
                value = self.mem.r32(where) & 0xFFFFFF
                if self.unused(where, index == 0):
                    continue
                if self.ends_table(where, table) or not self.plausible(value) or value in self.instructions:
                    break
                entries.append(value)
                self.table_ends[table] = where + 4
        self.table_tables[table] = entries
        for value in entries:
            self.read_code_table(value)

    # ---- the address registers ----

    def read_long(self, address):
        """A long the program reads from its own ROM, as an address"""
        address &= 0xFFFFFF
        return (CONSTANT, self.mem.r32(address) & 0xFFFFFF) if address + 4 <= CODE_END else None

    def source_values(self, operand, size, state):
        """What a source operand can give an address register: a set, or None when nothing is known"""
        mode = operand.mode
        if mode == "immediate":
            value = operand.value & 0xFFFFFFFF
            if size == "w":
                value = (value & 0xFFFF) - (0x10000 if value & 0x8000 else 0)
            return frozenset({(CONSTANT, value & 0xFFFFFF)})
        if mode == "areg":
            return state.get(operand.reg)
        if size != "l":
            return None
        if mode in ("abs_short", "abs_long", "pc_disp"):
            value = self.read_long(operand.value)
            return frozenset({value}) if value else None
        if mode == "disp" and operand.reg == OBJECT_REGISTER and state.get(operand.reg) is None:
            # a field of the object: what the code stores there (found by the walk before)
            stored = self.object_fields.get(operand.disp)
            return frozenset(stored) if stored and len(stored) <= MAX_VALUES else None
        values = set()
        for kind, base in known(state.get(operand.reg) if mode in ("indirect", "disp", "index", "postinc", "predec")
                                else None):
            if mode == "indirect" and kind == CONSTANT:
                values.add(self.read_long(base))
            elif mode == "disp" and kind == CONSTANT:
                values.add(self.read_long(base + operand.disp))
            elif mode == "index" and kind in TABLE_BASES or mode in ("indirect", "disp") and kind == CURSOR:
                values.add((FIELD, (base, operand.disp)) if operand.disp else (ENTRY, base))
            elif mode in ("postinc", "predec") and kind in TABLE_BASES:
                # a list the code walks: its entries
                values.add((ENTRY, base))
            elif mode in ("postinc", "predec") and kind == ENTRY:
                # ... a list an entry of a table points at (009ABC "movea.l (A0)+, A6" after A0 took an entry)
                values.add((ENTRY2, base))
            elif mode == "index" and kind == ENTRY:
                # (a displacement indexes the inner table from below: 01C068 "movea.l (-$10,A6,D3.w), A6" with
                # D3 4 to 7 reads its first four entries)
                values.add((ENTRY2, base))
            elif mode in ("indirect", "disp") and kind == ENTRY:
                # a field of the record an entry points at
                values.add((FIELD2, (base, operand.disp)) if mode == "disp" and operand.disp else (ENTRY2, base))
            elif mode == "index" and kind == ENTRY2 and not operand.disp:
                values.add((ENTRY3, base))
        values.discard(None)
        return frozenset(values) or None

    def effective_addresses(self, operand, state):
        """The addresses lea and pea can compute"""
        if operand.mode in ("abs_short", "abs_long", "pc_disp"):
            return frozenset({(CONSTANT, operand.value & 0xFFFFFF)})
        if operand.mode == "pc_index":
            return frozenset({(CURSOR, (operand.base + operand.disp) & 0xFFFFFF)})
        if operand.mode not in ("indirect", "disp", "index"):
            return None
        offset = operand.disp if operand.mode in ("disp", "index") else 0
        values = frozenset(((CURSOR if operand.mode == "index" or kind == CURSOR else CONSTANT), (base + offset) & 0xFFFFFF)
                           for kind, base in known(state.get(operand.reg)) if kind in TABLE_BASES)
        return values or None

    def step(self, instruction, state):
        """What the instruction leaves in the address registers"""
        operands = instruction.operands
        # a register (An)+ or -(An) moves on is a place in the table it was at (read before it moves)
        moved = {}
        for operand in operands:
            if operand.mode in ("postinc", "predec"):
                moved[operand.reg] = frozenset((CURSOR, base) for kind, base in known(state.get(operand.reg))
                                               if kind in TABLE_BASES) or None
        before = dict(state)
        for register, values in moved.items():
            if values is None:
                state.pop(register, None)
            else:
                state[register] = values
        mnemonic = instruction.mnemonic
        if mnemonic == "movem" and operands and operands[-1].mode == "reglist":
            for register in operands[-1].registers:
                state.pop(register, None)
            return
        if mnemonic == "exg" and len(operands) == 2:
            first, second = (operand.text for operand in operands)
            values = {first: state.get(second), second: state.get(first)}
            for register, held in values.items():
                if held is None or register[0] != "A":
                    state.pop(register, None)
                else:
                    state[register] = held
            return
        if len(operands) != 2 or operands[1].mode != "areg" or mnemonic in NO_WRITE:
            return
        target = operands[1].reg
        source = operands[0]
        if mnemonic == "lea":
            values = self.effective_addresses(source, before)
        elif mnemonic in ("movea", "move"):
            values = self.source_values(source, instruction.size, before)
        elif mnemonic in ("adda", "suba", "addq", "subq") and source.mode in ("immediate", "quick"):
            # a single address moved by a constant; a register with more (a pointer a loop moves on: each time
            # round would add one more) is a place in each of their tables (TMNT's objects keep a pair of
            # animations, one a side, and pick the second with adda #4: 0484BC)
            current = known(state.get(target))
            amount = source.value if mnemonic in ("adda", "addq") else -source.value
            if len(current) == 1 and next(iter(current))[0] == CONSTANT:
                values = frozenset({(CONSTANT, (base + amount) & 0xFFFFFF) for kind, base in current})
            else:
                values = frozenset((CURSOR, base) for kind, base in current if kind in TABLE_BASES) or None
        elif mnemonic in ("adda", "suba") and source.mode == "dreg":
            # an address and an index: a place in the table there
            values = frozenset((CURSOR, base) for kind, base in known(state.get(target)) if kind in TABLE_BASES) or None
        else:
            values = None
        if values is None:
            state.pop(target, None)
        else:
            state[target] = values

    def note_store(self, instruction, state):
        """An address stored in a field of a structure: a handler there, if the field is a handler slot"""
        operands = instruction.operands
        if instruction.mnemonic != "move" or instruction.size != "l" or len(operands) != 2 or operands[1].mode != "disp":
            return
        source = operands[0]
        values = {(CONSTANT, source.value & 0xFFFFFF)} if source.mode == "immediate" else \
            known(state.get(source.reg)) if source.mode == "areg" else ()
        for kind, value in values:
            if kind == CONSTANT and self.plausible(value):
                self.stores.setdefault(operands[1].disp, set()).add(value)
        # a field of the object the profile's OBJECT_REGISTER holds (TMNT's A0): what is stored there, for the
        # next walk to read back (objects keep their animations and records at +24, +46...)
        if OBJECT_REGISTER and operands[1].reg == OBJECT_REGISTER and state.get(OBJECT_REGISTER) is None:
            held = self.source_values(source, "l", state)
            if held is not None and held != TOO_MANY:
                self.fields_found.setdefault(operands[1].disp, set()).update(held)

    def computed(self, instruction, state, block):
        """A jump or call through a register: where it can go"""
        operand = instruction.operands[0]
        kind = "func" if instruction.flow == dis.FLOW_CALL else "loc"
        if operand.mode not in ("indirect", "disp"):
            return
        values = state.get(operand.reg)
        if values is None and operand.mode == "indirect":
            # through a field of a structure: movea.l (d16,An), Am just before
            for previous in reversed(block[:-1]):
                if previous.operands and len(previous.operands) == 2 and previous.operands[1].mode == "areg" \
                        and previous.operands[1].reg == operand.reg:
                    if previous.mnemonic == "movea" and previous.operands[0].mode == "disp":
                        self.slots.add(previous.operands[0].disp)
                    break
        offset = operand.disp if operand.mode == "disp" else 0
        for held, value in known(values):
            if held == CONSTANT:
                # (with the registers, as a direct jump or call: the boot's tests return through one register
                # to code that goes on through another)
                self.enter(value + offset, state, kind)
            elif held == ENTRY and offset == 0:
                self.read_code_table(value)
            elif held == ENTRY2 and offset == 0:
                self.read_table_table(value)
            elif held == FIELD and offset == 0:
                self.read_field_table(*value)

    # ---- the walk ----

    def walk(self, address):
        state = dict(self.entry_states[address])
        start = address
        block = []
        while True:
            if not self.plausible(address):
                return
            instruction = self.instructions.get(address) or dis.decode(self.mem, address)
            if instruction.flow == dis.FLOW_INVALID:
                return
            self.instructions[address] = instruction
            block.append(instruction)
            self.note_store(instruction, state)
            flow = instruction.flow
            if flow == dis.FLOW_CALL:
                if instruction.target is not None:
                    self.enter(instruction.target, state, "func")
                else:
                    self.computed(instruction, state, block)
                # what the call leaves: the registers no way through the function changes (an earlier walk's)
                kept = self.preserved.get(instruction.target, ())
                state = {register: values for register, values in state.items() if register in kept}
            elif flow == dis.FLOW_BRANCH:
                self.enter(instruction.target, state, "loc")
            elif flow == dis.FLOW_JUMP:
                if instruction.target is not None:
                    self.enter(instruction.target, state, "loc")
                else:
                    self.computed(instruction, state, block)
                return
            elif flow == dis.FLOW_RETURN:
                return
            else:
                self.step(instruction, state)
            address += instruction.length
            if address in self.entry_states and address != start:
                self.enter(address, state)
                return

    def run(self):
        first = True
        while True:
            self.walk_all()
            # a place the code names as data inside a table read without symbols.txt: the table stops there, and
            # what it held from there on was no code (TMNT's 051AD2 went on into the pointers at 051AE2, and their
            # records decode). The walk starts again with it
            named = sorted({place for instruction in self.instructions.values() for value in data_places(instruction)
                            if (place := game_profile.rom.canonical_address(value & 0xFFFFFF)) is not None})
            inside = set()
            for table, stop in self.table_ends.items():
                inside.update(named[bisect_right(named, table):bisect_left(named, stop)])
            if not inside <= self.data_starts:
                self.data_starts |= inside
                self.reset()
                continue
            # the registers a call leaves alone, from the functions this walk found: the walk starts again with
            # them (each time with no more than the time before, so it ends)
            preserved = self.leaves_alone()
            if not first:
                preserved = {function: registers & self.preserved.get(function, frozenset())
                             for function, registers in preserved.items()}
                preserved = {function: registers for function, registers in preserved.items() if registers}
            # and what the object's fields can hold: the walk starts again with them (each time with no less)
            fields = {offset: frozenset(values) for offset, values in self.object_fields.items()}
            for offset, values in self.fields_found.items():
                fields[offset] = fields.get(offset, frozenset()) | frozenset(values)
            if preserved == self.preserved and fields == self.object_fields:
                break
            self.preserved = preserved
            self.object_fields = fields
            first = False
            self.reset()
        self.finish()

    def leaves_alone(self):
        """{function: the address registers no way through it changes}: what its instructions and the functions
        it calls write (a call or jump through a register, or code not found, may change them all)"""
        own, calls = {}, {}
        for function in self.functions:
            seen, work, written, callees = set(), [function], set(), set()
            while work:
                address = work.pop()
                if address in seen:
                    continue
                seen.add(address)
                instruction = self.instructions.get(address)
                if instruction is None or instruction.flow in (dis.FLOW_CALL, dis.FLOW_JUMP) and instruction.target is None:
                    written = set(ADDRESS_REGISTERS)
                    break
                written |= writes(instruction)
                after = address + instruction.length
                if instruction.flow == dis.FLOW_CALL:
                    callees.add(instruction.target)
                    work.append(after)
                elif instruction.flow == dis.FLOW_BRANCH:
                    work += [instruction.target, after]
                elif instruction.flow == dis.FLOW_JUMP:
                    work.append(instruction.target)
                elif instruction.flow != dis.FLOW_RETURN:
                    work.append(after)
            own[function], calls[function] = written, callees
        changed = {function: set(written) for function, written in own.items()}
        growing = True
        while growing:
            growing = False
            for function, callees in calls.items():
                for callee in callees:
                    more = changed.get(callee, ADDRESS_REGISTERS) - changed[function]
                    if more:
                        changed[function] |= more
                        growing = True
        return {function: frozenset(ADDRESS_REGISTERS - written) for function, written in changed.items()
                if not ADDRESS_REGISTERS <= written}

    def walk_all(self):
        for vector in VECTORS:
            self.enter(self.mem.r32(4 * vector), kind="func")
        for address, symbol in self.symbols.items():
            if symbol.kind in ("func", "code"):
                self.enter(address, kind="func" if symbol.kind == "func" else "loc")
            elif symbol.kind == "data" and symbol.layout is not None and symbol.layout.code:
                self.read_code_table(address)
        handled = set()
        while True:
            while self.work:
                self.walk(self.work.popleft())
            # the addresses stored in the handler slots found so far
            new = [(offset, value) for offset in self.slots for value in self.stores.get(offset, ())
                   if (offset, value) not in handled]
            if not new:
                break
            for offset, value in new:
                handled.add((offset, value))
                if self.clean(value):
                    self.enter(value, kind="func")

    def finish(self):
        """What the coverage reads: the bytes of the code, of the vectors and of the tables"""
        for start, instruction in self.instructions.items():
            for address in range(start, start + instruction.length):
                self.owner[address] = start
        self.described.append((0, VECTOR_TABLE))
        # the zeros between the vectors and the code
        if not any(self.image[VECTOR_TABLE:CODE_START]):
            self.fill.append((VECTOR_TABLE, CODE_START))
        for where in range(0, VECTOR_TABLE, 4):
            value = self.mem.r32(where) & 0xFFFFFF
            if value and value in self.instructions:
                self.data_words[where] = value
        for table, stop in self.table_ends.items():
            self.described.append((table, stop))
            for where in range(table, stop, 4):
                if self.mem.r32(where):
                    self.data_words[where] = self.mem.r32(where) & 0xFFFFFF
        for address, symbol in self.symbols.items():
            layout = symbol.layout
            if symbol.kind != "data" or layout is None:
                continue
            if layout.count is None or layout.lists is not None or layout.pointers is not None or layout.kinds is not None \
                    or any(base is not None for _, base in layout.code):
                raise SystemExit(f"{symbol.name}: only record=, count=, 32-bit code= and data= fields are read for the 68000")
            for index in range(layout.count):
                record = address + layout.record * index
                for offset, _ in layout.code:
                    if self.mem.r32(record + offset):
                        self.data_words[record + offset] = self.mem.r32(record + offset) & 0xFFFFFF
                for offset in layout.data:
                    value = self.mem.r32(record + offset) & 0xFFFFFF
                    if value and value < CODE_END:
                        self.data_fields[record + offset] = value
            self.described.append((address, address + layout.record * layout.count))


# What the coverage (the core's coverage.py) needs of the 68000: how its pointers are stored, where its functions
# start and what fills up to them (zeros, or nop), and the places in memory an instruction reads or names
BYTE_ORDER = "big"
FUNCTION_ALIGNMENT = 2
PADDING_BYTES = (0x00, 0xFF)      # (an EPROM's erased byte after data of an odd length)
DATA_MODES = ("abs_short", "abs_long", "pc_disp")


def data_places(instruction):
    """The fixed places an instruction reads, writes or takes the address of, and the base of the tables it
    indexes from the PC; a long it moves to an address register or into memory (an address, if it names the
    program past the vectors: below them it is a number). A jump, branch or call names code, not data."""
    if instruction.flow != dis.FLOW_NORMAL:
        return []
    places = []
    for operand in instruction.operands:
        if operand.mode in DATA_MODES:
            places.append(operand.value & 0xFFFFFF)
        elif operand.mode == "pc_index":
            places.append((operand.base + operand.disp) & 0xFFFFFF)
    operands = instruction.operands
    if instruction.mnemonic in ("move", "movea") and instruction.size == "l" and len(operands) == 2 \
            and operands[0].mode == "immediate" and operands[1].mode != "dreg" \
            and CODE_START <= operands[0].value & 0xFFFFFF:
        places.append(operands[0].value & 0xFFFFFF)
    return places


def write_listing(analysis, path):
    names = {address: symbol.name for address, symbol in analysis.symbols.items()}
    lines = []
    for address in sorted(analysis.instructions):
        if address in names:
            lines.append(f"\n{names[address]}:")
        elif address in analysis.functions:
            lines.append(f"\nfunc_{address:06X}:")
        elif address in analysis.labels:
            lines.append(f"loc_{address:06X}:")
        lines.append(f"    {address:06X}  {analysis.instructions[address].text}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def write_report(analysis, path, executed=None):
    instructions = analysis.instructions
    code_bytes = sum(instruction.length for instruction in instructions.values())
    lines = [f"instructions: {len(instructions)} ({code_bytes} bytes of {CODE_END - CODE_START} from "
             f"{CODE_START:06X})",
             f"functions: {len(analysis.functions)}, labels: {len(analysis.labels)}",
             f"code tables: {len(analysis.code_tables)} ({sum(map(len, analysis.code_tables.values()))} entries), "
             f"tables of them: {len(analysis.table_tables)}",
             f"tables of records with a code field (symbols.txt): {len(analysis.field_tables)}",
             f"handler slots: {sorted(analysis.slots)}"]
    for table, offset in sorted(analysis.undescribed):
        lines.append(f"a table of records symbols.txt does not describe: {table:06X}, its code field at +{offset}")
    if executed is not None:
        found = sum(1 for address in executed if address in instructions)
        inside = sorted(address for start, instruction in instructions.items()
                        for address in range(start + 2, start + instruction.length, 2) if address in executed)
        lines.append(f"executed: {len(executed)}, found without traces: {found}, missing: {len(executed) - found}")
        lines.append(f"executed addresses inside a decoded instruction: {len(inside)} "
                     + " ".join(f"{address:06X}" for address in inside[:40]))
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    print("\n".join(lines))


def main():
    arguments = sys.argv[1:]
    traces = []
    while "--trace" in arguments:
        position = arguments.index("--trace")
        traces.append(Path(arguments[position + 1]))
        del arguments[position:position + 2]
    source = Path(arguments[0]) if arguments else ROOT / "rom"
    image = game_profile.rom.build_program(game_profile.rom.load_set(source))
    symbols_path = ROOT / "symbols.txt"
    symbols = load_symbols(symbols_path) if symbols_path.exists() else {}
    analysis = Analysis(image, symbols)
    analysis.run()
    out = ROOT / "build" / "listing"
    out.mkdir(parents=True, exist_ok=True)
    write_listing(analysis, out / f"{game_profile.BINARY}.s")
    executed = load_trace(traces)[0] if traces else None
    write_report(analysis, out / "report.txt", executed)


if __name__ == "__main__":
    main()
