"""
Name:        stackargs.py
Description: the arguments a caller pushes on the stack and its callee reads there, as the registers they are to the data flow
Version:     0.1.0
Created:     23-11-2026
Modified:    24-11-2026
Author:      Sebastian Toyos

The 68000's C compilers pass arguments on the stack: the caller pushes them (move.l D0,-(A7), pea n.w), calls, and
takes them back with addq or lea; the callee reads them at (n,A7), or at (n,A6) after LINK. stack.py turns the stack
into slots relative to where SP stood when each function started, so a callee's argument is the slot at +4 (the return
address is at 0) and the same bytes are, in the caller, the slots it pushed, from the SP at the call (Call.sp) up.
Nothing in the data flow links the two: a push nothing reads is dead, and a read of the caller's stack has no value.

The data flow already passes values from caller to callee as registers, so each argument becomes one: a virtual
register per argument, numbered from BASE by its place (the one at +4 is BASE, the one at +6 BASE + 1, ...), the width
the callee reads it with (a word or a long; a function that reads a long and a word inside it has the long, the word
is a part of it). Then:

  - the callee reads the register where it read the slot; a callee that calls others (their setups set the
    register in turn) or writes the slot starts by copying each register into the slot, and reads that;
  - before each call the caller sets the registers of what its callees read, from the slots it pushed (what a callee at
    +4 reads is what the caller stored at its SP; a tail call is the caller's own +n);
  - the two 16-bit stores the 68000's lift makes of a pushed long are one 32-bit slot, so that a slot has one width;
  - a slot of the function's own stack it uses at two extents that overlap (a long stored and its high word read back,
    a word whose high byte "bset" changes, the arguments' area pushed again at another width) is one slot of the whole
    extent, of which a narrower access reads or stores its part (widen()).

Signatures and calls then see ordinary register parameters. Off by default (stack.STACK_ARGUMENTS, the profile's
STACK_ARGUMENTS): the games that pass nothing on the stack keep their C the same.
"""
import dataclasses

from v60 import ir

from . import cpu, stack

BASE = 64    # the first virtual register: past the carried ones (m68000/decompile.py VIRTUAL_BASE, SEEDED_BASE)


class ArgumentError(stack.StackError):
    pass


def register_for(offset):
    return BASE + (offset - cpu.current.return_address_bytes) // 2


def name_of(index):
    return f"arg_{cpu.current.return_address_bytes + 2 * (index - BASE)}"


def merge_pushes(function):
    """The two 16-bit slots of one 32-bit store are one 32-bit slot: (hi, lo) of the same value, or of a constant."""
    for block in function.blocks.values():
        statements = block.statements
        index = 0
        while index + 1 < len(statements):
            first, second = statements[index], statements[index + 1]
            if isinstance(first, ir.SetStack) and isinstance(second, ir.SetStack) \
                    and first.bits == second.bits == 16 and abs(first.offset - second.offset) == 2:
                low, high = (first, second) if first.offset > second.offset else (second, first)
                whole = joined(high.value, low.value)
                if whole is not None:
                    merged = ir.SetStack(high.offset, whole, 32)
                    for source in (high, low, first):
                        if hasattr(source, "origin"):
                            merged.origin = source.origin
                            break
                    statements[index:index + 2] = [merged]
                    continue
            index += 1


def joined(high, low):
    """The 32-bit value whose halves are 'high' and 'low' (16-bit expressions), or None."""
    if isinstance(high, ir.Const) and isinstance(low, ir.Const):
        return ir.const((high.value << 16) | low.value)
    if not (isinstance(high, ir.Op) and high.name == "trunc" and high.bits == 16):
        return None
    top = high.args[0]
    if not (isinstance(top, ir.Op) and top.name == "shr" and top.args[1] == ir.Const(16, 8) and top.bits == 32):
        return None
    value = top.args[0]    # the long both halves were cut from
    if isinstance(low, ir.Op) and low.name == "trunc" and low.bits == 16 and low.args[0] == value:
        return value
    if isinstance(low, ir.Const) and isinstance(value, ir.Const) and value.value & 0xFFFF == low.value:
        return value    # (clr.l and move.l #n: the long is a constant the lift cuts into halves)
    return None


