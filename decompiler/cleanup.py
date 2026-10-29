"""
Name:        cleanup.py
Description: removes the statements whose results nothing reads (flags no branch tests, values overwritten
             before use), once every function's signature is known
Version:     0.1.0
Created:     11-10-2026
Modified:    27-10-2026
Author:      Sebastian Toyos

Liveness runs backwards over each function with the data flow's own rules (demanded bits, what calls
read and write), from what is live where the function ends: its results, and the context registers,
which are global variables in C. A statement goes when none of what it writes is live after it, and it
then asks for nothing, so values only dead code reads go too. Memory writes, calls and effects always
stay. A memory read whose value nothing needs goes as well, unless its address is a constant outside the
plain memories (ROM, RAM, video, sprites, palette): reading the board's I/O, interrupt controller or
random number generator does things.

What a call passes by value is worked out as the original does it even where the callee's use of it does
not matter to this caller (point_to_screen works out a screen x the caller drops, from the projected x
the caller loads into R0 for it): the C writes the argument, so it has to be what the code passes, not
whatever the register held before. Where that takes a register this function's own callers do not hand
it, the C reads the 0 its variable starts with, as for every input no caller sets. A register the callee
reads only to hand it back as it was (draw_object's R0, which it returns untouched when the object is
not drawn) is what the code passes only where this caller reads it after the call: elsewhere nothing
the callee does depends on it.
"""
from v60 import ir

from . import cpu

from . import dataflow
from . import signatures

# function -> the bits of the register it returns that its callers read (decompile.py's result_widths): the C
# returns that much, so only those bits are live where it ends
RESULT_BITS = {}
# function -> {register it hands back through a pointer: the parts its callers read} where not all of them
RESULT_PARTS = {}

# address >> 20 of the areas where a read has no side effect
PLAIN_MEMORY = {0x0, 0x1, 0x2, 0x3, 0x4, 0x6, 0x7, 0xF}
# in place of PLAIN_MEMORY, a function of an address that says reading it does something (a board's
# registers): a read there stays even where nothing uses its value (decompile_sound.py sets it from the
# profile's SOUND_KEEP_HARDWARE_READS: reading TMNT's K007232 at B005 starts its channel A)
EFFECT_READS = None


def variables_of(values):
    """Data flow variables of C values (("r", n) or ("f", name))."""
    found = set()
    for value in values:
        if value[0] == "r":
            found |= dataflow.register_parts(value[1], dataflow.FULL)
        else:
            found.add(value)
    return found


def has_effect(value):
    """True if evaluating 'value' reads a place where reading does something."""
    for node in ir.walk(value):
        if isinstance(node, ir.Load):
            address = node.address
            if not isinstance(address, ir.Const) and EFFECT_READS is not None:
                # (a place worked out from a constant there: "0x140400 + d1", the K051960's RAM read by index)
                base = address.args[0] if isinstance(address, ir.Op) and address.name == "add" else None
                base = base if isinstance(base, ir.Const) else \
                    address.args[1] if isinstance(address, ir.Op) and address.name == "add" else None
                if isinstance(base, ir.Const) and EFFECT_READS(base.value):
                    return True
                continue
            if not isinstance(address, ir.Const):
                continue
            if EFFECT_READS is not None:
                if EFFECT_READS(address.value):
                    return True
            elif ((address.value & 0xFFFFFF) >> 20) not in PLAIN_MEMORY:
                return True
    return False


class Liveness:
    """Liveness of one function, with dead statements left out of it."""

    def __init__(self, flow, function, exit_live, passed=None):
        self.flow = flow
        self.function = function
        self.exit_live = exit_live
        # id(call or tail call) -> (what it reads to pass by value, live anyway; {register: what it reads of
        # it, live where the register is live after the call}: the ones the callee only hands back)
        self.passed = passed or {}

    def needed(self, statement, live, temps):
        if isinstance(statement, ir.SetTemp):
            if statement.temp.name in temps:
                return True
            if has_effect(statement.value):
                temps[statement.temp.name] = dataflow.width(statement.temp.bits)
                return True
            return False
        if isinstance(statement, ir.SetReg):
            if not cpu.kept(statement.index):
                return True
            if EFFECT_READS is not None and has_effect(statement.value):
                return True    # (a read of a register that does something, its value unused)
            parts, _, _ = dataflow.partial_write(statement)
            return any(("r", statement.index, part) in live for part in parts)
        if isinstance(statement, ir.SetFlag):
            return ("f", statement.name) in live
        if isinstance(statement, ir.SetStack):
            return any(("s", statement.offset, part) in live for part in (0, 1, 2))
        return True

    def end_live(self, block, live_in):
        end = block.end
        live = set()
        for successor in block.successors():
            live |= live_in.get(successor, set())
        if isinstance(end, (ir.Return, ir.TailCall, ir.InterruptReturn)):
            live |= self.exit_live
        out = set(live)
        temps = {}
        self.flow.transfer(end, live, temps, end=True)
        live |= self.passed_parts(end, out)
        return live, temps, out

    def passed_parts(self, statement, after):
        """What 'statement' passes by value that is live anyway, given what is live after it."""
        found = self.passed.get(id(statement))
        if found is None:
            return set()
        always, handed_back = found
        parts = set(always)
        for index, read in handed_back.items():
            if any(variable[0] == "r" and variable[1] == index for variable in after):
                parts |= read
        return parts

    def backwards(self, block, live_in, keep=None):
        """Live variables at the start of 'block'. With 'keep', records the statements to keep, each with the
        variables live after it, and (under ("end", id(block))) what is live when the block is left."""
        live, temps, out = self.end_live(block, live_in)
        if keep is not None:
            keep[("end", id(block))] = out
        for statement in reversed(block.statements):
            if not self.needed(statement, live, temps):
                continue
            if keep is not None:
                keep[id(statement)] = set(live)
            after = set(live)
            self.flow.transfer(statement, live, temps)
            live |= self.passed_parts(statement, after)
        return live

    def solve(self):
        blocks = self.function.blocks
        live_in = {label: set() for label in blocks}
        order = list(blocks)
        changed = True
        while changed:
            changed = False
            for label in reversed(order):
                live = self.backwards(blocks[label], live_in)
                if live != live_in[label]:
                    live_in[label] = live
                    changed = True
        return live_in


