# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        analysis.py
Description: recursive-descent disassembly of a Konami CPU program (the 052001 and the 053248) without traces,
             its banks followed, into a listing and a report
Version:     0.1.0
Created:     05-11-2026
Modified:    05-11-2026
Author:      Sebastian Toyos

Usage: python tools/disassemble.py [rom folder or .zip] [--trace executed.txt ...]   (the game's shim; default
       ROM: rom/). Writes build/listing/BINARY.s and build/listing/report.txt. --trace adds the places a real run
       executed (konami/reference/kref --trace) to check the analysis against them: how many it finds, and
       executed places that fall inside an instruction it decoded (one of the two readings is wrong).

The CPU sees 64 KB: its program's last banks fixed at 8000-FFFF and one bank at a time in the window at
6000-7FFF, which the CPU's output lines choose (SETLN). An instruction's place is its offset in the program
region (the ROM set's module: FIXED_PROGRAM, PROGRAM_BANK_SIZE), as kref's traces and symbols.txt give it: in
the window, the bank's; above it, the fixed program's. Code in a bank knows its bank; code in the fixed program
can run with any, so what the analysis knows of the bank is part of the state it follows, with the registers.

Starts at the vectors (the reset's, with the bank its reset selects, RESET_BANK; the interrupts' and the software
interrupts', with no bank known) and at the entry points symbols.txt gives, and follows every branch, jump and
call. The state that goes from block to block, and from a caller into the routine it calls or jumps to, gives
each of A, B, X, Y and U the set of values some way into the block gives it (a way that says nothing about a
register adds nothing: what is wanted is every place the code can go), each one of these:
  - a constant: from an immediate load (LDA #, LDX #...), CLRA, CLRB, CLRD, LEA with an offset from one, a byte or
    word read from the program at a fixed place (LDX $xxxx, LDX #n,R with R a constant);
  - in an accumulator, an index the analysis does not know times a scale (each ASLA doubles it): what the code
    takes a table's entry with;
  - in an index register, a cursor in a table: its address plus an entry's offset (LEAX A,X with X a constant
    and A a scaled index), which moves on with ,X+ and LEA;
  - a byte or a word of a table's entry (LDB A,U with U a constant, LDB ,X+ with X a cursor; LDX A,Y): the table,
    the length of its entries (the scale) and the offset in the entry;
and the bank: the set of banks the CPU can have, from SETLN with an immediate, or with the byte the program keeps
the bank it chose in (the profile's BANK_SHADOW, an offset in the direct page: the program does LDB #n; STB <$0E;
SETLN <$0E), or a byte of a table's entry (LDB A,U; STB <$0E; SETLN <$0E; ...; JSR [A,U]: a table of banks beside
a table of code addresses, both by the same index: each code address in its own bank). A call leaves the bank as it
was (a routine that changes it puts it back), and every register unknown.

Where a jump or call goes through memory, it reads it: JMP [A,X] with X a constant, or [,X] with X a cursor, reads a
table of code addresses there (its entries the scale apart); [,X] or [#n,X] with X a constant, one address;
[$xxxx], one address; JMP ,X goes where X can point, to a constant or to the entries of a table it was read from. A
table's addresses are CPU addresses, read in the bank the CPU has where it jumps (or each in the bank its table of
banks gives): an address in the window is the bank's. A table's length is what symbols.txt says (a data line with
record= code= count=) or, if it says nothing, its entries up to the first that is not a program address a trial
decode accepts, or whose entry starts where the code is or names data (an immediate it loads into X, Y, U or S or
compares them with, an absolute operand, a PC-relative one: what a table read on into holds can decode, so the
walk starts again with
the table stopping there once the code that names it is found), or that a walk found to start an instruction
inside one the code reaches without tables; read backwards (a negative index), at an entry with a byte of any code
in it, an operand too; a zero or FFFF entry (TABLE_UNUSED_ENTRIES) before one that is not is an unused one. A word
read at a constant offset (an index the state knows) is an address only if what it leads to decodes, as an entry's.
A code line with calls=TABLE at a jump or call through memory says where it goes instead: the described table's
entries, none for count=0 (code nothing reaches whose tables hold no code addresses).
"""
import sys
from bisect import bisect_left, bisect_right
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import game_profile  # noqa: E402  (the game's: its root, its ROM set)
import disassemble  # noqa: E402  (symbols.txt and traces, in the format all the games share)
from konami_cpu import disassembler as dis  # noqa: E402

ROOT = game_profile.ROOT
rom = game_profile.rom
BANK_SIZE = rom.PROGRAM_BANK_SIZE
FIXED_START = rom.FIXED_PROGRAM[0]
CODE_END = rom.PROGRAM_SIZE
WINDOW = 0x6000                 # the bank's window, 6000-7FFF; the fixed program from 8000
FIXED_CPU = 0x8000
BANKS = CODE_END // BANK_SIZE
# the vectors (MAME's m6809_base_device), the reset's first
VECTORS = (("reset", 0xFFFE), ("nmi", 0xFFFC), ("swi", 0xFFFA), ("irq", 0xFFF8), ("firq", 0xFFF6), ("swi2", 0xFFF4),
           ("swi3", 0xFFF2))
VECTOR_AREA = 0xFFF0            # the vectors are no code: an erased one (FFFF, the swi2's and swi3's) points into them,
                                # and a table's 0000 FFF8 (bank 16's 7B44, coordinates) would be an RTS there
RESET_BANK = getattr(game_profile, "RESET_BANK", 0)            # the bank the board's reset selects
BANK_SHADOW = getattr(game_profile, "BANK_SHADOW", None)        # the direct page byte the program keeps its bank in
UNUSED_ENTRIES = frozenset(getattr(game_profile, "TABLE_UNUSED_ENTRIES", (0x0000, 0xFFFF)))
TRIAL_INSTRUCTIONS = 12         # a trial decode: this many valid instructions, or fewer up to a jump or return
MAX_TABLE = 256
# the values a register can hold where a block starts before it is no longer followed: few, as a loop that moves a
# register on (LEAX #$60,X over the objects) would otherwise walk its code again for each value. The bank and the
# byte it is kept in can take them all
MAX_VALUES = 16
MAX_BANKS = 64
MAX_SCALE = 16                  # the longest entry a table indexed by a scaled accumulator has

load_symbols = disassemble.load_symbols
load_trace = disassemble.load_trace

# What a register, the bank's byte or the bank can hold (a set of these): (CONSTANT, value); (SCALED, scale), an
# index the analysis does not know times 'scale'; (BYTE, table) and (WORD, table), a byte or a word of an entry of a
# table; (CURSOR, table), a place in a table; 'table' is (address, entry length, offset in the entry, the bank the
# CPU has where the code takes it); UNKNOWN, what a way into the block gives that the analysis does not know (merged
# with the others' values, so that an index some way leaves unknown is not taken for the others' constants). TOO_MANY
# once more than MAX_VALUES; nothing is known of an absent one
CONSTANT, SCALED, BYTE, WORD, CURSOR = "constant", "scaled index", "byte of an entry", "word of an entry", "cursor"
UNKNOWN = ("unknown", 0)
REGISTERS = ("A", "B", "X", "Y", "U")
TOO_MANY = frozenset({("too many", 0)})
LOADS_8 = {"lda": "A", "ldb": "B"}
LOADS_16 = {"ldd": "D", "ldx": "X", "ldy": "Y", "ldu": "U"}
STEPS = {"postinc": 1, "postinc2": 2, "predec": -1, "predec2": -2}


def place_of(bank, cpu):
    """The place of CPU address 'cpu' with 'bank' in the window: None for the RAM and the devices, or for the window
    with no bank known"""
    cpu &= 0xFFFF
    if cpu >= FIXED_CPU:
        return FIXED_START + cpu - FIXED_CPU
    if cpu >= WINDOW and bank is not None:
        return bank * BANK_SIZE + cpu - WINDOW
    return None


def bank_of(place):
    """The bank a place's code runs with: its own in the window, None in the fixed program"""
    return None if place >= FIXED_START else place // BANK_SIZE


def cpu_of(place):
    return FIXED_CPU + place - FIXED_START if place >= FIXED_START else WINDOW + place % BANK_SIZE


class View:
    """The CPU's 64 KB with a bank in the window (None: the window reads FF): what the disassembler decodes"""

    def __init__(self, image, bank):
        self.image = image
        self.bank = bank

    def r8(self, address):
        place = place_of(self.bank, address)
        return self.image[place] if place is not None and place < len(self.image) else 0xFF

    def r16(self, address):
        return (self.r8(address) << 8) | self.r8(address + 1)


def merge(first, second):
    """What two ways into a block can give each register and the bank: their values, and UNKNOWN where a way knows
    nothing of it"""
    merged = {}
    for key in set(first) | set(second):
        one, other = first.get(key), second.get(key)
        union = (one or frozenset({UNKNOWN})) | (other or frozenset({UNKNOWN}))
        limit = MAX_BANKS if key in ("bank", "shadow") else MAX_VALUES
        merged[key] = TOO_MANY if TOO_MANY <= union or len(union) > limit else union
    return merged


def known(values):
    """The values the analysis knows (UNKNOWN and TOO_MANY left out)"""
    if values is None or values == TOO_MANY:
        return ()
    return tuple(value for value in values if value != UNKNOWN)


def unknown_too(values):
    """Whether a way gives a value the analysis does not know"""
    return values is None or values == TOO_MANY or UNKNOWN in values


def banks_in(state):
    """The banks the CPU can have, constants: a tuple, (None,) when none is known"""
    banks = sorted(value for kind, value in known(state.get("bank")) if kind == CONSTANT)
    return tuple(banks) or (None,)


def constant(value):
    return frozenset({(CONSTANT, value)})


class Analysis:
    """The program's code, found from its entry points. Attributes: instructions ({place: Instruction}, the
    Instruction's address the CPU's), functions (call targets), labels (branch and jump targets), code_tables
    ({(place, entry length, offset, banks): entries}), mem (the program by place)."""

    def __init__(self, image, symbols, executed=None):
        self.image = image
        self.mem = rom.ProgramMemory(image)
        self.views = {}
        self.symbols = symbols
        self.executed = executed    # (the coverage passes a trace: the analysis does not use it)
        self.data_starts = set()    # places inside a table read the code names as data: the table stops there
        # what an earlier walk found, which a later walk's tables keep to: a table stops where code the walk reaches
        # without tables starts, and at an entry that starts an instruction inside such code
        self.code_starts = set()
        self.bad_entries = set()
        self.code_behind = set()    # code's bytes, operands too, that a table read backwards stops at
        self.decoded = {}           # {(bank, cpu): Instruction}: what a decode gives does not change from walk to walk
        self.table_starts = set()   # where tables of code start, as any walk found them: no place named as data there
                                    # stops another table (tables overlap)
        self.reset()

    def decode(self, bank, cpu):
        """The instruction at CPU address 'cpu' with 'bank' in the window (None in the fixed program)"""
        key = (bank if cpu < FIXED_CPU else None, cpu)
        instruction = self.decoded.get(key)
        if instruction is None:
            instruction = self.decoded[key] = dis.decode(self.view(key[0]), cpu)
        return instruction

    def reset(self):
        """Everything a walk finds (run() walks again when a table turns out to stop sooner)"""
        self.instructions = {}
        self.block_of = {}          # {place: the place its block starts at}
        self.functions = set()
        self.labels = set()
        self.code_tables = {}
        self.table_spans = {}       # {table place: (first, stop) of its entries} of the tables read without symbols.txt
        self.named = set()          # the places the code names as data
        self.named_by = {}          # {instruction place: the places it names as data}
        self.bank_entries = set()   # the places of the bytes of the tables of banks the tables of code were read with
        self.entry_fields = {}      # {place of a table entry's code address: the place it holds}
        self.from_flow = set()      # block starts the walk reaches by a branch, a jump, a call or a vector
        self.from_table = set()     # and by a table's entry
        self.unresolved = {}        # {place: what} of the jumps and calls the analysis cannot follow
        self.entry_states = {}      # what the registers and the bank can hold where a block starts
        self.work = deque()

    def view(self, bank):
        if bank not in self.views:
            self.views[bank] = View(self.image, bank)
        return self.views[bank]

    # ---- what can be code ----

    def clean(self, place, strict=False):
        """A trial decode: valid instructions up to a jump or a return, or TRIAL_INSTRUCTIONS of them; 'strict', valid
        instructions up to a branch, jump, call or return within twice as many (what a table read backwards takes:
        data before a table decodes as long runs of loads and adds, bank 1E's 7ECA "FE03" as "LDA #$14; ADDA #$14...")"""
        bank = bank_of(place)
        cpu = cpu_of(place)
        for _ in range(TRIAL_INSTRUCTIONS * (2 if strict else 1)):
            if place_of(bank, cpu) is None:
                return False
            instruction = self.decode(bank, cpu)
            if instruction.flow == dis.FLOW_INVALID:
                return False
            if instruction.flow in (dis.FLOW_JUMP, dis.FLOW_RETURN) or strict and instruction.flow != dis.FLOW_NORMAL:
                return True
            cpu = (cpu + instruction.length) & 0xFFFF
        return not strict

    def enter(self, place, state=None, kind=None, source="flow"):
        """A block starts at 'place' with the registers and the bank in 'state'"""
        if place is None or not 0 <= place < CODE_END or place >= place_of(None, VECTOR_AREA):
            return
        if kind == "func":
            self.functions.add(place)
        elif kind == "loc":
            self.labels.add(place)
        (self.from_table if source == "table" else self.from_flow).add(place)
        state = dict(state or {})
        bank = bank_of(place)
        if bank is not None:
            state["bank"] = constant(bank)
        before = self.entry_states.get(place)
        if before is not None:
            state = merge(before, state)
            if state == before:
                return
        self.entry_states[place] = state
        self.work.append(place)

    def go(self, cpu, state, kind, source="flow"):
        """A jump, branch or call to CPU address 'cpu': the place in each bank the CPU can have"""
        cpu &= 0xFFFF
        if cpu >= FIXED_CPU:
            self.enter(place_of(None, cpu), state, kind, source)
            return True
        if cpu < WINDOW:
            return False
        banks = banks_in(state)
        if banks == (None,):
            return False
        for bank in banks:
            entered = dict(state)
            entered["bank"] = constant(bank)
            self.enter(place_of(bank, cpu), entered, kind, source)
        return True

    # ---- the program's bytes ----

    def byte_at(self, cpu, bank):
        return self.view(bank).r8(cpu) if place_of(bank, cpu) is not None else None

    def word_at(self, cpu, bank):
        if place_of(bank, cpu) is None or place_of(bank, cpu + 1) is None:
            return None
        return self.view(bank).r16(cpu)

    # ---- tables ----

    def layout_of(self, table):
        """The entries symbols.txt gives a table: (count, record, code offset), or None"""
        symbol = self.symbols.get(table)
        if symbol is None or symbol.kind != "data" or symbol.layout is None:
            return None
        layout = symbol.layout
        if layout.count is None or len(layout.code) != 1 or layout.code[0][1] is not None:
            raise SystemExit(f"{symbol.name}: only record=, count= and one code= field are read for the Konami CPU")
        return layout.count, layout.record, layout.code[0][0]

    def ends_table(self, place, table):
        """Where a table without a described length stops: code, or a place the code names as data (another table's
        start is none: tables overlap)"""
        return place != table and (place in self.instructions or place in self.code_starts
                                   or (place in self.data_starts and place not in self.table_starts))

    def read_code_table(self, address, length, offset, bank, banks_from=None):
        """The code addresses of a table at CPU address 'address' in 'bank' (entries 'length' bytes apart, the code
        address 'offset' bytes into each): [(cpu, bank)], each in 'bank', or in the bank the table of banks
        'banks_from' (a BYTE value's table) gives for the same entry.

        The index is an 8-bit accumulator, which the CPU adds signed (A,X: konami.lst's INDEXED takes (int8_t) A):
        an entry with a negative index lies before the table's address, in the table of banks too (TheSimpsons'
        objects of a negative kind: F2EC - 2k, F387 - k). A table read without symbols.txt is read both ways from
        its address, each up to its first entry that is not one, as far as the index's scale keeps the offset
        from wrapping round"""
        table = place_of(bank, address)
        if table is None:
            return []
        key = (table, length, offset, banks_from)
        if key in self.code_tables:
            return self.code_tables[key]
        view = self.view(bank)
        entries = []
        described = self.layout_of(table)
        if described is not None:
            count, length, offset = described
            runs = [range(count)]
        else:
            runs = [range(0, max(1, 0x80 // length)), range(-1, -max(1, 0x80 // length) - 1, -1)]
        for run in runs:
            for index in run:
                record = (address + length * index) & 0xFFFF
                where = (record + offset) & 0xFFFF
                record_place, where_place = place_of(bank, record), place_of(bank, where)
                if record_place is None or where_place is None:
                    break
                entry_bank = bank
                bank_place = None   # (where the entry's bank is: the coverage's)
                if banks_from is not None:
                    # (the CPU's lines take the byte's bits 0-5: FF is the last bank, the fixed program's own, which
                    # an entry with its code in the fixed program has)
                    banks_table, banks_length, banks_offset, banks_bank = banks_from
                    held_cpu = (banks_table + banks_length * index + banks_offset) & 0xFFFF
                    held = self.byte_at(held_cpu, banks_bank)
                    if held is None:
                        break
                    bank_place = place_of(banks_bank, held_cpu)
                    entry_bank = held & 0x3F
                value = view.r16(where)
                if described is None:
                    step = length if index >= 0 else -length
                    if value in UNUSED_ENTRIES and view.r16((where + step) & 0xFFFF) not in UNUSED_ENTRIES:
                        continue
                    target = place_of(entry_bank, value)
                    # (read backwards, any code's byte in the record stops it too: see run())
                    if (index and self.ends_table(record_place, table)) \
                            or (index < 0 and any(place in self.code_behind
                                                  for place in range(record_place, record_place + length))) \
                            or target is None or target >= place_of(None, VECTOR_AREA) or target in self.bad_entries \
                            or not self.clean(target, strict=index < 0):
                        break
                    low, high = self.table_spans.get(table, (table, table))
                    self.table_spans[table] = (min(low, record_place), max(high, record_place + length))
                entries.append((value, entry_bank))
                target = place_of(entry_bank, value)
                if target is not None and value not in UNUSED_ENTRIES:
                    self.entry_fields[where_place] = target
                if bank_place is not None:
                    self.bank_entries.add(bank_place)
        self.code_tables[key] = entries
        return entries

    # ---- the registers ----

    def index_options(self, accumulator, state):
        """What an accumulator used as an index holds, each way into the code: ('offset', n), a signed offset (the CPU
        adds A and B signed, konami.lst's INDEXED), or ('scale', s), an index the analysis does not know times s"""
        if accumulator == "D":
            return [("scale", 1)]
        held = state.get(accumulator)
        options = {("scale", 1)} if unknown_too(held) else set()
        for kind, value in known(held):
            if kind == CONSTANT:
                options.add(("offset", value - 0x100 if value & 0x80 else value))
            elif kind == SCALED:
                options.add(("scale", value))
            else:
                options.add(("scale", 1))
        return sorted(options)

    def read_through(self, operand, state, word):
        """What a load through an indexed operand gives: a set of values, or None when nothing is known (an
        indexed operand that moves its register on is handled by the caller)"""
        if operand.indirect:
            return None
        banks = banks_in(state)
        read = self.word_at if word else self.byte_at
        if operand.kind == "extended":
            values = {(CONSTANT, read(operand.value, bank)) for bank in banks}
            return None if (CONSTANT, None) in values else frozenset(values)
        if operand.kind == "offset" and operand.reg == "PC":
            values = {(CONSTANT, read(operand.target, bank)) for bank in banks}
            return None if (CONSTANT, None) in values else frozenset(values)
        if operand.reg not in REGISTERS:
            return None
        held = state.get(operand.reg)
        bases = known(held)
        if not bases:
            return None
        values = {UNKNOWN} if unknown_too(held) else set()
        for kind, base in bases:
            if operand.kind == "accumulator" and kind == CONSTANT:
                for option, amount in self.index_options(operand.accumulator, state):
                    if option == "offset":
                        for bank in banks:
                            held = read((base + amount) & 0xFFFF, bank)
                            if held is None:
                                return None
                            values.add((CONSTANT, held))
                    else:
                        values.update((WORD if word else BYTE, (base, amount, 0, bank)) for bank in banks)
            elif operand.kind in ("offset", "register", "postinc", "postinc2", "predec", "predec2"):
                disp = operand.disp if operand.kind == "offset" else min(STEPS.get(operand.kind, 0), 0)
                if kind == CONSTANT:
                    for bank in banks:
                        held = read((base + disp) & 0xFFFF, bank)
                        if held is None:
                            return None
                        values.add((CONSTANT, held))
                elif kind == CURSOR:
                    table, length, offset, bank = base
                    values.add((WORD if word else BYTE, (table, length, offset + disp, bank)))
                else:
                    return None
            else:
                return None
        return frozenset(values)

    def step(self, instruction, state):
        """What an instruction leaves in the registers, the bank's byte and the bank"""
        mnemonic = instruction.mnemonic
        operands = instruction.operands
        operand = operands[0] if operands else None

        def put(register, values):
            if values and values != TOO_MANY and register in ("X", "Y", "U"):
                # (an address in the RAM or the devices finds no code: what the index registers point at there,
                # the objects a loop walks, is not followed, but it is a value the analysis does not know)
                kept = frozenset(value for value in values
                                 if not (value[0] in (CONSTANT, CURSOR)
                                         and (value[1] if value[0] == CONSTANT else value[1][0]) < WINDOW))
                values = kept if kept == values else kept | {UNKNOWN}
            if values and values != frozenset({UNKNOWN}):
                state[register] = values
            else:
                state.pop(register, None)

        def moved_on(values, amount):
            """A register's values moved on by 'amount' (a constant, or a cursor in its table); None when one of
            them cannot be"""
            moved = {UNKNOWN} if unknown_too(values) else set()
            for kind, value in known(values):
                if kind == CONSTANT:
                    moved.add((CONSTANT, (value + amount) & 0xFFFF))
                elif kind == CURSOR:
                    table, length, offset, bank = value
                    moved.add((CURSOR, (table, length, offset + amount, bank)))
                else:
                    return None
            return frozenset(moved)

        # what an indexed operand reads, before it moves its register on
        loaded = None
        if mnemonic in LOADS_8 or mnemonic in LOADS_16:
            if operand.mode == "immediate":
                loaded = constant(operand.value)
            else:
                loaded = self.read_through(operand, state, mnemonic in LOADS_16)
        # a register an indexed operand moves on
        if operand is not None and operand.mode == "indexed" and operand.kind in STEPS and operand.reg in REGISTERS:
            put(operand.reg, moved_on(state.get(operand.reg), STEPS[operand.kind]))

        if mnemonic in LOADS_8:
            put(LOADS_8[mnemonic], loaded)
        elif mnemonic in LOADS_16:
            register = LOADS_16[mnemonic]
            if register == "D":
                values = known(loaded)
                consts = [value for kind, value in values if kind == CONSTANT]
                whole = bool(consts) and len(consts) == len(values)
                put("A", frozenset((CONSTANT, value >> 8) for value in consts) if whole else None)
                put("B", frozenset((CONSTANT, value & 0xFF) for value in consts) if whole else None)
            else:
                put(register, loaded)
        elif mnemonic in ("leax", "leay", "leau"):
            register = mnemonic[3].upper()
            values = None
            if not operand.indirect:
                if operand.kind in ("offset", "register") and operand.reg in REGISTERS:
                    values = moved_on(state.get(operand.reg), operand.disp)
                elif operand.kind == "accumulator" and operand.reg in REGISTERS:
                    bases = known(state.get(operand.reg))
                    if bases and all(kind == CONSTANT for kind, _ in bases):
                        moved = {UNKNOWN} if unknown_too(state.get(operand.reg)) else set()
                        for option, amount in self.index_options(operand.accumulator, state):
                            for _, base in bases:
                                if option == "offset":
                                    moved.add((CONSTANT, (base + amount) & 0xFFFF))
                                else:
                                    moved.update((CURSOR, (base, amount, 0, bank)) for bank in banks_in(state))
                        values = frozenset(moved)
                elif operand.kind == "offset" and operand.reg == "PC":
                    values = constant(operand.target)
                elif operand.kind == "extended":
                    values = constant(operand.value)
            put(register, values)
        elif mnemonic in ("clra", "clrb"):
            put(mnemonic[3].upper(), constant(0))
        elif mnemonic == "clrd":
            put("A", constant(0))
            put("B", constant(0))
        elif mnemonic in ("asla", "aslb"):
            register = mnemonic[3].upper()
            # (an index some way leaves unknown, doubled, is an index the analysis does not know times 2)
            doubled = {(SCALED, 2)} if unknown_too(state.get(register)) else set()
            for kind, value in known(state.get(register)):
                if kind == CONSTANT:
                    doubled.add((CONSTANT, (value << 1) & 0xFF))
                elif kind == SCALED:
                    # (no table has entries longer than MAX_SCALE: a scale past it, ASLA in a loop, is no index)
                    doubled.add((SCALED, value * 2) if value * 2 <= MAX_SCALE else UNKNOWN)
                else:
                    doubled.add((SCALED, 2))    # (a byte of a table, doubled: an index times 2)
            put(register, frozenset(doubled))
        elif mnemonic in ("exg", "tfr"):
            source, destination = operand.source, operand.destination
            words = {"X", "Y", "U"}
            if (source in words and destination in words) or source == destination == "A":
                held_source, held_destination = state.get(source), state.get(destination)
                put(destination, held_source)
                if operand.exchange:
                    put(source, held_destination)
            else:
                for register in (source, destination):
                    state.pop(register, None)
                    if register == "B":
                        state.pop("A", None)
        elif mnemonic in ("pulls", "pullu"):
            for register in operand.registers:
                if register == "D":
                    state.pop("A", None)
                    state.pop("B", None)
                elif register in REGISTERS:
                    state.pop(register, None)
        elif mnemonic in ("stb", "sta", "std", "stx", "sty", "stu", "sts", "cmpa", "cmpb", "cmpd", "cmpx", "cmpy",
                          "cmpu", "cmps", "bita", "bitb", "tst", "tsta", "tstb", "tstd", "tstw", "pushs", "pushu",
                          "setln", "andcc", "orcc", "nop", "clr", "clrw", "com", "neg", "inc", "dec", "lsr", "ror",
                          "asr", "asl", "rol", "lsrw", "rorw", "asrw", "aslw", "rolw", "negw", "incw", "decw",
                          "lsrwa", "rorwa", "asrwa", "aslwa", "rolwa", "leas"):
            pass    # (no register of A, B, X, Y, U written but by the indexed operand above)
        else:
            # everything else writes its register: the names end in it (ADDA, ASLD...), or the CPU's own
            written = {"mul": "AB", "lmul": "XY", "div": "XB", "abx": "X", "dxjnz": "X", "dbjnz": "B", "sex": "A",
                       "daa": "A", "bmove": "XYUB", "move": "XYUB", "bset": "XUB", "bsetw": "XUB"}.get(mnemonic)
            if written is None:
                last = mnemonic[-1].upper()
                written = "AB" if last == "D" or mnemonic.endswith("di") else last if last in "AB" else "ABXYU"
            for register in written:
                state.pop(register, None)

        # the byte the program keeps its bank in, and the bank
        if BANK_SHADOW is not None and operand is not None and operand.mode == "indexed" and operand.kind == "direct" \
                and not operand.indirect and operand.value in (BANK_SHADOW, BANK_SHADOW - 1):
            if mnemonic in ("sta", "stb") and operand.value == BANK_SHADOW:
                put("shadow", state.get(mnemonic[2].upper()))
            elif mnemonic == "std":
                put("shadow", state.get("B" if operand.value == BANK_SHADOW - 1 else "A"))
            elif mnemonic in ("clr", "clrw"):
                put("shadow", constant(0))
            elif mnemonic.startswith(("st", "inc", "dec", "com", "neg", "lsr", "ror", "asr", "asl", "rol", "clr")):
                state.pop("shadow", None)
        if mnemonic == "setln":
            values = None
            if operand.mode == "immediate":
                values = constant(operand.value & 0x3F)
            elif operand.mode == "indexed" and operand.kind == "direct" and not operand.indirect \
                    and operand.value == BANK_SHADOW:
                held = known(state.get("shadow"))
                if held:
                    values = frozenset((CONSTANT, value & 0x3F) if kind == CONSTANT else (kind, value)
                                       for kind, value in held)
            put("bank", values)

    def named_places(self, at, instruction, state):
        """The places of the program an instruction names as data: an address it loads into a register, an
        absolute or PC-relative operand of a normal instruction"""
        if instruction.flow != dis.FLOW_NORMAL:
            return
        values = []
        for operand in instruction.operands:
            # (D holds numbers as often as addresses: LDD #$F500 in bank 19, 032ADF, is no place of the program)
            if operand.mode == "immediate" and operand.size == 2 and instruction.mnemonic in (
                    "ldx", "ldy", "ldu", "lds", "cmpx", "cmpy", "cmpu"):
                values.append(operand.value)
            elif operand.mode == "indexed" and operand.kind == "extended":
                values.append(operand.value)
            elif operand.mode == "indexed" and operand.target is not None:
                values.append(operand.target)
        for value in values:
            for bank in banks_in(state):
                place = place_of(bank, value)
                if place is not None:
                    self.named.add(place)
                    self.named_by.setdefault(at, set()).add(place)

    def table_targets(self, address, length, offset, state, table_bank=None):
        """The code addresses of a table, in the banks the CPU can have where the code jumps through it"""
        banks_from = [value for kind, value in known(state.get("bank")) if kind == BYTE]
        targets = []
        if banks_from:
            for source in banks_from:
                targets += self.read_code_table(address, length, offset, table_bank, source)
        else:
            for bank in (banks_in(state) if table_bank is None else (table_bank,)):
                targets += self.read_code_table(address, length, offset, bank)
        return targets

    def computed(self, place, instruction, state):
        """A jump or call through a register or memory: where it can go"""
        kind = "func" if instruction.flow == dis.FLOW_CALL else "loc"
        operand = instruction.operands[0]
        targets = []    # (cpu, bank)
        banks = banks_in(state)
        told = self.symbols.get(place)
        if told is not None and told.calls is not None:
            # (symbols.txt says where: "code NAME calls=TABLE", the table's described entries; none, a jump that goes
            # nowhere, as in code nothing reaches whose tables hold no code addresses)
            table = next((address for address, symbol in self.symbols.items() if symbol.name == told.calls), None)
            described = self.layout_of(table) if table is not None else None
            if described is None:
                raise SystemExit(f"symbols.txt: {told.name} calls {told.calls}, which is no described table")
            count, length, offset = described
            view = self.view(bank_of(table))
            for index in range(count):
                target = view.r16((cpu_of(table) + length * index + offset) & 0xFFFF)
                self.go(target, dict(state, bank=constant(bank_of(table))), kind, "table")
            return
        if operand.mode == "registers":
            register = operand.source if operand.destination == "PC" else operand.destination
            for held, value in known(state.get(register)):
                if held == CONSTANT:
                    targets += [(value, bank) for bank in banks]
                elif held == WORD:
                    table, length, offset, bank = value
                    targets += self.table_targets(table, length, offset, state, bank)
        elif operand.indirect and operand.kind == "extended":
            targets += [(word, bank) for bank in banks if (word := self.word_at(operand.value, bank)) is not None]
        elif operand.indirect and operand.kind == "accumulator" and operand.reg in REGISTERS:
            for held, base in known(state.get(operand.reg)):
                if held != CONSTANT:
                    continue
                for option, amount in self.index_options(operand.accumulator, state):
                    if option == "offset":
                        # (only words that lead to code that decodes, as a table's entries: an index the state
                        # merges from ways that do not go here is no entry, 0205C7 "JSR [A,U]" with A from 44,X
                        # where X is still a table of handlers' address, which would take 7C28's 7CD8, a record)
                        targets += [(word, bank) for bank in banks
                                    if (word := self.word_at((base + amount) & 0xFFFF, bank)) is not None
                                    and (place_of(bank, word) is None or self.clean(place_of(bank, word)))]
                    else:
                        targets += self.table_targets(base, max(amount, 2), 0, state)
        elif operand.indirect and operand.kind in ("register", "offset") and operand.reg in REGISTERS:
            for held, base in known(state.get(operand.reg)):
                if held == CONSTANT:
                    targets += [(word, bank) for bank in banks
                                if (word := self.word_at((base + operand.disp) & 0xFFFF, bank)) is not None]
                elif held == CURSOR:
                    table, length, offset, bank = base
                    targets += self.table_targets(table, length, offset + operand.disp, state, bank)
        elif not operand.indirect and operand.kind in ("register", "offset") and operand.reg in REGISTERS:
            for held, value in known(state.get(operand.reg)):
                if held == CONSTANT:
                    targets += [((value + operand.disp) & 0xFFFF, bank) for bank in banks]
                elif held == WORD and operand.disp == 0:
                    table, length, offset, bank = value
                    targets += self.table_targets(table, length, offset, state, bank)
        if not targets:
            self.unresolved[place] = instruction.text
        for cpu, bank in targets:
            if place_of(bank, cpu) in self.bad_entries:
                continue    # (a word a walk found to start an instruction inside code reached without tables)
            entered = dict(state)
            entered["bank"] = constant(bank) if bank is not None else frozenset()
            if not entered["bank"]:
                entered.pop("bank")
            self.go(cpu, entered, kind, "table")

    # ---- the walk ----

    def walk(self, place):
        state = dict(self.entry_states[place])
        start = place
        bank = bank_of(place)
        while True:
            if place is None or not 0 <= place < CODE_END:
                return
            cpu = cpu_of(place)
            # (in the fixed program the bank does not change what is decoded there: only where it goes)
            instruction = self.instructions.get(place) or self.decode(bank, cpu)
            if instruction.flow == dis.FLOW_INVALID:
                return
            self.instructions[place] = instruction
            self.block_of.setdefault(place, start)
            self.named_places(place, instruction, state)
            flow = instruction.flow
            if flow == dis.FLOW_CALL:
                if instruction.target is not None:
                    if not self.go(instruction.target, state, "func"):
                        self.unresolved[place] = instruction.text
                else:
                    self.computed(place, instruction, state)
                # what the call leaves: the bank as it was, the registers unknown
                state = {key: values for key, values in state.items() if key in ("bank", "shadow")}
            elif flow == dis.FLOW_BRANCH:
                if not self.go(instruction.target, state, "loc"):
                    self.unresolved[place] = instruction.text
            elif flow == dis.FLOW_JUMP:
                if instruction.target is not None:
                    if not self.go(instruction.target, state, "loc"):
                        self.unresolved[place] = instruction.text
                else:
                    self.computed(place, instruction, state)
                return
            elif flow == dis.FLOW_RETURN:
                return
            else:
                self.step(instruction, state)
                if instruction.mnemonic == "setln" and bank is not None:
                    # (code in the window that changes the bank goes on in another bank's bytes)
                    if known(state.get("bank")) != constant(bank):
                        after = (cpu + instruction.length) & 0xFFFF
                        if after < FIXED_CPU:
                            self.go(after, state, "loc")
                            return
            following = place_of(bank, (cpu + instruction.length) & 0xFFFF)
            if following is not None and following != place + instruction.length and bank is not None:
                # past the window's end: the fixed program
                self.enter(following, state)
                return
            place = following
            if place in self.entry_states and place != start:
                self.enter(place, state)
                return

    def walk_all(self):
        fixed = self.view(None)
        for name, vector in VECTORS:
            target = fixed.r16(vector)
            if target >= FIXED_CPU:
                state = {"bank": constant(RESET_BANK)} if name == "reset" else {}
                self.go(target, state, "func")
        for place, symbol in self.symbols.items():
            if symbol.kind in ("func", "code"):
                self.enter(place, kind="func" if symbol.kind == "func" else "loc")
        while self.work:
            self.walk(self.work.popleft())

    def run(self):
        while True:
            self.walk_all()
            # a place the code names as data inside a table read without symbols.txt: the table stops there, and
            # what it held from there on was no code; code the walk reaches without tables inside one: the table
            # stops there too; and an entry that starts an instruction inside such code is no code address. The
            # walk starts again with them
            # (a named place where another table of code starts is no end: tables overlap, and an index past the
            # other's start is the other's entry: bank 30's 7F62, whose entry 13 is 7F7C's first)
            starts = {key[0] for key, entries in self.code_tables.items() if entries}
            self.table_starts |= starts
            named = sorted(self.named - starts)
            inside = set()
            for table, (low, high) in self.table_spans.items():
                inside.update(place for place in named[bisect_left(named, low):bisect_left(named, high)]
                              if place != table)
            by_flow = {place for place, start in self.block_of.items()
                       if start in self.from_flow or start not in self.from_table}
            cut = set()
            for table, (low, high) in self.table_spans.items():
                cut.update(place for place in range(low, high) if place != table and place in by_flow)
            # a table read backwards from its address (a negative index) stops where an entry would be any code's
            # bytes, the operand of an instruction too: bank 1A's 7300 runs back into 72F9 "LBRA $7038", whose offset
            # FD3C would be an entry, bank 2F's 6277 into 6274 "LBRA $603B"
            interiors = {place for start, instruction in self.instructions.items()
                         for place in range(start, start + instruction.length)}
            covered = set()
            for table, (low, high) in self.table_spans.items():
                covered.update(place for place in range(low, table) if place in interiors)
            bad = set()
            for start, instruction in self.instructions.items():
                for place in range(start + 1, start + instruction.length):
                    if place in self.instructions:
                        # two readings of the same bytes: the one only a table's entry reaches is wrong
                        first, second = self.block_of[start], self.block_of[place]
                        if start in by_flow and place not in by_flow:
                            bad.add(second)
                        elif place in by_flow and start not in by_flow:
                            bad.add(first)
            if inside <= self.data_starts and cut <= self.code_starts and bad <= self.bad_entries \
                    and covered <= self.code_behind:
                self.finish()
                break
            self.data_starts |= inside
            self.code_starts |= cut
            self.code_behind |= covered
            self.bad_entries |= bad
            self.reset()


    def finish(self):
        """What the coverage (the core's coverage.py) reads: every byte of code -> the place of its instruction
        (owner), the places of the code addresses of the tables (data_words, -> the place each holds), the data
        fields of the tables symbols.txt describes (data_fields), and the bytes whose layout is known (described):
        the vectors, the tables of code and the bytes of the tables of banks read with them"""
        self.owner = {}
        self.data_words = dict(self.entry_fields)
        self.data_fields = {}
        self.described = []
        self.fill = []
        for start, instruction in self.instructions.items():
            bank = bank_of(start)
            for offset in range(instruction.length):
                place = place_of(bank, (instruction.address + offset) & 0xFFFF)
                if place is not None:
                    self.owner[place] = start
        self.described.append((place_of(None, 0xFFF0), CODE_END))
        self.described += sorted(self.table_spans.values())
        self.described += [(place, place + 1) for place in sorted(self.bank_entries)]
        for address, symbol in self.symbols.items():
            layout = symbol.layout
            if symbol.kind != "data" or layout is None or layout.count is None:
                continue
            bank = bank_of(address)
            for index in range(layout.count):
                record = address + layout.record * index
                for offset, _ in layout.code:
                    target = place_of(bank, self.mem.r16(record + offset))
                    if target is not None and self.mem.r16(record + offset) not in UNUSED_ENTRIES:
                        self.data_words[record + offset] = target
                for offset in layout.data:
                    target = place_of(bank, self.mem.r16(record + offset))
                    if target is not None:
                        self.data_fields[record + offset] = target
            self.described.append((address, address + layout.record * layout.count))


# What the coverage (the core's coverage.py) needs of the Konami CPU: its pointers are 16-bit CPU addresses, big
# endian, and its code has no alignment (no padding before a function)
BYTE_ORDER = "big"
POINTER_SIZE = 2
FUNCTION_ALIGNMENT = 1
PADDING_BYTES = ()


def pointers_to(analysis, targets):
    """Words outside code whose value is the CPU address of one of 'targets' where the word is: in the same bank, or
    in the fixed program for a target there"""
    found = {}
    by_cpu = {}
    for target in targets:
        by_cpu.setdefault(cpu_of(target), []).append(target)
    for place in range(0, CODE_END - 1):
        if place in analysis.owner:
            continue
        for target in by_cpu.get(analysis.mem.r16(place), ()):
            if bank_of(target) is None or bank_of(target) == bank_of(place):
                found.setdefault(target, []).append(place)
    return found


def write_listing(analysis, path):
    names = {place: symbol.name for place, symbol in analysis.symbols.items()}
    lines = []
    for place in sorted(analysis.instructions):
        instruction = analysis.instructions[place]
        if place in names:
            lines.append(f"\n{names[place]}:")
        elif place in analysis.functions:
            lines.append(f"\nfunc_{place:06X}:")
        elif place in analysis.labels:
            lines.append(f"loc_{place:06X}:")
        lines.append(f"    {place:06X} {instruction.address:04X}  {instruction.text}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def write_report(analysis, path, executed=None):
    instructions = analysis.instructions
    code_bytes = sum(instruction.length for instruction in instructions.values())
    lines = [f"instructions: {len(instructions)} ({code_bytes} bytes of {CODE_END})",
             f"functions: {len(analysis.functions)}, labels: {len(analysis.labels)}",
             f"code tables: {len(analysis.code_tables)} ({sum(map(len, analysis.code_tables.values()))} entries)",
             f"jumps and calls not followed: {len(analysis.unresolved)}"]
    for place, text in sorted(analysis.unresolved.items())[:60]:
        lines.append(f"  {place:06X}  {text}")
    if executed is not None:
        found = sum(1 for place in executed if place in instructions)
        inside = sorted(place for start, instruction in instructions.items()
                        for place in range(start + 1, start + instruction.length) if place in executed)
        lines.append(f"executed: {len(executed)}, found without traces: {found}, missing: {len(executed) - found}")
        lines.append(f"executed places inside a decoded instruction: {len(inside)} "
                     + " ".join(f"{place:06X}" for place in inside[:40]))
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
    image = rom.build_program(rom.load_set(source))
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
