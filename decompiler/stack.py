"""
Name:        stack.py
Description: follows the V60 stack pointer through each function and turns stack memory into slots
Version:     0.1.0
Created:     11-10-2026
Modified:    24-11-2026
Author:      Sebastian Toyos

The game passes nothing on the stack: it saves registers there (PUSH, PUSHM) and the V60 keeps return
addresses there. Knowing how far SP is from where it stood when the function started, every push,
pop and [SP] access becomes a numbered slot (ir.Stack / ir.SetStack) that data flow treats like a
register, and SP itself drops out. Slot 0 holds the caller's return address. A call leaves SP where
it was. Code that sets SP to an address of its own (boot) starts a new stack there. A function that
drops its own return address before returning (add.w #4, SP; rsr) returns from its caller as well:
link_caller_exits makes its callers return too.
"""
import copy

from v60 import ir

from . import cpu

NEW_STACK = -0x100000    # slot offsets after code sets SP itself: far from anything the caller's stack holds
# a block that starts by setting SP to an address of its own takes any SP in: ways with SP in different places
# may meet there (The Simpsons' Z80, whose failed self test loops back to its LD SP); off by default, where such
# ways are an error (decompile_sound.py sets it from the profile's SOUND_STACK_RESTARTS)
RESTARTS = False
MEMORY_SP = 32           # the register SP becomes in a function whose stack stays in memory (the V60; the 68000's
                         # to_memory())
# the registers a frame may go through (the 68000's LINK An: An = SP, the frame's places An+n, UNLK gives SP back):
# one set to a stack address is followed as that offset, its loads and stores are slots; empty (the V60, the Z80,
# Konami's) such a register is not followed (m68000/decompile.py sets it from the profile's LINK_FRAMES)
FRAME_REGISTERS = frozenset()
# the stack's temporaries are followed where memory is read and written too, not only where a stack address is
# computed (the 68000's pushes and "($n,A7)" reads go through one); off, as before (decompiler/stackargs.py)
STACK_ARGUMENTS = False
# a function whose stack addresses serve as values (a buffer's address handed to a callee or walked with an address
# register: the marker the C writer refuses) keeps its slots for the data flow and is marked memory_frame, for
# to_memory() to put its frame in memory once the slots have served (m68000/decompile.py sets it from the profile's
# MEMORY_STACK; off, as before)
ADDRESSES_IN_MEMORY = False
LEFT_CALLER = 33         # a register the V60 does not have: set when the function just called returned for
                         # its caller too (link_caller_exits)


class StackError(Exception):
    pass


def offset_of(expression, sp, temps=None, frames=None):
    """The stack offset 'expression' computes when SP is 'sp' bytes from the start, or None.
    'temps' maps temporaries already known to hold stack addresses to their offsets, 'frames' the frame
    registers (FRAME_REGISTERS) that hold one."""
    bits = cpu.current.address_bits
    if isinstance(expression, ir.Reg) and expression.index == cpu.current.sp:
        return sp
    if isinstance(expression, ir.Reg) and frames and expression.index in frames:
        return frames[expression.index]
    if isinstance(expression, ir.Temp) and temps:
        return temps.get(expression.name)
    if isinstance(expression, ir.Op) and expression.name in ("add", "sub") and expression.bits == bits:
        base, amount = expression.args
        if isinstance(amount, ir.Const):
            inner = offset_of(base, sp, temps, frames)
            if inner is not None:
                delta = amount.value if amount.value < 1 << (bits - 1) else amount.value - (1 << bits)
                return inner + delta if expression.name == "add" else inner - delta
    return None


def redirect(end, old, new):
    """Sends the ways of a block end that go to 'old' to 'new' instead."""
    if isinstance(end, ir.Goto) and end.target == old:
        end.target = new
    elif isinstance(end, ir.Branch):
        end.target = new if end.target == old else end.target
        end.otherwise = new if end.otherwise == old else end.otherwise
    elif isinstance(end, ir.Switch):
        end.cases = {case: new if target == old else target for case, target in end.cases.items()}


