# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        decompile.py
Description: decompiles a Konami CPU program (the 053248's, banked) into readable C, one C function per original
             function
Version:     0.1.0
Created:     06-11-2026
Modified:    07-11-2026
Author:      Sebastian Toyos

Usage: python tools/decompile.py [rom folder or .zip] [--check] [--checks] [--trace] [--out DIR] [--symbols FILE]
                                 [--variables FILE]
       (the core's decompile.py runs this when the game's profile says DECOMPILER = "konami_cpu.decompile")
  --check     build every function's IR and report what does not lift or whose stack the C cannot follow, without
              writing anything
  --checks    a jump through a table with an index the table does not have stops the program (invalid_jump)
  --trace     every function and block reports itself when it runs (game_trace: the last ones are shown when the
              program stops)
  --out       where to write the C (src/ by default)
  --symbols   the names to use instead of symbols.txt
  --variables also list every function's local variables and the instructions that write them

The program goes through the decompiler the V60, 68000 and Z80 programs go through (decompiler/), with the Konami
CPU's profile (konami_cpu/function.KONAMI): konami_cpu/function.py splits it into functions from the static
analysis (symbols.txt, no traces) and lifts them, the passes work out what each one takes and hands back, D's bytes
are variables of their own where a function uses them apart (z80/pairs.py), and konami_cpu/emit.py writes them. It
writes src/: game.h (prototypes, file by file), ram.h (the variables, the tables, the board's registers, the
structures), the functions' files (symbols.txt's file lines lay them out; game_NN.c without them) and table.c (the
code the board runs by address: the reset and the interrupts' handlers; the registry of the tables the game loads
by name). Beside the build, in build/decompile/: index.txt (where each function is, its callers and callees, how
much of it the scenarios ran) and unnamed_addresses.txt.

Before the C, the split is settled against the stack (settle): code two functions share that runs on a stack its
callers pushed goes back into each of them, and an entry that runs on a stack the code reaching it pushed (the
cases of a table its dispatcher pushed for, a stretch of a routine a table names by mistake) is that code's own
(function.Program.weaken); one that stays a function of its own though its stack cannot be followed (only a
mistaken table leads to it) is written as lost_stack().

