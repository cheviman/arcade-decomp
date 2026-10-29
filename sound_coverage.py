"""
Name:        sound_coverage.py
Description: byte map of the sound program: its code, its data as sound_symbols.txt describes it, its fill,
             and what is still unknown
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Usage: python tools/sound_coverage.py [rom folder or .zip] [--trace FILE|FOLDER ...] [--reads FOLDER ...]
       (default ROM: rom/)
Writes build/listing/sound_coverage.txt.

Runs the Z80 analysis (z80/analysis.py, with the profile's sound settings and sound_symbols.txt) and gives
every byte of the sound program (below the profile's SOUND_CODE_END) one class:
  code             the instructions the analysis decodes
  described data   what sound_symbols.txt describes, by subject:
                     ADDRESS jumps NAME COUNT [first=K]   COUNT 16-bit code addresses (from entry K on)
                     ADDRESS data NAME TYPE[COUNT] [TARGET]  COUNT values of TYPE (u8, s8, u16, s16, or a
                                         record of the program ROM: struct NAME SIZE rom, and its field
                                         lines); a u16 that points at something says what: a record, or
                                         elements (values a pointer walks, NAME u8: what the driver reads
                                         an item at a time, which the game's sound data walk measures);
                                         TYPE elements and COUNT 1: elements no table points at, which the
                                         walk measures from ADDRESS
                     field STRUCT OFFSET NAME TYPE[N] TARGET  a record's field that points at one
                     param FUNCTION REGISTER NAME STRUCT  every constant the analysis sees in REGISTER where
                                         FUNCTION starts (what it is called or jumped to with) is the address
                                         of a record of STRUCT; an entry of a table there, the table's
                   and what the game's sound data walk (the profile's SOUND_DATA_WALK, a module of its tools
                   whose spans(program, starts) gives {first: (stop, kind)} and its problems) reads from the
                   places the tables point at that are elements
  fill             ADDRESS fill COUNT lines (bytes nothing reads, each with its reason), and runs of at
                   least 16 bytes of 0xFF (the erased state of an EPROM)
  unknown          everything else
Code the program also reads as data stays code, and is declared as such with a codedata line (the layout
of a data line: ADDRESS codedata NAME TYPE[COUNT]; the address keeps its func or code line). With --trace (kref's
sound_executed.txt files, or the folders that hold them) the report says how many of the executed
addresses the analysis finds without traces, and lists any it does not or that fall inside another
instruction; with --reads (kref --sound-rom-reads: every sound_rom_reads.txt under each FOLDER) it lists the
bytes the Z80 read as data that nothing describes, and the code it read as data that no data line declares
(an instruction that reads the whole program is summing it, the ROM check: its reads are left out).
"""
import importlib
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import game_profile  # noqa: E402  (the game's: its root, its ROM set, its settings)
import disassemble  # noqa: E402  (the names' lines: struct, field, elements, param)
from decompiler import cpu  # noqa: E402  (the Z80's register names, for param lines)
from z80 import analysis  # noqa: E402

ROOT = game_profile.ROOT
SYMBOLS = ROOT / "sound_symbols.txt"
USAGE = "Usage: python tools/sound_coverage.py [rom folder or .zip] [--trace FILE|FOLDER ...] [--reads FOLDER ...]"

CODE = "code"
DESCRIBED = "described data"
FILL = "fill"
UNKNOWN = "unknown"
CLASSES = (CODE, DESCRIBED, FILL, UNKNOWN)
FILL_BYTE = 0xFF
FILL_MINIMUM = 16
VALUE_SIZES = {"u8": 1, "s8": 1, "u16": 2, "s16": 2}
PAIR_NUMBERS = {0: "bc", 1: "de", 2: "hl", 3: "ix", 4: "iy"}     # cpu.Z80's register numbers of the pairs
REGISTERS = {name: number for number, name in cpu.Z80.register_names.items()}


@dataclass
class Piece:
    """A described piece of the sound program: [first, stop), what it is and its name"""
    first: int
    stop: int
    kind: str
    name: str
    code_read: bool = False     # a codedata line's: code the program also reads as data


