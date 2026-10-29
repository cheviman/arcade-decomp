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
  - a field of an entry of a table: movea.l (d8,An,Xn) with a displacement, a field of a table of records.
    How long the records are the code does not say: symbols.txt does (record=, code=, count=), and the
    report lists the ones it does not describe.
A table's length is what symbols.txt says (a data line with record=4 code=0 count=N) or, if it says
nothing, its entries up to the first that is not a code address a trial decode accepts, or that is code or
another table already; a zero entry before one that is not (and first, or after one that is not) is an
unused one (a state with no handler). A jump or call through a field of a structure, movea.l (d16,An), Am
then jsr (Am), makes that offset a handler slot: an address stored in such a slot (move.l #code, or a
register holding it) is code. symbols.txt keeps the V60's format (disassemble.load_symbols): func, code and
data lines.
"""
import sys
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
TRIAL_INSTRUCTIONS = 12         # a trial decode: this many valid instructions, or fewer up to a jump or return
MAX_TABLE = 1024
MAX_VALUES = 1024               # values a register can hold before it is no longer followed (the objects' state
                                # dispatcher at TMNT's 048236 gets a table from each of some 170 places)

load_symbols = disassemble.load_symbols
load_trace = disassemble.load_trace

# What an address register can hold: a set of (CONSTANT, address), (ENTRY, table), (ENTRY2, table) and (FIELD,
# (table, offset)); TOO_MANY once it could hold more than MAX_VALUES; nothing is known of a register that is absent
CONSTANT, ENTRY, ENTRY2, FIELD = "address", "entry", "entry of an entry", "field of an entry"
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

    def __init__(self, image, symbols):
        self.mem = dis.FlatMemory(image)
        self.symbols = symbols
        self.instructions = {}
        self.functions = set()
        self.labels = set()
        self.code_tables = {}
        self.table_tables = {}
        self.slots = set()
        self.field_tables = {}      # {(table, offset): entries} of the tables of records symbols.txt describes
        self.undescribed = set()    # (table, offset) of the ones it does not
        self.stores = {}            # handler slot candidates: {offset: {addresses stored there}}
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

    def described(self, table):
        """The entries symbols.txt gives a table: (count, record, code offset), or None"""
        symbol = self.symbols.get(table)
        if symbol is None or symbol.kind != "data" or symbol.layout is None:
            return None
        layout = symbol.layout
        if layout.count is None or len(layout.code) != 1 or layout.code[0][1] is not None:
            raise SystemExit(f"{symbol.name}: only record=, count= and one 32-bit code= field are read for the 68000")
        return layout.count, layout.record, layout.code[0][0]

    def ends_table(self, address, table):
        """Where a table without a described length stops: code, or another table"""
        return address != table and (address in self.instructions or address in self.code_tables
                                     or address in self.table_tables)

    def unused(self, where, first):
        """A zero entry before one that is not, and first or after one that is not: a state with no handler"""
        return self.mem.r32(where) == 0 and self.mem.r32(where + 4) != 0 and (first or self.mem.r32(where - 4) != 0)

    def read_code_table(self, table):
        if table in self.code_tables:
            return
        entries = []
        described = self.described(table)
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
        self.code_tables[table] = entries
        for value in entries:
            self.enter(value, kind="loc")

    def read_field_table(self, table, offset):
        """A code address in a field of a table of records: as long as symbols.txt describes the records"""
        if (table, offset) in self.field_tables:
            return
        described = self.described(table)
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
        described = self.described(table)
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
        values = set()
        for kind, base in known(state.get(operand.reg) if mode in ("indirect", "disp", "index") else None):
            if mode == "indirect" and kind == CONSTANT:
                values.add(self.read_long(base))
            elif mode == "disp" and kind == CONSTANT:
                values.add(self.read_long(base + operand.disp))
            elif mode == "index" and kind == CONSTANT:
                values.add((FIELD, (base, operand.disp)) if operand.disp else (ENTRY, base))
            elif mode == "index" and kind == ENTRY and not operand.disp:
                values.add((ENTRY2, base))
        values.discard(None)
        return frozenset(values) or None

    def effective_addresses(self, operand, state):
        """The addresses lea and pea can compute"""
        if operand.mode in ("abs_short", "abs_long", "pc_disp"):
            return frozenset({(CONSTANT, operand.value & 0xFFFFFF)})
        if operand.mode not in ("indirect", "disp"):
            return None
        offset = operand.disp if operand.mode == "disp" else 0
        values = frozenset((CONSTANT, (base + offset) & 0xFFFFFF)
                           for kind, base in known(state.get(operand.reg)) if kind == CONSTANT)
        return values or None

    def step(self, instruction, state):
        """What the instruction leaves in the address registers"""
        operands = instruction.operands
        for operand in operands:
            if operand.mode in ("postinc", "predec"):
                state.pop(operand.reg, None)
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
        if len(operands) != 2 or operands[1].mode != "areg":
            return
        target = operands[1].reg
        source = operands[0]
        if mnemonic == "lea":
            values = self.effective_addresses(source, state)
        elif mnemonic in ("movea", "move"):
            values = self.source_values(source, instruction.size, state)
        elif mnemonic in ("adda", "suba", "addq", "subq") and source.mode in ("immediate", "quick"):
            # a single address moved by a constant; a register with more (a pointer a loop moves on: each time
            # round would add one more) is no longer followed
            current = known(state.get(target))
            amount = source.value if mnemonic in ("adda", "addq") else -source.value
            values = frozenset({(CONSTANT, (base + amount) & 0xFFFFFF) for kind, base in current}) \
                if len(current) == 1 and next(iter(current))[0] == CONSTANT else None
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
                state = {}
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
