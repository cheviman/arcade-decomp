"""
Name:        ranges.py
Description: the few values a register can hold where the code reads it, followed from the constants the
             functions and their callers set: a bit test by a register's number reads only the bits those
             numbers reach
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

"test1 R1, R2" tests the bit of R2 whose number R1 holds. Taken alone it may test any bit of R2, so the
whole of R2 is an input of the function, and a caller that sets only R2's low byte (mov.b) before the call
keeps the rest of R2 for it: in C, a part of one value put into another. But network_check_show_ics counts
R1 up from 0 while it counts R0, the number of chips, down to 0, and its two callers pass 4 and 5: R1 is
never more than 4, and the test reads only R2's low byte.

Each function is followed with the values its registers, temporaries, flags and stack slots hold, as a set
of states: one for each combination of values that reaches a statement, a value known on one way and not
on another missing from the state of the second. Where a branch's condition is known in a state, only the
way it takes is followed; a call forgets what the callee may write (the data flow's summaries). The
registers the whole program shares (the context) are never followed: an interrupt may change them. Memory
is not followed either, but for the program's ROM, which nothing changes: a read at a known address there
is its value (insert_coin's chute entries, whose callers pass their addresses, give the bit it tests).

A function starts from the states of its calls and tail calls, each with the registers the callee reads,
worked out again as the callers' states change until nothing does. One that something else reaches (a
memory slot, a task, an interrupt, a second way in, or nothing at all: 'open') starts from nothing known.
Where more than LIMIT states reach a block or start a function, they become one, with what all of them
hold alike; a function followed more than RUNS times starts from nothing known.

reaches() gives, for each statement and block end with a shift of a constant by a count ("shl(1, n)", the
mask of a bit test) whose count is known in every state that gets there, the bits that shift can set:
dataflow.demanded takes them as the only bits an "and" with that mask reads of its other side.
"""
from v60 import ir

LIMIT = 64
RUNS = 40
NOTHING = frozenset()
ROM = None      # (address, bits) -> the program ROM's value there, None outside it (set by reaches)


def mask_of(bits):
    return (1 << bits) - 1


def signed(value, bits):
    return value - (1 << bits) if value >> (bits - 1) & 1 else value


def count_of(count, bits):
    """A shift count as the C helpers take it: the low 5 bits, 6 for a 64-bit value."""
    return count & (63 if bits == 64 else 31)


def evaluate(node, state):
    """The value of 'node' in 'state' (key -> value), or None when something in it is not known."""
    if isinstance(node, ir.Const):
        return node.value
    if isinstance(node, ir.Reg):
        return state.get(("r", node.index))
    if isinstance(node, ir.Temp):
        return state.get(("t", node.name))
    if isinstance(node, ir.Flag):
        return state.get(("f", node.name))
    if isinstance(node, ir.Stack):
        return state.get(("s", node.offset, node.bits))
    if isinstance(node, ir.Load):
        address = evaluate(node.address, state)
        return ROM(address, node.bits) if address is not None and ROM is not None else None
    if not isinstance(node, ir.Op):
        return None
    name, args, bits = node.name, node.args, node.bits
    if name == "select":
        condition = evaluate(args[0], state)
        if condition is None:
            first, second = evaluate(args[1], state), evaluate(args[2], state)
            return first if first is not None and first == second else None
        return evaluate(args[1] if condition else args[2], state)
    values = [evaluate(arg, state) for arg in args]
    if any(value is None for value in values):
        return None
    mask = mask_of(bits)
    width = getattr(args[0], "bits", 32)
    values = [value & mask_of(getattr(arg, "bits", 32)) for value, arg in zip(values, args)]
    if name in ("add", "sub", "mul", "and", "or", "xor"):
        a, b = values
        return {"add": a + b, "sub": a - b, "mul": a * b, "and": a & b, "or": a | b, "xor": a ^ b}[name] & mask
    if name == "not":
        return ~values[0] & mask
    if name == "neg":
        return -values[0] & mask
    if name == "shl":
        return values[0] << count_of(values[1], width) & mask
    if name == "shr":
        return values[0] >> count_of(values[1], width) & mask
    if name == "sar":
        return signed(values[0], width) >> count_of(values[1], width) & mask
    if name in ("eq", "ne", "ult", "ule"):
        a, b = values
        return int({"eq": a == b, "ne": a != b, "ult": a < b, "ule": a <= b}[name])
    if name in ("lt", "le"):
        a, b = signed(values[0], width), signed(values[1], width)
        return int(a < b if name == "lt" else a <= b)
    if name in ("zext", "trunc"):
        return values[0] & mask
    if name == "sext":
        source = args[1].value
        return signed(values[0] & mask_of(source), source) & mask
    if name in ("carry_add", "overflow_add", "carry_sub", "overflow_sub"):
        a, b, carry = values
        sign = 1 << (width - 1)
        if name == "carry_add":
            return (a + b + carry) >> width & 1
        if name == "carry_sub":
            return (a - b - carry) >> width & 1
        if name == "overflow_add":
            result = a + b + carry
            return int(bool((result ^ b) & (result ^ a) & sign))
        result = a - b - carry
        return int(bool((a ^ b) & (a ^ result) & sign))
    return None


