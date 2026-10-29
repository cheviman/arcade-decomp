# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        compare_lift_with_mame.py
Description: runs every kind of instruction the 68000 program has on MAME's 68000 core and on its lift
             (m68000/lift.py, run by m68000/interpreter.py) from the same states, and compares what they leave
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Usage: python tools/core/konami/reference/compare_lift_with_mame.py [rom folder or .zip] [--states N]
           [--instances N] [--seed N] [--c] [--all-opcodes] [--only MNEMONIC ...] [--show N]
Needs build/bin/oracle_m68k_step (built by CMake with the reference tools): MAME's 68000 core running one
instruction at a time.

The instructions are the ones the static analysis finds in the program (m68000/analysis.py and symbols.txt),
grouped by their first word (the opcode, which names the operation, its size and its addressing modes): of
each opcode, --instances of its places in the program (3 by default), each from --states random states (40
by default): the registers drawn from values at the edges (0, $7F, $80, $7FFF, $8000, $FFFF...) and at
random, the address registers even and mostly in the work RAM, the flags at random, the CPU in supervisor
mode or in user mode, and now and then an interrupt on the CPU's lines. The instruction runs where it is in
the program, so its PC-relative operands read the program's own tables. Both sides start every state from
the same memory (the program image, then a pattern of the address) and must leave the same registers, the
same status register, the same next PC (an exception's or an interrupt's handler, when one is taken) and the
same bus cycles in the same order: reads and writes, their addresses, mem_masks and words.

A state whose instruction would make a word access at an odd address (the address error exception), or leave
the trace bit set (an RTE popping it: the trace exception), is left out, and counted: the program never takes
those exceptions, and the lift does not model them.

--all-opcodes takes, instead of the program's, every opcode a 68000 has: an image of its own holds each of the
65536 words followed by the same four extension words (EXTENSION_WORDS), and every one that decodes as a valid
instruction is compared, from --states states, the ones the program never uses too.