def load_lines(path):
    """sound_symbols.txt's data and codedata lines and its fill lines: ([(address, name, type, count, target,
    code read, where)], [(address, count, where)])"""
    tables, fills = [], []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parts = line.split("#", 1)[0].split()
        where = f"{path.name}:{number}"
        if len(parts) >= 2 and parts[1] == "fill":
            if len(parts) != 3 or not parts[2].isdigit():
                raise SystemExit(f"{where}: expected 'ADDRESS fill COUNT'")
            fills.append((int(parts[0], 16), int(parts[2]), where))
        elif len(parts) >= 4 and parts[1] in ("data", "codedata"):
            layout = re.fullmatch(r"(\w+)\[(\d+)\]", parts[3])
            if layout is None or len(parts) > 5:
                raise SystemExit(f"{where}: expected 'ADDRESS {parts[1]} NAME TYPE[COUNT] [TARGET]'")
            tables.append((int(parts[0], 16), parts[2], layout.group(1), int(layout.group(2)),
                           parts[4] if len(parts) == 5 else None, parts[1] == "codedata", where))
    return tables, fills


class Description:
    """What sound_symbols.txt says the sound program's data is, followed from its tables and from what the
    analysis sees the functions take: pieces ([Piece]), starts ({elements name: {address: where from}}),
    the pieces the game's walk read, and problems ([text])"""

    def __init__(self, program, names, tables, fills):
        self.program = program
        self.names = names
        self.pieces = []
        self.starts = defaultdict(dict)
        self.problems = []
        self.over_code = []         # data lines over code: what the program also reads as data
        self.records = set()        # (address, struct) already described
        self.tables = {}            # address of a described table -> (name, type, count, target)
        self._users = None          # (users())
        for address, symbol in sorted(program.symbols.items()):
            if symbol.kind == "jumps":
                self.pieces.append(Piece(address + 2 * symbol.first, address + 2 * symbol.count, "code addresses",
                                         symbol.name))
        for address, name, kind, count, target, code_read, where in tables:
            piece = self.table(address, name, kind, count, target, where)
            if code_read and piece is not None:
                piece.code_read = True
        self.parameters()
        for address, count, where in fills:
            self.pieces.append(Piece(address, address + count, FILL, f"fill {address:04X}"))
        self.walked = {}
        if getattr(game_profile, "SOUND_DATA_WALK", None):
            walked, problems = importlib.import_module(game_profile.SOUND_DATA_WALK).spans(program, self.starts)
            self.walked = walked
            self.problems += problems
            # a kind may carry a name after a colon: what the walk read it from
            for first, (stop, kind) in walked.items():
                kind, _, name = kind.partition(": ")
                self.pieces.append(Piece(first, stop, kind, name or kind))
        code = set()
        for address, instruction in program.analysis.instructions.items():
            code.update(range(address, address + instruction.length))
        for piece in self.pieces:
            over = piece.kind != FILL and any(address in code for address in range(piece.first, piece.stop))
            if over and piece.code_read:
                self.over_code.append(piece)
            elif over or piece.code_read:
                self.problems.append(f"{piece.first:04X}-{piece.stop - 1:04X} {piece.name}: "
                                     + ("described as data over code" if over else "a codedata line over no code"))
        # two pieces that share bytes (one of them is wrong, or the same thing is described twice)
        reach = None
        for piece in sorted(self.pieces, key=lambda piece: (piece.first, -piece.stop)):
            if reach is not None and piece.first < reach.stop:
                self.problems.append(f"{piece.first:04X}-{piece.stop - 1:04X} {piece.name} overlaps "
                                     f"{reach.first:04X}-{reach.stop - 1:04X} {reach.name}")
            if reach is None or piece.stop > reach.stop:
                reach = piece

    def size(self, kind, where):
        if kind in VALUE_SIZES:
            return VALUE_SIZES[kind]
        if kind in self.names.rom_structs:
            return self.names.structs[kind]
        raise SystemExit(f"{where}: '{kind}' is no value type nor a record of the program ROM")

    def table(self, address, name, kind, count, target, where):
        """A data line: its piece (None for elements, which the game's walk measures)"""
        if kind in self.names.arrays and kind in self.names.rom_structs and count == 1 and target is None:
            # elements no table points at: the walk measures them from here
            self.starts[kind].setdefault(address, name)
            return None
        size = self.size(kind, where)
        self.tables[address] = (name, kind, count, target)
        piece = Piece(address, address + size * count, kind if kind in VALUE_SIZES else f"{kind} records", name)
        self.pieces.append(piece)
        if kind not in VALUE_SIZES:
            for index in range(count):
                self.fields(address + size * index, kind, f"{name}[{index}]")
        elif target is not None:
            if size != 2:
                raise SystemExit(f"{where}: only a table of u16 points at something")
            for index in range(count):
                self.pointer(self.word(address + 2 * index), target, f"{name}[{index}]", where)
        return piece

    def word(self, address):
        return self.program.image[address] | (self.program.image[address + 1] << 8)

    def pointer(self, value, target, source, where):
        """A u16 at 'source' that points at 'target': a record, or elements"""
        if target in self.names.arrays:
            self.starts[target].setdefault(value, source)
        elif target in self.names.rom_structs:
            self.record(value, target, source)
        else:
            raise SystemExit(f"{where}: '{target}' is no record of the program ROM nor elements")

    def record(self, address, struct, source):
        if (address, struct) in self.records:
            return
        self.records.add((address, struct))
        self.pieces.append(Piece(address, address + self.names.structs[struct], f"{struct} record",
                                 f"{struct} {address:04X} ({source})"))
        self.fields(address, struct, f"{struct} {address:04X}")

    def fields(self, address, struct, source):
        """The pointers a record's fields hold"""
        for (owner, offset), (target, bits) in self.names.targets.items():
            if owner != struct or bits != 16:
                continue
            for index in range(self.names.field_arrays.get((owner, offset)) or 1):
                self.pointer(self.word(address + offset + 2 * index), target,
                             f"{source}.{self.names.fields[(owner, offset)][0]}[{index}]", f"field {owner} {offset:X}")

    def users(self):
        """{constant: "of NAMES"}: the named functions whose code loads each 16-bit constant (LD rr,nn), by
        subject: a function with a name of its own (a func or code line) is its own, one without belongs to
        the named ones that call it, jump to it or load its address (the handler a channel runs next) on
        ways that go through no other named one"""
        if self._users is not None:
            return self._users
        program = self.program
        entries = program.entries
        named = {entry for entry, name in program.names.items() if not name.startswith("sound_")}
        links = defaultdict(set)
        loads = defaultdict(set)        # constant -> entries whose body loads it
        for entry, body in program.bodies.items():
            for address, instruction in body.items():
                found = [successor for successor in program.successors(instruction) if successor in entries]
                if instruction.target is not None:
                    found.append(instruction.target)
                for operand in instruction.operands:
                    if operand.mode == "immediate" and len(operand.text) == 5:    # $nnnn
                        loads[operand.value].add(entry)
                        found.append(operand.value)
                links[entry].update(target for target in found if target in entries and target != entry)
        owners = defaultdict(set)
        for start in named:
            work = [start]
            seen = {start}
            while work:
                entry = work.pop()
                owners[entry].add(start)
                for target in links[entry]:
                    if target not in seen and target not in named:
                        seen.add(target)
                        work.append(target)
        self._users = {}
        for value, places in loads.items():
            names = sorted({program.names[owner] for place in places for owner in owners.get(place, ())})
            if names:
                self._users[value] = "of " + ", ".join(names)
        return self._users

    def parameters(self):
        """The records the functions take, from what the analysis sees in their registers where they start"""
        walk = self.program.analysis.walk
        entries = {name: entry for entry, name in self.program.names.items()}
        for (function, register), struct in sorted(self.names.param_structs.items()):
            if struct not in self.names.rom_structs:
                continue
            entry = entries.get(function)
            pair = PAIR_NUMBERS.get(register)
            if entry is None or pair is None or walk is None:
                self.problems.append(f"param {function}: no function of that name, or no pair, or no walk of the "
                                     "registers (SOUND_FOLLOW_VALUES)")
                continue
            found = set(walk.arguments(entry).get(pair, ()))
            # a word of a record: what the program stores in that field (an effect keeps its voice there)
            for word in [word for word in found if word[0] == "fw"]:
                found |= walk.slot_words(word[1], word[2])
            users = self.users()
            for word in sorted(found):
                if word[0] == "w":
                    self.record(word[1], struct, users.get(word[1]) or f"{function}'s {pair}")
                elif word[0] == "e":
                    table = self.tables.get(word[1])
                    if table is None or table[3] != struct:
                        self.problems.append(f"{function} takes in {pair} an entry of the table at {word[1]:04X}, "
                                             f"which no data line describes as pointers to {struct}")


