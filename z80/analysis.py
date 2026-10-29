# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        analysis.py
Description: the Z80 sound program, found from its entry points and split into functions
Version:     0.1.0
Created:     13-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Starts at the entry points (System 32's by default: the reset, 0000, and the interrupt handler, 0038: the
board's Z80 runs in interrupt mode 1, and nothing is wired to its NMI), follows every jump, branch and call,
and through the jump tables sound_symbols.txt describes. Every call target is a function, as is every "func"
line; a function's body is the code reachable from its entry without going through another function's entry
(running into one is a tail call). The program lives below the profile's SOUND_CODE_END (0000-9FFF on System
32: the banked page at A000-BFFF only ever holds data). Code the entry points never reach (routines nothing
calls) comes in through its "func" lines: such a function is in 'unreached'.

The game's profile (tools/game_profile.py) gives what differs from System 32's sound board: SOUND_ENTRIES
({address: "func"}), SOUND_CODE_END, and SOUND_FOLLOW_VALUES: with it, a jump through a register goes where
what the register holds says (z80/values.py walks it without traces), not only where a via line says: a
constant, the entries of a table of code addresses (a switch, as a via line makes it; its length comes from
its jumps line), or the words the program stores in a record's slot, which are handlers, functions of their
own that the jump calls through the slot (a tail call). Those are in analysis.slot_jumps ({address of the
jump: slots}), analysis.slots ({(IX or IY, offset): handlers}) and analysis.handler_tables ({table:
handlers}, tables of handlers the program stores in a slot).

sound_symbols.txt's lines here: func, code, loop (function entries and labels), data, and
    ADDRESS  jumps  NAME COUNT [first=K]   a table of COUNT 16-bit code addresses at ADDRESS, the one the code
                                           indexes; with first=K, the code never reads its first K entries
                                           (the table starts K entries on)
    ADDRESS  via    TABLE                  the JP (HL), JP (IX) or JP (IY) at ADDRESS goes to an entry of TABLE

Program has the attributes the decompiler takes from tools/recompile.py's: entries, bodies, names,
symbols, mem, successors(), and an analysis with instructions and jump_tables; and 'reached', the
addresses the program runs, and 'unreached', the functions outside them.
"""
from dataclasses import dataclass, field
from pathlib import Path

from . import disassembler as dis
from . import values

ENTRIES = {0x0000: "func", 0x0038: "func"}    # System 32's: the reset and the interrupt handler (IM 1)
CODE_END = 0xA000                              # System 32's: the program below, a banked page of data above
KINDS = ("func", "code", "loop", "data", "jumps", "via")


def settings():
    """The entry points, the end of the program's code and whether to follow what the registers hold: the
    game's profile's (SOUND_ENTRIES, SOUND_CODE_END, SOUND_FOLLOW_VALUES), System 32's where it says nothing"""
    try:
        import game_profile
    except ImportError:
        game_profile = None
    return (getattr(game_profile, "SOUND_ENTRIES", ENTRIES), getattr(game_profile, "SOUND_CODE_END", CODE_END),
            getattr(game_profile, "SOUND_FOLLOW_VALUES", False))


@dataclass
class Symbol:
    kind: str
    name: str
    count: int = 0          # jumps: entries in the table
    table: str = ""         # via: the table the jump uses
    first: int = 0          # jumps: the first entry the code reads


def load_symbols(path):
    """sound_symbols.txt: {address: Symbol}. Lines of other kinds (names for the decompiler) are skipped."""
    symbols = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3 or parts[1] not in KINDS:
            continue
        where = f"{Path(path).name}:{number}"
        try:
            address = int(parts[0], 16)
        except ValueError:
            raise SystemExit(f"{where}: '{parts[0]}' is not an address") from None
        if parts[1] == "jumps":
            first = parts[4].partition("=")[2] if len(parts) == 5 and parts[4].startswith("first=") else None
            if len(parts) not in (4, 5) or not parts[3].isdigit() or (len(parts) == 5 and not (first or "").isdigit()):
                raise SystemExit(f"{where}: expected 'ADDRESS jumps NAME COUNT [first=K]'")
            symbols[address] = Symbol("jumps", parts[2], count=int(parts[3]), first=int(first or 0))
        elif parts[1] == "via":
            symbols[address] = Symbol("via", f"via_{address:04X}", table=parts[2])
        else:
            symbols[address] = Symbol(parts[1], parts[2])
    return symbols


