"""
Name:        width_census.py
Description: what the C leans on of the processor it runs on, from the census of the original program's
             accesses of memory (the reference's --access-widths) over the scenarios: the reads of the game's
             state a big-endian processor gives another value, the program's data read at more than one width,
             and the accesses at an address out of line with their width, which a processor like the SH-2 does
             not take
Version:     0.1.0
Created:     05-11-2026
Modified:    05-11-2026
Author:      Sebastian Toyos

Usage: python tools/width_census.py run [--jobs N]   run every script of tests/controls on the reference with
                                                    --access-widths (build/access_widths/NAME.txt)
       python tools/width_census.py report          read them: build/access_widths/report.txt

The census lists every access the program makes of memory as data, by the address it starts at, its width,
whether it writes and the kind of instruction (a string move's, a string compare's or search's, a bit's of a
word, any other), with the first instruction that made it. The reference also follows the work RAM as a
big-endian processor running the C would hold it, the C keeping the V70's places and widths: each value the
program writes goes there in that order, a string move takes its bytes along, a bit instruction changes the
byte its bit is in, and the board's memories and the program's data give the values the V70 reads. A read that
gives another value there is a place the C reads in another way than it was written (a word written, a byte of
it read): the first part of the report, folded onto the place it is in (a field of a structure the game keeps
many of, at its offset: every object in struct object, every race record in struct race, the screen's car...; a
variable; the stacks are left out, as the C keeps its own). Bytes a string move brought from the program's data
are not followed: how they lie is the loader's.

Then the program's data: the places its tables are read at more than one width (the loader puts each value at
one), and the accesses out of line with their width, in the work RAM and in the data. The routines whose
accesses say nothing of how the game lays out what they reach are left out (the game profile's WIDTH_ROUTINES:
the memory test, a copy), and so are the ones the board puts its own in place of (runtime/replaced.txt). Last, the board's memories (the profile's WIDTH_MEMORIES): how many instructions reach
them a byte, a halfword and a word at a time, which their accessors take in the V70's order on any processor.
"""
import bisect
import re
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_profile  # noqa: E402  (the game's: its root, its memories, its state's blocks)
from decompiler import memory as state_memory  # noqa: E402

ROOT = game_profile.ROOT
OUT = ROOT / "build" / "access_widths"
REFERENCE = getattr(game_profile, "REFERENCE", "s32ref")
SOUND_TIMING = ROOT / "build" / "sound" / "timing.txt"
KINDS = {"-": "", "S": "string move", "C": "string search", "B": "bit"}


def reference():
    for path in (ROOT / "build" / "bin" / (REFERENCE + ".exe"), ROOT / "build" / "bin" / REFERENCE):
        if path.exists():
            return path
    raise SystemExit(f"{ROOT / 'build' / 'bin' / REFERENCE}: no such executable")


def run_one(script):
    """One scenario on the reference, as check_scenarios runs it with the sound: (name, problem or None)"""
    name = script.stem
    sound = ["--sound-timing", str(SOUND_TIMING)] if SOUND_TIMING.exists() else []
    result = subprocess.run([str(reference()), "--script", str(script), "--hashes", str(OUT / f"{name}_hashes.txt"),
                             "--sound-hashes", str(OUT / f"{name}_sound.txt"), "--access-widths",
                             str(OUT / f"{name}.txt")] + sound, capture_output=True, text=True)
    if result.returncode != 0:
        return name, (result.stderr.strip().splitlines() or ["stopped"])[-1][:200]
    stored = ROOT / "build" / "scenarios" / name / "hashes.txt"
    if stored.exists() and stored.read_text(encoding="ascii") != (OUT / f"{name}_hashes.txt").read_text(encoding="ascii"):
        return name, "its state differs from the reference's run in build/scenarios"
    return name, None


def run(jobs):
    OUT.mkdir(parents=True, exist_ok=True)
    scripts = sorted((ROOT / "tests" / "controls").glob("*.txt"))
    failures = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for name, problem in pool.map(run_one, scripts):
            failures += problem is not None
            print(f"{name:<24} {problem or 'done'}", flush=True)
    print(f"{len(scripts)} scenarios: {OUT}")
    return 1 if failures else 0


