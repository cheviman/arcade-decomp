"""
Name:        decompile_sound.py
Description: decompiles the Z80 sound program into readable C, one C function per original function
Version:     0.1.0
Created:     13-10-2026
Modified:    01-11-2026
Author:      Sebastian Toyos

Usage: python tools/decompile_sound.py [--check] [--checks] [--out DIR] [--timing FILE]
  --check     build every function's IR and report what does not lift and what the analysis finds,
              without writing anything
  --checks    a jump through a table with an index the table does not have stops the program
              (sound_invalid_jump), as decompile.py --checks does
  --out       where to write the C (src/sound/ by default)
  --timing    where to write the blocks of the timing model for system32/reference/s32ref (--sound-timing;
              build/sound/timing.txt by default)
  --symbols FILE  the names to use instead of sound_symbols.txt
  --variables FILE  lists every local variable with the instructions that write it, which is what
              sound_symbols.txt's "local" lines name them by (decompile.py --variables)

The sound program (epr-15550, read from the ROM set in rom/ by tools/game_profile.rom.py) goes
through the decompiler the V60 program goes through (tools/decompiler/), with the Z80's profile
(decompiler/cpu.py): tools/z80/analysis.py splits it into functions with sound_symbols.txt,
tools/z80/function.py lifts them to IR with the sound board's timing model, and the passes work out
what each function takes and hands back. It writes src/sound/: sound.h (prototypes, file by file) and the
functions' files (sound_symbols.txt's file lines lay them out; sound_NN.c without them).

Code the program never runs (routines nothing calls) is decompiled too, from its "func" lines: as on the
V60's side (decompile.py), a function nothing calls takes every register it reads as a parameter, and
hands back what sound_symbols.txt's "result" lines say.

The functions the game's sound board puts its own in place of (runtime/sound_replaced.txt, a name a line, as
runtime/replaced.txt is for the main program: TMNT's ROM test) are written as BOARD_ORIGINAL(name), and declared
both ways in sound.h.
"""
import re
import sys
import time
from collections import Counter
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import game_profile  # noqa: E402  (the game's: its root, its ROM set, its settings)
ROOT = game_profile.ROOT
import disassemble  # noqa: E402
from decompile import call_results  # noqa: E402
from decompile import grouped_prototypes  # noqa: E402
from decompile import fewer_controls  # noqa: E402
from decompile import fewer_write_backs  # noqa: E402
from decompile import twin  # noqa: E402
from decompile import source_files  # noqa: E402
from decompile import write_sources  # noqa: E402
from decompile import write_variables  # noqa: E402
from decompile import replaced_functions  # noqa: E402
from decompile import name_params_by_use  # noqa: E402
from decompiler import cleanup  # noqa: E402
from decompiler.gotos import GotoError  # noqa: E402
from decompiler import cprint  # noqa: E402
from decompiler import memory  # noqa: E402
from decompiler import pointers  # noqa: E402
from decompiler import conditions  # noqa: E402
from decompiler import cpu  # noqa: E402
from decompiler import dataflow  # noqa: E402
from decompiler import expressions  # noqa: E402
from decompiler import knownbits  # noqa: E402
from decompiler import preserved  # noqa: E402
from decompiler import signatures  # noqa: E402
from decompiler import stack  # noqa: E402
from decompiler import structure  # noqa: E402
from decompiler import variables  # noqa: E402
from v60 import ir  # noqa: E402
from z80 import analysis  # noqa: E402
from z80 import emit  # noqa: E402
from z80 import function as functions  # noqa: E402
from z80 import lift  # noqa: E402
from z80 import pairs  # noqa: E402

SYMBOLS = ROOT / "sound_symbols.txt"
ROM_SET = ROOT / "rom"
REPORTS = ROOT / "build" / "sound"    # the listing and the index naming works from
INTERRUPT_HANDLER = 0x0038
# when the C was first written (the profile's SOUND_CREATED; System 32's by default); the files say when it was
# last written too
CREATED = getattr(game_profile, "SOUND_CREATED", "13-10-2026")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
REGISTERS = {name: index for index, name in cpu.Z80.register_names.items()}    # param and local lines