def forget(state, keys):
    for key in keys:
        state.pop(key, None)


def step(statement, state, flow, context):
    """'state' (a dict) after 'statement', in place."""
    if isinstance(statement, ir.SetTemp):
        key = ("t", statement.temp.name)
    elif isinstance(statement, ir.SetReg):
        key = ("r", statement.index) if statement.index not in context else None
    elif isinstance(statement, ir.SetFlag):
        key = ("f", statement.name)
    elif isinstance(statement, ir.SetStack):
        end = statement.offset + statement.bits // 8
        forget(state, [key for key in state if key[0] == "s" and key[1] < end and statement.offset < key[1] + key[2] // 8])
        key = ("s", statement.offset, statement.bits)
    elif isinstance(statement, (ir.Call, ir.Effect)):
        writes = flow.call_effect(statement)[1] if isinstance(statement, ir.Call) else flow.effect(statement)[1]
        for variable in writes:
            if variable[0] == "r":
                state.pop(("r", variable[1]), None)
            elif variable[0] == "f":
                state.pop(("f", variable[1]), None)
            elif variable[0] == "s":
                forget(state, [key for key in state if key[0] == "s" and key[1] == variable[1]])
        return
    else:
        return
    if key is None:
        return
    value = evaluate(statement.value, state)
    if value is None:
        state.pop(key, None)
    else:
        state[key] = value


def ways_out(end, state):
    """The blocks a block end goes on to in 'state'."""
    if isinstance(end, ir.Branch):
        condition = evaluate(end.condition, state)
        if condition is None:
            return [end.target, end.otherwise]
        return [end.target if condition else end.otherwise]
    if isinstance(end, ir.Goto):
        return [end.target]
    if isinstance(end, ir.Switch):
        address = evaluate(end.address, state)
        if address is not None and address in end.cases:
            return [end.cases[address]]
        return list(dict.fromkeys(end.cases.values()))
    return []


def common(states):
    """One state with what all of 'states' hold alike."""
    states = list(states)
    found = set(states[0])
    for state in states[1:]:
        found &= state
    return frozenset(found)


def shifts(statement):
    """The shifts of a constant by a count that is not one, in a statement's expressions."""
    found = []
    for expression in ir.statement_expressions(statement):
        for node in ir.walk(expression):
            if isinstance(node, ir.Op) and node.name == "shl" and isinstance(node.args[0], ir.Const) \
                    and not isinstance(node.args[1], ir.Const) and node not in found:
                found.append(node)
    return found


def follow(function, starts, flow, context, seen=None):
    """Follows 'function' from the states 'starts'; returns {target: states at its calls and tail calls}.
    With 'seen' ({id(statement): {shift: set of counts, or None}}), what each shift's count is there."""
    blocks = function.blocks
    at = {function.entry: set(starts)} if starts else {}
    if len(at.get(function.entry, ())) > LIMIT:
        at[function.entry] = {common(at[function.entry])}
    done = {}
    merged = set()
    pending = [function.entry] if starts else []
    calls = {}

    def arrive(label, states):
        old = at.get(label, set())
        new = old | states
        if label in merged or len(new) > LIMIT:
            merged.add(label)
            new = {common(new)}
        if new != old:
            at[label] = new
            if label not in pending:
                pending.append(label)

    def look(statement, state):
        if seen is None:
            return
        for node in shifts(statement):
            counts = seen.setdefault(id(statement), {})
            count = evaluate(node.args[1], state)
            if count is None:
                counts[node] = None
            elif counts.get(node, set()) is not None:
                counts.setdefault(node, set()).add(count)

    while pending:
        label = pending.pop()
        block = blocks[label]
        fresh = at[label] - done.get(label, set())
        done.setdefault(label, set()).update(fresh)
        going = {}
        for start in fresh:
            state = dict(start)
            for statement in block.statements:
                look(statement, state)
                if isinstance(statement, ir.Call) and statement.slot is None:
                    for target in flow.call_targets(statement):
                        calls.setdefault(target, set()).add(passed(state, flow.summaries[target].reads))
                step(statement, state, flow, context)
            look(block.end, state)
            if isinstance(block.end, ir.TailCall):
                target = block.end.target & 0xFFFFFF
                if target in flow.summaries:
                    calls.setdefault(target, set()).add(passed(state, flow.summaries[target].reads))
            # (temporaries are the statements of one instruction: none goes on to another block)
            kept = frozenset((key, value) for key, value in state.items() if key[0] != "t")
            for successor in ways_out(block.end, state):
                going.setdefault(successor, set()).add(kept)
        for successor, states in going.items():
            arrive(successor, states)
    return calls


def passed(state, reads):
    """What a call hands its callee of 'state': the registers the callee reads (a summary's reads)."""
    registers = {variable[1] for variable in reads if variable[0] == "r"}
    return frozenset((key, value) for key, value in state.items() if key[0] == "r" and key[1] in registers)


def reaches(functions, flow, open_entries, context=(), rom=None):
    """{id(statement): {shift: bits it can set}} (see the module's notes); 'rom' reads the program's ROM."""
    global ROM
    ROM = rom
    context = frozenset(context)
    open_entries = {entry for entry in open_entries if entry in functions}
    starts = {entry: {NOTHING} for entry in open_entries}
    given = {}          # caller -> {target: states}
    runs = {}
    pending = list(starts)
    while pending:
        entry = pending.pop()
        runs[entry] = runs.get(entry, 0) + 1
        if runs[entry] > RUNS:
            starts[entry] = {NOTHING}
        calls = follow(functions[entry], starts.get(entry, set()), flow, context)
        before = given.get(entry, {})
        given[entry] = calls
        for target in set(before) | set(calls):
            if target not in functions or target in open_entries:
                continue
            states = set()
            for found in given.values():
                states |= found.get(target, set())
            if len(states) > LIMIT:
                states = {common(states)}
            if runs.get(target, 0) > RUNS:
                states = {NOTHING}
            if states != starts.get(target):
                starts[target] = states
                if target not in pending:
                    pending.append(target)
    seen = {}
    for entry, function in functions.items():
        follow(function, starts.get(entry, set()), flow, context, seen)
    found = {}
    for key, counts in seen.items():
        for node, values in counts.items():
            if not values:
                continue
            width = node.args[0].bits
            bits = 0
            for value in values:
                bits |= node.args[0].value << count_of(value, width) & mask_of(node.bits)
            found.setdefault(key, {})[node] = bits
    return found
