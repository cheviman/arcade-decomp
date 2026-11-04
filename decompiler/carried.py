"""
Name:        carried.py
Description: what the registers carry from one function to another for later code to read, part by part: those
             parts of the registers become global variables in C, the rest stay locals and parameters
Version:     0.1.0
Created:     01-11-2026
Modified:    01-11-2026
Author:      Sebastian Toyos

A routine can leave something in a register that a later one, which no call links to it, reads before it sets it:
TMNT's state handlers leave the high word of D7 to the next one, which adds it to a position. In C each function
would pass it on as a parameter and a result, from the frame's start down. This module finds those values and moves
them out of the registers into virtual ones (one per register, past the processor's own), which the decompiler keeps
as global variables (signatures.CONTEXT, with signatures.SCRATCH_CONTEXT saying no part of them is live where a
function ends unless its callers read it): the game's state the frame carries, as the V60 games keep their context
registers.

By part: every function's webs of each part of each register (byte, byte, high halfword: the definitions and the
uses they reach, with the data flow's liveness and demanded bits) are joined across functions through the ports
they meet: what a function reads from its caller and hands back, what a call reads and hands back, what an
interrupt's handlers read (an interrupt may come where the code waits for the frame; its handler reads the
registers the code it interrupted had). A class of webs that holds a read no caller sets (an input that starts at
0 in C: the handler's own registers, which the interrupted code left) is what the code carries: its webs, and only
them, go to the virtual register. D7's low byte, where the routines put a sound code or a count of their own for a
call, stays a local or a parameter; its high word, which a handler adds to a position after the one before left the
sign of a step there, becomes the global.

Analysis works on the program once its data flow is solved; split() changes the functions' statements in place: a
read of a carried part reads the virtual register, a write of one writes it (and the register too, for the parts the
write puts in webs that stay). Where a function works out a value itself and reads its carried part together with
parts that stay (draw_box_frame counts the tile offset up in D1's word and leaves its high byte for later code), the
value stays a local of the function, copied into the virtual register before a call or an interrupt that reads it
and where the function ends: "(tile_offset & 0xFF) | (v & 0xFF00)" at every use would say the same less plainly. The
data flow is then solved again with the virtual registers among the context ones.
"""
from v60 import ir

from . import cleanup
from . import cpu
from . import dataflow

PARTS = dataflow.PARTS
FULL = dataflow.FULL


class Union:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[b] = a


def parts_of(mask):
    return [part for part, bits in PARTS if mask & bits]


def demands(node, mask, out, temps):
    """dataflow.demanded, noting each register node it reaches and the bits wanted of it in 'out' ([(node, mask)])"""
    if not mask:
        return
    if isinstance(node, ir.Reg):
        if cpu.kept(node.index):
            out.append((node, mask))
        return
    if isinstance(node, ir.Temp):
        temps[node.name] = temps.get(node.name, 0) | mask
        return
    if isinstance(node, ir.Load):
        demands(node.address, FULL, out, temps)
        return
    if not isinstance(node, ir.Op):
        return
    name, args = node.name, node.args
    width = dataflow.width
    if name in ("trunc", "zext"):
        demands(args[0], mask & width(min(node.bits, args[0].bits)), out, temps)
    elif name == "sext":
        source = args[1].value
        needed = mask & width(source)
        if mask & ~width(source):
            needed |= 1 << (source - 1)
        demands(args[0], needed, out, temps)
    elif name == "and":
        left, right = args
        demands(left, mask & right.value if isinstance(right, ir.Const) else mask, out, temps)
        demands(right, mask & left.value if isinstance(left, ir.Const) else mask, out, temps)
    elif name in ("or", "xor", "not"):
        for arg in args:
            demands(arg, mask, out, temps)
    elif name in ("add", "sub", "mul", "neg"):
        for arg in args:
            demands(arg, dataflow.below(mask) & width(arg.bits), out, temps)
    elif name == "shl" and isinstance(args[1], ir.Const):
        demands(args[0], (mask >> args[1].value) & width(args[0].bits), out, temps)
    elif name == "shr" and isinstance(args[1], ir.Const):
        demands(args[0], (mask << args[1].value) & width(args[0].bits), out, temps)
    elif name == "sar" and isinstance(args[1], ir.Const):
        demands(args[0], dataflow.shifted_right(mask, args[1].value, args[0].bits, True), out, temps)
    elif name == "select":
        demands(args[0], FULL, out, temps)
        demands(args[1], mask, out, temps)
        demands(args[2], mask, out, temps)
    else:
        for arg in args:
            demands(arg, width(arg.bits), out, temps)