def code_values(program):
    """Addresses of the program's functions it loads into a register pair as values (ld hl, nn): the handlers it
    stores in its channels' records and compares (0, the reset's, is a zero)"""
    found = set()
    for body in program.bodies.values():
        for instruction in body.values():
            if instruction.mnemonic != "ld" or len(instruction.operands) != 2:
                continue
            to, source = instruction.operands
            if to.mode == "reg" and to.reg in ("bc", "de", "hl", "ix", "iy") and source.mode == "immediate" \
                    and source.value in program.entries and source.value:
                found.add(source.value)
    return found


def load_program():
    if not ROM_SET.exists():
        raise SystemExit(f"{ROM_SET}: no such ROM folder")
    return analysis.load_program(game_profile.rom.build_sound(game_profile.rom.load_set(ROM_SET)), SYMBOLS)


def configure():
    """The Z80 profile, and the sound program's own: no registers it keeps for the whole program. With the
    profile's SOUND_KEEP_HARDWARE_READS, a read of the board's registers (SOUND_HARDWARE) stays even where its
    value goes unused: reading one may do something (TMNT's K007232 starts a channel when its key is read)."""
    cpu.use(cpu.Z80)
    if getattr(game_profile, "SOUND_KEEP_HARDWARE_READS", False):
        areas = [(start, end) for start, end, _ in game_profile.SOUND_HARDWARE]
        cleanup.EFFECT_READS = lambda address: any(start <= (address & 0xFFFF) < end for start, end in areas)
    conditions.DIRECT_FLAGS = True
    # (the profile's SOUND_STACK_RESTARTS, off by default: a loop back to an LD SP takes any SP in, decompiler/stack.py)
    stack.RESTARTS = getattr(game_profile, "SOUND_STACK_RESTARTS", False)
    # (the profile's SOUND_CONTEXT, none by default: registers the whole program keeps, global variables in C by the
    # names of sound_symbols.txt's register lines; The Simpsons' self test leaves its results in the other bank's)
    signatures.CONTEXT = tuple(sorted(REGISTERS[name] for name in getattr(game_profile, "SOUND_CONTEXT", ())))
    signatures.SCRATCH_CONTEXT = {}
    signatures.CONSTANT = {}


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


def open_entries(program, built):
    """The functions nothing in the program reaches and no other function calls or jumps to: what they
    take has no caller to say it (signatures.OPEN). A function only code nothing reaches calls takes what
    those callers give it."""
    called = set()
    for function in built.values():
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                if isinstance(statement, (ir.Call, ir.TailCall)) and statement.target is not None:
                    called.add(statement.target)
    return {entry for entry in program.unreached if entry in built and entry not in called}


def declared_results(program, open_entries):
    """What the functions nothing calls hand back, from the "result" lines of sound_symbols.txt:
    {entry: {register numbers}}."""
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


def declared_unread(program):
    """The registers functions read before setting them only on paths the program never takes, from the "unread"
    lines of sound_symbols.txt: {entry: {register number}}."""
    entries = {name: entry for entry, name in program.names.items()}
    found = {}
    for function_name, register in disassemble.load_names(SYMBOLS, REGISTERS).unread:
        entry = entries.get(function_name)
        if entry is None:
            raise SystemExit(f"{SYMBOLS.name}: an unread register of {function_name}, which is no function")
        found.setdefault(entry, set()).add(register)
    return found


def analyse(program, built):
    """Stack slots, preserved registers and the data flow summaries, in place."""
    for function in built.values():
        stack.analyse(function)
    flow = dataflow.Program(built, program.analysis, [INTERRUPT_HANDLER], signatures.CONTEXT)
    signatures.OPEN = open_entries(program, built)
    flow.declared = declared_results(program, signatures.OPEN)
    # (sound_symbols.txt's unread lines, none by default: a register a function reads before setting it only on a
    # path the program never takes is no parameter, it starts at 0 there, as decompile.py's)
    signatures.UNREAD = declared_unread(program)
    for entry, registers in signatures.UNREAD.items():
        flow.zeroed[entry] = {("r", register) for register in registers}
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


def name(variable):
    if variable[0] == "r":
        return lift.NAMES.get(variable[1], f"r{variable[1]}")
    return variable[1]


