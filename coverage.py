"""
Name:        coverage.py
Description: byte map of the program ROM: decoded code, data the code points at, fill, and what is still unknown
Version:     0.1.0
Created:     11-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Usage: python tools/coverage.py [rom folder or .zip] [--trace executed.txt ...] [--reads FOLDER ...]
       (default ROM: rom/)
Writes build/listing/coverage.txt.

Runs the same analysis as tools/disassemble.py (the profile's ANALYSIS, the V60's by default) and gives
every byte of the program image one class:
  code             decoded instructions, branch tables included
  described data   tables whose layout is known: the ones symbols.txt describes, the pointer tables the
                   analysis follows, and what the game's data walk reads (the profile's DATA_WALK: the
                   data its engines take, walked as they read it)
  read data        (with --reads) bytes the reference read as data in a run: every rom_reads.txt under
                   each FOLDER ("PC FIRST-LAST" lines, as s32ref and kref write them). An instruction
                   that reads a whole area of the ROM is summing it (the memory test): its reads are left
                   out. Code that another one reads is listed: a table the analysis took for code, or a
                   table the game reads on past its end
  referenced data  from a place the code reads or takes the address of, a data symbol, or the first
                   byte of a run the reference read, up to the next known thing
  fill             what the analysis says only fills the program (the 68000's zeros between its vectors
                   and its code); runs of at least 16 bytes of 0xFF, the erased state of an EPROM, and the
                   0xFF that end an area even after referenced data (a table read on into them keeps the
                   bytes it was read as: an end mark); the zeros that take
                   the code after a function to the next word it starts on; up to three padding bytes
                   (zeros, or the V60's nops, CD) between two known things (before data, on a 16-bit word)
  unknown          everything else
Unknown runs are listed largest first, with two hints about what they are: how much of the run decodes
as instructions from its start, and how many 32-bit words elsewhere in the ROM hold its address.
"""
import importlib
import sys
from collections import Counter, defaultdict
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import game_profile  # noqa: E402  (the game's: its root, its ROM set, its settings)
ROOT = game_profile.ROOT
# the game's CPU: its analysis and its disassembler (the V60's by default)
disassemble = importlib.import_module(getattr(game_profile, "ANALYSIS", "disassemble"))
dis = importlib.import_module(getattr(game_profile, "DISASSEMBLER", "v60.disassembler"))

CODE = "code"
DESCRIBED = "described data"
READ = "read data"
DATA = "referenced data"
FILL = "fill"
UNKNOWN = "unknown"
CLASSES = (CODE, DESCRIBED, READ, DATA, FILL, UNKNOWN)

FILL_BYTE = 0xFF
FILL_MINIMUM = 16
# What the CPU's analysis module says of its code, the V60's by default: the byte order of its pointers, the
# word functions start on (the zeros before one are fill) and the bytes that fill up to it (zeros, or nops)
BYTE_ORDER = getattr(disassemble, "BYTE_ORDER", "little")
ALIGNMENT = getattr(disassemble, "FUNCTION_ALIGNMENT", 4)
PADDING_BYTES = getattr(disassemble, "PADDING_BYTES", (0x00, 0xCD))
# Operand modes whose Operand.value is a fixed place in memory
FIXED_MODES = {"pc_disp", "pc_disp_indexed", "pc_disp_indirect", "pc_disp_indirect_indexed", "pc_double_disp",
               "direct", "direct_indexed", "direct_deferred", "direct_deferred_indexed"}
# ... and the ones among them where a jump or call reads its target from that place
INDIRECT_MODES = {"pc_disp_indirect", "pc_disp_indirect_indexed", "pc_double_disp",
                  "direct_deferred", "direct_deferred_indexed"}
CODE_CHIPS = game_profile.rom.PROGRAM_AREAS[:2]


def v60_data_places(instruction):
    """The fixed places a V60 instruction reads or takes the address of"""
    jumps = instruction.flow != dis.FLOW_NORMAL
    # a jump or call names code with its operand, unless it reads the target from memory
    return [operand.value for operand in instruction.operands
            if operand.mode in FIXED_MODES and not (jumps and operand.mode not in INDIRECT_MODES)]