class Symbols:
    """What symbols.txt says of the places: the structures' sizes, members and fields, and the names at addresses"""

    def __init__(self, path):
        self.sizes = {}
        self.members = defaultdict(list)       # struct -> (offset, type, count)
        self.fields = defaultdict(lambda: defaultdict(set))    # struct -> offset -> names
        functions, names = [], []
        for raw in path.read_text(encoding="utf-8").splitlines():
            words = raw.split("#", 1)[0].split()
            if len(words) < 3:
                continue
            if words[0] == "struct":
                self.sizes[words[1]] = int(words[2], 16)
            elif words[0] == "member" and len(words) >= 5:
                kind = re.fullmatch(r"(\w+)(?:\[([0-9A-Fa-f]+)\])?", words[4])
                self.members[words[1]].append((int(words[2], 16), kind[1], int(kind[2] or "1", 16)))
            elif words[0] == "field" and len(words) >= 4:
                self.fields[words[1]][int(words[2], 16)].add(words[3])
            elif words[0] == "scoped" and len(words) >= 5 and re.fullmatch(r"[0-9A-Fa-f]+", words[3]):
                self.fields[words[1]][int(words[3], 16)].add(f"{words[2]}.{words[4]}")
            elif re.fullmatch(r"[0-9A-F]{6}", words[0]):
                if words[1] == "func":
                    functions.append((int(words[0], 16), words[2]))
                elif words[1] in ("table", "data", "var", "io"):
                    names.append((int(words[0], 16), words[2]))
        functions.sort()
        names.sort()
        self.function_starts = [address for address, _ in functions]
        self.function_names = [name for _, name in functions]
        self.name_starts = [address for address, _ in names]
        self.name_names = [name for _, name in names]
        self.functions = {}

    def function_at(self, pc):
        function = self.functions.get(pc)
        if function is None:
            index = bisect.bisect_right(self.function_starts, pc) - 1
            function = self.functions[pc] = self.function_names[index] if index >= 0 else f"{pc:06X}"
        return function

    def name_at(self, address):
        """The named place an address falls in, and how far into it"""
        index = bisect.bisect_right(self.name_starts, address) - 1
        if index < 0:
            return None, address
        return self.name_names[index], address - self.name_starts[index]

    def inside(self, struct, offset):
        """The innermost structure of a member array a place of 'struct' is in, and the offset in it"""
        for start, kind, count in self.members.get(struct, ()):
            size = self.sizes.get(kind)
            if size and start <= offset < start + size * count:
                return self.inside(kind, (offset - start) % size)
        return struct, offset


def folded(address):
    """The memory an access reaches and the place's address in its first copy, or None (a device's registers)"""
    for first, end, name, size in game_profile.WIDTH_MEMORIES:
        if first <= address < end:
            return name, first + (address - first) % size
    canonical = game_profile.rom.canonical_address(address)
    return ("program data", canonical) if canonical is not None else None


def place_of(symbols, memory, address):
    """(group, offset): a structure of the game's state and the offset in it, or the game's variables by
    address; a table of the program's data and the offset in it; the memory by address. None for the stacks"""
    if memory == "work RAM":
        for base, _, struct, count in state_memory.STATE_BLOCKS:
            size = symbols.sizes.get(struct) if struct else None
            if struct is None and base <= address < base + count:
                return None
            if size and base <= address < base + size * count:
                return symbols.inside(struct, (address - base) % size)
        return "variables", address
    if memory == "program data":
        name, offset = symbols.name_at(address)
        return ("program data", address) if name is None else (name, offset)
    return memory, address


def replaced_functions():
    """The functions the board puts its own in place of (runtime/replaced.txt), which the C never runs"""
    path = ROOT / "runtime" / "replaced.txt"
    if not path.exists():
        return set()
    return {line.split("#", 1)[0].split()[0] for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()}


def load_units():
    """Where the loader lays out the program's data's values wider than a byte: {address: width}, from
    build/access_widths/units.txt (the headless game run with ORUNNERS_DUMP_UNITS set to it), or None"""
    path = OUT / "units.txt"
    if not path.exists():
        return None
    units = {}
    for line in path.read_text(encoding="ascii").splitlines():
        if line and not line.startswith("#"):
            address, width = line.split()
            units[int(address, 16)] = int(width)
    return units


def census_files():
    return sorted(path for path in OUT.glob("*.txt") if not re.search(r"_(hashes|sound)\.txt$", path.name)
                  and path.name not in ("report.txt", "units.txt"))