def classify(program, description):
    """The class of every byte of the program, by address"""
    end = program.code_end
    classes = [UNKNOWN] * end
    for piece in description.pieces:
        for address in range(max(piece.first, 0), min(piece.stop, end)):
            if classes[address] != DESCRIBED:
                classes[address] = FILL if piece.kind == FILL else DESCRIBED
    for address, instruction in program.analysis.instructions.items():
        for inside in range(address, min(address + instruction.length, end)):
            classes[inside] = CODE
    image = program.image
    address = 0
    while address < end:
        stop = address
        while stop < end and image[stop] == FILL_BYTE and classes[stop] == UNKNOWN:
            stop += 1
        if stop - address >= FILL_MINIMUM:
            for inside in range(address, stop):
                classes[inside] = FILL
        address = max(stop, address + 1)
    return classes


def runs(addresses):
    found = []
    for address in sorted(addresses):
        if found and address == found[-1][1] + 1:
            found[-1][1] = address
        else:
            found.append([address, address])
    return found


def load_executed(paths):
    executed = set()
    for path in paths:
        path = Path(path)
        files = sorted(path.rglob("sound_executed.txt")) if path.is_dir() else [path]
        for file in files:
            executed.update(int(line, 16) for line in file.read_text(encoding="ascii").splitlines()
                            if line.strip() and not line.startswith("#"))
    return executed