def slot_reader(sp, frames=None, temps=None):
    """A rebuild() change that turns reads of stack memory into slots."""
    def change(node):
        if isinstance(node, ir.Load):
            offset = offset_of(node.address, sp, temps, frames)
            if offset is not None:
                return ir.Stack(offset, node.bits)
        return None
    return change


def mentions_sp(expression):
    return any(isinstance(node, ir.Reg) and node.index == cpu.current.sp for node in ir.walk(expression))


def analyse(function):
    """Rewrites 'function' in place; returns the SP offset at the start of each block. A function whose
    stack addresses serve as values (an address into the frame handed on, SP set to a register that walked
    the frame) keeps its stack in memory instead: SP becomes MEMORY_SP, a register the whole program
    shares, and pushes, pops and [SP] accesses go to memory, as on the V60 (code the game never calls is
    all that does it)."""
    if MEMORY_SP not in cpu.current.registers:
        return slots(function)
    if ADDRESSES_IN_MEMORY:
        before = copy.deepcopy(function.blocks)
        try:
            found = slots(function)
        except StackError:
            # (SP the slots cannot follow: the whole stack in memory, from the start)
            function.blocks = before
            return whole(function)
        function.memory_frame = addresses_left(function)
        return found
    saved = copy.deepcopy(function.blocks)
    try:
        found = slots(function)
        if not leftovers(function):
            return found
    except StackError:
        pass
    function.blocks = saved
    function.exits_caller = False
    function.memory_stack = True

    def change(node):
        if isinstance(node, ir.Reg) and node.index == cpu.current.sp:
            return ir.Reg(MEMORY_SP, node.bits)
        return None
    for block in function.blocks.values():
        for index, statement in enumerate(block.statements):
            if isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
                block.statements[index] = statement = ir.SetReg(MEMORY_SP, statement.value)
            ir.map_statement(statement, change)
        ir.map_statement(block.end, change)
    return {label: 0 for label in function.blocks}


