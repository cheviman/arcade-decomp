"""
Name:        stack.py
Description: follows the V60 stack pointer through each function and turns stack memory into slots
Version:     0.1.0
Created:     11-10-2026
Modified:    20-10-2026
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
MEMORY_SP = 32           # the register SP becomes in a function whose stack stays in memory (V60 only)
# the registers a frame may go through (the 68000's LINK An: An = SP, the frame's places An+n, UNLK gives SP back):
# one set to a stack address is followed as that offset, its loads and stores are slots; empty (the V60, the Z80,
# Konami's) such a register is not followed (m68000/decompile.py sets it from the profile's LINK_FRAMES)
FRAME_REGISTERS = frozenset()
# the stack's temporaries are followed where memory is read and written too, not only where a stack address is
# computed (the 68000's pushes and "($n,A7)" reads go through one); off, as before (decompiler/stackargs.py)
STACK_ARGUMENTS = False
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
                frames.pop(statement.index, None)
                continue
            if isinstance(statement, ir.SetReg) and statement.index == cpu.current.sp:
                new = offset_of(statement.value, sp, temps, frames)
                if new is None:
                    if not isinstance(statement.value, ir.Const):
                        raise StackError(f"{function.name}: SP set to {statement.value}")
                    stacks += 1    # the code's own stack: a fresh range of slots
                    new = NEW_STACK * stacks
                sp = new
                continue
            if isinstance(statement, ir.Effect) and statement.name == "return_from_interrupt":
                # RETIS to code the function pushed: the PC it pops is the jump, the PSW takes effect
                frame = statement.args[0]
                if not isinstance(frame, ir.Const):
                    raise StackError(f"{function.name}: RETIS with a computed frame")
                statements.append(ir.Effect("write_psw", (ir.Stack(sp + 4),)))
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
                    continue
            if STACK_ARGUMENTS and isinstance(statement, ir.Call):
                statement.sp = sp    # where the stack stood at the call: its arguments are the slots from here up
            ir.map_statement(statement, slot_reader(sp, frames, known))
            statements.append(statement)
        block.statements = statements
        if STACK_ARGUMENTS and isinstance(block.end, ir.TailCall):
            block.end.sp = sp
        ir.map_statement(block.end, slot_reader(sp, frames, known))
        # a stack address that still serves as a value (the flags of "add.w #4, SP") has none in C: it
        # becomes a marker the C writer refuses, so it has to turn out unused
        def opaque(node):
            if isinstance(node, ir.Temp) and node.name in temps:
                return ir.op("stack_address", (ir.const(temps[node.name] & 0xFFFFFFFF),), cpu.current.address_bits)
            if isinstance(node, ir.Reg) and node.index in frames:
                return ir.op("stack_address", (ir.const(frames[node.index] & 0xFFFFFFFF),), cpu.current.address_bits)
            return None
        for statement in statements + [block.end]:
            ir.map_statement(statement, opaque)
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