def load_reads(folders, end):
    """{pc: bytes it read}, every sound_rom_reads.txt under the folders; the summing readers apart"""
    by_reader = defaultdict(set)
    for folder in folders:
        for path in sorted(Path(folder).rglob("sound_rom_reads.txt")):
            for line in path.read_text(encoding="ascii").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                pc, span = line.split()
                first, last = (int(value, 16) for value in span.split("-"))
                by_reader[int(pc, 16)].update(range(first, last + 1))
    summing = {pc: len(read) for pc, read in by_reader.items() if len(read) >= end}
    return {pc: read for pc, read in by_reader.items() if pc not in summing}, summing


def report(program, description, classes, executed=None, reads=None, summing=None):
    end = program.code_end
    counts = Counter(classes)
    instructions = program.analysis.instructions
    lines = [f"Sound program coverage (0000-{end - 1:04X}, {end} bytes)", ""]
    for name in CLASSES:
        lines.append(f"  {name:<16} {counts[name]:6d} bytes  {100 * counts[name] / end:5.1f}%")
    lines += ["", f"code: {len(instructions)} instructions, {len(program.entries)} functions "
              f"({len(program.unreached)} that the entry points never reach: "
              + " ".join(f"{entry:04X}" for entry in sorted(program.unreached)) + ")"]
    found = program.analysis
    switches = [f"{address:04X} switch of {len(targets)}" for address, targets in sorted(found.jump_tables.items())]
    through = [f"{address:04X} through the records' word at {offset:X} ({register})"
               for address, slots in sorted(found.slot_jumps.items()) for register, offset in sorted(slots)]
    lines.append("jumps through a register: " + ", ".join(switches + through))
    for (register, offset), handlers in sorted(found.slots.items()):
        lines.append(f"  handlers stored at {offset:X} of the records {register} points at: {len(handlers)}")
    for table, handlers in sorted(found.handler_tables.items()):
        lines.append(f"  table of handlers {program.symbols[table].name} ({table:04X}): {len(handlers)}")

    by_kind = Counter()
    for piece in description.pieces:
        by_kind[piece.kind] += piece.stop - piece.first
    lines += ["", "Described data by kind (bytes, before code and overlaps are taken out):"]
    lines += [f"  {kind:<28} {count:6d}" for kind, count in sorted(by_kind.items())]
    lines += ["", "Pieces, by address:"]
    for piece in sorted(description.pieces, key=lambda piece: (piece.first, piece.stop)):
        lines.append(f"  {piece.first:04X}-{piece.stop - 1:04X} {piece.stop - piece.first:5d}  {piece.kind:<24} "
                     f"{piece.name}")
    if description.over_code:
        lines += ["", "Code the program also reads as data (data lines over code):"]
        lines += [f"  {piece.first:04X}-{piece.stop - 1:04X}  {piece.name}" for piece in description.over_code]

    unknown = runs(address for address in range(end) if classes[address] == UNKNOWN)
    lines += ["", f"Unknown runs: {len(unknown)}, largest first"]
    for first, last in sorted(unknown, key=lambda run: (run[0] - run[1], run[0]))[:200]:
        lines.append(f"  {first:04X}-{last:04X} {last - first + 1:5d}")
    lines += ["", f"Problems: {len(description.problems)}"] + [f"  {problem}" for problem in description.problems]

    if executed is not None:
        interior = {inside for address, instruction in instructions.items()
                    for inside in range(address + 1, address + instruction.length)}
        inside = sorted(executed & interior)
        missing = sorted(address for address in executed if address not in instructions)
        lines += ["", f"Executed (traces): {len(executed)} addresses; the analysis finds "
                  f"{len(executed) - len(missing)} of them without traces; {len(missing)} missing, {len(inside)} "
                  "inside another instruction"]
        lines += [f"  missing {first:04X}-{last:04X}" for first, last in runs(missing)[:40]]
        lines += [f"  inside {address:04X}" for address in inside[:40]]
    if reads is not None:
        read = set().union(*reads.values()) if reads else set()
        declared = {address for piece in description.over_code for address in range(piece.first, piece.stop)}
        code_read = {address for address in read if classes[address] == CODE and address not in declared}
        undescribed = {address for address in read if classes[address] not in (CODE, DESCRIBED)}
        lines += ["", f"Read as data (kref --sound-rom-reads): {len(read)} bytes by {len(reads)} instructions "
                  f"(summing the whole program, left out: " + ", ".join(f"{pc:04X}" for pc in sorted(summing)) + ")",
                  f"  code read as data that no data line declares: {len(code_read)}"]
        lines += [f"    {first:04X}-{last:04X}" for first, last in runs(code_read)[:40]]
        lines.append(f"  read but not described: {len(undescribed)}")
        for first, last in runs(undescribed)[:40]:
            readers = sorted(pc for pc, addresses in reads.items() if first in addresses)
            lines.append(f"    {first:04X}-{last:04X}  read by " + " ".join(f"{pc:04X}" for pc in readers[:8]))
    return lines