def slots(function):
    """The stack as slots: see analyse()."""
    function.exits_caller = False
    function.deepest = 0    # (the lowest SP the function's own stack reaches: to_memory())
    stacks = 0
    restarts = {label for label, block in function.blocks.items() if sets_sp_first(block)} if RESTARTS else set()
    start = {function.entry: 0}
    start_frames = {function.entry: {}}
    originals = {label: list(block.statements) for label, block in function.blocks.items()}
    pending = [function.entry]
    done = set()
    while pending:
        label = pending.pop()
        if label in done:
            continue
        done.add(label)
        block = function.blocks[label]
        sp = start[label]
        frames = dict(start_frames[label])
        statements = []
        # the frame registers where each statement reads them: a read before the register became a frame (LINK's
        # push of the caller's A6) is the register's value, not the frame's address
        frames_at = []
        temps = {}
        known = temps if STACK_ARGUMENTS else None    # (the temporaries the 68000's lift reaches memory through)
        for statement in block.statements:
            if isinstance(statement, ir.SetTemp):
                offset = offset_of(statement.value, sp, temps, frames)
                if offset is not None:
                    # a stack address held for a moment: followed as an offset, the temporary goes away
                    temps[statement.temp.name] = offset
                    continue
            if isinstance(statement, ir.SetReg) and statement.index in FRAME_REGISTERS:
                offset = offset_of(statement.value, sp, temps, frames)
                if offset is not None:
                    frames[statement.index] = offset    # (a frame: its places are slots, the register drops out)
                    continue
                ir.map_statement(statement, slot_reader(sp, frames, known))
                statements.append(statement)
                frames_at.append(dict(frames))
                frames.pop(statement.index, None)
                continue
            if isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
                new = offset_of(statement.value, sp, temps, frames)
                if new is None:
                    if not isinstance(statement.value, ir.Const):
                        raise StackError(f"{function.name}: SP set to {statement.value}")
                    stacks += 1    # the code's own stack: a fresh range of slots
                    new = NEW_STACK * stacks
                elif new > NEW_STACK // 2:
                    function.deepest = min(function.deepest, new)
                sp = new
                continue
            if isinstance(statement, ir.Effect) and statement.name == "return_from_interrupt":
                # RETIS to code the function pushed: the PC it pops is the jump, the PSW takes effect
                frame = statement.args[0]
                if not isinstance(frame, ir.Const):
                    raise StackError(f"{function.name}: RETIS with a computed frame")
                statements.append(ir.Effect("write_psw", (ir.Stack(sp + 4),)))
                frames_at.append(dict(frames))
                sp += 8 + frame.value
                continue
            if isinstance(statement, ir.Store):
                offset = offset_of(statement.address, sp, known, frames)
                if offset is not None:
                    value = ir.rebuild(statement.value, slot_reader(sp, frames, known))
                    slot = ir.SetStack(offset, value, statement.bits)
                    if hasattr(statement, "origin"):
                        slot.origin = statement.origin    # the instruction that stores it ("local" lines)
                    statements.append(slot)
                    frames_at.append(dict(frames))
                    continue
            if STACK_ARGUMENTS and isinstance(statement, ir.Call):
                statement.sp = sp    # where the stack stood at the call: its arguments are the slots from here up
            ir.map_statement(statement, slot_reader(sp, frames, known))
            statements.append(statement)
            frames_at.append(dict(frames))
        block.statements = statements
        if STACK_ARGUMENTS and isinstance(block.end, ir.TailCall):
            block.end.sp = sp
        ir.map_statement(block.end, slot_reader(sp, frames, known))
        # a stack address that still serves as a value (the flags of "add.w #4, SP") has none in C: it
        # becomes a marker the C writer refuses, so it has to turn out unused
        def opaque(frames):
            def change(node):
                if isinstance(node, ir.Temp) and node.name in temps:
                    return ir.op("stack_address", (ir.const(temps[node.name] & 0xFFFFFFFF),), cpu.current.address_bits)
                if isinstance(node, ir.Reg) and node.index in frames:
                    return ir.op("stack_address", (ir.const(frames[node.index] & 0xFFFFFFFF),), cpu.current.address_bits)
                return None
            return change
        for statement, where in zip(statements + [block.end], frames_at + [frames]):
            ir.map_statement(statement, opaque(where))
        if isinstance(block.end, ir.Return) and sp == cpu.current.return_address_bytes:
            function.exits_caller = True
            block.end.leaves_caller = True
        elif isinstance(block.end, (ir.Return, ir.InterruptReturn)) and sp != 0 and sp != NEW_STACK * stacks:
            # (code that set up a stack of its own may hand over to its caller with that stack in place: the
            # Z80's reset gives the board its main loop)
            raise StackError(f"{function.name}: returns with SP {sp:+d} from where it started")
        for successor in block.successors():
            if successor in start and start[successor] != sp and successor in restarts:
                continue
            if successor in start and start[successor] != sp and frames                     and start_frames.get(successor) == frames and resets_sp_from_frame(function, successor, frames, originals):
                continue    # (SP where it is does not matter: the frame gives it back before anything uses it)
            if successor in start_frames and start_frames[successor] != frames:
                raise StackError(f"{function.name}: frames {start_frames[successor]} and {frames} meet at {successor}")
            start_frames[successor] = dict(frames)
            if successor in start and start[successor] != sp:
                target = function.blocks[successor]
                if target.statements or not isinstance(target.end, ir.Return):
                    raise StackError(f"{function.name}: SP {start[successor]} and {sp} meet at {successor}")
                # a return two ways share with SP in different places (0015C0: one has dropped its own
                # return address, so it returns for its caller too): each way gets a return of its own
                clone = ("return", successor, sp)
                if clone not in function.blocks:
                    function.blocks[clone] = type(target)(clone, [], ir.Return(), list(target.addresses))
                    function.blocks[clone].end.origin = getattr(target.end, "origin", None)
                redirect(block.end, successor, clone)
                successor = clone
                if successor in start:
                    continue
            start[successor] = sp
            pending.append(successor)
    return start


