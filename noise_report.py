"""
Name:        noise_report.py
Description: counts what is left of the CPU and of the decompiler in the C (src/, src/sound/) and compares it
             with the counts last saved
Version:     0.1.0
Created:     17-10-2026
Modified:    27-10-2026
Author:      Sebastian Toyos

Every kind of leftover the C can still have, counted in one pass over the generated C, so a round of work
shows what it took away and nothing comes back unseen: gotos, functions and locals named after an address
or a register, values the decompiler works out once named by a number, the registers the C keeps as globals,
fields, memory and tables of the ROM named after their place, memory reached through computed addresses, results written back
again before an early return (a function writes them once, at its end, where its ways out can meet), jumps
out of a table, ROM addresses printed as numbers, functions without a comment, data read through its ROM
address instead of a table or structure with a name, tables the C names over the ROM (src/ram.h), functions
the board puts its own in place of. The counts are compared with tools/noise_baseline.txt; a count that went up makes the run fail.

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
ROOT = game_profile.ROOT
BASELINE = ROOT / "tools" / "noise_baseline.txt"
UNNAMED_ADDRESSES = ROOT / "build" / "decompile" / "unnamed_addresses.txt"

FUNCTION = re.compile(r"^(?:static )?\w[\w ]*?\b(\w+)\((.*)\)$")
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
    "unk_names": ("memory named after its address (unk_XXXXXX)", re.compile(r"(?<!->)(?<!\.)\bunk_[0-9A-F]{4,6}\b")),
    "rom_names": ("tables of the program ROM named after their address (rom_XXXXXX)",
                  re.compile(r"(?<!->)(?<!\.)\brom_[0-9A-F]{6}\b")),
    "unk_fields": ("fields of a structure named after their offset (->unk_XX)",
                   re.compile(r"(?:->|\.)unk_[0-9A-F]{2,4}\b")),
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
}
INFORMATION = {
    "functions": "functions",
    "lines": "lines of C",
    "casts": "casts",
    "partial_writes": "partial writes of a variable ((x & ~0xFFFFu) | ...)",
    "board_registers": "reads and writes of a device of the board, through the bus (io_read8...)",
    "clean_functions": "functions with nothing of the above in them (the partial writes too), per thousand",
    "clean_lines": "lines of code in the functions with nothing of the above, per thousand",
}


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
            counts["board_registers"] += len(re.findall(r"\bio_(?:read|write)\d+\(", line))
    return counts


PARTIAL_WRITE = re.compile(r"& ~0xF+u\) \|")


def clean_measure(paths, extra):
    """(functions with no leftover, functions, lines of code with none, lines of code in functions): a
    leftover is any kind of KINDS on the line, a register the C keeps as a global, a table over an address
    ('extra'), a partial write, a bool control variable, a result written back before an early return; a
    parameter named after a register leaves its function unclean."""
    patterns = [pattern for _, pattern in KINDS.values() if pattern is not None] + list(extra) + [PARTIAL_WRITE]
    clean_functions = functions = clean_lines = lines = 0
    for path in paths:
        text = path.read_text(encoding="ascii", errors="replace").splitlines()
        z80 = "sound" in path.parts
        inside, dirty = False, False
        for index, line in enumerate(text):
            if line.startswith(("/*", "   ", " *")):
                continue
            found = FUNCTION.match(line)
            if found and index + 1 < len(text) and text[index + 1] == "{":
                inside = True
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
            leftover = any(pattern.search(line) for pattern in patterns) or line.startswith("\tbool ")
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
    measure = clean_measure(sources, [pattern for pattern in (table_use, register_use) if pattern is not None])
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