def check(program):
    built, failures = build_functions(program)
    for entry, function in built.items():
        try:
            stack.analyse(function)
        except stack.StackError as problem:
            failures.append((entry, str(problem)))
        left = stack.leftovers(function)
        if left:
            failures.append((entry, f"{len(left)} statements still use SP, the first: {left[0]}"))
    ends = Counter(type(block.end).__name__ for function in built.values() for block in function.blocks.values())
    print(f"functions:  {len(built)}")
    print(f"blocks:     {sum(ends.values())}")
    print("block ends: " + ", ".join(f"{kind} {count}" for kind, count in ends.most_common()))
    print(f"problems:   {len(failures)}")
    for entry, problem in failures[:20]:
        print(f"  {entry:04X}  {problem}")
    if failures:
        return 1
    built, _ = build_functions(program)
    flow = analyse(program, built)
    found = signatures.compute(flow)
    kept = Counter(len(getattr(function, "preserved", ())) for function in built.values())
    print("preserved registers per function: " + ", ".join(f"{k}: {v}" for k, v in sorted(kept.items())))
    print(f"conditions recovered: {flow.conditions_recovered}")
    for entry in sorted(built):
        signature = found[entry]
        params = ", ".join(sorted(name(variable) for variable in signature.params))
        results = ", ".join(sorted(name(variable) for variable in signature.results))
        print(f"  {entry:04X} {program.names[entry]:<24} takes ({params})  hands back ({results})")
    handler = flow.summaries[INTERRUPT_HANDLER]
    changed = sorted(name(variable) for variable in handler.writes if variable[0] == "r")
    print("interrupt handler writes: " + (", ".join(changed) or "nothing"))
    return 0


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
            f" *    {game_profile.GAME}: the Z80 sound program decompiled into C by\n"
            " *    tools/decompile_sound.py, with the names of sound_symbols.txt.\n"
            " */\n")


def load_arrays(path):
    """The "array STRUCT FIRST COUNT" lines of sound_symbols.txt: {struct: (first address, count)}, the
    structures that stand one after another in the shared RAM (a pointer moved by the size of one points
    at the next)."""
    arrays = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        parts = line.split("#", 1)[0].split()
        if parts and parts[0] == "array":
            if len(parts) != 4 or not parts[3].isdigit():
                raise SystemExit(f"{Path(path).name}:{number}: expected 'array STRUCT FIRST COUNT'")
            arrays[parts[1]] = (int(parts[2], 16), int(parts[3]))
    return arrays


def sound_files(entries):
    """The files of src/sound/ for the sound program's functions (sound_symbols.txt's file lines)."""
    return source_files(SYMBOLS, entries, lambda entry: entry, "sound_{number:02}",
                        "the sound program's functions, part {number:02} (in the order of their Z80 addresses)")


