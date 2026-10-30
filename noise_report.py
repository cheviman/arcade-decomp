"""
Name:        noise_report.py
Description: counts what is left of the CPU and of the decompiler in the C (src/, src/sound/) and compares it
             with the counts last saved
Version:     0.1.0
Created:     17-10-2026
Modified:    30-10-2026
Author:      Sebastian Toyos

Every kind of leftover the C can still have, counted in one pass over the generated C, so a round of work
shows what it took away and nothing comes back unseen: gotos, functions and locals named after an address
or a register, values the decompiler works out once named by a number, the registers the C keeps as globals,
fields, memory and tables of the ROM named after their place, memory reached through computed addresses, results written back
again before an early return (a function writes them once, at its end, where its ways out can meet), jumps
out of a table, ROM addresses printed as numbers, functions without a comment, data read through its ROM
address instead of a table or structure with a name, tables the C names over the ROM (src/ram.h), functions
the board puts its own in place of. The counts are compared with tools/noise_baseline.txt; a count that went up makes the run fail.

Two measures of how much of the C is clean, function by function and line by line: with nothing at all
of the above (clean), and with nothing but what the C has to keep to do what the original does
(faithful): the registers the C keeps as globals, where a value goes from one function to another
through them (the scratch ones are locals wherever it does not), several fields read or written in
one access, as the original's one instruction does (AS/AT, a table read at another width), and a part
of a value changed in place, the rest of it kept, as a byte or halfword instruction on a wider value does
(x = (x & ~0xFFu) | ..., or a pointer moved within its 64 KB page, (p & ~0xFFFFu) | (uint16_t)(p + d)).
A partial write that puts a part of one value into another ((y & ~0xFFu) | x) is a leftover in both
measures (mixed_partial_writes). Memory reached through an address the code works out is faithful only in
the routines the game's profile lists as working on memory by its address, whatever is there
(game_profile.MEMORY_ROUTINES: a memory test, a checksum, a copy), each with why. The README explains
each of those.

Apart from those, what keeps the game's state in the board's memory model (MODEL, in the game's C): the
structures it reaches by casting an address of the work RAM, the variables src/ram.h keeps at a fixed
address, addresses of the work RAM, of code and of tables used as values, places worked out within a page,
memory moved, filled or searched through its address; and how much of the game's C has none of them. Names made up of an address or an offset count where a
comment still gives one, or a structure declares one no code reads.

Usage: python tools/noise_report.py [--save] [--src DIR] [--files]
  --save   write the counts as the new baseline (after a round that took something away)
  --src    count another copy of the C (tools/decompile.py --out DIR) against the same baseline
  --files  also the data read through its ROM address and the tables named after theirs, file by file
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_profile  # noqa: E402  (the game's: its root)
from decompiler import memory  # noqa: E402  (the game's work RAM, which its profile sets)
ROOT = game_profile.ROOT
# function -> why it works on memory by its address (the faithful measure takes its mem_access as the original's)
MEMORY_ROUTINES = getattr(game_profile, "MEMORY_ROUTINES", {})
BASELINE = ROOT / "tools" / "noise_baseline.txt"
UNNAMED_ADDRESSES = ROOT / "build" / "decompile" / "unnamed_addresses.txt"

FUNCTION = re.compile(r"^(?:static )?\w[\w *]*?\b(\w+)\((.*)\)$")
# a result written back through its pointer from the function's variable for it, whole or the part the
# function writes: *p_x = x; *p_x = (*p_x & 0xFFFF0000u) | x; *p_x = (*p_x & 0xFFFF0000u) | (x & 0xFFFFu);
# the Z80's registers, which the sound program's C (src/sound/) names its values after until a line names them
Z80_REGISTER = r"(?:a|b|c|d|e|h|l|af|bc|de|hl|ix|iy|ixh|ixl|iyh|iyl)(?:_\d+)?"
COPY_BACK = re.compile(r"^\t+\*p_(\w+) = (?:\w+|\(\*p_\1 & 0x[0-9A-F]+u\) \| (?:\w+|\(\w+ & 0x[0-9A-F]+u\)));$")
# kind -> (what it is, pattern counted in the code lines; None: counted below)
KINDS = {
    "goto": ("goto statements", re.compile(r"\bgoto\b")),
    "control_variables": ("functions with bool control variables", None),
    "sub_functions": ("functions named after their address (sub_XXXXXX, the sound program's sound_XXXX)", None),
    "unk_names": ("memory named after its address (unk_XXXXXX, and as its address unk_XXXXXX_ADDRESS)",
                  re.compile(r"(?<!->)(?<!\.)\bunk_[0-9A-F]{4,6}(?:_ADDRESS)?\b")),
    "rom_names": ("tables of the program ROM named after their address (rom_XXXXXX)",
                  re.compile(r"(?<!->)(?<!\.)\brom_[0-9A-F]{6}(?:_ADDRESS)?\b")),
    "unk_fields": ("fields of a structure named after their offset (->unk_XX)",
                   re.compile(r"(?:->|\.)unk_[0-9A-F]{2,4}\b")),
    "made_up_names": ("names made up of an address or an offset that a comment of the C still gives, or that "
                      "a structure declares and no code reads (unk_30;)", None),
    "register_names": ("parameters and locals named after a register (r1, p_r4, r0_2; the Z80's a, hl)", None),
    "temp_names": ("values the decompiler works out once, named by a number (t1)", None),
    "stack_names": ("stack slots named after their offset (stack_4)", None),


    "context_registers": ("uses of the registers the C keeps as globals, still named after them (src/game.h)", None),
    "var_fields": ("object fields without a name (var_XX)", re.compile(r"\bvar_[0-9A-F]{2}\b")),
    "mem_access": ("memory reached through a computed address (mem_read/mem_write)",
                   re.compile(r"\bmem_(?:read|write)\d+\(")),
    "wide_access": ("several fields read or written at once (AS/AT), a table read at another width (TABLE16)",
                    re.compile(r"\b(?:A[ST]|TABLE)\d+\(")),
    "copy_backs": ("results written back again before an early return (*p_x = x; return;)", None),
    "invalid_jump": ("jumps through a table with an index it does not have", re.compile(r"\binvalid_jump\(")),
    "unknown_code": ("calls to code the analysis does not know", re.compile(r"\bunknown_code\(")),
    "raw_rom_addresses": ("ROM addresses printed as numbers (build/decompile/unnamed_addresses.txt)", None),
    "uncommented": ("functions without a comment above them", None),
    "rom_reads": ("data read through an address (DATA8/16/32, data_at(), ROM8/16/32, rom(), SOUND_ROM8/16, "
                  "sound_rom_at)",
                  re.compile(r"\b(?:DATA(?:8|16|32)S?|data_at|ROM(?:8|16|32)S?|SOUND_ROM(?:8|16)|rom|sound_rom_at)\(")),
    "rom_table_reads": ("reads of a table the C names over an address (src/ram.h: rom(...); src/sound/sound.h: "
                        "sound_rom_at(...))", None),
    "board_replaced": ("functions the board puts its own in place of (BOARD_ORIGINAL, runtime/replaced.txt)", None),
    "ram_structures": ("structures reached by casting an address of the work RAM (OBJECT(x)->field)", None),
    "ram_variables": ("variables and arrays src/ram.h keeps at a fixed address of the work RAM (RAM16(NAME_ADDRESS))",
                      None),
    "ram_addresses": ("addresses of the work RAM used as values (NAME_ADDRESS, SCREEN_1, PLAYER_BLOCK(n))", None),
    "code_addresses": ("functions kept as their address (src/game.h's NAME_ADDRESS, call_code)", None),
    "table_addresses": ("tables reached through an address (casts over data_at(), a table's NAME_ADDRESS)", None),
    "page_links": ("places worked out within a page of the board's memory (LINKED, PAGE_ADD, IN_PAGE)", None),
    "ram_blocks": ("memory moved, filled or searched through its address, as the V60's string instructions do "
                   "(move_halfwords, move_string, set_bytes, search_bytes)", None),
    "pointer_conversions": ("pointers the C keeps turned back into the address the original holds where the C "
                            "still needs it, or made from one (address_of(current_object), pointer_to(address))",
                            None),
}
# the kinds that keep the game's state in the board's memory model, counted in the game's C (not the sound
# program's, whose memory is the sound board's)
MODEL = ("ram_structures", "ram_variables", "ram_addresses", "code_addresses", "table_addresses", "page_links",
         "ram_blocks", "pointer_conversions")
INFORMATION = {
    "functions": "functions",
    "lines": "lines of C",
    "casts": "casts",
    "partial_writes": "partial writes of a variable ((x & ~0xFFFFu) | ...)",
    "mixed_partial_writes": "of those, the ones that put a part of one value into another ((y & ~0xFFu) | x)",
    "board_registers": "reads and writes of a device of the board, through the bus (io_read8...)",
    "clean_functions": "functions with nothing of the above in them (the partial writes too), per thousand",
    "clean_lines": "lines of code in the functions with nothing of the above, per thousand",
    "faithful_functions": "functions with nothing of the above but the faithful leftovers (the globals' registers, "
                          "fields at once, parts changed in place, the memory routines' addresses), per thousand",
    "faithful_lines": "lines of code in those functions with nothing but those, per thousand",
}
# the kinds the original's own doing leaves in the C (see the module's notes), with the registers kept as globals
FAITHFUL = ("wide_access",)


def count(paths):
    counts = {kind: 0 for kind in list(KINDS) + list(INFORMATION)}
    for path in paths:
        lines = path.read_text(encoding="ascii", errors="replace").splitlines()
        counts["lines"] += len(lines)
        in_function = False
        z80 = "sound" in path.parts    # (the sound program's C: its registers have names of their own)
        for index, line in enumerate(lines):
            if line.startswith(("/*", "   ", " *")):
                continue    # (comments)
            found = FUNCTION.match(line)
            if found and index + 1 < len(lines) and lines[index + 1] == "{":
                in_function = True
                counts["functions"] += 1
                if not lines[index - 1].endswith("*/"):
                    counts["uncommented"] += 1
                if re.fullmatch(r"sub_[0-9A-F]{6}|sound_[0-9A-F]{4}", found.group(1)):
                    counts["sub_functions"] += 1
                if found.group(1) == "BOARD_ORIGINAL":
                    counts["board_replaced"] += 1
                counts["register_names"] += len(re.findall(r"(?<!\w)(?:p_)?r\d+(?:_\d+)?(?=[,)]|$)", found.group(2)))
                if z80:
                    counts["register_names"] += len(re.findall(rf"(?<!\w)(?:p_)?{Z80_REGISTER}(?=[,)]|$)",
                                                               found.group(2)))
                continue
            if not in_function:
                continue
            if line == "}":
                in_function = False    # (what follows a function's end, a table of data, is no code of it)
                continue
            for kind, (_, pattern) in KINDS.items():
                if pattern is not None:
                    counts[kind] += len(pattern.findall(line))
            if re.match(r"^\t(?:uint\d+_t|int\d+_t) ", line):
                counts["register_names"] += len(re.findall(r"\br\d+(?:_\d+)? = ", line))
                if z80:
                    counts["register_names"] += len(re.findall(rf"\b{Z80_REGISTER} = ", line))
                counts["temp_names"] += len(re.findall(r"\bt\d+ = ", line))
                counts["stack_names"] += len(re.findall(r"\bstack_[0-9A-F]+ = ", line))
            if line.startswith("\tbool "):
                counts["control_variables"] += 1
            if COPY_BACK.match(line) and not COPY_BACK.match(lines[index - 1]):
                # a run of results written back: counted when a return follows it (a way out before the
                # end, which writes them again), not at the end of the function, where they are written once
                end = index
                while end < len(lines) and COPY_BACK.match(lines[end]):
                    end += 1
                if end < len(lines) and lines[end].strip().startswith("return"):
                    counts["copy_backs"] += end - index
            counts["casts"] += len(re.findall(r"\((?:u?int\d+_t)\)", line))
            counts["partial_writes"] += len(re.findall(r"& ~0xF+u\) \|", line))
            if PARTIAL_WRITE.search(line) and not in_place(line):
                counts["mixed_partial_writes"] += 1
            counts["board_registers"] += len(re.findall(r"\bio_(?:read|write)\d+\(", line))
    return counts


PARTIAL_WRITE = re.compile(r"& ~0xF+u\) \|")
# one partial write: the value whose other parts it keeps, (VALUE & ~0xFFu) |
PARTIAL_OCCURRENCE = re.compile(r"\(((?:[^()]|\([^()]*\))+?) & ~0xF+u\) \|")


def in_place(line):
    """True if every partial write on 'line' changes a part of a value and keeps the rest of that same
    value: the variable it assigns ("x = (x & ~0xFFu) | ..."), or a part worked out from the value itself
    ("(p & ~0xFFFFu) | (uint16_t)(p + d)")."""
    target = re.match(r"^\s*(\*?[\w>().\-\[\]]+?) = ", line)
    found = False
    for match in PARTIAL_OCCURRENCE.finditer(line):
        found = True
        kept = match.group(1).strip()
        while kept.startswith("(") and kept.endswith(")") and kept[1:-1].count("(") == kept[1:-1].count(")"):
            kept = kept[1:-1]
        rest = line[match.end():]
        if target and kept == target.group(1):
            continue
        if re.match(r"\s*\(uint\d+_t\)\(\(?" + re.escape(kept) + r"\b", rest):
            continue
        return False
    return found


def clean_measure(paths, extra, registers=None, faithful=False):
    """(functions with no leftover, functions, lines of code with none, lines of code in functions): a
    leftover is any kind of KINDS on the line, a table over an address ('extra'), a register the C keeps
    as a global ('registers'), a partial write, a bool control variable, a result written back before an
    early return; a parameter named after a register leaves its function unclean. With 'faithful', the
    FAITHFUL kinds, the registers kept as globals, the partial writes in place and the memory routines'
    memory reached by address (MEMORY_ROUTINES) are no leftovers."""
    patterns = [pattern for kind, (_, pattern) in KINDS.items()
                if pattern is not None and not (faithful and kind in FAITHFUL)] + list(extra) \
        + ([registers] if registers is not None and not faithful else [])
    by_address = KINDS["mem_access"][1]
    routine_patterns = [pattern for pattern in patterns if pattern is not by_address]
    clean_functions = functions = clean_lines = lines = 0
    for path in paths:
        text = path.read_text(encoding="ascii", errors="replace").splitlines()
        z80 = "sound" in path.parts
        inside, dirty, checked = False, False, patterns
        for index, line in enumerate(text):
            if line.startswith(("/*", "   ", " *")):
                continue
            found = FUNCTION.match(line)
            if found and index + 1 < len(text) and text[index + 1] == "{":
                inside = True
                checked = routine_patterns if faithful and found.group(1) in MEMORY_ROUTINES else patterns
                dirty = bool(re.findall(r"(?<!\w)(?:p_)?r\d+(?:_\d+)?(?=[,)]|$)", found.group(2))) or \
                    (z80 and bool(re.findall(rf"(?<!\w)(?:p_)?{Z80_REGISTER}(?=[,)]|$)", found.group(2))))
                continue
            if not inside:
                continue
            if line == "}":
                functions += 1
                clean_functions += not dirty
                inside = False
                continue
            if line.strip() in ("", "{", "}"):
                continue
            lines += 1
            leftover = any(pattern.search(line) for pattern in checked) or line.startswith("\tbool ") \
                or bool(PARTIAL_WRITE.search(line)) and not (faithful and in_place(line))
            if not leftover and COPY_BACK.match(line):
                end = index
                while end < len(text) and COPY_BACK.match(text[end]):
                    end += 1
                leftover = end < len(text) and text[end].strip().startswith("return")
            if leftover:
                dirty = True
            else:
                clean_lines += 1
    return clean_functions, functions, clean_lines, lines


def memory_model(source):
    """(the MODEL counts, {kind: pattern counted in the code}) of the game's C under 'source': what keeps
    the game's state in the board's memory model, the way src/ram.h and src/game.h define it. Variables
    and arrays count where ram.h puts them at their address (their uses read like any C variable's); the
    rest counts where the code uses it."""
    counts = {kind: 0 for kind in MODEL}
    ram_h, game_h = source / "ram.h", source / "game.h"
    if not ram_h.exists():
        return counts, {}
    header = ram_h.read_text(encoding="ascii")
    start, end = memory.WORK_RAM
    casts = {}
    for name, function in re.findall(r"^#define (\w+)\(address\) \(\((?:const )?(?:struct )?\w+ \*\)(\w+)\(address\)\)",
                                     header, re.M):
        casts.setdefault(function, set()).add(name)
    variables = re.findall(r"^#define (\w+) (?:RAM(?:8|16|32)S?|\(\((?:const )?(?:struct )?\w+ \*\)ram)\(\1_ADDRESS\)",
                           header, re.M)
    counts["ram_variables"] = len(variables)
    ram_names, table_names = {"WORK_RAM", "PLAYER_BLOCK"}, set()
    for name, value in re.findall(r"^#define (\w+) 0x([0-9A-F]+)u", header, re.M):
        if start <= int(value, 16) < end:
            ram_names.add(name)
        elif name.endswith("_ADDRESS"):
            table_names.add(name)
    code_names = set(re.findall(r"^#define (\w+_ADDRESS) 0x", game_h.read_text(encoding="ascii"), re.M)) \
        if game_h.exists() else set()

    def words(names, call=""):
        return re.compile(r"\b(?:" + "|".join(sorted(names)) + r")\b" + call) if names else None
    patterns = {
        "ram_structures": words(casts.get("ram", ()), r"\("),
        "ram_addresses": words(ram_names),
        "code_addresses": words(code_names | {"call_code"}),
        "table_addresses": words(casts.get("data_at", set()) | table_names),
        "page_links": re.compile(r"\b(?:LINKED|PAGE_ADD|IN_PAGE)\("),
        "pointer_conversions": re.compile(r"\b(?:address_of|pointer_to)\("),
        "ram_blocks": re.compile(r"\b(?:move_(?:string|bytes|halfwords|bytes_backward|halfwords_backward|bits_down|bits_up)"
                                 r"|set_(?:bytes|halfwords)|search_(?:bytes|bits_up))\("),
    }
    patterns = {kind: pattern for kind, pattern in patterns.items() if pattern is not None}
    numbers = re.compile(r"\b0x([0-9A-F]+)u\b")
    for path in sorted(source.rglob("*.c")):
        if "sound" in path.relative_to(source).parts:
            continue
        code = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="ascii", errors="replace"), flags=re.S)
        for kind, pattern in patterns.items():
            counts[kind] += len(pattern.findall(code))
        # and the work RAM's addresses the C prints as numbers (the tasks' stacks, places in the blocks)
        counts["ram_addresses"] += sum(start <= int(value, 16) < end for value in numbers.findall(code))
    return counts, patterns


def model_measure(source, patterns):
    """(functions of the game's C with none of the memory model's 'patterns', functions, lines of code with
    none, lines of code in functions)."""
    free_functions = functions = free_lines = lines = 0
    start, end = memory.WORK_RAM
    numbers = re.compile(r"\b0x([0-9A-F]+)u\b")
    for path in sorted(source.rglob("*.c")):
        if "sound" in path.relative_to(source).parts:
            continue
        text = path.read_text(encoding="ascii", errors="replace").splitlines()
        inside, touched = False, False
        for index, line in enumerate(text):
            if line.startswith(("/*", "   ", " *")):
                continue
            if FUNCTION.match(line) and index + 1 < len(text) and text[index + 1] == "{":
                inside, touched = True, False
                continue
            if not inside:
                continue
            if line == "}":
                functions += 1
                free_functions += not touched
                inside = False
                continue
            if line.strip() in ("", "{", "}"):
                continue
            lines += 1
            if any(pattern.search(line) for pattern in patterns.values()) \
                    or any(start <= int(value, 16) < end for value in numbers.findall(line)):
                touched = True
            else:
                free_lines += 1
    return free_functions, functions, free_lines, lines


def main():
    arguments = sys.argv[1:]
    source = ROOT / "src"
    if "--src" in arguments:
        source = Path(arguments[arguments.index("--src") + 1]).resolve()
    sources = sorted(path for path in source.rglob("*.c"))
    counts = count(sources)
    # the tables src/ram.h and src/sound/sound.h define over an address ("#define NAME ((const uint16_t
    # *)rom(NAME_ADDRESS))"): every use of one reads by the address, which the game has to read by name
    tables = []
    for header, function in ((source / "ram.h", "rom"), (source / "sound" / "sound.h", "sound_rom_at")):
        if header.exists():
            tables += re.findall(r"^#define (\w+) \(\(const (?:struct )?\w+ \*\)\(?" + function + r"\(",
                                 header.read_text(encoding="ascii"), re.M)
    table_use = re.compile(r"\b(?:" + "|".join(tables) + r")\b") if tables else None
    if table_use:
        counts["rom_table_reads"] = sum(len(table_use.findall(path.read_text(encoding="ascii", errors="replace")))
                                        for path in sources)
    # the registers the whole program shares, globals of the C ("extern uint32_t r20, ..." in src/game.h and
    # src/sound/sound.h): the ones still named after the register
    registers = set()
    for header in (source / "game.h", source / "sound" / "sound.h"):
        if header.exists():
            for declaration in re.findall(r"^extern \w+ ([^;]*);", header.read_text(encoding="ascii"), re.M):
                registers |= {name.strip() for name in declaration.split(",") if re.fullmatch(r"r\d+", name.strip())}
    register_use = re.compile(r"\b(?:" + "|".join(sorted(registers)) + r")\b") if registers else None
    if register_use:
        counts["context_registers"] = sum(len(register_use.findall(path.read_text(encoding="ascii", errors="replace")))
                                          for path in sources)
    if UNNAMED_ADDRESSES.exists():
        counts["raw_rom_addresses"] = sum(1 for line in UNNAMED_ADDRESSES.read_text().splitlines()
                                          if line and not line.startswith("#"))
    # names made up of an address or an offset: in a comment, a description written before the thing got
    # its name; declared as a field no code reads, a name for bytes the code reaches some other way
    made_up = re.compile(r"\b(?:unk|rom|sub|var|field)_[0-9A-F]{2,6}\b")
    code_words = set()
    for path in sorted(source.rglob("*.[ch]")):
        text = path.read_text(encoding="ascii", errors="replace")
        counts["made_up_names"] += sum(len(made_up.findall(comment)) for comment in re.findall(r"/\*.*?\*/", text, re.S))
        if path.suffix == ".c":
            code_words.update(re.findall(r"\w+", re.sub(r"/\*.*?\*/", "", text, flags=re.S)))
    for header in (source / "ram.h", source / "sound" / "sound.h"):
        if header.exists():
            counts["made_up_names"] += sum(name not in code_words for name in re.findall(
                r"^\s+\w+ (unk_[0-9A-F]{2,4})(?:\[[^]]*\])?;", header.read_text(encoding="ascii"), re.M))
    model, model_patterns = memory_model(source)
    counts.update(model)
    measure = clean_measure(sources, [table_use] if table_use is not None else [], register_use)
    faithful = clean_measure(sources, [table_use] if table_use is not None else [], register_use, faithful=True)
    counts["faithful_functions"], counts["faithful_lines"] = faithful[0] * 1000 // max(faithful[1], 1), \
        faithful[2] * 1000 // max(faithful[3], 1)
    counts["clean_functions"], counts["clean_lines"] = measure[0] * 1000 // max(measure[1], 1), \
        measure[2] * 1000 // max(measure[3], 1)
    saved = {}
    if BASELINE.exists():
        for line in BASELINE.read_text().splitlines():
            if line and not line.startswith("#"):
                kind, value = line.split()[:2]
                saved[kind] = int(value)
    rose = []
    print(f"{'':20s} {'now':>7s} {'saved':>7s}")
    for kind, (what, _) in list(KINDS.items()) + [(kind, (what, None)) for kind, what in INFORMATION.items()]:
        before = saved.get(kind)
        change = "" if before is None or before == counts[kind] else f"{counts[kind] - before:+d}"
        print(f"{kind:20s} {counts[kind]:7d} {'' if before is None else before:>7} {change:>6s}  {what}")
        if kind in KINDS and before is not None and counts[kind] > before:
            rose.append(kind)
    print(f"\nclean: {measure[0]} of {measure[1]} functions ({measure[0] * 100 / max(measure[1], 1):.1f}%), "
          f"{measure[2]} of {measure[3]} lines of code ({measure[2] * 100 / max(measure[3], 1):.1f}%)")
    print(f"faithful: {faithful[0]} of {faithful[1]} functions ({faithful[0] * 100 / max(faithful[1], 1):.1f}%), "
          f"{faithful[2]} of {faithful[3]} lines of code ({faithful[2] * 100 / max(faithful[3], 1):.1f}%) "
          "with nothing but the globals' registers, fields at once, parts changed in place and the memory "
          "routines' addresses")
    if model_patterns:
        free = model_measure(source, model_patterns)
        print(f"out of the board's memory model: {free[0]} of {free[1]} functions of the game "
              f"({free[0] * 100 / max(free[1], 1):.1f}%), {free[2]} of {free[3]} lines of code "
              f"({free[2] * 100 / max(free[3], 1):.1f}%) with none of its {', '.join(MODEL)}")
    if "--files" in arguments:
        print()
        print(f"{'rom_reads':>9s} {'rom_names':>9s}")
        for path in sources:
            found = count([path])
            if found["rom_reads"] or found["rom_names"]:
                print(f"{found['rom_reads']:9d} {found['rom_names']:9d}  {path.relative_to(source).as_posix()}")
    if "--save" in arguments and source == ROOT / "src":
        BASELINE.write_text("# KIND COUNT: tools/noise_report.py --save writes it\n" +
                            "".join(f"{kind} {value}\n" for kind, value in counts.items()), encoding="ascii")
        print(f"saved in {BASELINE.relative_to(ROOT)}")
    if rose:
        print("went up: " + ", ".join(rose))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