data_places = getattr(disassemble, "data_places", v60_data_places)


def load_reads(folders):
    """The program's bytes the reference read as data, {address: [instructions that read it]}, from every
    rom_reads.txt under the folders; and the instructions left out because they read a whole area, {pc: bytes}"""
    by_reader = defaultdict(set)
    for folder in folders:
        for path in sorted(Path(folder).rglob("rom_reads.txt")):
            for line in path.read_text(encoding="ascii").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                pc, span = line.split()
                first, last = (int(value, 16) for value in span.split("-"))
                by_reader[int(pc, 16)].update(range(first, last + 1))
    reads = defaultdict(list)
    summing = {}
    for pc, read in sorted(by_reader.items()):
        if any(all(address in read for address in range(start, end)) for start, end in game_profile.rom.PROGRAM_AREAS):
            summing[pc] = len(read)
            continue
        for address in read:
            reads[address].append(pc)
    return reads, summing


def data_references(analysis, reads=None):
    """ROM places the code reads, or takes the address of, that are not code: {address: [instruction addresses]};
    the data the described tables' data fields hold too, from the field's address (a field that holds the
    address of code holds a number there, not data); and where each run the reference read starts"""
    references = defaultdict(list)
    for field, target in analysis.data_fields.items():
        if target not in analysis.owner:
            references[target].append(field)
    for instruction in analysis.instructions.values():
        for value in data_places(instruction):
            place = game_profile.rom.canonical_address(value)
            if place is not None and place not in analysis.owner:
                references[place].append(instruction.address)
    # data symbols count as references made by symbols.txt (no instruction address)
    for address, symbol in analysis.symbols.items():
        if symbol.kind == "data":
            references.setdefault(address, [])
    for address, readers in (reads or {}).items():
        if address - 1 not in reads:
            references[address].extend(readers)
    return references


def classify(analysis, references, reads=None):
    """One class per byte of the program image: {address: class}, areas in PROGRAM_AREAS only"""
    image = analysis.image
    reads = reads or {}
    described = {entry + offset for entry in analysis.data_words for offset in range(4)}
    for first, stop in analysis.described:
        described.update(range(first, stop))
    fill = {address for first, stop in getattr(analysis, "fill", ()) for address in range(first, stop)}
    classes = {}
    for start, end in game_profile.rom.PROGRAM_AREAS:
        current = UNKNOWN
        for address in range(start, end):
            if address in analysis.owner:
                classes[address] = CODE
                current = UNKNOWN
            elif address in described:
                classes[address] = DESCRIBED
                current = UNKNOWN
            elif address in fill:
                classes[address] = FILL
                current = UNKNOWN
            else:
                # referenced data runs from its reference to the next known thing
                if address in references:
                    current = DATA
                classes[address] = READ if address in reads else current
        # the erased end of an area is fill, after referenced data too (not the bytes a table was read as)
        tail = end
        while tail > start and image[tail - 1] == FILL_BYTE and classes[tail - 1] in (DATA, UNKNOWN):
            tail -= 1
        if end - tail >= FILL_MINIMUM:
            for address in range(tail, end):
                classes[address] = FILL
        # fill only replaces unknown bytes: a referenced table can hold 0xFF on purpose
        address = start
        while address < end:
            if classes[address] != UNKNOWN or image[address] != FILL_BYTE:
                address += 1
                continue
            stop = address
            while stop < end and classes[stop] == UNKNOWN and image[stop] == FILL_BYTE:
                stop += 1
            if stop - address >= FILL_MINIMUM:
                for byte_address in range(address, stop):
                    classes[byte_address] = FILL
            address = stop
        # the zeros that take the code after a function to the next 32-bit word
        for address in range(start + 1, end):
            if classes[address] != UNKNOWN or classes[address - 1] != CODE:
                continue
            stop = address
            while stop < end and stop % ALIGNMENT and classes[stop] == UNKNOWN and image[stop] == 0:
                stop += 1
            if stop % ALIGNMENT == 0 and stop > address:
                for byte_address in range(address, stop):
                    classes[byte_address] = FILL
        # a few zeros or nops (CD) between two known things: before code, or before data on a 16-bit word
        for address in range(start + 1, end):
            if classes[address] != UNKNOWN or classes[address - 1] == UNKNOWN:
                continue
            stop = address
            while stop < end and classes[stop] == UNKNOWN and image[stop] in PADDING_BYTES:
                stop += 1
            if address < stop < end and stop - address < ALIGNMENT and classes[stop] != UNKNOWN \
                    and (classes[stop] == CODE or stop % 2 == 0):
                for byte_address in range(address, stop):
                    classes[byte_address] = FILL
    return classes