@dataclass
class Analysis:
    instructions: dict = field(default_factory=dict)     # address -> dis.Instruction
    jump_tables: dict = field(default_factory=dict)      # address of a JP (rr) -> its targets
    call_tables: dict = field(default_factory=dict)
    pointer_tables: dict = field(default_factory=dict)
    return_jumps: dict = field(default_factory=dict)
    push_jumps: dict = field(default_factory=dict)
    pointer_writes: list = field(default_factory=list)
    described_targets: dict = field(default_factory=dict)
    slot_jumps: dict = field(default_factory=dict)       # address of a JP (rr) through a slot -> its slots
    slots: dict = field(default_factory=dict)            # (register, offset) -> the handlers stored there
    handler_tables: dict = field(default_factory=dict)   # table of handlers stored in a slot -> its handlers
    walk: object = None                                  # the last walk of the registers (values.Walk)


class Program:
    """The analysed sound program, split into functions."""

    def __init__(self, image, symbols):
        self.image = image
        self.mem = dis.FlatMemory(image, 0xFFFF)
        self.symbols = symbols
        self.entry_points, self.code_end, follow = settings()
        self.analysis = Analysis()
        tables = {symbol.name: address for address, symbol in symbols.items() if symbol.kind == "jumps"}
        for address, symbol in symbols.items():
            if symbol.kind != "via":
                continue
            if symbol.table not in tables:
                raise SystemExit(f"sound_symbols.txt: {address:04X} uses '{symbol.table}', which is not a jumps table")
            start = tables[symbol.table]
            self.analysis.jump_tables[address] = self.table_entries(start, skip_unused=False)

        self.entries = set(self.entry_points) | {address for address, symbol in symbols.items()
                                                 if symbol.kind in ("func", "loop")}
        if follow:
            self.follow_values()
        else:
            self.decode_all()
        self.entries |= {instruction.target for instruction in self.analysis.instructions.values()
                         if instruction.flow == dis.FLOW_CALL and instruction.target is not None}
        # code nothing reaches from the reset and the interrupt is decompiled too, from its "func" lines
        self.reached = self.reachable()
        self.unreached = {entry for entry in self.entries if entry not in self.reached}
        self.bodies = {entry: self.walk(entry) for entry in sorted(self.entries)}
        self.split_shared_code()
        self.names = self.choose_names()

    def table_entries(self, start, skip_unused=True):
        """The code addresses of the jumps table at 'start' (its jumps line's), from its first=; an entry of
        0 is an unused one (no handler) when 'skip_unused'"""
        symbol = self.symbols[start]
        found = (self.mem.r16(start + 2 * index) for index in range(symbol.first, symbol.count))
        return list(dict.fromkeys(entry for entry in found if entry or not skip_unused))

    def reachable(self):
        """The addresses the program runs from the reset and the interrupt handler: every jump, branch, call,
        jump table and slot followed."""
        found = set()
        work = list(self.entry_points)
        while work:
            address = work.pop()
            if address in found:
                continue
            found.add(address)
            instruction = self.analysis.instructions[address]
            work += self.successors(instruction)
            if instruction.flow == dis.FLOW_CALL and instruction.target is not None:
                work.append(instruction.target)
            for slot in self.analysis.slot_jumps.get(address, ()):
                work += self.analysis.slots.get(slot, [])
        return found

    def split_shared_code(self):
        """Code two functions both run into (one jumps into the middle of the other) becomes a function of
        its own, which both reach with a tail call: it is written once (also when one of them is code the
        program never reaches)."""
        while True:
            owners = {}
            for entry, body in self.bodies.items():
                for address in body:
                    owners.setdefault(address, set()).add(entry)
            starts = {successor for body in self.bodies.values() for address, instruction in body.items()
                      for successor in self.successors(instruction)
                      if successor in body and len(owners[successor]) > 1 and owners[successor] != owners[address]}
            if not starts:
                return
            self.entries |= starts
            self.bodies = {entry: self.walk(entry) for entry in sorted(self.entries)}

    def decode_all(self, strict=True):
        """Decodes everything the entries reach. A jump through a register that goes nowhere known stops it
        when 'strict'; else it is left for follow_values()."""
        work = sorted(self.entries)
        if not strict:
            # (follow_values: the tables found for jumps already decoded)
            work += [target for targets in self.analysis.jump_tables.values() for target in targets]
        instructions = self.analysis.instructions
        while work:
            address = work.pop()
            while address not in instructions:
                if address >= self.code_end:
                    raise SystemExit(f"code at {address:04X}, outside the program")
                instruction = dis.decode(self.mem, address)
                if instruction.flow == dis.FLOW_INVALID:
                    raise SystemExit(f"{address:04X}: not an instruction")
                instructions[address] = instruction
                if instruction.target is not None and instruction.flow in (dis.FLOW_CALL, dis.FLOW_JUMP, dis.FLOW_BRANCH):
                    work.append(instruction.target)
                work += self.analysis.jump_tables.get(address, [])
                if strict and instruction.flow == dis.FLOW_JUMP and instruction.target is None \
                        and address not in self.analysis.jump_tables:
                    raise SystemExit(f"{address:04X}: {instruction.text} goes where sound_symbols.txt does not say")
                if instruction.flow in (dis.FLOW_JUMP, dis.FLOW_RETURN):
                    break
                address = (address + instruction.length) & 0xFFFF
        # an instruction that starts inside another one
        for address, instruction in instructions.items():
            for inside in range(address + 1, address + instruction.length):
                if inside in instructions:
                    raise SystemExit(f"{inside:04X} is inside the instruction at {address:04X}")

    def follow_values(self):
        """Decodes the program, then walks what its registers hold (z80/values.py) into its jumps through a
        register, decoding where they go, until they go nowhere new"""
        analysis = self.analysis
        walk = None
        while True:
            self.decode_all(strict=False)
            if walk is None:
                walk = values.Walk(analysis.instructions, self.entries, self.image, self.code_end,
                                   analysis.jump_tables, analysis.slot_jumps, analysis.slots)
                analysis.walk = walk
            else:
                # on from the jumps that go somewhere new
                walk.run(self.entries, set(analysis.jump_tables) | set(analysis.slot_jumps))
            new = set()
            for address, words in walk.jumps.items():
                instruction = analysis.instructions[address]
                switch = set(analysis.jump_tables.get(address, []))
                for word in words:
                    if word[0] == "w":
                        switch.add(word[1])
                    elif word[0] == "e":
                        switch.update(self.code_table(word[1], f"the jump at {address:04X}, {instruction.text}"))
                    elif word[0] == "fw":
                        analysis.slot_jumps.setdefault(address, set()).add((word[1], word[2]))
                if switch - set(analysis.jump_tables.get(address, [])):
                    analysis.jump_tables[address] = sorted(switch)
                    new |= switch
            for slot in {slot for slots in analysis.slot_jumps.values() for slot in slots}:
                handlers = set(analysis.slots.get(slot, []))
                for word in walk.slot_words(*slot):
                    if word[0] == "w":
                        handlers.add(word[1])
                    elif word[0] == "e":
                        entries = self.code_table(word[1], f"handlers stored in the records' word at {slot[1]:X}")
                        analysis.handler_tables[word[1]] = entries
                        handlers.update(entries)
                if handlers - set(analysis.slots.get(slot, [])):
                    analysis.slots[slot] = sorted(handlers)
                    new |= handlers
            new -= set(analysis.instructions)
            self.entries |= {handler for handlers in analysis.slots.values() for handler in handlers}
            if not new:
                break
        for address, instruction in analysis.instructions.items():
            if instruction.flow == dis.FLOW_JUMP and instruction.target is None \
                    and address not in analysis.jump_tables and address not in analysis.slot_jumps:
                raise SystemExit(f"{address:04X}: {instruction.text} goes where neither sound_symbols.txt nor "
                                 "what the registers hold says")

    def code_table(self, start, user):
        """The entries of the table of code addresses at 'start', which 'user' goes through"""
        symbol = self.symbols.get(start)
        if symbol is None or symbol.kind != "jumps":
            raise SystemExit(f"the table of code addresses at {start:04X} ({user}) has no length in "
                             "sound_symbols.txt")
        return self.table_entries(start)

    def successors(self, instruction):
        """Addresses execution can continue at inside the same function."""
        following = []
        flow = instruction.flow
        after = (instruction.address + instruction.length) & 0xFFFF
        if flow in (dis.FLOW_NORMAL, dis.FLOW_BRANCH, dis.FLOW_CALL):
            following.append(after)
        if flow in (dis.FLOW_BRANCH, dis.FLOW_JUMP) and instruction.target is not None:
            following.append(instruction.target)
        following += self.analysis.jump_tables.get(instruction.address, [])
        return following

    def walk(self, entry):
        body = {}
        stack = [entry]
        while stack:
            address = stack.pop()
            if address in body or (address != entry and address in self.entries):
                continue
            body[address] = self.analysis.instructions[address]
            stack += self.successors(body[address])
        return body

    def choose_names(self):
        named = {address: symbol.name for address, symbol in self.symbols.items() if symbol.kind in ("func", "code", "loop")}
        return {entry: named.get(entry, f"sound_{entry:04X}") for entry in sorted(self.entries)}


def load_program(rom_image, symbols_path):
    """The sound program from the first 64 KB of the sound region (a shorter one is followed by zeros: what
    the Z80 sees above it is no part of the program)."""
    image = rom_image[:0x10000]
    if len(image) < 0x10000:
        image = bytes(image) + bytes(0x10000 - len(image))
    return Program(image, load_symbols(symbols_path))