def main():
    arguments = sys.argv[1:]
    traces, read_folders = [], []
    for option, found in (("--trace", traces), ("--reads", read_folders)):
        while option in arguments:
            position = arguments.index(option)
            if position + 1 >= len(arguments):
                raise SystemExit(USAGE)
            found.append(arguments[position + 1])
            del arguments[position:position + 2]
    if len(arguments) > 1 or any(argument.startswith("--") for argument in arguments):
        raise SystemExit(USAGE)
    source = Path(arguments[0]) if arguments else ROOT / "rom"

    image = game_profile.rom.build_sound(game_profile.rom.load_set(source))
    program = analysis.load_program(image, SYMBOLS)
    names = disassemble.load_names(SYMBOLS, REGISTERS)
    tables, fills = load_lines(SYMBOLS)
    description = Description(program, names, tables, fills)
    classes = classify(program, description)
    executed = load_executed(traces) if traces else None
    reads, summing = load_reads(read_folders, program.code_end) if read_folders else (None, None)
    lines = report(program, description, classes, executed, reads, summing)

    out_dir = ROOT / "build" / "listing"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sound_coverage.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
    # on the screen: the counts, and from the unknown runs on (the first 10 of them), not the pieces
    pieces = lines.index("Pieces, by address:")
    unknown_at = next(index for index, line in enumerate(lines) if line.startswith("Unknown runs"))
    problems_at = next(index for index, line in enumerate(lines) if line.startswith("Problems"))
    print("\n".join(lines[:pieces]).rstrip())
    print("\n".join(lines[unknown_at:min(unknown_at + 11, problems_at)] + [""] + lines[problems_at:]))
    print(f"\nCoverage: {out_dir / 'sound_coverage.txt'}")
    unknown = classes.count(UNKNOWN)
    return 1 if unknown or description.problems else 0


if __name__ == "__main__":
    sys.exit(main())
