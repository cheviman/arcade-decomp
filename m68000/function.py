# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        function.py
Description: the 68000 program split into functions, and each one as a control-flow graph of IR blocks for the
             decompiler
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Program takes what the static analysis finds (m68000/analysis.py, symbols.txt, no traces) and splits it into
functions as the translation scaffold does (m68000/translate.py): a function starts at each vector's handler, at
each target of a call, at each place a jump or a call through a register can go (the tables of handlers, the
addresses loaded before), at each func or code line of symbols.txt, and where code that two functions run into
is long enough to be written once. Builder lifts every instruction (m68000/lift.py for the data, here for the
control) into the decompiler's blocks (decompiler/function.py), with the 68000's ways of calling and returning
said as calls and returns, as the C has them:
  - BSR and JSR are calls: the return address they push is the call itself; RTS is a return;
  - a jump or call through a register goes to the places the analysis finds the register can hold: a switch on
    the address (each case a tail call), or a call with those choices;
  - code that keeps its return address in a register, with no stack (a game's boot, before it has tested its
    RAM: "lea back, A6; bra test" and "jmp (A6)" at the test's end), is a call and a return too: the routine is a
    function, the code at 'back' goes on in its caller;
  - an exception an instruction may take (a division by zero, CHK) is a call of the vector's handler on that
    condition; the check of a privileged instruction in user mode is left out where the program runs it in
    supervisor mode only (the reset and the interrupt handlers: the profile's SUPERVISOR_CODE);
  - an interrupt handler that saves the registers in fixed memory (movem.l Dn-An, abs) and puts them back before
    it returns gives the interrupted code its registers back: the C has them in its own variables, so those
    two moves are left out; a stretch the profile's INTERRUPT_STACK_SWITCH names, where the handler moves its
    return frame to the user's stack, is left out too (the C has no stack), and RTE and RTR are returns;
  - the profile's FRAME_WAIT, where the main loop finds nothing to do, waits for the next frame before its
    instruction (Effect frame_wait) and takes the interrupts after it (take_interrupts), as the scaffold does;
    after an instruction that writes the status register, the interrupts it lets through are taken too.
The status register's mode bit swaps the stack pointers on the 68000: the C has one stack (the decompiler's
slots), so a write of the status register is the interrupt mask and the flags (write_sr), and USP is never read
but in the stretch left out.
"""
from decompiler.function import Block, Function
from v60 import ir

import game_profile
import recompile

from . import analysis
from . import disassembler as dis
from . import lift

VECTORS = range(1, 48)
FRAME_WAIT = getattr(game_profile, "FRAME_WAIT", None)
# where the interrupt handler moves its return frame to the user's stack ("move USP, A6" ... "move A6, USP"):
# (first, past the last) instruction left out
INTERRUPT_STACK_SWITCH = getattr(game_profile, "INTERRUPT_STACK_SWITCH", None)
# the code the program runs in supervisor mode only (the reset's, the interrupt handlers'): (first, past the last)
# stretches where a privileged instruction needs no check of the mode
SUPERVISOR_CODE = getattr(game_profile, "SUPERVISOR_CODE", ())
REPLACED_CODE = getattr(game_profile, "REPLACED_CODE", {})
# the devices that do something when read (TMNT's K051960 in its ROM readback mode): a read there stays, its value
# used or not
EFFECT_READ_AREAS = list(getattr(game_profile, "EFFECT_READ_AREAS", ()))
MEMORY_MODES = ("indirect", "disp", "index", "postinc", "predec")
SHIFTS = ("asl", "asr", "lsl", "lsr")


def key(address):
    return address & 0xFFFFFF


class Found(analysis.Analysis):
    """The static analysis, keeping what each jump or call through a register can reach"""

    def reset(self):
        super().reset()
        self.computed_targets = set()
        self.computed_values = {}       # {address of the jump or call: what the register can hold}
        self.in_computed = False
        self.effect_reads = set()       # instructions that may read a device that does something when read

    def note_store(self, instruction, state):
        super().note_store(instruction, state)
        # a read through an address register that can hold a place in a device that does something when read
        for operand in instruction.operands[:1] if instruction.mnemonic not in ("cmpm", "addx", "subx", "abcd", "sbcd")                 else instruction.operands:
            if operand.mode in MEMORY_MODES and any(
                    kind in (analysis.CONSTANT, analysis.CURSOR) and any(start <= value < end for start, end in EFFECT_READ_AREAS)
                    for kind, value in analysis.known(state.get(operand.reg))):
                self.effect_reads.add(instruction.address)

    def computed(self, instruction, state, block):
        operand = instruction.operands[0]
        values = state.get(operand.reg) if operand.mode in ("indirect", "disp") else None
        found = self.computed_values.setdefault(instruction.address, set())
        found.update(analysis.known(values))
        self.in_computed = True
        try:
            super().computed(instruction, state, block)
        finally:
            self.in_computed = False

    def enter(self, address, state=None, kind=None):
        if self.in_computed:
            self.computed_targets.add(address & 0xFFFFFF)
        super().enter(address, state, kind)

    def targets(self, address):
        """The code a jump or call through a register at 'address' can reach (sorted), from what the register can
        hold: addresses, entries of tables of code, entries of tables of tables, fields of tables of records"""
        instruction = self.instructions[address]
        operand = instruction.operands[0]
        offset = operand.disp if operand.mode == "disp" else 0
        found = set()
        for kind, value in self.computed_values.get(address, ()):
            if kind == analysis.CONSTANT:
                if key(value + offset) in self.instructions:
                    found.add(key(value + offset))
            elif kind == analysis.ENTRY and offset == 0:
                found.update(self.code_tables.get(value, ()))
            elif kind == analysis.ENTRY2 and offset == 0:
                for table in self.table_tables.get(value, ()):
                    found.update(self.code_tables.get(table, ()))
            elif kind == analysis.FIELD and offset == 0:
                found.update(self.field_tables.get(value, ()))
        return sorted(target for target in found if target in self.instructions)


class Program(recompile.Program):
    """The analysed 68000 program, split into functions for the decompiler"""

    def __init__(self, image, symbols):
        self.image = image
        self.mem = dis.FlatMemory(image)
        self.symbols = symbols
        self.analysis = Found(image, symbols)
        self.analysis.run()
        # (what the decompiler's data flow asks of the V60's analysis: no stores of code addresses into slots
        # it follows, no tables of calls; the 68000's calls through registers carry their choices)
        self.analysis.pointer_writes = []
        self.analysis.table_writes = []
        self.analysis.pointer_tables = {}
        self.analysis.table_targets = {}
        instructions = self.analysis.instructions
        self.handlers = [key(self.mem.r32(4 * vector)) for vector in VECTORS]
        self.find_register_returns()

        entries = set(self.analysis.functions) | self.analysis.computed_targets
        entries.update(self.handlers)
        for table in self.analysis.code_tables.values():
            entries.update(table)
        for table in self.analysis.field_tables.values():
            entries.update(table)
        entries.update(address for address, symbol in symbols.items() if symbol.kind in ("func", "code"))
        entries -= set(self.return_points)
        entries |= set(self.register_routines)
        self.entries = {address for address in entries if address in instructions}

        self.bodies = {entry: self.walk(entry) for entry in sorted(self.entries)}
        covered = set().union(*self.bodies.values())
        self.orphans = sorted(address for address in instructions if address not in covered)
        while True:
            covered = set().union(*self.bodies.values())
            starts = [address for address in self.orphans if address not in covered]
            if not starts:
                break
            self.entries.add(starts[0])
            self.bodies[starts[0]] = self.walk(starts[0])
        self.split_shared_code()
        self.names = self.choose_names()
        self.unreached = {address for address, symbol in symbols.items()
                          if symbol.kind == "func" and symbol.name.startswith("unreached_") and address in self.entries}
        self.second_entries = {}
        self.inner_entries = {}

    def find_register_returns(self):
        """The routines that return through a register, and the calls of them: "lea back, An" then, a few
        instructions on that leave An alone, "bra routine" (or jmp), where the routine ends with "jmp (An)" to
        back. self.register_calls: {address of the bra: (routine, back, register)}; self.return_points: the
        backs; self.register_routines: {routine: register}; self.register_returns: the jmp (An)."""
        instructions = self.analysis.instructions
        self.register_calls = {}
        self.register_routines = {}
        self.register_returns = set()
        self.return_points = set()
        backs = {}
        for address, instruction in instructions.items():
            if instruction.mnemonic != "jmp" or instruction.target is not None \
                    or instruction.operands[0].mode != "indirect":
                continue
            values = self.analysis.computed_values.get(address, ())
            constants = {key(value) for kind, value in values if kind == analysis.CONSTANT}
            if not constants or not constants <= set(instructions):
                continue
            backs[address] = (instruction.operands[0].reg, constants)
        wanted = {}
        for jump, (register, constants) in backs.items():
            for back in constants:
                wanted.setdefault((back, register), []).append(jump)
        for address, instruction in instructions.items():
            if instruction.mnemonic not in ("lea", "movea") or len(instruction.operands) != 2:
                continue
            source, destination = instruction.operands
            if destination.mode != "areg" or source.mode not in ("abs_short", "abs_long", "pc_disp", "immediate"):
                continue
            back = key(source.value)
            jumps = wanted.get((back, destination.reg))
            if not jumps:
                continue
            # on to the jump into the routine, past instructions that leave the register alone
            where = address + instruction.length
            for _ in range(4):
                following = instructions.get(where)
                if following is None:
                    break
                if following.mnemonic in ("bra", "jmp") and following.target is not None:
                    routine = key(following.target)
                    self.register_calls[where] = (routine, back, destination.reg)
                    self.register_routines[routine] = destination.reg
                    self.return_points.add(back)
                    break
                if following.flow != dis.FLOW_NORMAL or destination.reg in analysis.writes(following):
                    break
                where += following.length
        for jump, (register, constants) in backs.items():
            if constants <= self.return_points:
                self.register_returns.add(jump)

    def successors(self, instruction):
        """Addresses execution can go on at inside the same function"""
        flow = instruction.flow
        address = instruction.address
        if address in self.register_calls:
            return [self.register_calls[address][1]]
        if address in self.register_returns:
            return []
        following = []
        if flow in (dis.FLOW_NORMAL, dis.FLOW_BRANCH, dis.FLOW_CALL):
            following.append(key(address + instruction.length))
        if flow in (dis.FLOW_BRANCH, dis.FLOW_JUMP) and instruction.target is not None:
            following.append(key(instruction.target))
        return following

    def choose_names(self):
        named = {address: symbol.name for address, symbol in self.symbols.items() if symbol.kind in ("func", "code")}
        return {entry: named.get(entry, f"func_{entry:06X}") for entry in self.entries}


class Builder:
    """Lifts the functions of a Program"""

    def __init__(self, program):
        self.program = program
        self.analysis = program.analysis
        self.mem = program.mem
        self.entries = program.entries
        self.handlers = set(program.handlers)
        self.saved = self.saved_registers()

    def saved_registers(self):
        """The interrupt handlers' moves of every register to fixed memory and back (movem.l regs, abs at the
        start; movem.l abs, regs before the return): {address of either move}"""
        found = set()
        for handler in self.handlers:
            body = self.program.bodies.get(handler)
            first = body.get(handler) if body else None
            if first is None or first.mnemonic != "movem" or first.operands[0].mode != "reglist" \
                    or first.operands[1].mode not in ("abs_short", "abs_long"):
                continue
            place = first.operands[1].value
            back = [address for address, instruction in body.items() if instruction.mnemonic == "movem"
                    and instruction.operands[0].mode in ("abs_short", "abs_long") and instruction.operands[0].value == place
                    and instruction.operands[1].registers == first.operands[0].registers]
            if back:
                found |= {handler, *back}
        return found

    def build(self, entry):
        body = self.program.bodies[entry]
        function = Function(entry, self.program.names[entry])
        self.extra_blocks = []
        leaders = self.leaders(entry, body)
        order = sorted(body)
        current = None
        for position, address in enumerate(order):
            instruction = body[address]
            if current is not None and address in leaders:
                if current.end is None:
                    current.end = ir.Goto(address)
                current = None
            if current is None:
                current = Block(address)
                function.blocks[address] = current
            current.addresses.append(address)
            before, extra = len(current.statements), len(self.extra_blocks)
            ended = self.lift(function, current, instruction)
            for statement in current.statements[before:]:
                statement.origin = address
            for block in self.extra_blocks[extra:]:
                for statement in block.statements:
                    statement.origin = address
                if block.end is not None and not hasattr(block.end, "origin"):
                    block.end.origin = address
            if ended is not None:
                # the instruction went on in a block of its own (after an exception's check)
                current = ended
            if current.end is not None and not hasattr(current.end, "origin"):
                current.end.origin = address
            following = key(address + instruction.length)
            if current.end is None and (position + 1 >= len(order) or order[position + 1] != following):
                current.end = self.transfer(entry, following)
                current.end.origin = address
            if current.end is not None:
                current = None
        for block in self.extra_blocks:
            function.blocks[block.label] = block
        self.close_transfers(function)
        # (the code after a stack the C cannot follow, which nothing else in the function reaches, goes)
        reached, work = set(), [entry]
        while work:
            label = work.pop()
            if label not in reached:
                reached.add(label)
                work += function.blocks[label].successors()
        function.blocks = {entry: function.blocks[entry],
                           **{label: block for label, block in function.blocks.items()
                              if label != entry and label in reached}}
        return function

    def leaders(self, entry, body):
        leaders = {entry}
        for address, instruction in body.items():
            following = key(address + instruction.length)
            for target in self.program.successors(instruction):
                if target != following:
                    leaders.add(target)
            if instruction.flow in (dis.FLOW_BRANCH, dis.FLOW_JUMP, dis.FLOW_RETURN) \
                    or address in self.program.register_calls:
                leaders.add(following)
            if instruction.mnemonic in ("divu", "divs", "chk", "trap", "trapv") or address == FRAME_WAIT:
                leaders.add(following)    # (the exception's call goes on there)
        return leaders & set(body)

    def lost_stack(self, instruction, body):
        """True for an instruction after which the C cannot follow the stack, in code no path reaches: A7 set to a
        value read from memory or worked out (not a place of the stack itself nor a constant), or the function's
        own return address popped into the register a jump with no known target goes through (a routine that
        hands its results back on the stack: 0021E6), or a frame made with LINK"""
        operands = instruction.operands
        if instruction.mnemonic == "link":
            return True    # (a frame through a register the stack's slots do not follow: 00218A's)
        if len(operands) != 2 or operands[1].mode != "areg":
            return False
        source, destination = operands
        if destination.reg == "A7" and instruction.mnemonic in ("movea", "move")                 and source.mode not in ("immediate", "abs_short", "abs_long"):
            return True
        if instruction.mnemonic == "movea" and source.mode == "postinc" and source.reg == "A7":
            return any(other.mnemonic == "jmp" and other.operands[0].mode == "indirect"
                       and other.operands[0].reg == destination.reg and not self.analysis.targets(other.address)
                       for other in body.values())
        return False

    def transfer(self, entry, target):
        """How execution gets to 'target': a jump in this function, or into another one"""
        target = key(target)
        if target != entry and target in self.entries:
            return ir.TailCall(target)
        return ir.Goto(target)

    def close_transfers(self, function):
        """Branch and switch targets in other functions get a block of their own with the tail call"""
        def resolve(target, origin):
            if target in function.blocks:
                return target
            if target in self.entries:
                label = ("tail", target)
                if label not in function.blocks:
                    function.blocks[label] = Block(label, end=ir.TailCall(target))
                    function.blocks[label].end.origin = origin
                return label
            raise lift.Unsupported(f"function {function.entry:06X}: no code at {target:06X}")

        for block in list(function.blocks.values()):
            end = block.end
            origin = block.addresses[-1] if block.addresses else getattr(end, "origin", None)
            if isinstance(end, ir.Goto):
                end.target = resolve(end.target, origin)
            elif isinstance(end, ir.Branch):
                end.target = resolve(end.target, origin)
                end.otherwise = resolve(end.otherwise, origin)
            elif isinstance(end, ir.Switch):
                end.cases = {case: resolve(target, origin) for case, target in end.cases.items()}

    # ---- one instruction ----

    def lift(self, function, block, instruction):
        """Lifts 'instruction' into 'block' (its end when it changes the flow). Returns the block the code goes
        on in when the instruction split it (an exception's check), else None."""
        address = instruction.address
        mnemonic = instruction.mnemonic
        following = key(address + instruction.length)
        statements = block.statements
        if address in self.saved:
            return None    # (the interrupted code's registers, which the C keeps in its own variables)
        if INTERRUPT_STACK_SWITCH and INTERRUPT_STACK_SWITCH[0] <= address < INTERRUPT_STACK_SWITCH[1]:
            return None    # (the return frame moved to the user's stack: the C has no stack)
        if address == FRAME_WAIT:
            statements.append(ir.Effect("frame_wait", ()))

        if self.lost_stack(instruction, self.program.bodies[function.entry]):
            # (the program would go on with a stack the C does not have: it stops)
            statements.append(ir.Effect("lost_stack", (ir.const(address),)))
            block.end = ir.Return()
            return None
        if address in self.program.register_calls:
            routine, back, _ = self.program.register_calls[address]
            statements.append(ir.Call(target=routine, return_address=back))
            block.end = ir.Goto(back)
            return None
        if address in self.program.register_returns:
            block.end = ir.Return()
            return None
        if mnemonic == "bra" or (mnemonic == "jmp" and instruction.target is not None):
            block.end = self.transfer(function.entry, instruction.target)
            return None
        if mnemonic == "bcc":
            block.end = ir.Branch(lift.condition_of(instruction.condition), key(instruction.target), following)
            return None
        if mnemonic in ("bsr", "jsr"):
            if instruction.target is not None:
                statements.append(ir.Call(target=key(instruction.target), return_address=following))
                return None
            lifter = lift.Lifter(instruction, prefix=f"t{address:06X}_")
            target = lifter.bound(lifter.address_only(instruction.operands[0], "target"), "target")
            statements += lifter.statements
            choices = tuple(self.analysis.targets(address))
            if not choices:
                raise lift.Unsupported(f"{address:06X}: {instruction.text} goes to no code the analysis finds")
            statements.append(ir.Call(address=target, choices=choices, return_address=following))
            return None
        if mnemonic == "jmp":
            lifter = lift.Lifter(instruction, prefix=f"t{address:06X}_")
            target = lifter.address_only(instruction.operands[0], "target")
            statements += lifter.statements
            choices = self.analysis.targets(address)
            # (none: code no path reaches, after a stack the C cannot follow; the C writer refuses one it reaches)
            block.end = ir.Switch(target, {choice: choice for choice in choices}) if choices else ir.ComputedJump(target)
            return None
        if mnemonic in ("rts", "rtr", "rte"):
            if mnemonic == "rte":
                statements.append(ir.Effect("return_from_exception", ()))
            block.end = ir.Return()
            return None
        if mnemonic == "movem" and self.stack_movem(statements, instruction):
            return None
        if mnemonic == "dbcc":
            return self.decrement_and_branch(block, instruction)

        lifter = lift.Lifter(instruction, prefix=f"t{address:06X}_")
        try:
            lift.lift_instruction(lifter, instruction)
        except lift.Unsupported as problem:
            raise lift.Unsupported(f"{address:06X}: {problem}") from problem
        control = lifter.control
        lifted = [self.shift(statement) for statement in lifter.statements]
        if address in self.analysis.effect_reads:
            # (the device's reads stay, their values used or not: Effect read keeps each one)
            lifted += [ir.Effect("read", (statement.temp,)) for statement in lifted
                       if isinstance(statement, ir.SetTemp) and isinstance(statement.value, ir.Load)]
        ended = None
        writes_sr = False
        exception = None    # (condition, handler, where it goes on) of the exception the instruction may take
        for position, statement in enumerate(lifted):
            if isinstance(statement, ir.Effect) and statement.name == "exception_if":
                condition, vector, pc = statement.args
                if vector.value == lift.VECTOR_PRIVILEGE and self.supervisor(address):
                    continue    # (run in supervisor mode only)
                if isinstance(condition, ir.Const) and not condition.value:
                    continue
                if exception is not None:
                    raise lift.Unsupported(f"{address:06X}: {instruction.text} takes two exceptions")
                exception = (condition, key(self.mem.r32(4 * vector.value)), key(pc.value))
                continue
            if exception is not None:
                # what the instruction does after the check happens only when the exception is not taken: the
                # registers and flags it writes keep their values when it is (the same block: the instruction's
                # temporaries are its own)
                taken = exception[0]
                if isinstance(statement, ir.SetReg):
                    statement = ir.SetReg(statement.index, ir.op("select", (taken, ir.Reg(statement.index),
                                                                          statement.value), 32))
                elif isinstance(statement, ir.SetFlag):
                    statement = ir.SetFlag(statement.name, ir.op("select", (taken, ir.Flag(statement.name),
                                                                          statement.value), 1))
                elif not isinstance(statement, ir.SetTemp):
                    raise lift.Unsupported(f"{address:06X}: {instruction.text} writes memory after its exception")
            if isinstance(statement, ir.Effect) and statement.name == "write_sr":
                writes_sr = True
                value = statement.args[0]
                block.statements.append(statement)
                for name in lift.FLAGS:
                    block.statements.append(ir.SetFlag(name, ir.bit(value, lift.FLAG_BITS[name])))
                continue
            block.statements.append(statement)
        if exception is not None:
            # the exception: its handler runs, then the code goes on after the instruction
            condition, handler, resume = exception
            taken = ("exception", address)
            self.extra_blocks.append(Block(taken, [ir.Call(target=handler, return_address=resume)], ir.Goto(resume)))
            if control.kind != "next":
                raise lift.Unsupported(f"{address:06X}: {instruction.text} takes an exception and changes the flow")
            block.end = ir.Branch(condition, taken, following)
            return None
        target_block = ended or block
        if control.kind == "branch":
            # DBcc
            target_block.end = ir.Branch(control.condition, key(control.target), following)
        elif control.kind != "next":
            raise lift.Unsupported(f"{address:06X}: {instruction.text}")
        elif writes_sr or address == FRAME_WAIT:
            target_block.statements.append(ir.Effect("take_interrupts", ()))
        if ended is not None and ended.end is None:
            ended.end = ir.Goto(following)
        return ended

    def decrement_and_branch(self, block, instruction):
        """DBcc: when the condition holds, on to the next instruction; else the counter's low word goes down by one,
        and the loop goes on to the target unless it went past 0. DBRA (DBF) only counts. Returns the block the
        counting is in when the condition needed one of its own."""
        address = instruction.address
        following = key(address + instruction.length)
        counter = lift.register(instruction.operands[0].reg)
        counting = block
        if instruction.condition != "f":
            counting = Block(("count", address))
            self.extra_blocks.append(counting)
            block.end = ir.Branch(lift.condition_of(instruction.condition), following, counting.label)
        low = ir.Temp(f"t{address:06X}_1_counter", 16)
        counting.statements.append(ir.SetTemp(low, ir.op("sub", (ir.trunc(ir.Reg(counter), 16), lift.const(1, 16)), 16)))
        kept = ir.op("and", (ir.Reg(counter), lift.const(0xFFFF0000)), 32)
        counting.statements.append(ir.SetReg(counter, ir.op("or", (kept, ir.zext(low, 32)), 32)))
        counting.end = ir.Branch(ir.op("ne", (low, lift.const(0xFFFF, 16)), 1), key(instruction.target), following)
        return None

    def supervisor(self, address):
        return any(first <= address < stop for first, stop in SUPERVISOR_CODE)

    def stack_movem(self, statements, instruction):
        """MOVEM of registers to or from the stack (-(A7), (A7)+): longs and words in the order the registers
        are, the stack pointer moved once, as the decompiler's slots read them (the bus's order of the halves
        of a long does not matter to the stack, which the C does not have). False for any other MOVEM."""
        first, second = instruction.operands
        size = 4 if instruction.size == "l" else 2
        bits = size * 8
        if first.mode == "reglist" and second.mode == "predec" and second.reg == "A7":
            registers = [lift.register(name) for name in first.registers]
            for position, index in enumerate(registers):
                place = ir.op("sub", (ir.Reg(lift.SP), lift.const(size * (len(registers) - position))), 32)
                statements.append(ir.Store(place, ir.trunc(ir.Reg(index), bits), bits))
            statements.append(ir.SetReg(lift.SP, ir.op("sub", (ir.Reg(lift.SP), lift.const(size * len(registers))), 32)))
            return True
        if second.mode == "reglist" and first.mode == "postinc" and first.reg == "A7":
            registers = [lift.register(name) for name in second.registers]
            temps = []
            for position, index in enumerate(registers):
                place = ir.op("add", (ir.Reg(lift.SP), lift.const(size * position)), 32) if position else ir.Reg(lift.SP)
                temp = ir.Temp(f"t{instruction.address:06X}_{position + 1}_saved", bits)
                statements.append(ir.SetTemp(temp, ir.Load(place, bits)))
                temps.append((index, temp))
            for index, temp in temps:
                statements.append(ir.SetReg(index, ir.sext(temp, 32)))
            statements.append(ir.SetReg(lift.SP, ir.op("add", (ir.Reg(lift.SP), lift.const(size * len(registers))), 32)))
            return True
        return False

    @staticmethod
    def shift(statement):
        """A shift by a constant count (LSL, LSR, ASL, ASR #n) as the decompiler's operators: shl, shr, sar, and
        the bit shifted out last as the carry; a constant widened or cut, the constant it is"""
        def change(node):
            if isinstance(node, ir.Op) and node.name in ("sext", "zext", "trunc") and isinstance(node.args[0], ir.Const):
                value = node.args[0]
                if node.name == "sext" and value.value >> (value.bits - 1) & 1:
                    return ir.const(value.value | (ir.MASKS[node.bits] & ~ir.MASKS[value.bits]), node.bits)
                return ir.const(value.value, node.bits)
            if not (isinstance(node, ir.Op) and node.name.startswith("helper:")):
                return None
            name = node.name[7:]
            base, _, part = name.partition("_")
            if base not in SHIFTS or not isinstance(node.args[1], ir.Const) or not node.args[1].value:
                return None
            value, count = node.args[0], node.args[1].value
            bits = value.bits
            if count > bits:
                return None
            if part == "":
                if count == bits:
                    return None
                operation = {"lsl": "shl", "asl": "shl", "lsr": "shr", "asr": "sar"}[base]
                return ir.op(operation, (value, ir.const(count, 8)), bits)
            if part == "cy":
                if base in ("lsl", "asl"):
                    return ir.bit(value, bits - count)
                return ir.bit(value, count - 1)
            return None
        ir.map_statement(statement, change)
        return statement