--c checks the C too: the C the translator writes for each of those instructions (m68000/emit_c.py, on the
game runtime's m68000/ops.h) goes to build/lift_check/cases.c, CMake builds it into build/bin/lift_check_m68k
(konami/reference/lift_check_m68k.c, in the build tree build/msvc), and it runs the same states, to leave what
MAME's core leaves too.
"""
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

CORE = Path(__file__).resolve().parents[2]    # (arcade-decomp, the game's tools/core)
sys.path[:0] = [str(CORE), str(CORE.parent)]
import game_profile  # noqa: E402  (the game's: its root, its ROM set)
from m68000 import analysis  # noqa: E402
from m68000 import disassembler  # noqa: E402
from m68000 import emit_c  # noqa: E402
from m68000 import interpreter  # noqa: E402
from m68000 import lift  # noqa: E402
from v60 import ir  # noqa: E402

ROOT = game_profile.ROOT
rom = game_profile.rom
ORACLE = ROOT / "build" / "bin" / ("oracle_m68k_step.exe" if sys.platform == "win32" else "oracle_m68k_step")
C_CHECK = ROOT / "build" / "bin" / ("lift_check_m68k.exe" if sys.platform == "win32" else "lift_check_m68k")
CMAKE = "C:/Program Files/CMake/bin/cmake.exe" if sys.platform == "win32" else "cmake"
BUILD_TREE = ROOT / "build" / "msvc"

EDGES = (0, 1, 2, 3, 7, 8, 9, 15, 16, 31, 32, 33, 63, 64, 0x7F, 0x80, 0x81, 0xFF, 0x100, 0x7FFF, 0x8000,
         0x8001, 0xFFFF, 0x10000, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF, 0xFFFF8000, 0xFFFFFF80, 0x0000FF00,
         0x99, 0x09, 0x90, 0x9999, 0x59, 0x00010001, 0x12345678)
WORK_RAM = (0x060000, 0x064000)
# Instructions whose result in MAME's core no state can fix: TRAPV tests the core's ALU flags (m_isr) as the
# instruction before it left them, not the status register's V (the lift tests V, as the 68000 manual has it)
UNCOMPARED = {"trapv": "TRAPV, on the ALU flags of the instruction before"}

# --all-opcodes: every opcode, OPCODE_RECORD bytes apart from OPCODE_BASE, followed by these words: a brief
# extension word a 68000 takes (D3.w, $12), then words for the rest (a second extension, an immediate, an
# absolute address, a MOVEM mask, a branch's displacement); every vector of the image leads to VECTOR_TARGET
OPCODE_BASE = 0x1000
OPCODE_RECORD = 10
EXTENSION_WORDS = (0x3012, 0x0056, 0x7078, 0x1234)
VECTOR_TARGET = 0x000400


def opcode_image():
    """(the image, the valid instructions in it by opcode)"""
    image = bytearray(OPCODE_BASE + 0x10000 * OPCODE_RECORD)
    for vector in range(256):
        image[4 * vector:4 * vector + 4] = VECTOR_TARGET.to_bytes(4, "big")
    tail = b"".join(word.to_bytes(2, "big") for word in EXTENSION_WORDS)
    for opcode in range(0x10000):
        where = OPCODE_BASE + opcode * OPCODE_RECORD
        image[where:where + OPCODE_RECORD] = opcode.to_bytes(2, "big") + tail
    image = bytes(image)
    mem = disassembler.FlatMemory(image)
    instructions = {}
    for opcode in range(0x10000):
        instruction = disassembler.decode(mem, OPCODE_BASE + opcode * OPCODE_RECORD)
        if instruction.flow != disassembler.FLOW_INVALID:
            instructions[opcode] = [instruction]
    return image, instructions


def data_value(rng):
    kind = rng.random()
    if kind < 0.35:
        return rng.choice(EDGES)
    if kind < 0.5:
        return rng.randrange(0, 0x40)
    if kind < 0.65:
        return rng.getrandbits(16)
    return rng.getrandbits(32)


def address_value(rng):
    kind = rng.random()
    if kind < 0.7:
        return rng.randrange(*WORK_RAM) & ~1
    if kind < 0.9:
        return rng.getrandbits(24) & ~1
    return rng.getrandbits(32) & ~1


def random_state(rng, pc):
    """(registers as lift.py numbers them, flags, interrupt level)"""
    registers = [data_value(rng) for _ in range(8)] + [address_value(rng) for _ in range(8)]
    registers.append(address_value(rng))                        # the other stack pointer
    system = (lift.SR_S if rng.random() < 0.75 else 0) | rng.randrange(8) << 8
    registers.append(system)
    ccr = rng.randrange(32)
    flags = {name: ccr >> position & 1 for name, position in lift.FLAG_BITS.items()}
    irq = rng.randrange(1, 8) if rng.random() < 0.15 else 0
    return registers, flags, irq


def stack_pointers(registers):
    """(USP, SSP) of a state"""
    if registers[lift.SR_SYSTEM] & lift.SR_S:
        return registers[lift.OTHER_SP], registers[lift.SP]
    return registers[lift.SP], registers[lift.OTHER_SP]


def state_line(pc, registers, flags, irq):
    sr = registers[lift.SR_SYSTEM] | sum(flags[name] << position for name, position in lift.FLAG_BITS.items())
    usp, ssp = stack_pointers(registers)
    values = registers[:15] + [usp, ssp]
    return f"{pc:06X} {sr:04X} {irq:X} " + " ".join(f"{value & 0xFFFFFFFF:08X}" for value in values)


def result_line(machine, pc):
    """The interpreter's result in the oracle's format, without the cycles"""
    usp, ssp = stack_pointers(machine.regs)
    values = machine.regs[:15] + [usp, ssp]
    text = f"{pc & 0xFFFFFF:06X} {machine.sr():04X} " + " ".join(f"{value & 0xFFFFFFFF:08X}" for value in values)
    accesses = "".join(f" {kind}:{address:06X}:{mask:04X}:{data:04X}" for kind, address, mask, data in machine.accesses)
    return text + " |" + accesses


def mame_line(line):
    """MAME's result without the cycles"""
    if line.startswith("STOPPED"):
        return line
    head, _, accesses = line.partition(" |")
    return " ".join(head.split()[:-1]) + " |" + accesses


def case_c(number, lifted):
    """The C function of one lifted instruction for lift_check_m68k: its statements, then the address of the next
    instruction (an exception's handler, when one is taken)"""
    printer = emit_c.Printer()
    lines = []
    for statement in lifted.statements:
        if isinstance(statement, ir.Effect) and statement.name == "exception_if":
            condition, vector, pc = statement.args
            lines.append(f"if ({printer.expr(condition)}) return lift_check_exception({vector.value}u, 0x{pc.value:X}u, 0);")
        elif isinstance(statement, ir.SetTemp) and statement.temp.name in lifted.program_reads:
            # (a PC-relative operand: the oracle does not list the core's program space, so neither does this)
            line = printer.statement(statement)
            lines.append(line.replace(f"mem_read{statement.value.bits}(", f"program_read{statement.value.bits}(", 1))
        else:
            lines.append(printer.statement(statement))
    control = lifted.control
    following = f"0x{lifted.next_address:X}u"
    if control.kind == "next":
        lines.append(f"return {following};")
    elif control.kind == "goto":
        lines.append(f"return 0x{control.target:X}u;")
    elif control.kind == "branch":
        lines.append(f"return ({printer.expr(control.condition)}) ? 0x{control.target:X}u : {following};")
    elif control.kind == "call" and control.target is not None:
        lines.append(f"return 0x{control.target:X}u;")
    else:
        lines.append(f"return {printer.expr(control.address)};")
    return f"static uint32_t case_{number}(void)\n{{\n" + "".join(f"\t{line}\n" for line in lines) + "}\n"


def check_c(lifted_cases, cases, results, image_path, work, show):
    """The translator's C of the same instructions, built and run from the same states; how many states differ"""
    folder = ROOT / "build" / "lift_check"
    folder.mkdir(parents=True, exist_ok=True)
    numbers = {}
    functions = []
    for instruction, lifted in lifted_cases:
        numbers[instruction.address] = len(functions)
        functions.append(case_c(len(functions), lifted))
    text = ("/* Generated by compare_lift_with_mame.py --c from the user's own ROM. Do not redistribute. */\n"
            '#include "m68000/registers.h"\n#include "m68000/ops.h"\n\n'
            "uint32_t lift_check_exception(uint32_t vector, uint32_t pc, int level);\n"
            "uint32_t program_read8(uint32_t address);\nuint32_t program_read16(uint32_t address);\n"
            "uint32_t program_read32(uint32_t address);\n\n"
            + "\n".join(functions)
            + "\ntypedef uint32_t (*lift_case)(void);\nconst lift_case lift_cases[] =\n{\n"
            + "".join(f"\tcase_{number},\n" for number in range(len(functions)))
            + f"}};\nconst size_t lift_case_count = {len(functions)};\n")
    cases_path = folder / "cases.c"
    if not cases_path.exists() or cases_path.read_text(encoding="ascii") != text:
        cases_path.write_text(text, encoding="ascii")
    subprocess.run([CMAKE, "-S", str(ROOT), "-B", str(BUILD_TREE)], check=True, capture_output=True)
    built = subprocess.run([CMAKE, "--build", str(BUILD_TREE), "--config", "Release", "--target", "lift_check_m68k",
                            "--parallel", "6"], capture_output=True, text=True)
    if built.returncode != 0:
        raise SystemExit("lift_check_m68k did not build:\n" + built.stdout[-3000:])
    states_path = work / "lift_c_states.txt"
    results_path = work / "lift_c_results.txt"
    states_path.write_text("".join(f"{numbers[instruction.address]} {line}\n" for instruction, line, _ in cases),
                           encoding="ascii")
    subprocess.run([str(C_CHECK), str(image_path), str(states_path), str(results_path)], check=True)
    ours = results_path.read_text(encoding="ascii").splitlines()
    if len(ours) != len(cases):
        raise SystemExit(f"lift_check_m68k gave {len(ours)} results for {len(cases)} states")
    differences = Counter()
    shown = 0
    for (instruction, state, _), theirs, c_line in zip(cases, results, ours):
        theirs = mame_line(theirs)
        if c_line == theirs:
            continue
        differences[instruction.mnemonic] += 1
        if shown < show:
            shown += 1
            print(f"\n{instruction.address:06X}  {instruction.text} (the C)\n  state: {state}\n  MAME:  {theirs}\n"
                  f"  C:     {c_line}")
    for mnemonic in sorted(differences):
        print(f"  {mnemonic:<8} {differences[mnemonic]} states differ in the C")
    print(f"the C: {len(functions)} instructions, {len(cases)} states compared, {sum(differences.values())} differ")
    return sum(differences.values())


def main():
    arguments = sys.argv[1:]
    check_in_c = "--c" in arguments
    if check_in_c:
        arguments.remove("--c")
    all_opcodes = "--all-opcodes" in arguments
    if all_opcodes:
        arguments.remove("--all-opcodes")
    options = {"--states": 40, "--instances": 3, "--seed": 1, "--show": 12}
    for option in options:
        if option in arguments:
            position = arguments.index(option)
            options[option] = int(arguments[position + 1])
            del arguments[position:position + 2]
    only = set()
    if "--only" in arguments:
        position = arguments.index("--only")
        only = set(arguments[position + 1:])
        del arguments[position:]
    if not ORACLE.exists():
        raise SystemExit(f"{ORACLE} not found")
    source = Path(arguments[0]) if arguments else ROOT / "rom"
    started = time.perf_counter()
    by_opcode = defaultdict(list)
    if all_opcodes:
        image, decoded = opcode_image()
        for opcode, places in decoded.items():
            if not only or places[0].mnemonic in only:
                by_opcode[opcode] = places
        print(f"{len(decoded)} valid opcodes of 65536 ({time.perf_counter() - started:.1f} s)")
    else:
        image = rom.build_program(rom.load_set(source))
        found = analysis.Analysis(image, analysis.load_symbols(ROOT / "symbols.txt"))
        found.run()
        for address in sorted(found.instructions):
            instruction = found.instructions[address]
            if not only or instruction.mnemonic in only:
                by_opcode[found.mem.r16(address)].append(instruction)
        print(f"{len(found.instructions)} instructions, {len(by_opcode)} opcodes ({time.perf_counter() - started:.1f} s)")
    mem = disassembler.FlatMemory(image)

    rng = random.Random(options["--seed"])
    cases = []          # (instruction, state line, expected result)
    lifted_cases = []   # (instruction, its lift)
    unsupported = []
    skipped = Counter()
    for opcode in sorted(by_opcode):
        places = by_opcode[opcode]
        step = max(1, len(places) // options["--instances"])
        for instruction in places[::step][:options["--instances"]]:
            try:
                lifted = lift.lift(instruction)
            except lift.Unsupported as problem:
                unsupported.append((instruction, str(problem)))
                continue
            if instruction.mnemonic in UNCOMPARED:
                skipped[UNCOMPARED[instruction.mnemonic]] += options["--states"]
                continue
            lifted_cases.append((instruction, lifted))
            for _ in range(options["--states"]):
                registers, flags, irq = random_state(rng, instruction.address)
                machine = interpreter.Machine(image, registers, flags, irq)
                try:
                    pc = machine.run(lifted)
                except interpreter.AddressError:
                    skipped["address error"] += 1
                    continue
                except interpreter.Stopped:
                    skipped["stop"] += 1
                    continue
                except interpreter.Traced:
                    skipped["trace bit set"] += 1
                    continue
                cases.append((instruction, state_line(instruction.address, registers, flags, irq),
                              result_line(machine, pc)))

    work = ROOT / "build" / "reference"
    work.mkdir(parents=True, exist_ok=True)
    image_path = work / ("m68k_opcode_records.bin" if all_opcodes else "m68k_program.bin")
    image_path.write_bytes(image)
    states_path = work / "lift_states.txt"
    results_path = work / "lift_results.txt"
    states_path.write_text("".join(line + "\n" for _, line, _ in cases), encoding="ascii")
    subprocess.run([str(ORACLE), str(image_path), str(states_path), str(results_path)], check=True)
    results = results_path.read_text(encoding="ascii").splitlines()
    if len(results) != len(cases):
        raise SystemExit(f"the oracle gave {len(results)} results for {len(cases)} states")

    differences = Counter()
    tried = Counter()
    shown = 0
    for (instruction, state, ours), theirs in zip(cases, results):
        tried[instruction.mnemonic] += 1
        theirs = mame_line(theirs)
        if ours == theirs:
            continue
        differences[instruction.mnemonic] += 1
        if shown < options["--show"]:
            shown += 1
            print(f"\n{instruction.address:06X}  {instruction.text}\n  state: {state}\n  MAME:  {theirs}\n  ours:  {ours}")
    print()
    for mnemonic in sorted(tried):
        if differences[mnemonic]:
            print(f"  {mnemonic:<8} {differences[mnemonic]} of {tried[mnemonic]} states differ")
    for instruction, problem in unsupported[:20]:
        print(f"  not lifted: {instruction.address:06X}  {instruction.text}  ({problem})")
    c_differences = check_c(lifted_cases, cases, results, image_path, work, options["--show"]) if check_in_c else 0
    opcodes = len({mem.r16(instruction.address) for instruction, _, _ in cases})
    print(f"\n{opcodes} opcodes, {len(cases)} states compared, {sum(differences.values())} differ; "
          f"{len(unsupported)} instructions not lifted; left out: "
          + (", ".join(f"{count} ({reason})" for reason, count in skipped.items()) or "none")
          + f" ({time.perf_counter() - started:.1f} s)")
    return 0 if not differences and not unsupported and not c_differences else 1


if __name__ == "__main__":
    sys.exit(main())