def resets_sp_from_frame(function, label, frames, originals):
    """True when the code from block 'label' on sets SP from a frame register (UNLK) before anything reads SP or
    calls: the compiler's epilogue after calls whose arguments it never popped (0F7804's movem (-$18,A6), unlk)"""
    seen = set()
    while label not in seen and label in function.blocks:
        seen.add(label)
        block = function.blocks[label]
        for statement in originals.get(label, block.statements):
            if isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
                return offset_of(statement.value, 0, None, frames) is not None and not any(
                    isinstance(node, ir.Reg) and node.index == cpu.current.sp for node in ir.walk(statement.value))
            found = []

            def look(node):
                if isinstance(node, ir.Reg) and node.index == cpu.current.sp:
                    found.append(node)
                return None
            ir.map_statement(statement, look)
            if isinstance(statement, ir.Call) or found:
                return False
        if not isinstance(block.end, ir.Goto):
            return False
        label = block.end.target
    return False


def sets_sp_first(block):
    """True when the block sets SP to a constant before anything in it reads SP (RESTARTS)."""
    for statement in block.statements:
        if isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
            return isinstance(statement.value, ir.Const)
        if any(mentions_sp(expression) for expression in ir.statement_expressions(statement)):
            return False
    return False


def link_caller_exits(functions):
    """A function that drops its own return address and returns (exits_caller: "add.w #4, SP; rsr", 001544)
    goes back past its caller, to the caller's caller; so does one that ends by jumping to such a function.
    In C it sets LEFT_CALLER on that way out, and a call of it is followed by a return when it is set (the
    caller clears it before the call): {entry: function}, rewritten in place."""
    exiting = {entry for entry, function in functions.items() if getattr(function, "exits_caller", False)}
    changed = True
    while changed:
        changed = False
        for entry, function in functions.items():
            if entry not in exiting and any(isinstance(block.end, ir.TailCall) and block.end.target & 0xFFFFFF in exiting
                                            for block in function.blocks.values()):
                exiting.add(entry)
                changed = True
    for function in functions.values():
        for block in function.blocks.values():
            if getattr(block.end, "leaves_caller", False):
                leave = ir.SetReg(LEFT_CALLER, ir.const(1))
                leave.origin = getattr(block.end, "origin", None)
                block.statements.append(leave)

    def targets(call):
        return [call.target] if call.target is not None else list(call.choices)

    for function in functions.values():
        pending = list(function.blocks)
        while pending:
            label = pending.pop()
            block = function.blocks[label]
            found = next((index for index, statement in enumerate(block.statements) if isinstance(statement, ir.Call)
                          and any(target & 0xFFFFFF in exiting for target in targets(statement))), None)
            if found is None:
                continue
            call = block.statements[found]
            origin = getattr(call, "origin", None)
            rest = type(block)(("after", label, found), block.statements[found + 1:], block.end)
            back = type(block)(("left", label, found), [], ir.Return())
            back.end.origin = origin
            clear = ir.SetReg(LEFT_CALLER, ir.const(0))
            clear.origin = origin
            block.statements = block.statements[:found] + [clear, call]
            block.end = ir.Branch(ir.op("ne", (ir.Reg(LEFT_CALLER), ir.const(0)), 1), back.label, rest.label)
            block.end.origin = origin
            function.blocks[rest.label] = rest
            function.blocks[back.label] = back
            pending.append(rest.label)
    return exiting