The functions the board puts its own in place of (runtime/replaced.txt, a name a line: the boot's ROM test's sum,
which reads the program's code) are written as BOARD_ORIGINAL(name), and declared both ways in game.h.
"""
import re
import sys
import time
from collections import Counter
from pathlib import Path

import game_profile  # noqa: E402  (the game's: its root, its ROM set, its settings)
from decompile import call_results, fewer_controls, fewer_write_backs, grouped_prototypes  # noqa: E402
from decompile import replaced_functions, source_files, twin, write_sources, write_variables  # noqa: E402
from decompiler import cleanup  # noqa: E402
from decompiler import conditions  # noqa: E402
from decompiler import cpu  # noqa: E402
from decompiler import dataflow  # noqa: E402
from decompiler import emit as decompiler_emit  # noqa: E402
from decompiler import expressions  # noqa: E402
from decompiler import knownbits  # noqa: E402
from decompiler import memory  # noqa: E402
from decompiler import pointers  # noqa: E402
from decompiler import preserved  # noqa: E402
from decompiler import signatures  # noqa: E402
from decompiler import stack  # noqa: E402
from decompiler import structure  # noqa: E402
from decompiler.function import Block, Function  # noqa: E402
from decompiler.gotos import GotoError  # noqa: E402
from v60 import ir  # noqa: E402
from z80 import pairs  # noqa: E402
import disassemble  # noqa: E402

from . import analysis  # noqa: E402
from . import emit  # noqa: E402
from . import function as functions  # noqa: E402

ROOT = game_profile.ROOT
SYMBOLS = ROOT / "symbols.txt"
REPORTS = ROOT / "build" / "decompile"
CREATED = getattr(game_profile, "DECOMPILED", "06-11-2026")    # when src/ was first written
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
REGISTERS = {name: index for index, name in functions.KONAMI.register_names.items()}    # param and local lines
# names the runtime the decompiled code includes already uses (game/support.h)
TAKEN = {"frame_wait", "take_interrupts", "firq_point", "set_lines", "enable_irq", "disable_irq", "enable_firq",
         "disable_firq", "and_mask", "or_mask", "restore_mask", "interrupt_mask", "lost_stack", "illegal_opcode",
         "game_entry", "game_irq", "game_firq", "game_tables", "game_table_count", "invalid_jump", "main",
         "mem_read8", "mem_read16", "mem_write8", "mem_write16", "ram", "data_at", "data_record"}
SETTLE_ROUNDS = 30


def configure():
    """The Konami CPU's profile, and the program's own: no registers it keeps for the whole program, no handler
    slots; a read of the board's devices (0000-3FFF) stays even where its value goes unused (the watchdog, the
    Z80's interrupt, the ports polled), D's bytes in A and B, big endian"""
    cpu.use(functions.KONAMI)
    cleanup.EFFECT_READS = lambda address: (address & 0xFFFF) < 0x4000
    signatures.CONTEXT = ()
    signatures.SCRATCH_CONTEXT = {}
    signatures.CONSTANT = {}
    signatures.INITIAL = {}
    dataflow.HANDLER_SLOTS = ()
    dataflow.WORKLIST = True
    knownbits.SIGN_EXTENSIONS = True
    decompiler_emit.WIDENED_INDEXES = False
    pairs.PAIRS = {functions.D: (functions.A, functions.B)}
    pairs.BYTES = {functions.A: "a", functions.B: "b"}
    pairs.BIG_ENDIAN = True


def load_program(source):
    image = game_profile.rom.build_program(game_profile.rom.load_set(source))
    return functions.Program(image, analysis.load_symbols(SYMBOLS))


def caller_frame_read(function, own=True):
    """The offset of a slot of its caller's the function reads (what is past its return address; or the return
    address, but where a function of its own ('own': not a stretch the split of shared code made) returns past its
    caller: PULLS D,PC, D the return address), or None"""
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            for expression in ir.statement_expressions(statement):
                for node in ir.walk(expression):
                    if isinstance(node, ir.Stack) and node.offset >= 0 and (
                            node.offset >= cpu.current.return_address_bytes or not function.exits_caller or not own):
                        return node.offset
    return None


def overlapping_slots(function):
    """True if two of the function's stack slots overlap without being the same (a word read across two pushes, or
    a byte of a pushed word): its stack stays a byte at a time"""
    found = set()
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            if isinstance(statement, ir.SetStack):
                found.add((statement.offset, statement.bits))
            for expression in ir.statement_expressions(statement):
                for node in ir.walk(expression):
                    if isinstance(node, ir.Stack):
                        found.add((node.offset, node.bits))
    covered = {}
    for offset, bits in found:
        for byte in range(offset, offset + bits // 8):
            covered.setdefault(byte, set()).add((offset, bits))
    return any(len(slots) > 1 for slots in covered.values())


def build_all(program):
    """{entry: Function} with its stack's slots, and {entry: why the C cannot follow its stack}"""
    builder = functions.Builder(program)
    built, failures, unfollowed = {}, [], {}
    for entry in sorted(program.entries):
        try:
            function = builder.build(entry)
        except functions.Unsupported as problem:
            failures.append((entry, str(problem)))
            continue
        built[entry] = function
        try:
            stack.analyse(function)
        except stack.StackError as problem:
            unfollowed[entry] = str(problem)
            continue
        left = stack.leftovers(function)
        if left:
            unfollowed[entry] = f"{len(left)} statements still use S, the first: {left[0]}"
            continue
        offset = caller_frame_read(function, entry in program.base_entries)
        if offset is not None:
            unfollowed[entry] = f"reads its caller's stack at {offset:+d}"
        elif entry not in program.byte_stack and overlapping_slots(function):
            unfollowed[entry] = "bytes"
    return built, failures, unfollowed


def settle(program, report=print):
    """The split of the program, settled against the stack (see the module's notes): (built, failures,
    unfollowed) of the last round"""
    for number in range(SETTLE_ROUNDS):
        built, failures, unfollowed = build_all(program)
        bytes_now = {entry for entry, why in unfollowed.items() if why == "bytes"}
        if bytes_now:
            report(f"settle {number}: {len(bytes_now)} functions whose stack stays a byte at a time")
            program.byte_stack |= bytes_now
            continue
        bad = set(unfollowed)
        split = bad & (program.entries - program.base_entries)
        owners = {entry for entry in bad - program.weak
                  if any(place in program.table_inner and place != entry for place in program.bodies[entry])}
        table_owners = {entry for entry in owners
                        if entry in program.analysis.from_table and entry not in program.analysis.functions}
        culprits = {place for entry in owners - table_owners for place in program.bodies[entry]
                    if place in program.table_inner and place != entry}
        new_weak = bad - split - program.weak
        report(f"settle {number}: {len(built)} functions, {len(unfollowed)} whose stack the C cannot follow")
        if split:
            program.drop_split(split)
        elif table_owners or culprits:
            program.weaken(table_owners, untabled=culprits)
        elif new_weak:
            program.weaken(new_weak)
        else:
            return built, failures, unfollowed
    raise SystemExit("the split of the program does not settle against the stack")