def write_c(program, out_dir, variables_path=None):
    built, failures = build_functions(program)
    if failures:
        raise SystemExit(f"{len(failures)} functions do not lift, the first: {failures[0][0]:04X} {failures[0][1]}")
    flow = analyse(program, built)
    found = signatures.compute(flow)
    names = dict(program.names)
    for entry, function_name in names.items():
        if not IDENTIFIER.match(function_name):
            raise SystemExit(f"{entry:04X}: '{function_name}' cannot be a C function name")
    # a pair the function writes a byte at a time: its bytes as variables of their own
    for entry, function in built.items():
        pairs.split(function, flow, found[entry])

    before = sum(len(block.statements) for function in built.values() for block in function.blocks.values())
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
    # (the profile's SOUND_TABLE_REGISTERS, off by default: a load through a register the same block set to a table's
    # address plus an index reads that table, expressions.table_registers; the tables are those sound_symbols.txt
    # names, and the bases the code indexes from up to SOUND_NAMES_BEFORE_TABLES bytes before one)
    table_registers = getattr(game_profile, "SOUND_TABLE_REGISTERS", False)
    tables = set(disassemble.load_names(SYMBOLS, REGISTERS).tables) if table_registers else set()
    pairs.SUMS = table_registers    # (the two bytes of an entry read so are one word: z80/pairs.py)
    names_before = getattr(game_profile, "SOUND_NAMES_BEFORE_TABLES", 0)
    # (the profile's SOUND_CONSTANT_ADDRESSES, off by default: an access through a register that holds the same address
    # of the RAM on every way to it reads that address, expressions.constant_addresses: "ld hl,VARIABLE; dec (hl)" and
    # the reads of (hl) in the blocks after it no longer keep HL set for the C)
    constant_addresses = getattr(game_profile, "SOUND_CONSTANT_ADDRESSES", False)
    ram = getattr(game_profile, "SOUND_RAM_AREA", emit.SHARED_RAM)
    # (the profile's SOUND_CARRY_COMPARISONS, off by default: the borrow of "sbc hl,de" reads a < b + borrow,
    # expressions.py; and SOUND_JOIN_KEPT_PARTS: a byte written into a pair whose other byte is read afterwards is
    # the pair's value changed in part, decompiler/variables.py)
    expressions.CARRY_COMPARISONS = getattr(game_profile, "SOUND_CARRY_COMPARISONS", False)
    expressions.SHIFTED_OUT_PARTS = expressions.CARRY_COMPARISONS
    variables.JOIN_KEPT_PARTS = getattr(game_profile, "SOUND_JOIN_KEPT_PARTS", False)

    def is_variable(value):
        return ram[0] <= value < ram[1]

    def is_table(value):
        parts = memory.based(value)
        return parts is not None and any(parts[0] + distance in tables for distance in range(names_before + 1))
    for entry, function in built.items():
        expressions.drop_repacks(function, flow)
        after, start = cleanup.eliminate(flow, function, found[entry].results, signatures.CONTEXT, arguments)
        expressions.tidy(function, flow, after, whole)
        results_of_calls = call_results(function, found, flow)
        if expressions.propagate_constants(function, results_of_calls, signatures.CONTEXT) \
                | expressions.fold_kept_constants(function, results_of_calls, signatures.CONTEXT) \
                | (expressions.table_registers(function, flow, is_table) if table_registers else 0) \
                | (expressions.constant_addresses(function, results_of_calls, signatures.CONTEXT, is_variable)
                   if constant_addresses else 0):
            after, start = cleanup.eliminate(flow, function, found[entry].results, signatures.CONTEXT, arguments)
            expressions.tidy(function, flow, after, whole)
        if expressions.inline_registers(function, flow, after, whole):
            expressions.simplify_function(function)
        if expressions.switch_values(function, flow):
            after, start = cleanup.eliminate(flow, function, found[entry].results, signatures.CONTEXT, arguments)
            expressions.tidy(function, flow, after, whole)
        knownbits.simplify(function, found[entry], signatures.CONTEXT, results_of_calls)
        expressions.simplify_function(function)
        pairs.tidy_bytes(function, after)
        live_after[entry] = after
        live_at_start[entry] = start

    # the names: variables, tables, structures and ports of sound_symbols.txt, on the Z80's memory map
    known = disassemble.load_names(SYMBOLS, REGISTERS)
    arrays = load_arrays(SYMBOLS)
    # the variables, tables, registers and ports are macros: a field or a local by the same name would be
    # taken for one of them
    macros = {name for name, _ in list(known.variables.values()) + list(known.tables.values())
              + list(known.hardware.values())} | set(known.ports.values())
    # (the families' own names of a structure's fields too: scoped lines)
    clashes = sorted(({name for name, _ in known.fields.values()}
                      | {name for (struct, _), found in known.scoped.items() if struct != "global"
                         for name, _ in found.values()}) & macros)
    if clashes:
        raise SystemExit("sound_symbols.txt: fields named like a variable, table, register or port: "
                         + ", ".join(clashes))
    for struct in arrays:
        if struct not in known.structs:
            raise SystemExit(f"sound_symbols.txt: array of '{struct}', which is no struct")
    emit.use_memory_map(known, arrays)
    entries_by_name = {name: entry for entry, name in names.items()}
    hints = {}
    # (STRUCT, or STRUCT.FAMILY: a structure what is read through it reads as that family's code reads its own)
    for (function, register), struct in known.param_structs.items():
        if function in entries_by_name:
            hints.setdefault(entries_by_name[function], {})[register] = pointers.link_kind(struct)
    site_hints = {}
    for (function, spot, value), struct in known.local_structs.items():
        if not value.startswith("r"):
            continue    # (a temporary or a stack slot: the structure is for registers only)
        register = int(value[1:])
        if spot == "entry" and function in entries_by_name:
            hints.setdefault(entries_by_name[function], {})[register] = pointers.link_kind(struct)
        elif spot != "entry":
            site_hints[(spot, register)] = pointers.link_kind(struct)
    typing = pointers.Program(built, flow, found,
                              {entry: call_results(function, found, flow) for entry, function in built.items()},
                              signatures.CONTEXT, known.targets, hints, site_hints)
    fields, accesses = typing.run()
    through = [(hit[1], hit[2]) for found_fields in fields.values() for hit in found_fields.values()
               if hit[0] == "global"]
    # (a table of code addresses the code indexes from before its first entry, a jumps line with first=, is named by its
    # table line at that entry: the label the jumps line puts before it would be a second table of that name)
    table_names = {name for name, _ in known.tables.values()}
    data_names = {address: symbol.name for address, symbol in program.symbols.items()
                  if symbol.kind in ("data", "jumps") and (symbol.name not in table_names or address in known.tables)}
    named = memory.Memory(known, memory.accesses_of(built.values()) + through, data_names)
    cprint.PORT_NAMES = dict(known.ports)

    always = {entry: set(summary.always) for entry, summary in flow.summaries.items()}
    emitter = emit.SoundEmitter(built, found, names, named, always, pointers.Layouts(accesses, known), known.ports,
                                signatures.CONTEXT)
    emitter.register_names = dict(known.registers)    # (the context registers' names: register lines)
    emitter.writes = {entry: set(summary.writes) for entry, summary in flow.summaries.items()}
    emitter.param_pointers = {entry: set(kinds) for entry, kinds in hints.items()}
    emitter.flow = flow
    emitter.data_labels = {address: name for address, name in data_names.items() if address not in named.tables}
    emitter.code_labels = {address: symbol.name for address, symbol in program.symbols.items() if symbol.kind == "code"}
    # (the profile's SOUND_CODE_VALUES, off by default: the handlers the program stores in its channels' records, a
    # function's address it loads as a value, by name, NAME_ADDRESS, and the switches on them case by case)
    if getattr(game_profile, "SOUND_CODE_VALUES", False):
        from decompiler import emit as decompiler_emit
        emitter.code_addresses = {value: names[value] + "_ADDRESS" for value in code_values(program)}
        decompiler_emit.NAMED_CODE_CASES = True
    emitter.comments = dict(known.comments)
    emitter.constants = dict(known.constants)    # (sound_symbols.txt's constant lines: a number a function uses, by name)
    emitter.replaced = replaced_functions([(ROOT / "runtime" / "sound_replaced.txt", "BOARD_ORIGINAL")])
    entries_by_name = {name: entry for entry, name in names.items()}
    unknown = sorted({function for function, _ in known.params if function not in entries_by_name}
                     | {function for function, spot, _ in known.locals if function not in entries_by_name})
    if unknown:
        print(f"sound_symbols.txt names parameters or locals of functions that are not in the program: "
              + ", ".join(unknown[:5]))
    emitter.param_names = {(entries_by_name[function], register): name
                           for (function, register), name in known.params.items() if function in entries_by_name}
    emitter.result_names = {(entries_by_name[function], register): name
                            for (function, register), name in known.results.items() if function in entries_by_name}
    emitter.local_sites = {(spot, value): name for (function, spot, value), name in known.locals.items()
                           if spot != "entry"}
    emitter.local_names = {(entries_by_name[function], spot, value): name
                           for (function, spot, value), name in known.locals.items()
                           if spot == "entry" and function in entries_by_name}
    # a function that hands results back through pointers is written both ways, as the game's (decompile.py):
    # the C keeps the one that writes them back before fewer early returns
    joined = {}
    for entry, function in built.items():
        notes = (live_after[entry], fields[entry])
        if emitter.by_pointer(entry):
            joined[entry] = []
            for tail_calls, meet in ((False, False), (True, False), (False, True), (True, True)):
                way = twin(function, notes)
                structure.join_exits(way, notes, tail_calls)
                way.meet_at_exit = meet    # (the short ways to the one way back meet the others there)
                structure.split_loop_entries(way, notes)
                joined[entry].append(way)
        structure.split_loop_entries(function, notes)
    if getattr(game_profile, "SOUND_LOOP_COUNTERS", False):
        # (the profile's SOUND_LOOP_COUNTERS, off by default: the variable a DJNZ counts down is the loop's count)
        from decompiler import emit as decompiler_emit
        decompiler_emit.COUNTER_SITES = frozenset(address for body in program.bodies.values()
                                                  for address, instruction in body.items()
                                                  if instruction.mnemonic == "djnz")
    writers = {entry: emit.SoundWriter(emitter, function, live_after[entry], fields[entry], live_at_start[entry])
               for entry, function in built.items()}
    if emit.emit.PARAMS_BY_USE:
        # (the profile's SOUND_PARAMS_BY_USE, z80/emit.py: a parameter or result sound_symbols.txt does not name is
        # called for what the function does with it)
        name_params_by_use(emitter, writers, lambda entry: emit.SoundWriter(emitter, built[entry], live_after[entry],
                                                                             fields[entry], live_at_start[entry]))
    texts = {}
    problems = []
    for entry, writer in writers.items():
        try:
            texts[entry] = writer.write()
        except emit.emit.EmitError as problem:
            problems.append((entry, str(problem)))
    # a result some call gives the same variable as another one keeps a copy of its own: written again
    emitter.shared_results = emit.emit.shared_results(texts.values())
    for entry in texts:
        if emitter.shared_results.get(emitter.names.get(entry)):
            texts[entry] = writers[entry].write()
    # (the second way, once the shared results are known; then those of the C kept)
    for entry in joined:
        for way in joined[entry] if entry in texts else ():
            other = emit.SoundWriter(emitter, way, live_after[entry], fields[entry], live_at_start[entry])
            try:
                text = other.write()
            except (emit.emit.EmitError, GotoError):
                continue    # (a way of writing it the structure cannot take: the other one stays)
            if fewer_write_backs(text, texts[entry]):
                writers[entry], texts[entry] = other, text
    split = fewer_controls(
        texts, writers,
        lambda entry, way: emit.SoundWriter(emitter, way, live_after[entry], fields[entry], live_at_start[entry]),
        lambda entry: (live_after[entry], fields[entry]))
    emitter.shared_results = emit.emit.shared_results(texts.values())
    for entry in set(joined) | split:
        if entry in texts and emitter.shared_results.get(emitter.names.get(entry)):
            texts[entry] = writers[entry].write()
    # (a table the C reads by a register that holds a multiple of its elements' size, NAME[register / size], is an array
    # in sound.h; the count of indexes the header is made from cannot see the register's low bits: once such a read is
    # written, the table is made an array and the functions are written again, its other reads as NAME[0])
    scalar = {item.name: item for item in named.tables.values() if not item.indexed}
    subscripted = {name for name in scalar if any(re.search(rf"\b{name}\[", text) for text in texts.values())}
    if subscripted:
        for name in subscripted:
            scalar[name].indexed = True
        for entry in texts:
            texts[entry] = writers[entry].write()
    if problems:
        for entry, problem in problems[:20]:
            print(f"  {entry:04X}  {problem}")
        raise SystemExit(f"{len(problems)} functions could not be written")
    if variables_path is not None:
        write_variables(variables_path, writers, names, texts)
    # the reports of the sound program's source go beside the build; those of another copy, with it
    reports = REPORTS if out_dir.resolve() == (ROOT / "src" / "sound").resolve() else out_dir
    reports.mkdir(parents=True, exist_ok=True)
    write_reports(program, built, flow, names, reports)

    out_dir.mkdir(parents=True, exist_ok=True)
    entries = sorted(built)
    files = sound_files(entries)
    # (a function the board replaces is declared twice: as the program calls it and as its original)
    prototypes = grouped_prototypes(files, lambda entry: emitter.prototype(entry) + (
        ";\n" + emitter.definition(entry) if names[entry] in emitter.replaced else ""))
    ports = emitter.port_declarations()
    (out_dir / "sound.h").write_text(
        file_header("sound.h", "the sound program's ports, variables, tables, structures and functions")
        + f"#ifndef {game_profile.GUARD}_SOUND_H\n#define {game_profile.GUARD}_SOUND_H\n\n"
        + "#include \"sound/support.h\"\n\n"
        + ("/* registers the whole program keeps (sound_symbols.txt's register lines; defined in tables.c) */\n"
           + "\n".join(f"extern {context_type(index)} {emitter.context_name(index)};" for index in signatures.CONTEXT)
           + "\n\n" if signatures.CONTEXT else "")
        + ("/* the board's I/O ports the program uses */\n" + "\n".join(ports) + "\n\n" if ports else "")
        + "\n".join(named.declarations()) + "\n\n/* data in the program ROM sound_symbols.txt labels */\n"
        + "\n".join(f"#define {name}_ADDRESS 0x{address:04X}u" for address, name in sorted(emitter.data_labels.items()))
        + ("\n\n/* where the code the program holds as values is (the handlers it stores and switches on) */\n"
           + "\n".join(f"#define {name} 0x{address:04X}u" for address, name
                       in sorted({**emitter.case_addresses, **emitter.code_addresses}.items()))
           if emitter.code_addresses or emitter.case_addresses else "")
        + ("\n\n/* numbers the code uses, by name (sound_symbols.txt's constant lines) */\n"
           + "\n".join(f"#define {name} 0x{value:X}u" for value, name in sorted({(value, name) for (_, value), name
                                                                                in emitter.constants.items()}))
           if emitter.constants else "")
        + "\n\n/* the sound program's structures in the shared RAM */\n"
        + "\n".join(emitter.layouts.declarations()) + "\n\n"
        + "\n".join(prototypes) + "\n\n#endif\n", encoding="ascii")
    write_sources(out_dir, files, texts, "sound.h", header=file_header)
    loaded = named.loaded_tables()
    if getattr(game_profile, "SOUND_TABLES_USED_ONLY", False):
        # (only the tables the C reads: a number that is an address of the ROM, ld bc,$0007 a count, names none the
        # board would have to load)
        used = "\n".join(texts.values())    # (the functions' C)
        loaded = [(address, name) for address, name in loaded if re.search(rf"\b{re.escape(name)}\b", used)]
    if loaded or signatures.CONTEXT:
        (out_dir / "tables.c").write_text(
            file_header("tables.c", "the tables the sound program reads by name, which the board sets"
                        + (", and the registers it keeps" if signatures.CONTEXT else ""))
            + "#include \"sound.h\"\n\n"
            + ("/* registers the whole program keeps */\n"
               + "\n".join(f"{context_type(index)} {emitter.context_name(index)};" for index in signatures.CONTEXT)
               + "\n\n" if signatures.CONTEXT else "")
            + ("\n".join(memory.table_registry(loaded, 4, "sound")) + "\n" if loaded else ""),
            encoding="ascii")
    left = sum(len(block.statements) for function in built.values() for block in function.blocks.values())
    gotos = sum(text.count("goto ") for text in texts.values())
    print(f"functions:  {len(entries)} in {out_dir}")
    print(f"statements: {left} ({before - left} dead ones removed)")
    print(f"branches:   {flow.conditions_recovered} now compare values instead of flags")
    print(f"gotos:      {gotos}")
    return 0