def to_memory(function):
    """A function whose stack addresses serve as values (function.memory_frame, analyse()) keeps its frame in the
    console's memory once its slots have served the data flow (its arguments, the registers it saves): it starts by
    lowering MEMORY_SP past the deepest place its stack reaches, each slot is the memory at MEMORY_SP plus the slot's
    distance from that place (the arguments' copies above the return address, as the 68000 has them), a stack address
    is that sum, and MEMORY_SP goes back up before every return. A callee's own frame goes below it."""
    sp = ir.Reg(MEMORY_SP, 32)
    places = []

    def look(node):
        if isinstance(node, ir.Stack):
            places.append((node.offset, node.bits // 8))
        elif isinstance(node, ir.Op) and node.name == "stack_address":
            offset = node.args[0].value
            places.append((offset - (1 << 32) if offset & 0x80000000 else offset, 1))
        return None
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            if isinstance(statement, ir.SetStack):
                places.append((statement.offset, statement.bits // 8))
            ir.map_statement(statement, look)
    low = min([function.deepest] + [offset for offset, _ in places])
    high = max([cpu.current.return_address_bytes] + [offset + size for offset, size in places])
    size = (high - low + 3) & ~3

    def place(offset):
        return ir.op("add", (sp, ir.const((offset - low) & 0xFFFFFFFF)), 32)
    count = 0
    for block in function.blocks.values():
        statements = []
        for statement in block.statements + [block.end]:
            loads = []

            def change(node):
                nonlocal count
                if isinstance(node, ir.Stack):
                    count += 1
                    temp = ir.Temp(f"t{getattr(statement, 'origin', None) or 0:06X}_frame_{count}", node.bits)
                    read = ir.SetTemp(temp, ir.Load(place(node.offset), node.bits))
                    read.origin = getattr(statement, "origin", None)
                    loads.append(read)
                    return temp
                if isinstance(node, ir.Op) and node.name == "stack_address":
                    offset = node.args[0].value
                    return place(offset - (1 << 32) if offset & 0x80000000 else offset)
                return None
            if isinstance(statement, ir.SetStack):
                stored = ir.Store(place(statement.offset), ir.rebuild(statement.value, change), statement.bits)
                if hasattr(statement, "origin"):
                    stored.origin = statement.origin
                statement = stored
            else:
                ir.map_statement(statement, change)
            statements += loads
            if statement is not block.end:
                statements.append(statement)
        if isinstance(block.end, (ir.Return, ir.TailCall)):
            back = ir.SetReg(MEMORY_SP, ir.op("add", (sp, ir.const(size)), 32))
            back.origin = getattr(block.end, "origin", None)
            statements.append(back)
        block.statements = statements
    first = function.blocks[function.entry]
    down = ir.SetReg(MEMORY_SP, ir.op("sub", (sp, ir.const(size)), 32))
    down.origin = getattr(first.statements[0] if first.statements else first.end, "origin", None)
    if any(function.entry in block.successors() for block in function.blocks.values()):
        body = ("frame", function.entry)
        function.blocks[body] = type(first)(body, first.statements, first.end, list(first.addresses))
        for label, block in function.blocks.items():
            if label != function.entry and label != body:
                redirect(block.end, function.entry, body)
        first.statements = [down]
        first.end = ir.Goto(body)
    else:
        first.statements.insert(0, down)
    function.memory_frame = False
    function.memory_stack = True


def whole(function):
    """A function whose SP slots() cannot follow (bytes pushed in a loop and popped in another, a buffer of a size it
    computes, ways that meet with calls' arguments not yet popped) keeps its whole stack in memory from its start, as the
    68000 does: SP is MEMORY_SP, and every push, pop and (n,SP) access reads or writes the console's memory there. What
    its slots would have given the data flow is found where SP is known (followed from the start where every way that
    reaches a place agrees, and counted back from each way out, where SP stands where it did at the start):
      - the arguments it reads (function.stack_arguments, (offset, bytes) from SP at the start): decompiler/stackargs.py
        lowers MEMORY_SP past them as the function starts and stores their registers there, sets its callees' from
        what it pushed, and puts MEMORY_SP back before every way out;
      - a register it saves as it starts and puts back from the same place on every way out (movem, LINK's A6) keeps
        that place as a slot, which preserved.py drops as in any other function.
    Requires STACK_ARGUMENTS (the arguments' registers). A StackError where the code sets SP to something else than SP
    plus a value or a frame (FRAME_REGISTERS), or where SP is known and differs from what the ways out need."""
    if not STACK_ARGUMENTS:
        raise StackError(f"{function.name}: SP that slots cannot follow, and no stack arguments")
    states = positions(function)
    saved = saved_places(function, states)
    arguments = set()
    for label, block in function.blocks.items():
        for statement, (sp, frames, temps) in zip(block.statements + [block.end], states.get(label, ())):
            for address, size in accesses(statement):
                offset = offset_of(address, sp, temps, frames)
                if offset is None or offset + size <= 0:
                    continue
                if offset < cpu.current.return_address_bytes:
                    raise StackError(f"{function.name}: reads its return address")
                arguments.add((offset, size))

    def register(node):
        if isinstance(node, ir.Reg) and node.index == cpu.current.sp:
            return ir.Reg(MEMORY_SP, node.bits)
        return None
    for label, block in function.blocks.items():
        known = states.get(label) or [(None, {}, {})] * (len(block.statements) + 1)
        statements = []
        for statement, (sp, frames, temps) in zip(block.statements + [block.end], known):
            def slot(node):
                if isinstance(node, ir.Load):
                    offset = offset_of(node.address, sp, temps, frames)
                    if offset is not None and all(byte in saved for byte in range(offset, offset + node.bits // 8)):
                        return ir.Stack(offset, node.bits)
                return None
            if isinstance(statement, ir.Store):
                offset = offset_of(statement.address, sp, temps, frames)
                if offset is not None and all(byte in saved for byte in range(offset, offset + statement.bits // 8)):
                    stored = ir.SetStack(offset, ir.rebuild(ir.rebuild(statement.value, slot), register),
                                         statement.bits)
                    if hasattr(statement, "origin"):
                        stored.origin = statement.origin
                    statements.append(stored)
                    continue
            if isinstance(statement, ir.Call):
                statement.memory_sp = sp    # (where SP stands at the call, if known: an argument a slot holds)
            ir.map_statement(statement, slot)
            if isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
                moved = ir.SetReg(MEMORY_SP, statement.value)
                if hasattr(statement, "origin"):
                    moved.origin = statement.origin
                statement = moved
            ir.map_statement(statement, register)
            if statement is not block.end:
                statements.append(statement)
        block.statements = statements
        if isinstance(block.end, ir.TailCall):
            block.end.sp = 0    # (where SP stands as it jumps: the callee's arguments are the function's own places)
    function.exits_caller = False
    function.memory_frame = False
    function.memory_stack = True
    function.memory_whole = True
    function.stack_arguments = sorted(arguments)
    function.saved_places = saved
    return {label: found[0][0] for label, found in states.items()}


def accesses(statement):
    """(address, bytes) of every memory access in 'statement'"""
    found = [(statement.address, statement.bits // 8)] if isinstance(statement, ir.Store) else []
    for expression in ir.statement_expressions(statement):
        found += [(node.address, node.bits // 8) for node in ir.walk(expression) if isinstance(node, ir.Load)]
    return found


def stack_derived(expression, derived, frames):
    """Whether 'expression' is SP, a frame register or a temporary that holds one, plus or minus something"""
    return any(isinstance(node, ir.Reg) and (node.index == cpu.current.sp or node.index in frames)
               or isinstance(node, ir.Temp) and node.name in derived for node in ir.walk(expression))


def walk(function, block, sp, frames):
    """[(SP's offset or None, the frame registers' offsets, the temporaries' that hold a stack address)] before each
    statement of 'block' and at its end, from SP and the frames at its start: SP set from SP plus a value the code
    computes is None from there, set from anything but SP or a frame a StackError"""
    frames = dict(frames)
    temps = {}
    derived = set()
    found = []
    for statement in block.statements:
        found.append((sp, dict(frames), dict(temps)))
        if isinstance(statement, ir.SetTemp):
            name = statement.temp.name
            offset = offset_of(statement.value, sp, temps, frames)
            if offset is None:
                temps.pop(name, None)
            else:
                temps[name] = offset
            if stack_derived(statement.value, derived, frames):
                derived.add(name)
            else:
                derived.discard(name)
        elif isinstance(statement, ir.SetReg) and statement.index in FRAME_REGISTERS:
            offset = offset_of(statement.value, sp, temps, frames)
            if offset is None:
                frames.pop(statement.index, None)
            else:
                frames[statement.index] = offset
        elif isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
            if not stack_derived(statement.value, derived, frames):
                raise StackError(f"{function.name}: SP set to {statement.value}")
            sp = offset_of(statement.value, sp, temps, frames)
    found.append((sp, frames, temps))
    return found


def counted_back(block):
    """SP's offset before each statement of a block that leaves the function (a return, a jump to another function) and at
    its end, counted back from the end, where SP stands where it did at the start, to the last statement that sets SP
    from something else than SP plus a constant; None before that"""
    sp, temps, run = 0, {}, 0
    found = []
    for statement in block.statements:
        found.append((run, sp))
        if isinstance(statement, ir.SetTemp):
            offset = offset_of(statement.value, sp, temps)
            if offset is None:
                temps.pop(statement.temp.name, None)
            else:
                temps[statement.temp.name] = offset
        elif isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
            new = offset_of(statement.value, sp, temps)
            if new is None:
                run, sp, temps = run + 1, 0, {}    # (a new run, relative to what SP was set to)
            else:
                sp = new
    found.append((run, sp))
    return [offset - sp if where == run else None for where, offset in found]


def positions(function):
    """{label: [(SP's offset or None, the frame registers' offsets, the temporaries' offsets)] before each statement and at
    the end}: where every way that reaches a block agrees, followed from the start; else, in a block that leaves the
    function, counted back from its end"""
    start = {function.entry: (0, {})}
    pending = [function.entry]
    while pending:
        label = pending.pop()
        sp, frames, _ = walk(function, function.blocks[label], *start[label])[-1]
        for successor in function.blocks[label].successors():
            if successor not in function.blocks:
                continue
            old = start.get(successor)
            new = (sp, dict(frames)) if old is None else (
                old[0] if old[0] == sp else None, {index: offset for index, offset in old[1].items()
                                                   if frames.get(index) == offset})
            if new != old:
                start[successor] = new
                pending.append(successor)
    found = {}
    for label, (sp, frames) in start.items():
        block = function.blocks[label]
        forward = walk(function, block, sp, frames)
        leaves = isinstance(block.end, (ir.Return, ir.TailCall))
        if leaves and forward[-1][0] is not None and forward[-1][0] != 0:
            raise StackError(f"{function.name}: returns with SP {forward[-1][0]:+d} from where it started")
        back = counted_back(block) if leaves else [None] * len(forward)
        temps = {}
        states = []
        for index, (ahead, frames, _) in enumerate(forward):
            sp = ahead if ahead is not None else back[index]
            if ahead is not None and back[index] is not None and ahead != back[index]:
                raise StackError(f"{function.name}: SP {ahead} and {back[index]} at {label}")
            states.append((sp, frames, dict(temps)))
            if index < len(block.statements) and isinstance(block.statements[index], ir.SetTemp):
                statement = block.statements[index]
                offset = offset_of(statement.value, sp, temps, frames)
                if offset is None:
                    temps.pop(statement.temp.name, None)
                else:
                    temps[statement.temp.name] = offset
        found[label] = states
    return found


def saved_places(function, states):
    """The bytes of the stack (offsets from SP at the start) where the function saves a register as it starts, from
    which every way out puts it back, and which nothing else reads or writes where SP is known"""
    predecessors = {}
    for label, block in function.blocks.items():
        for successor in block.successors():
            predecessors.setdefault(successor, []).append(label)
    prologue = [function.entry]
    while isinstance(function.blocks[prologue[-1]].end, ir.Goto):
        following = function.blocks[prologue[-1]].end.target
        if following in prologue or following not in states or predecessors.get(following) != [prologue[-1]]:
            break
        prologue.append(following)

    def place(address, size, sp, frames, temps):
        offset = offset_of(address, sp, temps, frames)
        return None if offset is None or offset >= 0 else frozenset(range(offset, offset + size))

    saves = {}           # {register: bytes}
    written = set()
    values = {}          # what the prologue's temporaries hold
    done = False
    for label in prologue:
        if done or label not in states:
            break
        for statement, (sp, frames, temps) in zip(function.blocks[label].statements, states[label]):
            if isinstance(statement, ir.Call):
                done = True
                break
            if isinstance(statement, ir.SetTemp):
                values[statement.temp.name] = ir.rebuild(statement.value, lambda node: values.get(node.name)
                                                         if isinstance(node, ir.Temp) else None)
            elif isinstance(statement, ir.Store):
                value = ir.rebuild(statement.value, lambda node: values.get(node.name)
                                   if isinstance(node, ir.Temp) else None)
                registers = {node.index for node in ir.walk(value) if isinstance(node, ir.Reg)}
                bytes_ = place(statement.address, statement.bits // 8, sp, frames, temps)
                if bytes_ and len(registers) == 1 and not any(isinstance(node, (ir.Load, ir.Temp))
                                                              for node in ir.walk(value)):
                    register = registers.pop()
                    if register not in written and register != cpu.current.sp:
                        saves[register] = saves.get(register, frozenset()) | bytes_
            elif isinstance(statement, ir.SetReg):
                written.add(statement.index)
    exits = [label for label, block in function.blocks.items()
             if isinstance(block.end, (ir.Return, ir.TailCall)) and label in states]
    restored = []
    for label in exits:
        found = {}
        loads = {}       # {temporary: bytes of the stack it read}
        for statement, (sp, frames, temps) in zip(function.blocks[label].statements, states[label]):
            read = set()
            for expression in ir.statement_expressions(statement):
                for node in ir.walk(expression):
                    if isinstance(node, ir.Load):
                        bytes_ = place(node.address, node.bits // 8, sp, frames, temps)
                        read.add(bytes_)
                    elif isinstance(node, ir.Temp) and node.name in loads:
                        read.add(loads[node.name])
            if isinstance(statement, ir.SetTemp):
                if len(read) == 1:
                    loads[statement.temp.name] = next(iter(read))
            elif isinstance(statement, ir.SetReg):
                if len(read) == 1 and None not in read:
                    found[statement.index] = next(iter(read))
                else:
                    found.pop(statement.index, None)
        restored.append(found)
    places = {register: bytes_ for register, bytes_ in saves.items()
              if exits and all(found.get(register) == bytes_ for found in restored)}
    # (and nothing else where SP is known reads or writes them)
    claimed = set().union(*places.values()) if places else set()
    for label, block in function.blocks.items():
        for statement, (sp, frames, temps) in zip(block.statements + [block.end], states.get(label, ())):
            for address, size in accesses(statement):
                bytes_ = place(address, size, sp, frames, temps)
                if bytes_ and bytes_ & claimed and not any(bytes_ <= kept for kept in places.values()):
                    for register in [register for register, kept in places.items() if kept & bytes_]:
                        del places[register]
                    claimed = set().union(*places.values()) if places else set()
    return set().union(*places.values()) if places else set()


def addresses_left(function):
    """Whether a stack address still serves as a value after slots() (its 'stack_address' marker)."""
    return any(isinstance(node, ir.Op) and node.name == "stack_address"
               for block in function.blocks.values() for statement in block.statements + [block.end]
               for expression in ir.statement_expressions(statement) for node in ir.walk(expression))


def leftovers(function):
    """Statements that still read or set SP after analyse()."""
    found = []
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            if isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
                found.append(statement)
            elif any(mentions_sp(expression) for expression in ir.statement_expressions(statement)):
                found.append(statement)
    return found