def context_live(flow, entry, context):
    """The parts of the global registers live where a function ends: all of them, but of the scratch ones
    (signatures.SCRATCH_CONTEXT) only the bits later code may read without setting them, and those its
    callers read after calling it."""
    live = set()
    handed = flow.exit_live.get(entry, frozenset())
    for index in context:
        live |= dataflow.register_parts(index, signatures.SCRATCH_CONTEXT.get(index, dataflow.FULL))
        live |= {variable for variable in handed if variable[0] == "r" and variable[1] == index}
    return live


def passed_by_value(flow, function, arguments):
    """{id(call or tail call): the parts of the registers it passes by value that its callee reads} for the
    calls of 'function' to one known function; 'arguments' gives each callee's by-value registers."""
    found = {}
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            if isinstance(statement, ir.Call) and statement.slot is None and statement.target is not None:
                target = statement.target & 0xFFFFFF
            elif isinstance(statement, ir.TailCall):
                target = statement.target
            else:
                continue
            summary = flow.summaries.get(target)
            registers = arguments.get(target)
            if summary is None or not registers:
                continue
            # the whole of what the C passes: every part up to the highest one the callee reads (its parameter
            # is that wide), not only the parts it reads (add_to_touchable_list reads only R0's high half);
            # of a register it reads only to hand it back, only where the caller reads it afterwards
            parts, handed_back = set(), {}
            for index in registers:
                read = [variable[2] for variable in summary.reads if variable[0] == "r" and variable[1] == index]
                if not read:
                    continue
                passed = {("r", index, part) for part in range(max(read) + 1)}
                if only_handed_back(summary, index):
                    handed_back[index] = passed
                else:
                    parts |= passed
            if parts or handed_back:
                found[id(statement)] = (parts, handed_back)
    return found


def only_handed_back(summary, index):
    """True if a function reads register 'index' only to hand it back: for no memory write, call or other
    value it hands back."""
    if not summary.passes:
        return False
    if any(variable[0] == "r" and variable[1] == index for variable in summary.base):
        return False
    for key, reads in summary.passes.items():
        if key != ("r", index) and any(variable[0] == "r" and variable[1] == index for variable in reads):
            return False
    return ("r", index) in summary.passes


def eliminate(flow, function, results, context, arguments=None):
    """Removes the dead statements of 'function' in place. 'results' are its C results, 'context' the
    registers that are global variables, 'arguments' {callee: the registers it takes by value} (see the
    module's notes). Returns {id(statement): variables live after it} for the statements that stay, and
    the variables live at the start. Where it ends, a result it returns is live as wide as the C returns
    it (RESULT_BITS, else whole): the caller takes the whole variable, even where the original only wrote
    part of the register and the caller's other bits go through it ("and.b #FF, R0"). Results it hands
    back through pointers are copied back only in the parts the function may write (emit.py), so only
    those are live, and of those only the ones its callers read (RESULT_PARTS): the caller's variable
    takes the rest as the C has it, which nothing reads."""
    if len(results) > 1:
        written = flow.summaries[function.entry].writes
        read = RESULT_PARTS.get(function.entry, {})
        exit_live = {variable for variable in written if (variable[0], variable[1]) in set(results)
                     and (variable[0] != "r" or variable[2] in read.get(variable[1], (0, 1, 2)))}
    elif function.entry in RESULT_BITS and next(iter(results))[0] == "r":
        exit_live = dataflow.register_parts(next(iter(results))[1], ir.MASKS[RESULT_BITS[function.entry]])
    else:
        exit_live = variables_of(results)
    exit_live |= context_live(flow, function.entry, context)
    liveness = Liveness(flow, function, exit_live, passed_by_value(flow, function, arguments) if arguments else {})
    live_in = liveness.solve()
    after = {}
    for label, block in function.blocks.items():
        keep = {}
        liveness.backwards(block, live_in, keep)
        block.statements = [statement for statement in block.statements if id(statement) in keep]
        after.update(keep)
    return after, live_in[function.entry]