def load(symbols, excluded):
    """The census: every access by (memory, group, offset, width) with its kinds, reads or writes and functions;
    the accesses out of line; the reads a big-endian processor gives otherwise; how many reads were compared"""
    accesses = {}
    data_reads = {}                     # the program's data's reads by (address, width): [kinds, functions]
    out_of_line = defaultdict(set)      # memory -> {(address, width, function)}
    differing = {}                      # (group, offset, width) -> [count, functions, first values, address]
    compared = [0, 0, 0]
    tally = re.compile(r"# reads of the work RAM compared: (\d+); not compared: (\d+) .*, (\d+) by bit")
    for path in census_files():
        with path.open(encoding="ascii") as census:
            for line in census:
                if line.startswith("#"):
                    counts = tally.match(line)
                    if counts:
                        compared = [total + int(value) for total, value in zip(compared, counts.groups())]
                    continue
                if line.startswith("!"):
                    _, address, width, pc, count, value, other = line.split()
                    function = symbols.function_at(int(pc, 16))
                    place = place_of(symbols, "work RAM", 0x200000 | (int(address, 16) & 0x1FFFF))
                    if function in excluded or place is None:
                        continue
                    key = (place[0], place[1], int(width))
                    entry = differing.get(key)
                    if entry is None:
                        entry = differing[key] = [0, set(), (value, other), int(address, 16)]
                    entry[0] += int(count)
                    entry[1].add(function)
                    continue
                address, width, rw, kind, pc = line.split()
                where = folded(int(address, 16))
                if where is None:
                    continue
                memory, address = where
                width = int(width)
                function = symbols.function_at(int(pc, 16))
                if function in excluded:
                    continue
                place = place_of(symbols, memory, address)
                if place is None:
                    continue
                if width > 1 and address % width and kind == "-":
                    out_of_line[memory].add((address, width, function))
                if memory == "program data" and rw == "R":
                    read = data_reads.setdefault((address, width), [set(), set()])
                    read[0].add(kind)
                    read[1].add(function)
                key = (memory, place[0], place[1], width)
                entry = accesses.get(key)
                if entry is None:
                    entry = accesses[key] = [set(), set(), set()]
                entry[0].add(kind)
                entry[1].add(rw)
                entry[2].add(function)
    return accesses, out_of_line, differing, compared, data_reads


def mixed_places(accesses, memory):
    """The places of a memory read at more than one width by instructions other than string moves: runs of
    overlapping reads, each with its shapes (offset, width, entry)"""
    groups = defaultdict(list)
    for (where, group, offset, width), entry in accesses.items():
        if where == memory and "R" in entry[1] and entry[0] - {"S"}:
            groups[group].append((offset, width, entry))
    found = []
    for group, shapes in groups.items():
        shapes.sort(key=lambda shape: (shape[0], -shape[1]))
        run, end = [], -1
        for shape in shapes + [(None, 0, None)]:
            if shape[0] is None or shape[0] >= end:
                if len({(offset, width) for offset, width, _ in run}) > 1:
                    found.append((group, run))
                run, end = [], -1
                if shape[0] is None:
                    break
            run.append(shape)
            end = max(end, shape[0] + shape[1])
    return found


def names_at(symbols, group, offset):
    names = sorted(symbols.fields.get(group, {}).get(offset, ()))
    return f" [{', '.join(names[:3])}{', ...' if len(names) > 3 else ''}]" if names else ""


def where_text(symbols, group, offset):
    if group == "variables":
        name, into = symbols.name_at(offset)
        return f"{name or 'variables'}{f' +{into:X}' if into else ''} ({offset:06X})"
    return f"{group} +{offset:X}"