def references(built):
    """The entries some call, tail call or switch of 'built' reaches"""
    found = set()
    for function in built.values():
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                if isinstance(statement, ir.Call):
                    found |= {statement.target} if statement.target is not None else set(statement.choices)
                elif isinstance(statement, ir.TailCall):
                    found.add(statement.target)
    return found


def lost_stack_function(entry, name):
    """A function the C cannot follow the stack of, which only a mistaken table reaches: it stops the program"""
    function = Function(entry, name)
    effect = ir.Effect("lost_stack", (ir.const(entry, 32),))
    effect.origin = entry
    end = ir.Return()
    end.origin = entry
    function.blocks[entry] = Block(entry, [effect], end, [entry])
    function.exits_caller = False
    return function


def finish_split(program, built, unfollowed):
    """The weak entries nothing reaches any more go (their code is their dispatcher's own); the others whose stack
    cannot be followed become lost_stack functions"""
    reached = references(built) | set(program.handlers)
    for entry in sorted(program.weak):
        if entry in built and entry not in reached:
            del built[entry]
            program.entries.discard(entry)
            program.bodies.pop(entry, None)
    for entry, why in unfollowed.items():
        if entry not in built:
            continue
        if entry not in program.weak:
            raise SystemExit(f"{entry:06X}: the C cannot follow its stack: {why}")
        built[entry] = lost_stack_function(entry, program.names[entry])
    program.names = {entry: name for entry, name in program.names.items() if entry in program.entries}


def interrupt_handlers(program):
    """The FIRQ's and the IRQ's handlers"""
    fixed = program.analysis.view(None)
    return sorted({analysis.place_of(None, fixed.r16(vector)) for vector in functions.INTERRUPT_VECTORS})


def open_entries(program, built):
    """The functions nothing reaches: no call, tail call or switch leads to them, and no vector"""
    reached = references(built) | set(program.handlers)
    for function in built.values():
        for block in function.blocks.values():
            if isinstance(block.end, ir.Switch):
                reached |= {target for target in block.end.cases.values() if not isinstance(target, tuple)}
    return {entry for entry in built if entry not in reached}


def declared_results(program, open_set):
    entries = {name: entry for entry, name in program.names.items()}
    found = {}
    for function_name, register in disassemble.load_names(SYMBOLS, REGISTERS).results:
        entry = entries.get(function_name)
        if entry is None:
            raise SystemExit(f"{SYMBOLS.name}: a result of {function_name}, which is no function")
        if entry not in open_set:
            raise SystemExit(f"{SYMBOLS.name}: a result of {function_name}, whose callers say what it hands back")
        found.setdefault(entry, set()).add(register)
    return found


def caller_exits_always_said(built, exiting):
    """A function that may return past its caller (stack.link_caller_exits: PULLS D,PC) sets LEFT_CALLER on that way
    out; on its others it clears it, so that whether it did is a result it always hands back, not a value it passes
    on from its caller"""
    for entry in exiting:
        function = built.get(entry)
        if function is None:
            continue
        for block in function.blocks.values():
            end = block.end
            if isinstance(end, ir.Return) and not getattr(end, "leaves_caller", False) \
                    or isinstance(end, ir.TailCall) and end.target not in exiting:
                clear = ir.SetReg(stack.LEFT_CALLER, ir.const(0))
                clear.origin = getattr(end, "origin", None)
                block.statements.append(clear)


def caller_exits_consumed(built, exiting):
    """A call that returned past its caller (stack.link_caller_exits' "left" blocks) returns the function it was made
    from at once, to its own caller, which goes on as after any return: there LEFT_CALLER is cleared, so that a
    caller further up never takes it for a return past itself (07A5E3: an object handler's PULLS D,PC under 07B5DD
    made the step dispatch at 07A3B7 return). The functions that may return past their caller clear it on those
    ways already (caller_exits_always_said)."""
    for entry, function in built.items():
        if entry in exiting:
            continue
        for label, block in function.blocks.items():
            if isinstance(label, tuple) and label[0] == "left":
                clear = ir.SetReg(stack.LEFT_CALLER, ir.const(0))
                clear.origin = getattr(block.end, "origin", None)
                block.statements.insert(0, clear)