def context_type(index):
    """The C type of a register the whole program keeps (SOUND_CONTEXT): its width's."""
    return "uint8_t" if cpu.Z80.register_bits.get(index, 16) == 8 else "uint16_t"


def write_reports(program, built, flow, names, out_dir):
    """build/sound/listing.s, the program's instructions function by function with the names
    sound_symbols.txt gives, and build/sound/index.txt, what each function calls and is called by, the
    shared RAM it reads and writes at fixed addresses and the ports it uses: what naming it works from."""
    labels = {address: symbol.name for address, symbol in program.symbols.items()
              if symbol.kind in ("func", "code", "loop", "data", "jumps")}
    labels.update(names)
    lines = ["; the sound program (epr-15550), function by function: tools/decompile_sound.py"]
    for entry in sorted(program.bodies):
        lines += ["", f"; ---- {names[entry]} ({entry:04X})"]
        for address in sorted(program.bodies[entry]):
            instruction = program.bodies[entry][address]
            raw = " ".join(f"{program.image[(address + offset) & 0xFFFF]:02X}" for offset in range(instruction.length))
            label = labels.get(address) if address != entry else None
            if label:
                lines.append(f"{label}:")
            text = instruction.text
            if instruction.target is not None and instruction.target in labels:
                text += f"    ; {labels[instruction.target]}"
            lines.append(f"  {address:04X}  {raw:<12}  {text}")
    (out_dir / "listing.s").write_text("\n".join(lines) + "\n", encoding="ascii")

    callers = {}
    uses = {}
    for entry, function in built.items():
        callees, reads, writes, ports = set(), set(), set(), set()
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                if isinstance(statement, ir.Call):
                    callees |= set(flow.call_targets(statement))
                if isinstance(statement, ir.TailCall):
                    callees.add(statement.target)
                if isinstance(statement, ir.Store) and isinstance(statement.address, ir.Const):
                    writes.add(statement.address.value)
                if isinstance(statement, ir.Effect) and statement.name == "port_out" \
                        and isinstance(statement.args[0], ir.Const):
                    ports.add(f"out {statement.args[0].value:02X}")
                for expression in ir.statement_expressions(statement):
                    for node in ir.walk(expression):
                        if isinstance(node, ir.Load) and isinstance(node.address, ir.Const):
                            reads.add(node.address.value)
                        if isinstance(node, ir.Op) and node.name == "helper:port_in" \
                                and isinstance(node.args[0], ir.Const):
                            ports.add(f"in {node.args[0].value:02X}")
        uses[entry] = (callees, reads, writes, ports)
        for callee in callees:
            callers.setdefault(callee, set()).add(entry)
    lines = ["# FUNCTION (address): callers, callees, shared RAM read and written at fixed addresses, ports",
             "# (tools/decompile_sound.py; computed addresses, such as fields reached through IX and IY, are not listed)"]
    for entry in sorted(built):
        callees, reads, writes, ports = uses[entry]
        lines.append(f"{names[entry]} ({entry:04X})" + ("    never run: the reset and the interrupt do not reach it"
                                                         if entry in program.unreached else ""))
        lines.append("  called by: " + " ".join(sorted(names[c] for c in callers.get(entry, ()))))
        lines.append("  calls:     " + " ".join(sorted(names.get(c, f"{c:04X}") for c in callees)))
        lines.append("  reads:     " + " ".join(f"{a:04X}" for a in sorted(reads)))
        lines.append("  writes:    " + " ".join(f"{a:04X}" for a in sorted(writes)))
        lines.append("  ports:     " + " ".join(sorted(ports)))
    (out_dir / "index.txt").write_text("\n".join(lines) + "\n", encoding="ascii")