def report():
    symbols = Symbols(ROOT / "symbols.txt")
    excluded = set(getattr(game_profile, "WIDTH_ROUTINES", ())) | replaced_functions()
    files = census_files()
    if not files:
        raise SystemExit(f"{OUT}: no census; python tools/width_census.py run makes it")
    accesses, out_of_line, differing, compared, data_reads = load(symbols, excluded)
    lines = [f"# What the C leans on of the processor it runs on (tools/width_census.py, over {len(files)} scenarios",
             "# of build/access_widths): the places of the work RAM a big-endian processor reads another value,",
             "# the program's data read at more than one width, and the accesses out of line with their width"]
    summary = []

    # the work RAM read otherwise on a big-endian processor
    by_function = defaultdict(set)
    lines.append(f"\n== work RAM: {len(differing)} places and widths read with another value on a big-endian "
                 f"processor (of {compared[0]} reads compared; not compared: {compared[1]} of bytes a move "
                 f"brought from the program's data, {compared[2]} by bit instructions). WIDTH@PLACE READS "
                 "(FUNCTIONS) V70-VALUE/BIG-ENDIAN-VALUE [NAMES]")
    for (group, offset, width), (count, functions, values, _) in sorted(differing.items(), key=lambda item: (
            str(item[0][0]), item[0][1], item[0][2])):
        for function in functions:
            by_function[function].add((group, offset, width))
        lines.append(f"  {width}@{where_text(symbols, group, offset)} {count} ({', '.join(sorted(functions))}) "
                     f"{values[0]}/{values[1]}{names_at(symbols, group, offset)}")
    lines.append(f"\n-- work RAM: the {len(by_function)} functions that make those reads")
    for function, places in sorted(by_function.items(), key=lambda item: (-len(item[1]), item[0])):
        lines.append(f"  {function}: {len(places)}")
    summary.append(("work RAM read otherwise on big endian", len(differing), len(by_function)))

    # the program's data at more than one width
    mixed = mixed_places(accesses, "program data")
    functions = set()
    lines.append(f"\n== program data: {len(mixed)} places read at more than one width. WIDTH@OFFSET KINDS (FUNCTIONS)")
    for group, run in sorted(mixed, key=lambda item: (str(item[0]), item[1][0][0])):
        shapes = []
        for offset, width, entry in run:
            functions |= entry[2]
            kinds = ",".join(KINDS[kind] for kind in sorted(entry[0]) if KINDS[kind])
            shapes.append(f"{width}@{offset:X}{' ' + kinds if kinds else ''} ({', '.join(sorted(entry[2]))})")
        lines.append(f"  {where_text(symbols, group, run[0][0])}: " + "; ".join(shapes))
    summary.append(("program data at more than one width", len(mixed), len(functions)))

    # the program's data against where the loader lays out its values (the game with ORUNNERS_DUMP_UNITS)
    units = load_units()
    if units is not None:
        across, wider = defaultdict(lambda: [set(), []]), defaultdict(lambda: [set(), []])
        unit_of = {}
        for start, width in units.items():
            for byte in range(start, start + width):
                unit_of[byte] = (start, width)
        for (address, width), (kinds, functions) in sorted(data_reads.items()):
            if not kinds - {"S", "B"}:
                continue    # (a move takes the bytes as the V70 holds them, data_byte_at; a bit's byte is read alone)
            touched = {unit_of[byte] for byte in range(address, address + width) if byte in unit_of}
            if not touched and width == 1:
                continue
            # (a read over whole values, one or more, is fine: the C reads each and puts them together where the
            # original read several at once; one that takes a part of a value is not)
            cut = {(start, size) for start, size in touched if start < address or start + size > address + width}
            if not cut and touched:
                continue
            name, _ = symbols.name_at(address)
            target = across if cut else wider
            target[name][0].update(functions)
            target[name][1].append(f"{width}@{address:06X}")
        for title, found in (("reads that cut across a value the loader lays out wider or narrower, which a "
                              "big-endian processor reads the other way round", across),
                             ("reads of several bytes the loader lays out one by one (the C reads them as their "
                              "bytes where it reads them element by element)", wider)):
            lines.append(f"\n== program data: {sum(len(reads) for _, reads in found.values())} {title}, in "
                         f"{len(found)} tables. TABLE: READS (FUNCTIONS)")
            for name, (functions, reads) in sorted(found.items(), key=lambda item: str(item[0])):
                lines.append(f"  {name}: {' '.join(reads[:8])}{' ...' if len(reads) > 8 else ''} ({len(reads)}; "
                             f"{', '.join(sorted(functions))})")
        summary.append(("program data across the loader's values", sum(len(r) for _, r in across.values()),
                        len({f for functions, _ in across.values() for f in functions})))
        summary.append(("  (bytes read wider: tables)", sum(len(r) for _, r in wider.values()), len(wider)))

    # out of line
    for memory in ("work RAM", "program data"):
        by_function = defaultdict(list)
        for address, width, function in sorted(out_of_line[memory]):
            by_function[function].append(f"{width}@{address:06X}")
        lines.append(f"\n-- {memory}: {len(out_of_line[memory])} accesses out of line with their width, "
                     f"in {len(by_function)} functions")
        for function, where in sorted(by_function.items()):
            lines.append(f"  {function}: {' '.join(where[:8])}{' ...' if len(where) > 8 else ''} ({len(where)})")
        summary.append((f"{memory} out of line", len(out_of_line[memory]), len(by_function)))

    # the board's memories
    lines.append("\n== the board's memories: the instructions that reach them, by width (strings apart)")
    for _, _, memory, _ in game_profile.WIDTH_MEMORIES[1:]:
        widths = defaultdict(set)
        for (where, _, _, width), entry in accesses.items():
            if where == memory:
                for function in entry[2]:
                    widths["string" if entry[0] == {"S"} else width].add(function)
        lines.append(f"  {memory}: " + ", ".join(f"{len(widths[width])} functions {width if width == 'string' else str(width) + ' bytes'}"
                                                for width in (1, 2, 4, "string") if widths[width]))
        lines.append("    " + "; ".join(f"{width}: {', '.join(sorted(widths[width]))}" for width in (1, 4)
                                        if widths[width]))
    (OUT / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"{'':<40} {'places':>8} {'functions':>10}")
    for what, places, function_count in summary:
        print(f"{what:<40} {places:8} {function_count:10}")
    print(f"\n{len(files)} scenarios, {compared[0]} reads of the work RAM compared: {OUT / 'report.txt'}")
    return 0


def main():
    arguments = sys.argv[1:]
    if arguments[:1] == ["run"]:
        jobs = int(arguments[arguments.index("--jobs") + 1]) if "--jobs" in arguments else 6
        return run(jobs)
    if arguments[:1] == ["report"]:
        return report()
    print(__doc__.split("\n\n")[1])
    return 1


if __name__ == "__main__":
    sys.exit(main())