def analyse(program, built):
    """The returns past a caller, preserved registers and the data flow summaries, in place (the stack's slots are
    settle's)"""
    exiting = stack.link_caller_exits(built)
    caller_exits_always_said(built, exiting)
    caller_exits_consumed(built, exiting)
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


def statements_with(function, name):
    """The statements of 'function' with an operator 'name' in them, for a message"""
    found = []
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            if any(isinstance(node, ir.Op) and node.name == name
                   for expression in ir.statement_expressions(statement) for node in ir.walk(expression)):
                found.append(f"{getattr(statement, 'origin', 0) or 0:06X} {str(statement)[:160]}")
    return "".join("\n      " + line for line in found[:4])


def by_value(found):
    arguments = {}
    for entry, signature in found.items():
        registers = {value[1] for value in signature.params - signature.results if value[0] == "r"}
        returned = next(iter(signature.results)) if len(signature.results) == 1 else None
        if returned is not None and returned[0] == "r" and returned in signature.params:
            registers.add(returned[1])
        arguments[entry] = registers
    return arguments


def check(program):
    built, failures, unfollowed = settle(program)
    ends = Counter(type(block.end).__name__ for function in built.values() for block in function.blocks.values())
    print(f"functions:  {len(built)}")
    print(f"blocks:     {sum(ends.values())}")
    print("block ends: " + ", ".join(f"{kind} {count}" for kind, count in ends.most_common()))
    print(f"weak:       {len(program.weak)} entries walked through, {len(program.table_inner)} of them through tables")
    print(f"problems:   {len(failures)}")
    for entry, problem in failures[:40]:
        print(f"  {entry:06X}  {problem}")
    reached = {}
    for entry, function in built.items():
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                targets = [statement.target] if isinstance(statement, ir.TailCall) else                     ([statement.target] if statement.target is not None else list(statement.choices))                     if isinstance(statement, ir.Call) else []
                for target in targets:
                    reached.setdefault(target, set()).add(entry)
    print(f"unfollowed: {len(unfollowed)}")
    for entry in sorted(unfollowed):
        if entry in reached:
            print(f"  {entry:06X} reached from " + " ".join(f"{other:06X}" for other in sorted(reached[entry])))
    for entry, problem in sorted(unfollowed.items())[:40]:
        print(f"  {entry:06X}  {problem}{'  (weak)' if entry in program.weak else ''}")
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
                        "the game's functions, part {number:02} (in the order of their places)")


def keyed_names(known):
    """symbols.txt's names with the program's data keyed as the decompiler names it (emit.PLACES + place)"""
    tables = {emit.PLACES + address: value for address, value in known.tables.items()}
    record_tables = {emit.PLACES + address: value for address, value in known.record_tables.items()}
    rom = set(known.tables) | set(known.record_tables)
    counts = {(emit.PLACES + address if address in rom else address): value for address, value in known.counts.items()}
    # (what a table leads to, under its key too: pointers.data_key finds it so)
    targets = {(("global", emit.PLACES + key[1]) if key[0] == "global" and key[1] in rom else key): value
               for key, value in known.targets.items()}
    return known._replace(tables=tables, record_tables=record_tables, counts=counts, targets=targets)


def piece_names():
    """{key: name} of the pieces of the program's data the assets hold (assets/data/layout.txt)"""
    layout = ROOT / "assets" / "data" / "layout.txt"
    found = {}
    if not layout.exists():
        return found
    for line in layout.read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("#") and IDENTIFIER.match(parts[0]):
            try:
                found[emit.PLACES + int(parts[1], 16)] = parts[0]
            except ValueError:
                continue
    return found


def statement_accesses(statement):
    """(address, bits) of the memory a statement reads or writes (memory.accesses_of's, one statement)"""
    found = []
    if isinstance(statement, ir.Store):
        found.append((statement.address, statement.bits))
    for expression in ir.statement_expressions(statement):
        for node in ir.walk(expression):
            if isinstance(node, ir.Load):
                found.append((node.address, node.bits))
    return found