def runs_of(classes, wanted, start, end):
    """Maximal runs [first, stop) of bytes of class 'wanted' inside [start, end)"""
    runs = []
    address = start
    while address < end:
        if classes[address] != wanted:
            address += 1
            continue
        stop = address
        while stop < end and classes[stop] == wanted:
            stop += 1
        runs.append((address, stop))
        address = stop
    return runs


def sweep(analysis, start, end):
    """Bytes of [start, end) that decode as instructions, one after another, before the first invalid one"""
    address = start
    while address < end:
        instruction = dis.decode(analysis.mem, address)
        if instruction.flow == dis.FLOW_INVALID or address + instruction.length > end:
            break
        address += instruction.length
    return address - start


def pointers_to(analysis, targets):
    """32-bit words outside code (any alignment) whose value is one of 'targets', through any mirror"""
    image = analysis.image
    found = defaultdict(list)
    for start, end in game_profile.rom.PROGRAM_AREAS:
        for address in range(start, end - 3):
            if address in analysis.owner:
                continue
            value = int.from_bytes(image[address:address + 4], BYTE_ORDER)
            if value >= 0x1000000:
                continue
            target = game_profile.rom.canonical_address(value)
            if target in targets:
                found[target].append(address)
    return found


def write_coverage(analysis, references, classes, path, reads=None, summing=None):
    image = analysis.image
    # (the read data only when the reference's reads were given)
    shown = CLASSES if reads is not None else tuple(name for name in CLASSES if name != READ)
    lines = ["Program ROM coverage", "====================", ""]
    header = f"{'area':<14}" + "".join(f"{name:>16}" for name in shown) + f"{'total':>10}"
    lines.append(header)
    totals = Counter()
    for start, end in game_profile.rom.PROGRAM_AREAS:
        counts = Counter(classes[address] for address in range(start, end))
        if sum(counts.values()) != end - start:
            raise SystemExit(f"coverage: {start:06X}-{end - 1:06X} classes do not add up to the area size")
        totals.update(counts)
        lines.append(f"{start:06X}-{end - 1:06X} " + "".join(f"{counts[name]:16}" for name in shown)
                     + f"{end - start:10}")
    lines.append(f"{'total':<14}" + "".join(f"{totals[name]:16}" for name in shown)
                 + f"{sum(totals.values()):10}")
    image_size = sum(end - start for start, end in game_profile.rom.PROGRAM_AREAS)
    if sum(totals.values()) != image_size:
        raise SystemExit("coverage: classes do not add up to the program image size")

    unknown = []
    for start, end in CODE_CHIPS:
        unknown += runs_of(classes, UNKNOWN, start, end)
    pointers = pointers_to(analysis, {first for first, _ in unknown})
    rows = []
    for first, stop in unknown:
        decoded = sweep(analysis, first, stop)
        rows.append((first, stop, decoded, len(pointers.get(first, []))))
    as_code = [row for row in rows if row[2] == row[1] - row[0]]
    lines += [
        "",
        f"places the code or symbols.txt marks as data: {len(references)}",
    ]
    if reads is not None:
        readers = {pc for readers in reads.values() for pc in readers}
        lines += [
            f"bytes the reference read as data: {len(reads)}, by {len(readers)} instructions; left out, the reads "
            f"of the ones that sum a whole area: " + ", ".join(f"{pc:06X} ({count} bytes)" for pc, count in sorted(summing.items())),
            f"  read data no table describes yet: {sum(1 for address in reads if classes[address] == READ)} bytes",
        ]
        code_reads = sorted(address for address in reads if address in analysis.owner)
        lines.append(f"  code read as data: {len(code_reads)} bytes"
                     + "".join(f", {address:06X} by " + " ".join(f"{pc:06X}" for pc in sorted(set(reads[address])))
                               for address in code_reads[:20]))
    lines += [
        f"unknown runs in the code chips: {len(rows)}, {sum(stop - first for first, stop, _, _ in rows)} bytes",
        f"  decoding as instructions from start to end: {len(as_code)} runs, "
        f"{sum(stop - first for first, stop, _, _ in as_code)} bytes",
        f"  whose address is held by a word elsewhere: "
        f"{sum(1 for row in rows if row[3])} runs",
    ]
    title = "Unknown runs in the code chips, largest first"
    lines += ["", title, "-" * len(title),
              f"{'first':<7} {'last':<7} {'bytes':>6} {'decodes':>8} {'pointers':>8}  first bytes"]
    for first, stop, decoded, held in sorted(rows, key=lambda row: (row[0] - row[1], row[0])):
        verdict = "all" if decoded == stop - first else str(decoded)
        raw = image[first:min(stop, first + 12)].hex(" ")
        lines.append(f"{first:06X}  {stop - 1:06X}  {stop - first:6} {verdict:>8} {held:>8}  {raw}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return lines


USAGE = "Usage: python tools/coverage.py [rom folder or .zip] [--trace executed.txt ...] [--reads FOLDER ...]"


def main():
    arguments = sys.argv[1:]
    trace_paths = []
    while "--trace" in arguments:
        position = arguments.index("--trace")
        if position + 1 >= len(arguments):
            raise SystemExit(USAGE)
        trace_paths.append(arguments[position + 1])
        del arguments[position:position + 2]
    read_folders = []
    while "--reads" in arguments:
        position = arguments.index("--reads")
        if position + 1 >= len(arguments):
            raise SystemExit(USAGE)
        read_folders.append(arguments[position + 1])
        del arguments[position:position + 2]
    source = Path(arguments[0]) if arguments else ROOT / "rom"

    image = game_profile.rom.build_program(game_profile.rom.load_set(source))
    executed, _ = disassemble.load_trace(trace_paths)
    analysis = disassemble.Analysis(image, disassemble.load_symbols(ROOT / "symbols.txt"), executed)
    analysis.run()
    # the data the game's engines read, walked from where the code hands it over (the profile's DATA_WALK:
    # a module of the game's tools whose spans(analysis) gives {first: (stop, kind)}) is described data
    walked = {}
    if getattr(game_profile, "DATA_WALK", None):
        walked = importlib.import_module(game_profile.DATA_WALK).spans(analysis)
        analysis.described.extend((first, stop) for first, (stop, _) in walked.items())
    reads, summing = load_reads(read_folders) if read_folders else (None, None)
    references = data_references(analysis, reads)
    classes = classify(analysis, references, reads)

    out_dir = ROOT / "build" / "listing"
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = write_coverage(analysis, references, classes, out_dir / "coverage.txt", reads, summing)
    if walked:
        kinds = Counter()
        for first, (stop, kind) in walked.items():
            kinds[kind] += stop - first
        print(f"walked by {game_profile.DATA_WALK}: {sum(kinds.values())} bytes ("
              + ", ".join(f"{kind} {count}" for kind, count in kinds.most_common()) + ")")
    end = next(index for index, line in enumerate(lines) if line.startswith("Unknown runs in the code chips"))
    print("\n".join(lines[:end]).rstrip())
    print(f"\nCoverage: {out_dir / 'coverage.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
