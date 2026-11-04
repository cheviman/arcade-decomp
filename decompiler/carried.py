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
# whether a port where the function on the other side does something of its own with the value (a parameter it reads
# for itself, a result it works out) joins no classes: the value crosses it by a copy where the two sides keep it in
# different places (the caller in the global, the callee in its parameter). Off by default (every port joins: a class
# takes in every function a carried value goes through, and with them the values those functions are handed for
# themselves); the 68000's (m68000/decompile.py, the profile's CARRIED_CUTS)
CUT_OWN_PORTS = False


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
                 interrupt_effects=("frame_wait", "take_interrupts"), seeds=()):
        self.flow = flow
        self.built = built
        self.registers = set(registers)
        self.interrupt_effects = interrupt_effects
        # (function, register): what the register holds anywhere in the function is carried (the object loop hands
        # each object's routine what the routine before left), each its own global (groups)
        self.seeds = set(seeds)
        self.union = Union()         # the classes: definitions and ports
        self.webs = Union()          # the webs: the definitions of one function that reach a common use
        self.used = set()            # definitions some use reads (an interrupt's uses are taken apart: join)
        self.uses = {}               # entry -> [(id(statement), use, register, part, definitions reaching)]
        self.set_definitions = set() # the definitions of assignments (the rest: what calls hand back, the entry)
        self.call_definitions = []   # (entry, id(call), register, part, definition)
        self.owners = {}             # definition of an assignment or a call -> the function it is in
        self.call_targets_of = {}    # definition of a call -> the functions the call may run
        self.tail_links = []         # (function, function it goes on in, register, part): the one hands back the other's
        self.tail_calls = set()      # id(tail call)
        self.cut_definitions = set() # definitions of calls whose results cross by a copy (CUT_OWN_PORTS)
        self.cut_uses = set()        # (id(call), callee, register, part) whose parameter a copy hands over
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
                        self.owners[definition] = entry
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
                        self.owners[definition] = entry
                        self.call_targets_of[definition] = list(flow.call_targets(statement))
                        if not CUT_OWN_PORTS:
                            # (with CUT_OWN_PORTS, join() decides, once it knows what each callee hands back)
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
                self.tail_calls.add(id(end))
                _, writes, _ = flow.combine([end.target])
                for variable in handed:
                    if variable[0] == "r" and variable[1] in self.registers and variable in writes:
                        self.union.join(("port", "out", end.target, variable[1], variable[2]),
                                        ("port", "out", entry, variable[1], variable[2]))
                        self.tail_links.append((entry, end.target, variable[1], variable[2]))
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
        own_reads, called = self.own_ports() if CUT_OWN_PORTS else (set(), {})
        for entry, uses in self.uses.items():
            for key, use, index, part, reaching in uses:
                if use == ("interrupt",) and (index, part) not in handled:
                    continue
                reaching = sorted(reaching, key=repr)
                for other in reaching[1:]:
                    self.union.join(reaching[0], other)
                    self.webs.join(reaching[0], other)
                if reaching and isinstance(use, tuple):
                    if use[0] == "in" and CUT_OWN_PORTS \
                            and all((target, index, part) in own_reads for target in called.get((key, index, part), ())):
                        # (a parameter every function the call may run reads for itself: a copy hands it over)
                        self.cut_uses.add((key, use[1], index, part))
                        continue
                    self.union.join(reaching[0], ("port",) + use + (index, part))
        if CUT_OWN_PORTS:
            # (the results some caller reads for itself after the call: print_text_at's last character, which its
            # callers only leave for later code, stays the global's where it is worked out)
            read_after = {definition for _, _, _, reaching in self.node_uses for definition in reaching}
            wanted = {(target, definition[2], definition[3]) for definition, targets in self.call_targets_of.items()
                      if definition in read_after for target in targets}
            for _, _, index, part, definition in self.call_definitions:
                targets = self.call_targets_of.get(definition, ())
                if targets and all((target, index, part) not in self.passes and (target, index, part) in wanted
                                   for target in targets):
                    # (a result every function the call may run works out itself, which a caller reads for itself:
                    # a copy takes it)
                    self.cut_definitions.add(definition)
                    continue
                for target in targets:
                    self.union.join(definition, ("port", "out", target, index, part))
        # the explicit seeds: what the register holds anywhere in their function
        self.explicit = set()
        for definition, owner in self.owners.items():
            if (owner, definition[2]) in self.seeds:
                port = ("port", "seed", owner, definition[2], definition[3])
                self.union.join(definition, port)
                self.explicit.add(port)
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
                if (entry, index) in self.seeds:
                    port = ("port", "seed", entry, index, part)
                    self.union.join(definition, port)
                    self.explicit.add(port)

    def own_ports(self):
        """(the (function, register, part) whose entry value the function reads for itself, {(id(call), register,
        part): the functions the call may run}), and self.passes: the (function, register, part) that hand back what
        came in, or what a call that does hands back (enemy_nearest_turtles keeps A5 when no turtle answers), not
        only what they work out themselves"""
        own_reads = {definition[1:] for _, _, _, reaching in self.node_uses for definition in reaching
                     if definition[0] == "entry"}
        called = {}
        exits = {}
        for entry, uses in self.uses.items():
            for key, use, index, part, reaching in uses:
                if isinstance(use, tuple) and use[0] == "in":
                    called.setdefault((key, index, part), set()).add(use[1])
                elif use == ("out", entry):
                    exits.setdefault((entry, index, part), set()).update(reaching)
        passes = set()
        changed = True
        while changed:
            changed = False
            for found, reaching in exits.items():
                if found in passes:
                    continue
                _, index, part = found
                if any(definition[0] == "entry" or any((target, index, part) in passes
                                                       for target in self.call_targets_of.get(definition, ()))
                       for definition in reaching):
                    passes.add(found)
                    changed = True
            for entry, target, index, part in self.tail_links:
                if (entry, index, part) not in passes and (target, index, part) in passes:
                    passes.add((entry, index, part))
                    changed = True
        self.passes = passes
        return own_reads, called

    def group(self, item):
        """The global a carried class's item goes to: the function of an explicit seed in its class (the first by
        address), or None (what the frame carries)"""
        if not hasattr(self, "_groups"):
            self._groups = {}
            for port in sorted(self.explicit):
                self._groups.setdefault(self.union.find(port), port[2])
        return self._groups.get(self.union.find(item))

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
        found = {}
        for (index, _), parts in self.carried_groups().items():
            found.setdefault(index, set()).update(parts)
        return found

    def carried_groups(self):
        """{(register, group): the parts of it the carried classes of the group hold}"""
        seeded = self.seeded()
        found = {}
        for item in list(self.union.parent):
            if item[0] == "port" and self.union.find(item) in seeded:
                found.setdefault((item[-2], self.group(item)), set()).add(item[-1])
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
        """Moves the carried classes' webs out of their registers into the virtual ones ('virtual': {(register,
        group): its virtual register}), in place: a read of a part those webs hold reads the virtual register, a write
        of one writes it (and the register too, for the parts the write puts in webs that stay). A web that stays a
        local (kinds: what the function works out itself) is copied into the virtual register where it crosses: before
        a call or an interrupt that reads it, where the function ends. Where a port joins no classes (CUT_OWN_PORTS),
        a copy hands the value from where the one side keeps it to where the other does: before the call, into the
        register its parameter is or into the global; after it, from where the callee leaves its result. Returns the
        virtual registers something moved into"""
        global_webs, local_webs = self.kinds()
        self.global_webs, self.local_webs = global_webs, local_webs
        web = self.webs.find
        seeded = self.seeded()
        everywhere = {}
        for (index, group), parts in self.carried_groups().items():
            if (index, group) in virtual:
                everywhere[virtual[(index, group)]] = parts

        def place(item, index):
            """The virtual register a carried web or port is in, or None (its register)"""
            if item not in self.union.parent or self.union.find(item) not in seeded:
                return None
            if item[0] in ("def", "entry") and web(item) not in global_webs:
                return None
            return virtual.get((index, self.group(item)))

        carried_bits = {}    # (id(statement), id(node)) -> {virtual register: bits read of it}
        # a read that only what the function sets itself reaches reads its own variable, which the write sets as well
        # as the global (with CUT_OWN_PORTS: enemy_collide_with_objects reads the box of the object it checks, which it
        # leaves in A5 for later code when it finds no object, by its name and type)
        self.own_only = set()
        for key, node, part, reaching in self.node_uses:
            if not reaching:
                continue
            if CUT_OWN_PORTS and all(definition in self.set_definitions for definition in reaching):
                self.own_only.update(reaching)
                continue
            other = place(next(iter(reaching)), node.index)
            if other is not None:
                found = carried_bits.setdefault((key, id(node)), {})
                found[other] = found.get(other, 0) | PARTS[part][1]
        # the copies: id(statement) -> {(to, from): bits} before it, and after it
        before, after = {}, {}

        def copy(table, key, to, source, part):
            if to == source:
                return
            found = table.setdefault(key, {})
            found[(to, source)] = found.get((to, source), 0) | PARTS[part][1]
        for entry, uses in self.uses.items():
            for key, use, index, part, reaching in uses:
                if not isinstance(use, tuple) or not reaching:
                    continue
                if use == ("interrupt",) and (index, part) not in self.handled:
                    continue
                here = place(next(iter(reaching)), index)
                if (key, use[1] if len(use) > 1 else None, index, part) in self.cut_uses:
                    # (a parameter the callee reads for itself: in its register, or the global its class is in)
                    there = place(("entry", use[1], index, part), index)
                    copy(before, key, index if there is None else there, index if here is None else here, part)
                    continue
                if web(next(iter(reaching))) in local_webs:
                    there = virtual.get((index, self.group(next(iter(reaching)))))
                    if there is not None:
                        copy(before, key, there, index, part)
        for _, statement_id, index, part, definition in self.call_definitions:
            if definition not in self.cut_definitions:
                continue
            here = place(definition, index)
            # (where the callee leaves its result: the global its result's class is in, or the register)
            theres = {place(("port", "out", target, index, part), index)
                      for target in self.call_targets_of.get(definition, ())}
            there = next(iter(theres)) if len(theres) == 1 else None
            copy(after, statement_id, index if here is None else here, index if there is None else there, part)
        moved = set()

        def moved_copy(to, source, bits):
            """'to' = 'source' in the bits, the rest of 'to' as it was (all of it where 'to' is a virtual register
            whose carried parts the bits cover: the rest of one nothing reads)"""
            moved.update(register for register in (to, source) if register not in self.registers)
            if to not in self.registers:
                whole = 0
                for part in everywhere.get(to, ()):
                    whole |= PARTS[part][1]
                if not whole & ~bits:
                    return ir.SetReg(to, ir.Reg(source))
            elif bits == FULL:
                return ir.SetReg(to, ir.Reg(source))
            return ir.SetReg(to, ir.Op("or", (ir.Op("and", (ir.Reg(to), ir.Const(~bits & FULL, 32)), 32),
                                              ir.Op("and", (ir.Reg(source), ir.Const(bits, 32)), 32)), 32))

        def copies(statement, found):
            made = []
            for (to, source), bits in sorted(found.items()):
                made_copy = moved_copy(to, source, bits)
                if getattr(statement, "origin", None) is not None:
                    made_copy.origin = statement.origin
                made.append(made_copy)
            return made

        def reader(statement):
            def change(node):
                if not isinstance(node, ir.Reg):
                    return None
                found = carried_bits.get((id(statement), id(node)))
                if not found:
                    return None
                moved.update(found)
                wanted = self.demand.get((id(statement), id(node)), FULL)
                mask = 0
                for bits in found.values():
                    mask |= bits
                if len(found) == 1 and not wanted & ~mask:
                    other, = found
                    return ir.Reg(other, node.bits)
                value = ir.Op("and", (node, ir.Const(~mask & FULL, 32)), 32) if wanted & ~mask else None
                for other, bits in sorted(found.items()):
                    part = ir.Op("and", (ir.Reg(other, node.bits), ir.Const(bits, 32)), 32)
                    value = part if value is None else ir.Op("or", (value, part), 32)
                return value
            return change

        for entry, function in built.items():
            keep = self.keeps[entry]
            for label, block in function.blocks.items():
                statements = []
                for statement in block.statements:
                    statements += copies(statement, before.get(id(statement), {}))
                    if isinstance(statement, ir.SetReg) and statement.index in self.registers \
                            and id(statement) in keep:
                        statements += self.split_write(statement, virtual, reader(statement), global_webs, moved)
                    else:
                        ir.map_statement(statement, reader(statement))
                        statements.append(statement)
                    statements += copies(statement, after.get(id(statement), {}))
                statements += copies(block.end, before.get(id(block.end), {}))
                block.statements = statements
                ir.map_statement(block.end, reader(block.end))
        return moved

    def split_write(self, statement, virtual, change, global_webs, moved):
        """The statements that write what 'statement' (a SetReg of a register with a virtual one) writes: the parts
        whose webs a virtual register holds go to it, the rest stay in the register"""
        flow = self.flow
        index = statement.index
        written = flow.written_parts(statement)
        carried = {}    # part -> the virtual register it goes to
        for part in written:
            definition = ("def", id(statement), index, part)
            if self.webs.find(definition) in global_webs:
                other = virtual.get((index, self.group(definition)))
                if other is not None:
                    carried[part] = other
        if not carried:
            ir.map_statement(statement, change)
            return [statement]
        moved.update(carried.values())
        _, rest, keep = dataflow.partial_write(statement)
        kept_bits = FULL
        for part, bits in PARTS:
            if part in written:
                kept_bits &= ~bits
        origin = getattr(statement, "origin", None)
        own = written - set(carried)
        if any(("def", id(statement), index, part) in self.own_only for part in carried):
            own = written    # (a read of its own reads the variable: it gets all of it, the global its parts)
        found = []
        value = ir.rebuild(rest if keep else statement.value, change)

        def kept(register, bits, new, masked):
            # (the parts the write leaves as they were stay so: "R = (R & keep) | rest", "bset #7, D7")
            if not bits:
                return new
            return ir.Op("or", (ir.Op("and", (ir.Reg(register), ir.Const(bits, 32)), 32),
                                new if masked else ir.Op("and", (new, ir.Const(~bits & FULL, 32)), 32)), 32)
        others = sorted(set(carried.values()))
        if own:
            # the register as the code writes it, then the virtual registers from it ("x = ...; carried = x;")
            found.append(ir.SetReg(index, kept(index, keep, value, True) if keep else value))
            source = index
        else:
            first = others[0]
            found.append(ir.SetReg(first, kept(first, keep, value, True) if keep else kept(first, kept_bits, value,
                                                                                         False)))
            source, others = first, others[1:]
        for other in others:
            bits = 0
            for part, part_bits in PARTS:
                if carried.get(part) == other:
                    bits |= part_bits
            found.append(ir.SetReg(other, kept(other, FULL & ~bits if source != index else kept_bits, ir.Reg(source),
                                               False)))
        for new in found:
            if origin is not None:
                new.origin = origin
        return found