def keyed_accesses(built):
    """(address, bits) of every access, its program's addresses keyed as the bank of its function names them (in the
    fixed program, as the bank the CPU surely has at the statement's instruction: emit.statement_bank)"""
    found = []
    for entry, function in built.items():
        emit.current_bank = analysis.bank_of(entry)
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                emit.statement_bank(statement)
                for address, bits in statement_accesses(statement):
                    if isinstance(address, ir.Const) and address.value >= analysis.WINDOW:
                        key = emit.canonical(address.value)
                        found.append((ir.const(key, 32) if key < emit.OUTSIDE else address, bits))
                        continue
                    parts = memory.based(address)
                    if parts is not None and parts[0] >= analysis.WINDOW:
                        key = emit.canonical(parts[0])
                        if key < emit.OUTSIDE:
                            found.append((ir.op("add", (ir.const(key, 32), ir.zext(parts[1], 32)
                                                        if parts[1].bits < 32 else parts[1]), 32), bits))
                            continue
                    found.append((address, bits))
    emit.current_bank = None
    emit.origin_bank = None
    return found


def cpu_addresses(lines):
    """Header lines with the NAME_ADDRESS of the program's data at its CPU address (what the program holds as a
    value), not at the key the decompiler names it by"""
    found = []
    for line in lines:
        match = re.match(r"#define (\w+_ADDRESS) 0x([0-9A-F]+)u$", line)
        if match and emit.PLACES <= int(match.group(2), 16) < emit.PLACES + emit.PROGRAM_SIZE:
            key = int(match.group(2), 16)
            line = f"#define {match.group(1)} 0x{emit.cpu_of_key(key):04X}u    /* {key - emit.PLACES:06X} */"
        found.append(line)
    return found


def table_registry(named, texts):
    """The C of the registry of the tables the game loads (those the functions read): each table's pointer, name,
    place (where the assets have it) and the bytes of its elements (the loader turns each to the host's byte order)"""
    used = set(re.findall(r"[A-Za-z_]\w*", "\n".join(texts.values())))
    loaded = [(key, name) for key, name in named.loaded_tables() if name in used]
    lines = [f"const void *{name}_data;" for _, name in loaded]
    lines += ["", "/* the tables the game loads from its assets: their names, places, pointers and the bytes of their",
              "   elements (the loader puts each element in the host's byte order) */",
              "const game_table game_tables[] =", "{"]
    for key, name in loaded:
        item = named.tables.get(key)
        size = item.bits // 8 if item is not None else 1
        if item is None and key in pointers.RECORD_TABLES:
            size = pointers.STRUCTS[pointers.RECORD_TABLES[key][1]]    # (a record at a time)
        lines.append(f"\t{{ \"{name}\", 0x{key - emit.PLACES:06X}u, &{name}_data, {size} }},")
    if not loaded:
        lines.append("\t{ 0, 0, 0, 0 },")
    lines += ["};", "", f"const size_t game_table_count = {len(loaded)};"]
    return lines


def write_index(out_dir, program, built, names, entries, files):
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
    for trace in sorted((ROOT / "build" / "scenarios_frames").glob("*/executed.txt")):
        for line in trace.read_text(encoding="ascii").splitlines():
            if line and not line.startswith("#"):
                executed.add(int(line.split()[0], 16))
    location = {}
    for path, _, members in files:
        for entry in members:
            location[entry] = f"{path}.c"
    lines = ["# function  name  instructions  ran-in-a-scenario  file",
             "#   calls: ...   called by: ..."]
    for entry in entries:
        body = program.bodies.get(entry, {})
        ran = sum(1 for address in body if address in executed)
        lines.append(f"{entry:06X}  {names[entry]}  {len(body)}  {ran}/{len(body)}  {location.get(entry, '')}")
        lines.append("    calls: " + " ".join(sorted(names[target] for target in callees[entry])))
        lines.append("    called by: " + " ".join(sorted(names[caller] for caller in callers[entry])))
    (out_dir / "index.txt").write_text("\n".join(lines) + "\n", encoding="ascii")