def write_timing(program, path):
    """The blocks of the timing model as the C has them (runtime/src/sound_cpu.c), for the reference to
    keep the same time: every block's first instruction, its cycles, its last instruction and that one's
    length, and the extra cycles when its condition takes the longer way; the block moves; the loop's top
    and the interrupt handler."""
    builder = functions.Builder(program)
    lines = ["# the sound program's timing model (tools/decompile_sound.py): see runtime/src/sound_cpu.c",
             f"loop {builder.loop:04X}", f"irq {INTERRUPT_HANDLER:04X}"]
    written = set()    # (a function nothing calls has its own copy of blocks another function has too)
    for entry in sorted(program.entries):
        function = builder.build(entry)
        body = program.bodies[entry]
        for label, block in function.blocks.items():
            if not isinstance(label, int) or label in written:
                continue
            written.add(label)
            last = body[block.addresses[-1]]
            taken, short = functions.timing.cycles(last)
            lines.append(f"block {label:04X} {builder.block_cycles(body, block)} {last.address:04X} {last.length} "
                         f"{taken - short}")
            for address in block.addresses:
                if body[address].mnemonic in ("ldir", "lddr"):
                    lines.append(f"repeat {address:04X}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")


def main():
    global SYMBOLS
    arguments = sys.argv[1:]
    out_dir = ROOT / "src" / "sound"
    if "--out" in arguments:
        position = arguments.index("--out")
        out_dir = Path(arguments[position + 1])
        del arguments[position:position + 2]
    if "--symbols" in arguments:
        position = arguments.index("--symbols")
        SYMBOLS = Path(arguments[position + 1])
        del arguments[position:position + 2]
    variables_path = None
    if "--variables" in arguments:
        position = arguments.index("--variables")
        variables_path = Path(arguments[position + 1])
        del arguments[position:position + 2]
    from decompiler import emit as decompiler_emit
    decompiler_emit.CHECKED_JUMPS = "--checks" in arguments
    timing_path = ROOT / "build" / "sound" / "timing.txt"
    if "--timing" in arguments:
        position = arguments.index("--timing")
        timing_path = Path(arguments[position + 1])
        del arguments[position:position + 2]
    configure()
    program = load_program()
    if "--check" in arguments:
        return check(program)
    timing_path.parent.mkdir(parents=True, exist_ok=True)
    write_timing(program, timing_path)
    return write_c(program, out_dir, variables_path)


if __name__ == "__main__":
    sys.exit(main())