def accesses_of(function):
    """The (offset, bytes) of the slots a function reads or writes in its caller's stack."""
    found = set()
    start = cpu.current.return_address_bytes

    def look(node):
        if isinstance(node, ir.Stack) and node.offset >= start:
            found.add((node.offset, node.bits // 8))
        return None
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            if isinstance(statement, ir.SetStack) and statement.offset >= start:
                found.add((statement.offset, statement.bits // 8))
            ir.map_statement(statement, look)
    return found


def regions_of(accesses):
    """The arguments a function takes, [(offset, bytes)] by offset: a word or a long each; a word read inside a long
    is a part of the long, and where accesses of a long overlap each other the words are the arguments."""
    ranges = sorted((offset & ~1, (offset + size + 1) & ~1) for offset, size in accesses)
    joined_ranges = []
    for first, last in ranges:
        if joined_ranges and first < joined_ranges[-1][1]:
            joined_ranges[-1][1] = max(joined_ranges[-1][1], last)
        else:
            joined_ranges.append([first, last])
    regions = []
    for first, last in joined_ranges:
        if last - first in (2, 4):
            regions.append((first, last - first))
        else:
            regions.extend((word, 2) for word in range(first, last, 2))
    return regions


def extract(source, offset, size, first, last):
    """Bytes [first, last) of 'source', the value of the 'size' bytes at 'offset' (big endian: the lowest address
    holds the highest bits)."""
    shift = (offset + size - last) * 8
    value = source
    if shift:
        value = ir.op("shr", (value, ir.Const(shift, 8)), value.bits)
    return ir.trunc(value, (last - first) * 8)


def compose(pieces, first, last):
    """The value of bytes [first, last) from 'pieces', [(offset, size, source expression)]; None when they do
    not cover it."""
    bits = (last - first) * 8
    taken = []
    covered = first
    for offset, size, source in sorted(pieces, key=lambda piece: piece[0]):
        if offset + size <= first or offset >= last:
            continue
        low, high = max(offset, first), min(offset + size, last)
        if low != covered:
            return None
        taken.append((high, extract(source, offset, size, low, high)))
        covered = high
    if covered != last:
        return None
    result = None
    for high, value in taken:
        part = ir.zext(value, bits) if value.bits != bits else value
        shift = (last - high) * 8
        if shift:
            part = ir.op("shl", (part, ir.Const(shift, 8)), bits)
        result = part if result is None else ir.op("or", (result, part), bits)
    return result


def widen(function):
    """The slots of the function's own stack it reads or stores at extents that overlap, as one slot each of the
    whole extent: a narrower read is its part of that slot (extract()), a narrower store puts its part into it and
    keeps the rest (compose()), as the bytes in memory do. A joined extent wider than a long is an ArgumentError."""
    accesses = set()

    def look(node):
        if isinstance(node, ir.Stack) and node.offset < 0:
            accesses.add((node.offset, node.offset + node.bits // 8))
        return None
    for block in function.blocks.values():
        for statement in block.statements + [block.end]:
            if isinstance(statement, ir.SetStack) and statement.offset < 0:
                accesses.add((statement.offset, statement.offset + statement.bits // 8))
            ir.map_statement(statement, look)
    groups = []
    for first, last in sorted(accesses):
        if groups and first < groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], last)
            groups[-1][2].append((first, last))
        else:
            groups.append([first, last, [(first, last)]])
    whole = {}
    for first, last, members in groups:
        if len(members) == 1:
            continue
        if last - first not in (2, 4):
            raise ArgumentError(f"{function.name}: stack slots {sorted(members)} overlap over {last - first} bytes")
        for member in members:
            if member != (first, last):
                whole[member] = (first, last)
    if not whole:
        return

    def change(node):
        if isinstance(node, ir.Stack) and (node.offset, node.offset + node.bits // 8) in whole:
            first, last = whole[(node.offset, node.offset + node.bits // 8)]
            return extract(ir.Stack(first, (last - first) * 8), first, last - first,
                           node.offset, node.offset + node.bits // 8)
        return None
    for block in function.blocks.values():
        for index, statement in enumerate(block.statements):
            part = None
            if isinstance(statement, ir.SetStack):
                part = (statement.offset, statement.offset + statement.bits // 8)
            if part not in whole:
                ir.map_statement(statement, change)
                continue
            first, last = whole[part]
            old = ir.Stack(first, (last - first) * 8)
            pieces = [(part[0], part[1] - part[0], ir.rebuild(statement.value, change))]
            if first < part[0]:
                pieces.append((first, part[0] - first, extract(old, first, last - first, first, part[0])))
            if part[1] < last:
                pieces.append((part[1], last - part[1], extract(old, first, last - first, part[1], last)))
            joined_store = ir.SetStack(first, compose(pieces, first, last), (last - first) * 8)
            if hasattr(statement, "origin"):
                joined_store.origin = statement.origin
            block.statements[index] = joined_store
        ir.map_statement(block.end, change)


class Model:
    def __init__(self, functions):
        self.functions = functions
        self.start = cpu.current.return_address_bytes
        self.layouts = {}
        self.problems = {}
        self.skipped = 0          # arguments of a call the caller never pushed
        self.calls = 0

    def targets(self, call):
        if isinstance(call, ir.TailCall):
            found = [call.target]
        else:
            found = [call.target] if call.target is not None else list(call.choices)
        return [target for target in found if target is not None and target & 0xFFFFFF in self.functions]

    def key(self, target):
        return target & 0xFFFFFF

    def solve(self):
        """The arguments of every function: its own reads and, for a tail call, the callee's (the caller's own
        arguments are the callee's)."""
        direct = {entry: accesses_of(function) for entry, function in self.functions.items()}
        accesses = {entry: set(found) for entry, found in direct.items()}
        changed = True
        while changed:
            changed = False
            self.layouts = {entry: regions_of(found) for entry, found in accesses.items()}
            for entry, function in self.functions.items():
                for call in self.sites(function):
                    base = self.base(call)
                    if base is None:
                        continue
                    for target in self.targets(call):
                        for offset, size in self.layouts[self.key(target)]:
                            place = base + offset
                            if place >= self.start and (place, size) not in accesses[entry]:
                                accesses[entry].add((place, size))
                                changed = True
        return self.layouts

    def sites(self, function):
        for block in function.blocks.values():
            for statement in block.statements:
                if isinstance(statement, ir.Call):
                    yield statement
            if isinstance(block.end, ir.TailCall):
                yield block.end

    def base(self, call):
        """Where the callee's +0 (its return address) is, in the caller's offsets, or None."""
        sp = getattr(call, "sp", None)
        if sp is None:
            return None
        return sp - self.start if isinstance(call, ir.Call) else sp

    def pushed(self, function, first, last, block=None, upto=0):
        """What the function stored in bytes [first, last) of its stack, or None. The stores before the call, in
        its own block, count first, the last one to each byte; else all those of the function, which must not
        overlap each other."""
        if block is not None:
            owners = {}
            for statement in reversed(block.statements[:upto]):
                if isinstance(statement, ir.SetStack) and statement.offset < self.start:
                    for byte in range(max(first, statement.offset), min(last, statement.offset + statement.bits // 8)):
                        owners.setdefault(byte, statement)
            if len(owners) == last - first:
                pieces = []
                for byte in range(first, last):
                    statement = owners[byte]
                    if pieces and pieces[-1][2] is statement:
                        pieces[-1][1] = byte + 1
                    else:
                        pieces.append([byte, byte + 1, statement])
                return compose([(low, high - low, extract(ir.Stack(statement.offset, statement.bits),
                                                          statement.offset, statement.bits // 8, low, high))
                                for low, high, statement in pieces], first, last)
        slots = {(statement.offset, statement.bits // 8)
                 for block in function.blocks.values() for statement in block.statements
                 if isinstance(statement, ir.SetStack) and statement.offset < self.start}
        near = [slot for slot in slots if slot[0] < last and slot[0] + slot[1] > first]
        if not near:
            return None
        pieces = [(offset, size, ir.Stack(offset, size * 8)) for offset, size in near]
        exact = [piece for piece in pieces if piece[0] == first and piece[1] == last - first]
        if exact:
            return exact[0][2]
        if len({piece[:2] for piece in pieces}) != len(pieces):
            return None
        for piece in pieces:
            overlap = [other for other in pieces if other is not piece and other[0] < piece[0] + piece[1]
                       and piece[0] < other[0] + other[1]]
            if overlap:
                raise ArgumentError(f"{function.name}: stack slots {piece[0]} and {overlap[0][0]} overlap at a call")
        return compose(pieces, first, last)

    def callee_view(self, function, regions, copies):
        """A change() that turns the function's reads of its caller's stack into what holds them: the slot that
        copies a region (those in 'copies', by offset) or its register."""
        sources = []
        for offset, size in regions:
            if offset in copies:
                sources.append((offset, size, ir.Stack(offset, size * 8)))
            else:
                sources.append((offset, size, ir.trunc(ir.Reg(register_for(offset), 32), size * 8)))

        def change(node):
            if isinstance(node, ir.Stack) and node.offset >= self.start:
                value = compose(sources, node.offset, node.offset + node.bits // 8)
                if value is None:
                    raise ArgumentError(f"{function.name}: reads its caller's stack at {node.offset:+d}, {node.bits} bits")
                return value
            return None
        return change

    def copied(self, function, regions):
        """The offsets of the arguments the function keeps in slots: those it writes, and those it reads after a call,
        which sets the argument's register again for its own callee (and the data flow takes it as written)."""
        blocks = function.blocks
        after = set()
        pending = [successor for label, block in blocks.items()
                   if any(isinstance(statement, ir.Call) for statement in block.statements)
                   for successor in block.successors()]
        while pending:
            label = pending.pop()
            if label not in after:
                after.add(label)
                pending.extend(blocks[label].successors())
        found = set()

        def covering(offset, size):
            return {first for first, bytes_ in regions if first < offset + size and offset < first + bytes_}

        for label, block in blocks.items():
            called = label in after
            for statement in block.statements + [block.end]:
                if isinstance(statement, ir.SetStack) and statement.offset >= self.start:
                    found |= covering(statement.offset, statement.bits // 8)
                if called:
                    for expression in ir.statement_expressions(statement):
                        for node in ir.walk(expression):
                            if isinstance(node, ir.Stack) and node.offset >= self.start:
                                found |= covering(node.offset, node.bits // 8)
                if isinstance(statement, ir.Call):
                    called = True
                # (a function whose frame goes to memory and that takes an argument's address reads them through it:
                # all of them are in its frame, stack.to_memory)
                if getattr(function, "memory_frame", False) and any(
                        isinstance(node, ir.Op) and node.name == "stack_address"
                        and node.args[0].value < 0x80000000 and node.args[0].value >= self.start
                        for expression in ir.statement_expressions(statement) for node in ir.walk(expression)):
                    found |= {first for first, _ in regions}
        return found

    def rewrite(self, function):
        # the calls first: their setups read the function's own arguments for a tail call
        for block in function.blocks.values():
            self.setups(function, block)
        regions = self.layouts.get(function.entry, [])
        if not regions:
            return
        copies = self.copied(function, regions)
        change = self.callee_view(function, regions, copies)
        for block in function.blocks.values():
            for statement in block.statements:
                if isinstance(statement, ir.SetStack) and statement.offset >= self.start:
                    if (statement.offset, statement.bits // 8) not in regions:
                        raise ArgumentError(f"{function.name}: writes part of its argument at {statement.offset:+d}")
                    statement.value = ir.rebuild(statement.value, change)
                    continue
                ir.map_statement(statement, change)
            ir.map_statement(block.end, change)
        if copies:
            copy_in = [ir.SetStack(offset, ir.trunc(ir.Reg(register_for(offset), 32), size * 8), size * 8)
                       for offset, size in regions if offset in copies]
            self.prepend(function, function.blocks[function.entry], copy_in)

    def prepend(self, function, entry, statements):
        """The statements first in the function, once: a branch back to its first block does not run them again."""
        label = function.entry
        if any(label in block.successors() for block in function.blocks.values()):
            body = ("arguments", label)
            function.blocks[body] = type(entry)(body, entry.statements, entry.end, list(entry.addresses))
            for block in function.blocks.values():
                if block is not entry and block is not function.blocks[body]:
                    stack.redirect(block.end, label, body)
            entry.statements = list(statements)
            entry.end = ir.Goto(body)
            function.blocks[body].end.origin = getattr(function.blocks[body].end, "origin", None)
        else:
            entry.statements = list(statements) + entry.statements

    def setups(self, function, block):
        """Before each call of the block: the registers of what its callees read, from the slots the function pushed"""
        index = 0
        statements = block.statements
        while index <= len(statements):
            call = statements[index] if index < len(statements) else block.end
            if isinstance(call, (ir.Call, ir.TailCall)) and (isinstance(call, ir.Call) or index == len(statements)):
                setup = self.setup_for(function, call, block, index)
                if isinstance(call, ir.Call):
                    statements[index:index] = setup
                    index += len(setup)
                else:
                    statements.extend(setup)
                    index += len(setup)
            index += 1

    def setup_for(self, function, call, block=None, upto=0):
        base = self.base(call)
        if base is None:
            return []
        self.calls += 1
        wanted = {}
        for target in self.targets(call):
            for offset, size in self.layouts[self.key(target)]:
                wanted[offset] = max(size, wanted.get(offset, 0))
        setup = []
        for offset, size in sorted(wanted.items()):
            place = base + offset
            if place >= self.start:
                value = compose([(place, size, ir.Stack(place, size * 8))], place, place + size)
            else:
                value = self.pushed(function, place, place + size, block, upto)
            if value is None:
                self.skipped += 1
                continue
            statement = ir.SetReg(register_for(offset), ir.zext(value, 32))
            statement.origin = getattr(call, "origin", None)
            setup.append(statement)
        return setup


def apply(functions):
    """Rewrites the 68000 functions in place (stack.analyse() has run on each: the slots, and the SP at each call):
    their stack arguments are the virtual registers of the data flow. Returns the Model, for what it counted."""
    model = Model(functions)
    for function in functions.values():
        merge_pushes(function)
    layouts = model.solve()
    for entry, function in functions.items():
        try:
            # (first: a call's setup reads what was pushed, at one extent per slot; a frame that goes to memory keeps
            # each access at its own width, as the bytes there are one: stack.to_memory)
            if not getattr(function, "memory_frame", False):
                widen(function)
            model.rewrite(function)
        except ArgumentError as problem:
            model.problems[entry] = str(problem)
            function.argument_problem = str(problem)    # (emit.py refuses to write it)
    used = sorted({register_for(offset) for regions in layouts.values() for offset, _ in regions})
    if used:
        profile = cpu.current
        names = {index: name_of(index) for index in used}
        cpu.use(dataclasses.replace(profile, registers=profile.registers + tuple(used),
                                    register_bits={**profile.register_bits, **{index: 32 for index in used}},
                                    register_names={**profile.register_names, **names},
                                    variable_names={**profile.variable_names, **names}))
    return model