def own_uses(flow, statement, temps):
    """[(register node, bits wanted)] of a statement's own expressions (what temporaries are wanted goes to 'temps')"""
    out = []
    if isinstance(statement, ir.SetTemp):
        demands(statement.value, temps.pop(statement.temp.name, 0), out, temps)
    elif isinstance(statement, ir.SetReg):
        if not cpu.kept(statement.index):
            demands(statement.value, FULL, out, temps)
            return out
        _, rest, keep = dataflow.partial_write(statement)
        written = flow.written_parts(statement)
        bits = 0
        for part, part_bits in PARTS:
            if part in written:
                bits |= part_bits
        # (in "R = (R & keep) | rest" the kept parts are no read: they stay what they were)
        demands(rest if keep else statement.value, bits, out, temps)
    elif isinstance(statement, ir.SetFlag):
        demands(statement.value, 1, out, temps)
    elif isinstance(statement, ir.SetStack):
        demands(statement.value, dataflow.width(statement.bits), out, temps)
    elif isinstance(statement, ir.Store):
        demands(statement.address, FULL, out, temps)
        demands(statement.value, dataflow.width(statement.bits), out, temps)
    else:
        for expression in ir.statement_expressions(statement):
            demands(expression, FULL, out, temps)
    return out


def exit_live_of(flow, function, results):
    """What cleanup.eliminate takes for live where the function ends, with no RESULT_BITS or RESULT_PARTS"""
    if len(results) > 1:
        written = flow.summaries[function.entry].writes
        return {variable for variable in written if (variable[0], variable[1]) in set(results)}
    return cleanup.variables_of(results)