def write_c(program, out_dir, variables_path=None, trace=False):
    built, failures, unfollowed = settle(program)
    if failures:
        raise SystemExit(f"{len(failures)} functions do not lift, the first: {failures[0][0]:06X} {failures[0][1]}")
    finish_split(program, built, unfollowed)
    program.handlers = [handler for handler in program.handlers if handler in built]
    flow = analyse(program, built)
    found = signatures.compute(flow)
    names = dict(program.names)
    for entry, function_name in names.items():
        if not IDENTIFIER.match(function_name) or function_name in TAKEN:
            raise SystemExit(f"{entry:06X}: '{function_name}' cannot be a C function name")
    if len(set(names.values())) != len(names):
        raise SystemExit("two functions share a name")
    for entry, function in built.items():
        pairs.split(function, flow, found[entry])

    before = sum(len(block.statements) for function in built.values() for block in function.blocks.values())
    known = keyed_names(disassemble.load_names(SYMBOLS, REGISTERS))
    expressions.KEEP_SITES = {(spot, int(value[1:])) for (_, spot, value) in known.local_structs
                              if spot != "entry" and value.startswith("r")}
    arguments = by_value(found)
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
        if functions.narrow_cases(function):
            after, start = cleanup.eliminate(flow, function, found[entry].results, signatures.CONTEXT, arguments)
            expressions.tidy(function, flow, after, whole)
        if expressions.inline_registers(function, flow, after, whole):
            expressions.simplify_function(function)
        live_after[entry] = after
        live_at_start[entry] = start
    results_of_calls = {entry: call_results(function, found, flow) for entry, function in built.items()}
    closed = set(signatures.OPEN) | set(program.handlers)
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
        pairs.tidy_bytes(function, live_after[entry])

    emit.use_memory_map(known)
    emit.BANK_AT = program.analysis.single_bank    # (the bank the fixed program's code surely has, by instruction)
    data_names = {emit.PLACES + address: symbol.name for address, symbol in program.symbols.items()
                  if symbol.kind == "data"}
    data_names.update(piece_names())
    accesses = keyed_accesses(built)
    named = emit.Memory(known, accesses, data_names)
    hints, site_hints = {}, {}
    entries_by_name = {name: entry for entry, name in names.items()}
    for (function_name, register), struct in known.param_structs.items():
        if function_name in entries_by_name:
            hints.setdefault(entries_by_name[function_name], {})[register] = pointers.link_kind(struct)
    for (function_name, spot, value), struct in known.local_structs.items():
        if not value.startswith("r"):
            continue
        if spot == "entry" and function_name in entries_by_name:
            hints.setdefault(entries_by_name[function_name], {})[int(value[1:])] = pointers.link_kind(struct)
        elif spot != "entry":
            site_hints[(spot, int(value[1:]))] = pointers.link_kind(struct)
    typing = pointers.Program(built, flow, found, results_of_calls, signatures.CONTEXT, known.targets, hints,
                              site_hints)
    fields, accesses_typed = typing.run()
    emit.current_bank = None    # (pointers.ENTERING set it function by function)

    always = {entry: set(summary.always) for entry, summary in flow.summaries.items()}
    emitter = emit.Emitter(built, found, names, signatures.CONTEXT, set(), named, always, lambda address: 0, trace)
    emitter.register_names = {}
    emitter.layouts = pointers.Layouts(accesses_typed, known)
    emitter.reroll_statements = True
    emitter.writes = {entry: set(summary.writes) for entry, summary in flow.summaries.items()}
    emitter.param_pointers = {entry: set(kinds) for entry, kinds in hints.items()}
    emitter.flow = flow
    emitter.result_bits = dict(cleanup.RESULT_BITS)
    emitter.code_addresses = {}
    emitter.data_labels = {key: name for key, name in data_names.items() if key not in named.tables}
    emitter.code_labels = {}
    emitter.comments = dict(known.comments)
    emitter.constants = dict(known.constants)
    emitter.replaced = replaced_functions([(ROOT / "runtime" / "replaced.txt", "BOARD_ORIGINAL")])
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
            problems.append((entry, str(problem) + statements_with(built[entry], "stack_address")))
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
    texts = {entry: named.indexed_uses(text) if hasattr(named, "indexed_uses") else text
             for entry, text in texts.items()}

    out_dir.mkdir(parents=True, exist_ok=True)
    entries = sorted(built)
    files = game_files(entries)
    if any(Path(path).parts[0] == "sound" for path, _, _ in files):
        raise SystemExit(f"{SYMBOLS.name}: a file of the program in sound/, which is the sound program's")
    prototypes = grouped_prototypes(files, lambda entry: emitter.prototype(entry) + (
        ";\n" + emitter.definition(entry) if names[entry] in emitter.replaced else ""))
    guard = game_profile.GUARD
    (out_dir / "game.h").write_text(
        file_header("game.h", "prototypes of the game's functions")
        + f"#ifndef {guard}_GAME_H\n#define {guard}_GAME_H\n\n"
        + "#include \"game/support.h\"\n#include \"ram.h\"\n\n"
        + "\n".join(prototypes) + "\n\n#endif\n", encoding="ascii")
    layouts = cpu_addresses(emitter.layouts.declarations())
    declared = set(re.findall(r"^#define (\w+)_ADDRESS ", "\n".join(named.declarations() + layouts), re.M))
    labels = [f"#define {name}_ADDRESS 0x{emit.cpu_of_key(key):04X}u    /* {key - emit.PLACES:06X} */"
              for key, name in sorted(emitter.data_labels.items())
              if emit.PLACES <= key < emit.PLACES + emit.PROGRAM_SIZE and name not in declared]
    numbers = [f"#define {name} 0x{value:X}u" for value, name in sorted({(value, name) for (_, value), name
                                                                          in emitter.constants.items()})]
    (out_dir / "ram.h").write_text(
        file_header("ram.h", "the game's variables, tables, board registers and structures")
        + f"#ifndef {guard}_RAM_H\n#define {guard}_RAM_H\n\n"
        + "\n".join(named.declarations()) + "\n\n/* data of the program symbols.txt and the assets label (their CPU "
        "addresses) */\n" + "\n".join(labels) + "\n\n/* numbers the code uses, by name (symbols.txt constant lines) */\n"
        + "\n".join(numbers) + "\n\n/* the game's structures */\n"
        + "\n".join(layouts) + "\n\n#endif\n", encoding="ascii")
    write_sources(out_dir, files, texts, "game.h", header=file_header, keep=("sound",))

    reset, irq, firq = program.handlers_of_vectors()
    for handler in (reset, irq, firq):
        if emitter.wrapper(handler) is not None:
            raise SystemExit(f"the handler at {handler:06X} takes parameters")
    (out_dir / "table.c").write_text(
        file_header("table.c", "the code the board runs by itself (the reset's, the interrupts' handlers) and the "
                    "tables the game loads by name")
        + '#include "game.h"\n\n'
        "/* the code at the reset vector */\n"
        f"void game_entry(void)\n{{\n\t{names[reset]}();\n}}\n\n"
        "/* the IRQ's handler (the K052109's, at VBLANK) */\n"
        f"void game_irq(void)\n{{\n\t{names[irq]}();\n}}\n\n"
        "/* the FIRQ's handler (the sprite DMA's) */\n"
        f"void game_firq(void)\n{{\n\t{names[firq]}();\n}}\n\n"
        + "\n".join(table_registry(named, texts)) + "\n", encoding="ascii")

    reports = REPORTS if out_dir.resolve() == (ROOT / "src").resolve() else out_dir
    reports.mkdir(parents=True, exist_ok=True)
    write_index(reports, program, built, names, entries, files)
    kept = {names.get(entry, ""): text for entry, text in texts.items()}
    raw = []
    for address, users in sorted(emitter.raw_addresses.items()):
        if not analysis.WINDOW <= address < 0x10000:
            continue
        found_in = sorted(user for user in users if re.search(rf"\b0x0*{address:X}u", kept.get(user, "")))
        if found_in:
            raw.append(f"{address:04X}  {' '.join(found_in)}")
    (reports / "unnamed_addresses.txt").write_text(
        "# addresses of the program the C uses as values that symbols.txt and the assets do not name, and the\n"
        "# functions that use them\n" + "\n".join(raw) + "\n", encoding="ascii")
    left = sum(len(block.statements) for function in built.values() for block in function.blocks.values())
    gotos = sum(text.count("goto ") for text in texts.values())
    print(f"unnamed:    {len(raw)} addresses of the program used as values")
    print(f"functions:  {len(entries)} in {out_dir} ({sum(1 for f in built.values() if len(f.blocks) == 1 and any(isinstance(s, ir.Effect) and s.name == 'lost_stack' for s in f.blocks[f.entry].statements))} lost_stack)")
    print(f"statements: {left} ({before - left} dead ones removed)")
    print(f"branches:   {flow.conditions_recovered} now compare values instead of flags")
    print(f"gotos:      {gotos}, in {sum(1 for text in texts.values() if 'goto ' in text)} functions")
    print(f"tables:     {len(named.loaded_tables())} the game loads by name")
    return 0


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
