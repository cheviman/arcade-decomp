# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        decompile.py
Description: decompiles a 68000 program into readable C, one C function per original function
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Usage: python tools/decompile.py [rom folder or .zip] [--check] [--checks] [--trace] [--out DIR] [--symbols FILE]
                                 [--variables FILE]
       (the core's decompile.py runs this when the game's profile says DECOMPILER = "m68000.decompile")
  --check     build every function's IR and report what does not lift, without writing anything
  --checks    a jump through a table with an index the table does not have stops the program (invalid_jump)
  --trace     every function and block reports itself when it runs (game_trace, runtime/src/game_program.c: the
              last ones are shown when the program stops)
  --out       where to write the C (src/ by default)
  --symbols   the names to use instead of symbols.txt
  --variables also list every function's local variables and the instructions that write them

The program goes through the decompiler the V60 and Z80 programs go through (decompiler/), with the 68000's
profile (decompiler/cpu.py): m68000/function.py splits it into functions from the static analysis (symbols.txt,
no traces) and lifts them, the passes work out what each function takes and hands back, and m68000/emit.py writes
them. It writes src/: game.h (prototypes, file by file, and the addresses of the code the program keeps as
values), ram.h (the variables, the tables, the board's registers), the functions' files (symbols.txt's file lines
lay them out; game_NN.c without them) and table.c (the code the board runs by address: the vectors' handlers,
the reset; the registry of the tables the game loads by name). Beside the build, in build/decompile/: index.txt
(where each function is, its callers and callees).

The functions the board puts its own in place of (runtime/replaced.txt, a name a line: the boot's ROM test, which
sums chips the game does not have) are written as BOARD_ORIGINAL(name), and declared both ways in game.h.
"""
import re
import sys
import time
from collections import Counter
from pathlib import Path

import game_profile  # noqa: E402  (the game's: its root, its ROM set, its settings)
import disassemble  # noqa: E402
from decompile import call_results, fewer_controls, fewer_write_backs, grouped_prototypes  # noqa: E402
from decompile import replaced_functions, source_files, twin, write_sources, write_variables  # noqa: E402
from decompile import write_backs  # noqa: E402
from decompiler import cleanup  # noqa: E402
from decompiler import conditions  # noqa: E402
from decompiler import cpu  # noqa: E402
from decompiler import dataflow  # noqa: E402
from decompiler import emit as decompiler_emit  # noqa: E402
from decompiler import expressions  # noqa: E402
from decompiler import knownbits  # noqa: E402
from decompiler import pointers  # noqa: E402
from decompiler import preserved  # noqa: E402
from decompiler import signatures  # noqa: E402
from decompiler import stack  # noqa: E402
from decompiler import structure  # noqa: E402
from decompiler.gotos import GotoError  # noqa: E402
from v60 import ir  # noqa: E402

from . import analysis  # noqa: E402
from . import emit  # noqa: E402
from . import function as functions  # noqa: E402
from . import lift  # noqa: E402

ROOT = game_profile.ROOT
SYMBOLS = ROOT / "symbols.txt"
REPORTS = ROOT / "build" / "decompile"
CREATED = getattr(game_profile, "DECOMPILED", "29-10-2026")    # when src/ was first written
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
REGISTERS = {name: index for index, name in cpu.M68000.register_names.items()}    # param and local lines
INTERRUPT_VECTORS = range(25, 32)    # the autovectors: the board's interrupts
# names the runtime the decompiled code includes already uses (game/support.h, m68000/ops.h)
TAKEN = {"frame_wait", "take_interrupts", "write_status", "return_from_exception", "stop", "reset_devices",
         "game_code", "game_code_count", "game_entry", "game_tables", "game_table_count", "invalid_jump", "main",
         "mem_read8", "mem_read16", "mem_read32", "mem_write8", "mem_write16", "mem_write32", "ram", "data_at"}


def configure():
    """The 68000's profile, and the program's own: no registers it keeps for the whole program, no handler slots;
    a read of a device that does something when read (the profile's EFFECT_READ_AREAS) stays even where its value
    goes unused"""
    cpu.use(cpu.M68000)
    areas = list(getattr(game_profile, "EFFECT_READ_AREAS", ()))
    cleanup.EFFECT_READS = lambda address: any(start <= (address & 0xFFFFFF) < end for start, end in areas)
    signatures.CONTEXT = ()
    signatures.SCRATCH_CONTEXT = {}
    signatures.CONSTANT = {}
    signatures.INITIAL = {}
    dataflow.HANDLER_SLOTS = ()
    dataflow.WORKLIST = True
    knownbits.SIGN_EXTENSIONS = True
    decompiler_emit.WIDENED_INDEXES = True


def load_program(source):
    image = game_profile.rom.build_program(game_profile.rom.load_set(source))
    return functions.Program(image, analysis.load_symbols(SYMBOLS))


def build_functions(program):
    builder = functions.Builder(program)
    built = {}
    failures = []
    for entry in sorted(program.entries):
        try:
            built[entry] = builder.build(entry)
        except lift.Unsupported as problem:
            failures.append((entry, str(problem)))
    return built, failures


def interrupt_handlers(program):
    return sorted({program.handlers[vector - 1] for vector in INTERRUPT_VECTORS})


def open_entries(program, built):
    """The functions nothing reaches: no call, tail call or switch leads to them, and no vector"""
    reached = set(program.handlers)
    for function in built.values():
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                if isinstance(statement, ir.Call):
                    reached |= {statement.target} if statement.target is not None else set(statement.choices)
                elif isinstance(statement, ir.TailCall):
                    reached.add(statement.target)
    return {entry for entry in built if entry not in reached}


def declared_results(program, open_entries):
    entries = {name: entry for entry, name in program.names.items()}
    found = {}
    for function_name, register in disassemble.load_names(SYMBOLS, REGISTERS).results:
        entry = entries.get(function_name)
        if entry is None:
            raise SystemExit(f"{SYMBOLS.name}: a result of {function_name}, which is no function")
        if entry not in open_entries:
            raise SystemExit(f"{SYMBOLS.name}: a result of {function_name}, whose callers say what it hands back")
        found.setdefault(entry, set()).add(register)
    return found


def analyse(program, built):
    """Stack slots, preserved registers and the data flow summaries, in place"""
    for function in built.values():
        stack.analyse(function)
    stack.link_caller_exits(built)
    flow = dataflow.Program(built, program.analysis, interrupt_handlers(program), signatures.CONTEXT)
    signatures.OPEN = open_entries(program, built)
    flow.declared = declared_results(program, signatures.OPEN)
    flow.solve()
    while sum(len(preserved.analyse(function, flow.call_writes, context=signatures.CONTEXT))
              for function in built.values()):
        flow.solve()
    changed = sum(conditions.recover(function, flow) for function in built.values())
    if changed:
        flow.reset()
        flow.solve()
    flow.conditions_recovered = changed
    return flow


def check(program):
    built, failures = build_functions(program)
    for entry, function in built.items():
        try:
            stack.analyse(function)
        except stack.StackError as problem:
            failures.append((entry, str(problem)))
            continue
        left = stack.leftovers(function)
        if left:
            failures.append((entry, f"{len(left)} statements still use SP, the first: {left[0]}"))
    ends = Counter(type(block.end).__name__ for function in built.values() for block in function.blocks.values())
    print(f"functions:  {len(built)}")
    print(f"blocks:     {sum(ends.values())}")
    print("block ends: " + ", ".join(f"{kind} {count}" for kind, count in ends.most_common()))
    print(f"problems:   {len(failures)}")
    for entry, problem in failures[:40]:
        print(f"  {entry:06X}  {problem}")
    return 1 if failures else 0


def file_header(name, description):
    """The header every file of the project starts with, for a file this program writes."""
    return ("/**\n"
            f" *    Name:        {name}\n"
            f" *    Description: {description}\n"
            " *    Version:     0.1.0\n"
            f" *    Created:     {CREATED}\n"
            f" *    Modified:    {time.strftime('%d-%m-%Y')}\n"
            " *    Author:      Sebastian Toyos\n"
            " *\n"
            f" *    {game_profile.GAME}: the game's {game_profile.CPU} program decompiled into C by\n"
            " *    tools/decompile.py, with the names of symbols.txt.\n"
            " */\n")


def game_files(entries):
    return source_files(SYMBOLS, entries, lambda entry: entry, "game_{number:02}",
                        "the game's functions, part {number:02} (in the order of their 68000 addresses)")


def code_values(program):
    """Code addresses the program holds as values (lea, pea, movea.l #, move.l # of a function's address, and
    what the analysis saw stored in the objects' fields): NAME_ADDRESS in the C"""
    found = set()
    for instruction in program.analysis.instructions.values():
        if instruction.mnemonic not in ("lea", "pea", "movea", "move") or not instruction.operands:
            continue
        source = instruction.operands[0]
        if source.mode in ("abs_short", "abs_long", "pc_disp") and instruction.mnemonic in ("lea", "pea"):
            value = source.value & 0xFFFFFF
        elif source.mode == "immediate" and instruction.size == "l":
            value = source.value & 0xFFFFFF
        else:
            continue
        if value in program.entries and value >= analysis.CODE_START:
            found.add(value)
    for values in program.analysis.object_fields.values():
        found |= {value for kind, value in values if kind == analysis.CONSTANT and value in program.entries}
    return found


def write_index(out_dir, program, built, flow, names, texts, entries, files):
    callers = {entry: set() for entry in built}
    callees = {entry: set() for entry in built}
    for entry, function in built.items():
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                targets = []
                if isinstance(statement, ir.Call):
                    targets = [statement.target] if statement.target is not None else list(statement.choices)
                elif isinstance(statement, ir.TailCall):
                    targets = [statement.target]
                for target in targets:
                    if target in built:
                        callees[entry].add(target)
                        callers[target].add(entry)
    executed = set()
    traces = sorted((ROOT / "build" / "scenarios").glob("*/executed.txt"))
    if traces:
        found, _ = disassemble.load_trace(traces, fold_mirrors=False)
        executed = {address & 0xFFFFFF for address in found}
    location = {}
    for path, _, members in files:
        for entry in members:
            location[entry] = f"{path}.c"
    lines = ["# function  name  instructions  ran-in-a-scenario  file",
             "#   calls: ...   called by: ..."]
    for entry in entries:
        body = program.bodies[entry]
        ran = sum(1 for address in body if address in executed)
        lines.append(f"{entry:06X}  {names[entry]}  {len(body)}  {ran}/{len(body)}  {location.get(entry, '')}")
        lines.append("    calls: " + " ".join(sorted(names[target] for target in callees[entry])))
        lines.append("    called by: " + " ".join(sorted(names[caller] for caller in callers[entry])))
    (out_dir / "index.txt").write_text("\n".join(lines) + "\n", encoding="ascii")


def table_registry(named, loaded):
    """The C of the registry of the tables the game loads (each table's pointer, name, address and the width
    of its elements, which the loader turns to the host's byte order)"""
    lines = [f"const void *{name}_data;" for _, name in loaded]
    lines += ["", "/* the tables the game loads from its assets: their names, addresses, pointers and the bytes of",
              "   their elements (the loader puts each element in the host's byte order) */",
              "const game_table game_tables[] =", "{"]
    for address, name in loaded:
        item = named.tables.get(address)
        size = item.bits // 8 if item is not None else 1
        lines.append(f"\t{{ \"{name}\", 0x{address:06X}u, &{name}_data, {size} }},")
    lines += ["};", "", f"const size_t game_table_count = {len(loaded)};"]
    return lines


def write_c(program, out_dir, variables_path=None, trace=False):
    built, failures = build_functions(program)
    if failures:
        raise SystemExit(f"{len(failures)} functions do not lift, the first: {failures[0][0]:06X} {failures[0][1]}")
    flow = analyse(program, built)
    found = signatures.compute(flow)
    names = dict(program.names)
    for entry, function_name in names.items():
        if not IDENTIFIER.match(function_name) or function_name in TAKEN:
            raise SystemExit(f"{entry:06X}: '{function_name}' cannot be a C function name")
    if len(set(names.values())) != len(names):
        raise SystemExit("two functions share a name")

    before = sum(len(block.statements) for function in built.values() for block in function.blocks.values())
    known = disassemble.load_names(SYMBOLS, REGISTERS)
    # a register symbols.txt types where an instruction writes it stays a variable where its one use is an
    # access's address, so that the access reads as the structure's field
    expressions.KEEP_SITES = {(spot, int(value[1:])) for (_, spot, value) in known.local_structs
                              if spot != "entry" and value.startswith("r")}
    arguments = {}
    for entry, signature in found.items():
        registers = {value[1] for value in signature.params - signature.results if value[0] == "r"}
        returned = next(iter(signature.results)) if len(signature.results) == 1 else None
        if returned is not None and returned[0] == "r" and returned in signature.params:
            registers.add(returned[1])
        arguments[entry] = registers
    whole = set()
    live_after = {}
    live_at_start = {}
    for entry, function in built.items():
        expressions.drop_repacks(function, flow)
        after, start = cleanup.eliminate(flow, function, found[entry].results, signatures.CONTEXT, arguments)
        expressions.tidy(function, flow, after, whole)
        results_of_calls = call_results(function, found, flow)
        if expressions.propagate_constants(function, results_of_calls, signatures.CONTEXT) \
                | expressions.fold_kept_constants(function, results_of_calls, signatures.CONTEXT):
            after, start = cleanup.eliminate(flow, function, found[entry].results, signatures.CONTEXT, arguments)
            expressions.tidy(function, flow, after, whole)
        if expressions.inline_registers(function, flow, after, whole):
            expressions.simplify_function(function)
        if expressions.switch_values(function, flow):
            after, start = cleanup.eliminate(flow, function, found[entry].results, signatures.CONTEXT, arguments)
            expressions.tidy(function, flow, after, whole)
        live_after[entry] = after
        live_at_start[entry] = start
    results_of_calls = {entry: call_results(function, found, flow) for entry, function in built.items()}
    closed = set(signatures.OPEN) | set(program.handlers)
    # (a result's bits the function does not write are the caller's: only the written ones are known)
    parts = dict(dataflow.PARTS)
    written = {entry: {} for entry in built}
    for entry, summary in flow.summaries.items():
        for variable in summary.writes:
            if variable[0] == "r":
                written[entry][variable[1]] = written[entry].get(variable[1], 0) | parts[variable[2]]
    passed, handed = knownbits.across(built, found, signatures.CONTEXT, results_of_calls, closed, written=written)
    for entry, function in built.items():
        knownbits.simplify(function, found[entry], signatures.CONTEXT, results_of_calls[entry], passed.get(entry),
                           handed)
        expressions.simplify_function(function)

    check_clashes(known)
    emit.use_memory_map(known)
    # the names a "STRUCT@BASE" may give its base by: the program's data and its tables
    pointers.BASES = {symbol.name: address for address, symbol in program.symbols.items() if symbol.kind == "data"}
    pointers.BASES.update({name: address for address, (name, _) in known.tables.items()})
    entries_by_name = {name: entry for entry, name in names.items()}
    variable_addresses = {name: address for address, (name, _) in known.variables.items()}

    def hinted_kind(struct):
        # &NAME: a pointer to that variable; none: an address no structure's, whatever starts there (pointers.OFFSET)
        if struct == "none":
            return pointers.OFFSET
        return ("global", variable_addresses[struct[1:]]) if struct.startswith("&") else pointers.link_kind(struct)
    hints = {}
    for (function_name, register), struct in known.param_structs.items():
        if function_name in entries_by_name:
            hints.setdefault(entries_by_name[function_name], {})[register] = pointers.link_kind(struct)
    site_hints = {}
    for (function_name, spot, value), struct in known.local_structs.items():
        if not value.startswith("r"):
            continue
        if spot == "entry" and function_name in entries_by_name:
            hints.setdefault(entries_by_name[function_name], {})[int(value[1:])] = hinted_kind(struct)
        elif spot != "entry":
            site_hints[(spot, int(value[1:]))] = hinted_kind(struct)
    families, global_families, scoped_targets = object_families(known, names)
    typing = pointers.Program(built, flow, found, results_of_calls, signatures.CONTEXT, known.targets, hints,
                              site_hints, scoped_targets, families, global_families)
    fields, accesses = typing.run()
    joined_writes = join_long_writes(built, fields, accesses, live_after)
    through = [(hit[1], hit[2]) for found_fields in fields.values() for hit in found_fields.values()
               if hit[0] == "global"]
    data_names = {address: symbol.name for address, symbol in program.symbols.items() if symbol.kind == "data"}
    pieces = piece_names()
    data_names.update(pieces)
    emit.CODE_BYTES = set(program.analysis.owner)
    emit.PIECE_STARTS = sorted(pieces)
    from decompiler import memory
    named = emit.Memory(known, memory.accesses_of(built.values()) + through, data_names)

    always = {entry: set(summary.always) for entry, summary in flow.summaries.items()}
    emitter = emit.Emitter(built, found, names, signatures.CONTEXT, set(), named, always, program.mem.r32, trace)
    emitter.layouts = pointers.Layouts(accesses, known)
    emitter.reroll_statements = True
    emitter.writes = {entry: set(summary.writes) for entry, summary in flow.summaries.items()}
    emitter.param_pointers = {entry: set(kinds) for entry, kinds in hints.items()}
    emitter.flow = flow
    emitter.result_bits = dict(cleanup.RESULT_BITS)
    emitter.code_addresses = {address: names[address] + "_ADDRESS" for address in code_values(program)}
    emitter.data_labels = {address: name for address, name in data_names.items() if address not in named.tables}
    emitter.code_labels = {address: symbol.name for address, symbol in program.symbols.items() if symbol.kind == "code"}
    emitter.comments = dict(known.comments)
    emitter.constants = dict(known.constants)
    emitter.replaced = replaced_functions([(ROOT / "runtime" / "replaced.txt", "BOARD_ORIGINAL")])
    unknown = sorted({function_name for function_name, _ in known.params if function_name not in entries_by_name}
                     | {function_name for function_name, _, _ in known.locals if function_name not in entries_by_name})
    if unknown:
        print(f"{SYMBOLS.name} names parameters or locals of functions that are not in the program: "
              + ", ".join(unknown[:5]))
    emitter.param_names = {(entries_by_name[function_name], register): name
                           for (function_name, register), name in known.params.items()
                           if function_name in entries_by_name}
    emitter.result_names = {(entries_by_name[function_name], register): name
                            for (function_name, register), name in known.results.items()
                            if function_name in entries_by_name}
    emitter.local_sites = {(spot, value): name for (function_name, spot, value), name in known.locals.items()
                           if spot != "entry"}
    emitter.local_names = {(entries_by_name[function_name], spot, value): name
                           for (function_name, spot, value), name in known.locals.items()
                           if spot == "entry" and function_name in entries_by_name}

    joined = {}
    for entry, function in built.items():
        notes = (live_after[entry], fields[entry])
        if emitter.by_pointer(entry):
            joined[entry] = []
            for tail_calls, meet in ((False, False), (True, False), (False, True), (True, True)):
                way = twin(function, notes)
                structure.join_exits(way, notes, tail_calls)
                way.meet_at_exit = meet
                structure.split_loop_entries(way, notes)
                joined[entry].append(way)
            structure.split_small_exits(function, notes)
        structure.split_loop_entries(function, notes)
    writers = {entry: emit.Writer(emitter, function, live_after[entry], fields[entry], live_at_start[entry])
               for entry, function in built.items()}
    texts = {}
    problems = []
    for entry, writer in writers.items():
        try:
            texts[entry] = writer.write()
        except (decompiler_emit.EmitError, GotoError) as problem:
            problems.append((entry, str(problem)))
    emitter.shared_results = decompiler_emit.shared_results(texts.values())
    for entry in texts:
        if emitter.shared_results.get(emitter.names.get(entry)):
            texts[entry] = writers[entry].write()
    for entry in joined:
        for way in joined[entry] if entry in texts else ():
            other = emit.Writer(emitter, way, live_after[entry], fields[entry], live_at_start[entry])
            try:
                text = other.write()
            except (decompiler_emit.EmitError, GotoError):
                continue
            if fewer_write_backs(text, texts[entry]):
                writers[entry], texts[entry] = other, text
    split = fewer_controls(
        texts, writers,
        lambda entry, way: emit.Writer(emitter, way, live_after[entry], fields[entry], live_at_start[entry]),
        lambda entry: (live_after[entry], fields[entry]))
    emitter.shared_results = decompiler_emit.shared_results(texts.values())
    for entry in set(joined) | split:
        if entry in texts and emitter.shared_results.get(emitter.names.get(entry)):
            texts[entry] = writers[entry].write()
    if problems:
        for entry, problem in problems[:30]:
            print(f"  {entry:06X}  {problem}")
        raise SystemExit(f"{len(problems)} functions could not be written")
    if variables_path is not None:
        write_variables(variables_path, writers, names, texts)
    texts = {entry: named.indexed_uses(text) for entry, text in texts.items()}

    out_dir.mkdir(parents=True, exist_ok=True)
    entries = sorted(built)
    files = game_files(entries)
    if any(Path(path).parts[0] == "sound" for path, _, _ in files):
        raise SystemExit(f"{SYMBOLS.name}: a file of the program in sound/, which is the sound program's")
    prototypes = grouped_prototypes(files, lambda entry: emitter.prototype(entry) + (
        ";\n" + emitter.definition(entry) if names[entry] in emitter.replaced else ""))
    addresses = [f"#define {name} 0x{address:06X}u" for address, name in sorted(emitter.code_addresses.items())]
    guard = game_profile.GUARD
    (out_dir / "game.h").write_text(
        file_header("game.h", "prototypes of the game's functions and the addresses of the code it holds as values")
        + f"#ifndef {guard}_GAME_H\n#define {guard}_GAME_H\n\n"
        + "#include \"game/support.h\"\n#include \"ram.h\"\n\n"
        "/* where the code the program holds as values is (handlers it stores and compares), as the game's own\n"
        "   addresses */\n" + "\n".join(addresses) + "\n\n"
        + "\n".join(prototypes) + "\n\n#endif\n", encoding="ascii")
    labels = [f"#define {name}_ADDRESS 0x{address:06X}u" for address, name in sorted(emitter.data_labels.items())]
    numbers = [f"#define {name} 0x{value:X}u" for value, name in sorted({(value, name) for (_, value), name
                                                                          in emitter.constants.items()})]
    (out_dir / "ram.h").write_text(
        file_header("ram.h", "the game's variables, tables, board registers and structures")
        + f"#ifndef {guard}_RAM_H\n#define {guard}_RAM_H\n\n"
        + "\n".join(named.declarations()) + "\n\n/* data in the program symbols.txt and the assets label */\n"
        + "\n".join(labels) + "\n\n/* numbers the code uses, by name (symbols.txt constant lines) */\n"
        + "\n".join(numbers) + "\n\n/* the game's structures in work RAM */\n"
        + "\n".join(emitter.layouts.declarations()) + "\n\n#endif\n", encoding="ascii")
    write_sources(out_dir, files, texts, "game.h", header=file_header, keep=("sound",))

    reset = program.handlers[0]
    rows = [f"\t{{ 0x{address:06X}u, {names[address]} }},"
            for address in sorted(set(program.handlers)) if address in names]
    if emitter.wrapper(reset) is not None:
        raise SystemExit("the reset code takes parameters")
    for handler in set(program.handlers):
        if emitter.wrapper(handler) is not None:
            raise SystemExit(f"the handler at {handler:06X} takes parameters")
    loaded = named.loaded_tables()
    (out_dir / "table.c").write_text(
        file_header("table.c", "the code the board runs by address (the vectors' handlers), the reset entry, and "
                    "the tables the game loads by name")
        + '#include "game.h"\n\n'
        "/* the handlers of the vectors (the reset, the exceptions, the interrupts), by address */\n"
        "const game_code_entry game_code[] =\n{\n" + "\n".join(rows) + "\n};\n\n"
        f"const size_t game_code_count = {len(rows)};\n\n"
        f"/* the code at the reset vector */\nvoid game_entry(void)\n{{\n\t{names[reset]}();\n}}\n\n"
        + "\n".join(table_registry(named, loaded)) + "\n", encoding="ascii")

    reports = REPORTS if out_dir.resolve() == (ROOT / "src").resolve() else out_dir
    reports.mkdir(parents=True, exist_ok=True)
    write_index(reports, program, built, flow, names, texts, entries, files)
    # the program's addresses the C uses as numbers, which nothing names yet (the meter counts them)
    kept = {names.get(entry, ""): text for entry, text in texts.items()}
    raw = []
    for address, users in sorted(emitter.raw_addresses.items()):
        if not analysis.CODE_START <= address < emit.PROGRAM_AREA[1]:
            continue    # (the work RAM's and the board's are no data of the program)
        found_in = sorted(user for user in users if re.search(rf"\b0x0*{address:X}u", kept.get(user, "")))
        if found_in:
            raw.append(f"{address:06X}  {' '.join(found_in)}")
    (reports / "unnamed_addresses.txt").write_text(
        "# addresses of the program the C uses as values that symbols.txt and the assets do not name, and the\n"
        "# functions that use them\n" + "\n".join(raw) + "\n", encoding="ascii")
    print(f"unnamed:    {len(raw)} addresses of the program used as values")
    left = sum(len(block.statements) for function in built.values() for block in function.blocks.values())
    gotos = sum(text.count("goto ") for text in texts.values())
    print(f"functions:  {len(entries)} in {out_dir}")
    print(f"statements: {left} ({before - left} dead ones removed)")
    print(f"branches:   {flow.conditions_recovered} now compare values instead of flags")
    print(f"gotos:      {gotos}, in {sum(1 for text in texts.values() if 'goto ' in text)} functions")
    print(f"tables:     {len(loaded)} the game loads by name")
    print(f"longs:      {joined_writes} written to the work RAM a half at a time, as one")
    return 0


# the statements join_long_writes() took out: kept, so that no new statement takes the id the notes knew them by
JOINED = []


def split_address(address):
    """(base, constant) of an address: a constant, or something plus a constant, or something alone"""
    if isinstance(address, ir.Const):
        return None, address.value & 0xFFFFFF
    if isinstance(address, ir.Op) and address.name == "add" and isinstance(address.args[1], ir.Const):
        return address.args[0], address.args[1].value & 0xFFFFFFFF
    return address, 0


def long_value(low, high):
    """The 32-bit value whose low half 'low' and high half 'high' are, as the lift writes a long a half at a time
    (trunc(v, 16), then trunc(v >> 16, 16); fits where the bits above are known 0), or None"""
    if isinstance(low, ir.Const) and isinstance(high, ir.Const):
        return ir.Const((high.value & 0xFFFF) << 16 | (low.value & 0xFFFF), 32)
    if not (isinstance(low, ir.Op) and low.name in ("trunc", "fits") and low.args[0].bits == 32):
        return None
    whole = low.args[0]
    if isinstance(high, ir.Op) and high.name in ("trunc", "fits"):
        shifted = high.args[0]
        if isinstance(shifted, ir.Op) and shifted.name == "shr" and shifted.args[0] == whole                 and isinstance(shifted.args[1], ir.Const) and shifted.args[1].value == 16:
            return whole
    return None


def temp_uses(function, temp):
    """How many times the function's statements read the temporary 'temp'"""
    found = 0
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            for expression in ir.statement_expressions(statement):
                found += sum(1 for node in ir.walk(expression) if node == temp)
    return found


def in_work_ram(hit, address):
    """True when a store goes to the work RAM: a variable there (by its address, or through a pointer to it) or a
    field of one of the work RAM's structures"""
    if hit is None:
        start, constant = split_address(address)
        return start is None and emit.WORK_RAM_AREA[0] <= constant < emit.WORK_RAM_AREA[1]
    if hit[0] == "global":
        start, constant = split_address(hit[1])
        return start is None and emit.WORK_RAM_AREA[0] <= constant < emit.WORK_RAM_AREA[1]
    return hit[0] in pointers.STRUCTS and hit[0] not in pointers.ROM_STRUCTS and hit[0] not in pointers.BOARD_STRUCTS


def join_long_writes(built, fields, accesses, live_after):
    """The longs the 68000 writes its low word first (the read-modify-write instructions, a MOVE through -(An)): the
    lift makes them two 16-bit stores, the low half at the address plus 2, then the high half at the address
    (m68000/lift.py), as MAME's core does on the bus. In the work RAM no device sees the order of the two, and the
    two halves are one value of the program's: one 32-bit store (x += speed, not its two halves one by one). The
    notes of the typing ('fields', 'accesses') and the live registers follow. How many were joined"""
    joined = 0
    for entry, function in built.items():
        found = fields.get(entry, {})
        after = live_after.get(entry, {})
        for block in function.blocks.values():
            statements = block.statements
            index = 0
            while index + 1 < len(statements):
                low, high = statements[index], statements[index + 1]
                if not (isinstance(low, ir.Store) and isinstance(high, ir.Store) and low.bits == high.bits == 16):
                    index += 1
                    continue
                low_start, low_offset = split_address(low.address)
                high_start, high_offset = split_address(high.address)
                value = long_value(low.value, high.value)
                low_hit, high_hit = found.get(id(low)), found.get(id(high))
                if value is None or low_start != high_start or (low_offset - high_offset) & 0xFFFFFFFF != 2                         or not in_work_ram(low_hit, low.address) or not in_work_ram(high_hit, high.address):
                    index += 1
                    continue
                store = ir.Store(high.address, value, 32)
                if hasattr(high, "origin"):
                    store.origin = high.origin
                statements[index:index + 2] = [store]
                JOINED.extend((low, high))
                bound = statements[index - 1] if index else None
                if isinstance(value, ir.Temp) and isinstance(bound, ir.SetTemp) and bound.temp == value                         and temp_uses(function, value) == 1:
                    # (the value the lift bound for the two halves, worked out right before them: in the store)
                    store.value = bound.value
                    JOINED.append(statements.pop(index - 1))
                    index -= 1
                if high_hit is not None:
                    found[id(store)] = high_hit[:-1] + (32,) if high_hit[0] == "global" else high_hit
                    if high_hit[0] != "global" and low_hit is not None:
                        for gone in ((low_hit[0], low_hit[1], 16), (high_hit[0], high_hit[1], 16)):
                            if gone in accesses:
                                accesses.remove(gone)
                        accesses.append((high_hit[0], high_hit[1], 32))
                if id(high) in after:
                    after[id(store)] = after[id(high)]
                joined += 1
                index += 1
    return joined


def check_clashes(known):
    """Stops when a field or structure of symbols.txt has the name of a variable, table or register, which are
    macros of ram.h: the preprocessor would take the field for it"""
    macros = {name for name, _ in list(known.variables.values()) + list(known.tables.values())
              + list(known.hardware.values()) + list(known.record_tables.values())}
    macros |= {name for (struct, _, _), (name, _) in known.overlays.items() if struct == "global"}
    macros |= {name for (struct, _), found in known.scoped.items() if struct == "global" for name, _ in found.values()}
    fields = {name for name, _ in known.fields.values()} | {name for (struct, _), found in known.scoped.items()
                                                             if struct != "global" for name, _ in found.values()}
    clashes = sorted((fields | set(known.structs) | {struct.upper() for struct in known.structs}) & macros)
    if clashes:
        raise SystemExit(f"{SYMBOLS.name}: fields or structures named like a variable, table or register: "
                         + ", ".join(clashes))


def object_families(known, names):
    """({entry: family}, {entry: family}, scoped targets): the object a function of a family runs on is that
    family's, the longest family (scoped object lines) whose name starts the function's; the same for the
    families' own names of the work RAM (scoped global lines). A family's own name for a variable that says
    nothing of what it points at reads as a number in the family's code, whatever the variable's own line says"""
    object_names = sorted({family for (struct, family) in known.scoped if struct == "object"}, key=len, reverse=True)
    global_names = sorted({family for (struct, family) in known.scoped if struct == "global"}, key=len, reverse=True)
    families, global_families = {}, {}
    for entry, name in names.items():
        family = next((family for family in object_names if name == family or name.startswith(family + "_")), None)
        if family is not None:
            families[entry] = family
        family = next((family for family in global_names if name == family or name.startswith(family + "_")), None)
        if family is not None:
            global_families[entry] = family
    scoped_targets = dict(known.scoped_targets)
    for (struct, family), found_names in known.scoped.items():
        if struct == "global":
            for address, (_, kind) in found_names.items():
                scoped_targets.setdefault(("global", family, address), (None, int(kind[1:])))
    return families, global_families, scoped_targets


def piece_names():
    """{address: name} of the pieces of the program's data the assets hold (assets/data/layout.txt, which
    tools/extract_assets.py writes): the names the tables the C reads take"""
    layout = ROOT / "assets" / "data" / "layout.txt"
    found = {}
    if not layout.exists():
        return found
    for line in layout.read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("#"):
            found[int(parts[1], 16)] = parts[0]
    return found


def main(arguments):
    global SYMBOLS
    arguments = list(arguments)
    decompiler_emit.CHECKED_JUMPS = "--checks" in arguments
    out_dir = ROOT / "src"
    variables_path = None
    for option in ("--out", "--symbols", "--variables"):
        if option in arguments:
            position = arguments.index(option)
            value = Path(arguments[position + 1])
            del arguments[position:position + 2]
            if option == "--out":
                out_dir = value
            elif option == "--symbols":
                SYMBOLS = value
            else:
                variables_path = value
    checking = "--check" in arguments
    tracing = "--trace" in arguments
    arguments = [argument for argument in arguments if argument not in ("--check", "--checks", "--trace")]
    configure()
    started = time.time()
    program = load_program(Path(arguments[0]) if arguments else ROOT / "rom")
    print(f"analysis:   {len(program.entries)} functions, {time.time() - started:.0f} s")
    if checking:
        return check(program)
    return write_c(program, out_dir, variables_path, tracing)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