class Analysis:
    """The webs of every part of 'registers' in every function, joined into classes through the ports they meet.
    'signatures' are the functions' (signatures.compute), 'arguments' what each takes by value (as cleanup.eliminate
    has it)"""

    def __init__(self, flow, built, signatures, arguments, registers,
                 interrupt_effects=("frame_wait", "take_interrupts")):
        self.flow = flow
        self.built = built
        self.registers = set(registers)
        self.interrupt_effects = interrupt_effects
        self.union = Union()         # the classes: definitions and ports
        self.webs = Union()          # the webs: the definitions of one function that reach a common use
        self.used = set()            # definitions some use reads (an interrupt's uses are taken apart: join)
        self.uses = {}               # entry -> [(id(statement), use, register, part, definitions reaching)]
        self.set_definitions = set() # the definitions of assignments (the rest: what calls hand back, the entry)
        self.call_definitions = []   # (entry, id(call), register, part, definition)
        self.keeps = {}              # entry -> cleanup's {id(statement that stays): variables live after it}
        self.entries = {}            # entry -> [(register, part)] its callers set, or nothing sets (zeroed)
        self.demand = {}             # (id(statement), id(register node)) -> the bits the statement wants of it
        self.node_uses = []          # (id(statement), register node, part, definitions reaching)
        self.statements = []         # the statements analysed (kept, so that no new one takes their id)
        for entry, function in built.items():
            self.function(entry, function, exit_live_of(flow, function, signatures[entry].results), arguments)
        self.join()

    def function(self, entry, function, exit_live, arguments):
        flow = self.flow
        liveness = cleanup.Liveness(flow, function, exit_live,
                                    cleanup.passed_by_value(flow, function, arguments) if arguments else {})
        live_in = liveness.solve()
        keep = {}
        for label, block in function.blocks.items():
            liveness.backwards(block, live_in, keep)
            self.statements += block.statements + [block.end]
        self.keeps[entry] = keep
        summary = flow.summaries[entry]
        zeroed = flow.zeroed.get(entry, set())
        handed = flow.exit_live.get(entry, frozenset())
        # each statement's uses: (register node, or a port: ("in", callee), ("out", function), ("interrupt",))
        uses_at = {}
        for label, block in function.blocks.items():
            temps = {}
            uses_at[id(block.end)] = self.end_uses(entry, block.end, handed, temps)
            for statement in reversed(block.statements):
                if id(statement) not in keep:
                    continue
                own = own_uses(flow, statement, temps)
                for node, mask in own:
                    self.demand[(id(statement), id(node))] = self.demand.get((id(statement), id(node)), 0) | mask
                found = [(node, node.index, part) for node, mask in own
                         if node.index in self.registers for part in parts_of(mask)]
                if isinstance(statement, ir.Call):
                    for variable in flow.call_reads(statement, keep[id(statement)]):
                        if variable[0] == "r" and variable[1] in self.registers:
                            for target in flow.call_targets(statement):
                                found.append((("in", target), variable[1], variable[2]))
                elif isinstance(statement, ir.Effect) and statement.name in self.interrupt_effects:
                    found += [(("interrupt",), index, part) for index in self.registers for part, _ in PARTS]
                uses_at[id(statement)] = found
        starts = {function.entry: {(index, part): frozenset([("entry", entry, index, part)])
                                   for index in self.registers for part, _ in PARTS}}
        pending = [function.entry]

        def record(key, state):
            for use, index, part in uses_at.get(key, ()):
                reaching = state.get((index, part), frozenset())
                self.uses.setdefault(entry, []).append((key, use, index, part, reaching))
                if not isinstance(use, tuple):
                    self.node_uses.append((key, use, part, reaching))
                if use != ("interrupt",):
                    self.used.update(reaching)

        def define(statement, state, recording):
            if id(statement) not in keep:
                return
            if isinstance(statement, ir.SetReg) and statement.index in self.registers:
                for part in flow.written_parts(statement):
                    definition = ("def", id(statement), statement.index, part)
                    state[(statement.index, part)] = frozenset([definition])
                    if recording:
                        self.union.find(definition)
                        self.webs.find(definition)
                        self.set_definitions.add(definition)
            elif isinstance(statement, ir.Call):
                _, writes, always = flow.call_effect(statement)
                for variable in writes:
                    if variable[0] != "r" or variable[1] not in self.registers:
                        continue
                    definition = ("def", id(statement), variable[1], variable[2])
                    key = (variable[1], variable[2])
                    state[key] = frozenset([definition]) if variable in always \
                        else state.get(key, frozenset()) | {definition}
                    if recording:
                        self.call_definitions.append((entry, id(statement), variable[1], variable[2], definition))
                        for target in flow.call_targets(statement):
                            self.union.join(definition, ("port", "out", target, variable[1], variable[2]))

        while pending:
            label = pending.pop()
            state = dict(starts[label])
            for statement in function.blocks[label].statements:
                define(statement, state, False)
            for successor in function.blocks[label].successors():
                old = starts.get(successor)
                merged = dict(state) if old is None else {key: old.get(key, frozenset()) | state.get(key, frozenset())
                                                           for key in set(old) | set(state)}
                if merged != old:
                    starts[successor] = merged
                    pending.append(successor)
        for label, state in starts.items():
            state = dict(state)
            for statement in function.blocks[label].statements:
                record(id(statement), state)
                define(statement, state, True)
            end = function.blocks[label].end
            if isinstance(end, ir.TailCall):
                # (what the function it goes on in hands back is this one's)
                _, writes, _ = flow.combine([end.target])
                for variable in handed:
                    if variable[0] == "r" and variable[1] in self.registers and variable in writes:
                        self.union.join(("port", "out", end.target, variable[1], variable[2]),
                                        ("port", "out", entry, variable[1], variable[2]))
            record(id(end), state)
        self.entries[entry] = [(index, part) for index in self.registers for part, _ in PARTS
                               if ("r", index, part) in summary.reads or ("r", index) in zeroed]

    def end_uses(self, entry, end, handed, temps):
        """The uses of a block's end: what the function hands back, what the function it goes on in reads"""
        found = []
        if isinstance(end, ir.Return):
            found += [(("out", entry), v[1], v[2]) for v in handed if v[0] == "r" and v[1] in self.registers]
        elif isinstance(end, ir.TailCall):
            reads = self.flow.target_reads([end.target], set(handed))
            found += [(("in", end.target), v[1], v[2]) for v in reads if v[0] == "r" and v[1] in self.registers]
            _, _, always = self.flow.combine([end.target])
            found += [(("out", entry), v[1], v[2]) for v in handed
                      if v[0] == "r" and v[1] in self.registers and v not in always]
        for expression in ir.statement_expressions(end):
            out = []
            demands(expression, FULL, out, temps)
            for node, mask in out:
                self.demand[(id(end), id(node))] = self.demand.get((id(end), id(node)), 0) | mask
            found += [(node, node.index, part) for node, mask in out if node.index in self.registers
                      for part in parts_of(mask)]
        return found

    def join(self):
        """The webs (the definitions that reach a common use) and the ports they meet, once every function's uses are
        known. An interrupt reads what its handlers read for themselves, whatever the code it interrupts holds (the
        handler's own reads of the interrupt are no reads of it)"""
        flow = self.flow
        handled = set()
        for handler in flow.interrupt_handlers:
            for index, part in self.entries.get(handler, ()):
                if ("entry", handler, index, part) in self.used:
                    handled.add((index, part))
        self.handled = handled
        for entry, uses in self.uses.items():
            for _, use, index, part, reaching in uses:
                if use == ("interrupt",) and (index, part) not in handled:
                    continue
                reaching = sorted(reaching, key=repr)
                for other in reaching[1:]:
                    self.union.join(reaching[0], other)
                    self.webs.join(reaching[0], other)
                if reaching and isinstance(use, tuple):
                    self.union.join(reaching[0], ("port",) + use + (index, part))
        for entry, found in self.entries.items():
            summary = flow.summaries[entry]
            handler = entry in flow.interrupt_handlers
            for index, part in found:
                definition = ("entry", entry, index, part)
                if definition not in self.used:
                    continue
                if ("r", index, part) in summary.reads:
                    self.union.join(definition, ("port", "in", entry, index, part))
                else:
                    # (no caller sets it: what code before left there)
                    self.union.join(definition, ("port", "seed", entry, index, part))
                if handler:
                    self.union.join(definition, ("port", "interrupt", index, part))

    def seeded(self):
        """The roots of the classes that hold a seed: what the code carries"""
        return {self.union.find(item) for item in list(self.union.parent) if item[0] == "port" and item[1] == "seed"}

    def carries_through(self, function, index):
        """True if a carried class of register 'index' goes through 'function': it reads one in its code, or calls
        a function that reads or hands back one"""
        seeded = self.seeded()

        def carried(key):
            return key in self.union.parent and self.union.find(key) in seeded
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                if isinstance(statement, ir.SetReg) and statement.index == index \
                        and any(carried(("def", id(statement), index, part)) for part, _ in PARTS):
                    return True
                if isinstance(statement, ir.Call):
                    targets = self.flow.call_targets(statement)
                elif isinstance(statement, ir.TailCall):
                    targets = [statement.target]
                else:
                    continue
                for target in targets:
                    if any(carried(("port", kind, target, index, part)) for kind in ("in", "out") for part, _ in PARTS):
                        return True
        return any(carried(("entry", function.entry, index, part)) for part, _ in PARTS)

    def carried_parts(self):
        """{register: the parts of it some carried class holds}"""
        seeded = self.seeded()
        found = {}
        for item in list(self.union.parent):
            if item[0] == "port" and self.union.find(item) in seeded:
                found.setdefault(item[-2], set()).add(item[-1])
        return found

    def kinds(self):
        """(the webs of the carried classes the virtual registers hold, those that stay locals of their function with
        copies where they cross): what a function works out itself and reads (the tile offset draw_box_frame counts
        up in D1's word, whose high byte it leaves for later code; the table of places a handler points A5 at) stays
        a local, even when a register carries it on, copied into the virtual register where it crosses; the rest
        (what it reads of what came in, or only leaves for later code) is the global"""
        seeded = self.seeded()
        web = self.webs.find
        carried = set()
        for item in list(self.union.parent):
            if item[0] in ("def", "entry") and self.union.find(item) in seeded:
                carried.add(web(item))
        # the carried webs of one register's parts that one read reads, or one assignment writes, together go one way
        groups = Union()
        first = {}
        for definition in self.set_definitions:
            if web(definition) in carried:
                groups.join(first.setdefault(definition[1:3], web(definition)), web(definition))
        together = {}
        for key, node, part, reaching in self.node_uses:
            if reaching:
                together.setdefault((key, id(node)), []).append(web(next(iter(reaching))))
        read = set()
        for found in together.values():
            kept = [root for root in found if root in carried]
            for other in kept[1:]:
                groups.join(kept[0], other)
            if kept:
                read.add(kept[0])    # (a read of its own)
        read = {groups.find(root) for root in read}
        # (and only what the function works out itself: no value that comes in from its caller or a call)
        outside = {groups.find(web(item)) for item in list(self.webs.parent) if item[0] == "entry"}
        outside |= {groups.find(web(definition)) for _, _, _, _, definition in self.call_definitions}
        local = {root for root in carried if groups.find(root) in read and groups.find(root) not in outside}
        return carried - local, local

    def split(self, built, virtual):
        """Moves the carried classes' webs out of their registers into the virtual ones ('virtual': {register: its
        virtual register}), in place: a read of a part those webs hold reads the virtual register, a write of one
        writes it (and the register too, for the parts the write puts in webs that stay). A web that stays a local
        (kinds: what the function works out itself) is copied into the virtual register where it crosses: before a
        call or an interrupt that reads it, where the function ends. Returns the registers whose webs moved"""
        global_webs, local_webs = self.kinds()
        self.global_webs, self.local_webs = global_webs, local_webs
        web = self.webs.find
        everywhere = self.carried_parts()
        carried_bits = {}
        for key, node, part, reaching in self.node_uses:
            if node.index in virtual and reaching and web(next(iter(reaching))) in global_webs:
                carried_bits[(key, id(node))] = carried_bits.get((key, id(node)), 0) | PARTS[part][1]
        # the copies: id(statement) -> {register: bits} from the register into the virtual one before it
        before = {}
        for entry, uses in self.uses.items():
            for key, use, index, part, reaching in uses:
                if not isinstance(use, tuple) or index not in virtual or not reaching:
                    continue
                if use == ("interrupt",) and (index, part) not in self.handled:
                    continue
                if web(next(iter(reaching))) in local_webs:
                    found = before.setdefault(key, {})
                    found[index] = found.get(index, 0) | PARTS[part][1]
        moved = set()

        def into_virtual(index, bits):
            moved.add(index)
            other = virtual[index]
            whole = 0
            for part in everywhere.get(index, ()):
                whole |= PARTS[part][1]
            if not whole & ~bits:
                return ir.SetReg(other, ir.Reg(index))
            return ir.SetReg(other, ir.Op("or", (ir.Op("and", (ir.Reg(other), ir.Const(~bits & FULL, 32)), 32),
                                                 ir.Op("and", (ir.Reg(index), ir.Const(bits, 32)), 32)), 32))

        def copies(statement, found, make):
            made = []
            for index, bits in sorted(found.items()):
                copy = make(index, bits)
                if getattr(statement, "origin", None) is not None:
                    copy.origin = statement.origin
                made.append(copy)
            return made

        def reader(statement):
            def change(node):
                if not isinstance(node, ir.Reg) or node.index not in virtual:
                    return None
                mask = carried_bits.get((id(statement), id(node)), 0)
                if not mask:
                    return None
                moved.add(node.index)
                wanted = self.demand.get((id(statement), id(node)), FULL)
                other = ir.Reg(virtual[node.index], node.bits)
                if not wanted & ~mask:
                    return other
                return ir.Op("or", (ir.Op("and", (node, ir.Const(~mask & FULL, 32)), 32),
                                    ir.Op("and", (other, ir.Const(mask, 32)), 32)), 32)
            return change

        for entry, function in built.items():
            keep = self.keeps[entry]
            for label, block in function.blocks.items():
                statements = []
                for statement in block.statements:
                    statements += copies(statement, before.get(id(statement), {}), into_virtual)
                    if isinstance(statement, ir.SetReg) and statement.index in virtual and id(statement) in keep:
                        statements += self.split_write(statement, virtual[statement.index], reader(statement),
                                                       global_webs, moved)
                    else:
                        ir.map_statement(statement, reader(statement))
                        statements.append(statement)
                statements += copies(block.end, before.get(id(block.end), {}), into_virtual)
                block.statements = statements
                ir.map_statement(block.end, reader(block.end))
        return moved

    def split_write(self, statement, other, change, global_webs, moved):
        """The statements that write what 'statement' (a SetReg of a register with a virtual one) writes: the parts
        whose webs the virtual register holds go to 'other', the rest stay in the register"""
        flow = self.flow
        index = statement.index
        written = flow.written_parts(statement)
        carried = {part for part in written if self.webs.find(("def", id(statement), index, part)) in global_webs}
        if not carried:
            ir.map_statement(statement, change)
            return [statement]
        moved.add(index)
        _, rest, keep = dataflow.partial_write(statement)
        kept_bits = FULL
        for part, bits in PARTS:
            if part in written:
                kept_bits &= ~bits
        origin = getattr(statement, "origin", None)
        own = written - carried
        found = []
        value = ir.rebuild(rest if keep else statement.value, change)

        def kept(register, bits, new, masked):
            # (the parts the write leaves as they were stay so: "R = (R & keep) | rest", "bset #7, D7")
            if not bits:
                return new
            return ir.Op("or", (ir.Op("and", (ir.Reg(register), ir.Const(bits, 32)), 32),
                                new if masked else ir.Op("and", (new, ir.Const(~bits & FULL, 32)), 32)), 32)
        if own:
            # the register as the code writes it, then the virtual register from it ("x = ...; carried = x;")
            found.append(ir.SetReg(index, kept(index, keep, value, True) if keep else value))
            found.append(ir.SetReg(other, kept(other, kept_bits, ir.Reg(index), False)))
        else:
            found.append(ir.SetReg(other, kept(other, keep, value, True) if keep else kept(other, kept_bits, value,
                                                                                           False)))
        for new in found:
            if origin is not None:
                new.origin = origin
        return found
