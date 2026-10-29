"""
Name:        structure.py
Description: turns a function's blocks and jumps into if / else, loops, switch, break and continue
Version:     0.1.0
Created:     11-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

The control flow graph is read with dominators. A block a back edge jumps to heads a loop: its body is
every block that gets back to it without passing through it. A loop is left by break, to the block
the loop goes on at (where all its ways out meet, or the exit most of them share), or by return. A
two-way branch is an if whose two sides meet again at the block that follows it on every path (its
immediate post-dominator; inside a loop, where the ways that stay in it meet). A small block that only
ends the function (a few statements and a return) is copied into every place that jumps to it, the way
C reads: "if (x) { ...; return; }". So is a small piece of code a jump leads into when everything from
there on is small and never comes back to it (TAIL_COPY), or everything up to where the code the jump
comes from goes on (REGION_COPY): it is written again where the jump is. What still does not fit (a
jump into the middle of something already written, an irreducible loop) is a goto to a label here, and
gotos.py takes it out of the tree with a control variable.

The result is a tree of the node classes below; emit.py prints it.
"""
import copy
import copy as copy_module    # (copy_until has a local named copy)
from dataclasses import dataclass, field

from v60 import ir

from . import floats

SMALL_EXIT = 4    # statements of an ending block that is copied rather than jumped to
COPY_LATCHES = True    # a jump to the few statements a loop goes round again after is written as a copy of
                        # them ending in continue or break (copied_latch): a walk over objects or voices
TAIL_COPY = 20    # statements (and block ends) of all the code a jump leads to, at most, to write it again
WAY_OUT_BLOCKS = 4    # blocks, at most, of a short way out that ends the function, written where it is jumped to
REGION_COPY = 16  # statements (and block ends), at most, of the code a jump leads to up to where the code the
                  # jump comes from goes on anyway, to write it again there


# ---- the tree ----

@dataclass
class Code:
    label: object
    statements: list


@dataclass
class If:
    condition: object
    then: list
    otherwise: list = field(default_factory=list)


@dataclass
class Loop:
    body: list
    kind: str = "forever"         # "forever", "while" (condition before the body) or "do" (after it)
    condition: object = None


@dataclass
class Switch:
    value: object
    cases: list                   # [(case values, body)]


@dataclass
class End:
    """The end of the function: a Return, a TailCall or an InterruptReturn."""
    end: object


@dataclass
class Goto:
    label: object


@dataclass
class Label:
    label: object


class Break:
    pass


class Continue:
    pass


@dataclass
class Mark:
    """A control variable of the C set (gotos.py): 'name' = 'value', ir.Const 0 or 1 or a condition."""
    name: str
    value: object


# ---- graph facts ----

EXIT = "exit"
LOOP_EXIT = "loop exit"
CASE_EXIT = "case exit"
CASE_NEXT = "next case"    # (the case right after, which the case being written falls through into: case_ipdom)


def successors(block):
    return block.successors()


def reverse_postorder(blocks, entry):
    order = []
    seen = set()
    stack = [(entry, iter(successors(blocks[entry])))]
    seen.add(entry)
    while stack:
        label, children = stack[-1]
        advanced = False
        for child in children:
            if child not in seen:
                seen.add(child)
                stack.append((child, iter(successors(blocks[child]))))
                advanced = True
                break
        if not advanced:
            order.append(label)
            stack.pop()
    order.reverse()
    return order


def dominators(order, predecessors, entry):
    """Immediate dominators (Cooper, Harvey, Kennedy)."""
    index = {label: position for position, label in enumerate(order)}
    idom = {entry: entry}

    def intersect(a, b):
        while a != b:
            while index[a] > index[b]:
                a = idom[a]
            while index[b] > index[a]:
                b = idom[b]
        return a

    changed = True
    while changed:
        changed = False
        for label in order[1:]:
            done = [p for p in predecessors[label] if p in idom and p in index]
            if not done:
                continue
            new = done[0]
            for other in done[1:]:
                new = intersect(other, new)
            if idom.get(label) != new:
                idom[label] = new
                changed = True
    return idom


def post_dominator_tree(succ, root):
    """Immediate post-dominators of the graph 'succ' (label -> the labels after it) whose every way ends
    at 'root'."""
    rsucc = {root: []}
    for label, targets in succ.items():
        rsucc.setdefault(label, [])
        for target in targets:
            rsucc.setdefault(target, []).append(label)
    order, seen, stack = [], {root}, [(root, iter(rsucc[root]))]
    while stack:
        label, children = stack[-1]
        child = next((child for child in children if child not in seen), None)
        if child is None:
            order.append(label)
            stack.pop()
            continue
        seen.add(child)
        stack.append((child, iter(rsucc[child])))
    order.reverse()
    return dominators(order, {label: list(succ.get(label, ())) for label in order}, root)


def dominates(idom, a, b):
    """True if a dominates b."""
    while True:
        if a == b:
            return True
        parent = idom.get(b)
        if parent is None or parent == b:
            return False
        b = parent


class Graph:
    def __init__(self, function):
        self.blocks = function.blocks
        self.entry = function.entry
        self.function = function
        self.order = reverse_postorder(self.blocks, self.entry)
        self.position = {label: index for index, label in enumerate(self.order)}
        self.predecessors = {label: [] for label in self.order}
        for label in self.order:
            for successor in successors(self.blocks[label]):
                self.predecessors[successor].append(label)
        self.idom = dominators(self.order, self.predecessors, self.entry)
        self.loops = self.find_loops()
        self.ipdom = self.post_dominators()

    def find_loops(self):
        """{header: (body, latches)} for the natural loops; a back edge to a block that does not dominate
        its source (an irreducible loop) heads none."""
        latches = {}
        for label in self.order:
            for successor in successors(self.blocks[label]):
                if self.position[successor] <= self.position[label] and dominates(self.idom, successor, label):
                    latches.setdefault(successor, []).append(label)
        loops = {}
        for header, sources in latches.items():
            body = {header}
            stack = [source for source in sources if source != header]
            body |= set(stack)
            while stack:
                label = stack.pop()
                for predecessor in self.predecessors[label]:
                    if predecessor not in body:
                        body.add(predecessor)
                        stack.append(predecessor)
            loops[header] = (body, sources)
        return loops

    def post_dominators(self):
        """Immediate post-dominators, with EXIT after every block that ends the function. A loop nothing
        leaves is taken to leave from its header, so every block reaches EXIT. A small block that ends the
        function is written where it is jumped to ("if (x) { ...; return; }"), so a way into one does not
        count: what the other ways meet at is where the code goes on."""
        def written_in_place(label):
            block = self.blocks[label]
            if isinstance(block.end, ir.Return) and getattr(self.function, "single_exit", False):
                return False    # (the one way back: the ways into it meet there, join_exits)
            if isinstance(block.end, (ir.Return, ir.TailCall, ir.InterruptReturn)):
                # (with CASE_STOPS, one only one block jumps to, however long: written there, it is not copied)
                return len(block.statements) <= SMALL_EXIT or CASE_STOPS and len(self.predecessors[label]) == 1
            # a short way out: code that only ends the function, small enough to be written where it is
            # jumped to ("if (hit) { ...; return; }")
            reach = self.reachable_from(label)
            if getattr(self.function, "meet_at_exit", False) \
                    and any(isinstance(self.blocks[inner].end, ir.Return) for inner in reach):
                return False    # (a way to the one way back meets the others there: decompile.py tries it)
            return len(reach) <= WAY_OUT_BLOCKS and all(not successors(self.blocks[inner]) or
                                           all(after in reach for after in successors(self.blocks[inner]))
                                           for inner in reach) \
                and all(label not in successors(self.blocks[inner]) for inner in reach) \
                and sum(len(self.blocks[inner].statements) + 1 for inner in reach) <= TAIL_COPY
        def kept(label):
            targets = list(successors(self.blocks[label]))
            # an ending one of the other ways also runs into is where they meet ("if (x) { ... }" and then it)
            joined = {after for target in targets for after in successors(self.blocks[target])}
            onward = [target for target in targets if not written_in_place(target) or target in joined]
            # a loop's way out stays: leaving the loop is where its body goes on
            if any(self.position[target] > self.position[label] for target in onward):
                return onward
            return targets
        succ = {label: kept(label) for label in self.order}
        for label in self.order:
            if not succ[label]:
                succ[label].append(EXIT)
        reaches = self.reaching_exit(succ)
        # a loop nothing leaves (its ways out are all written in place) is taken to leave where it goes round
        # again: its header keeps the block its branches meet at. Of such loops, one no other one comes after
        # (a loop that runs into another leaves through that one), the outermost first
        while True:
            stuck = sorted((header for header in self.loops if header not in reaches),
                           key=lambda label: self.position[label])
            if not stuck:
                break
            header = next((header for header in stuck
                           if self.reach_in(succ, header) <= self.reach_back(succ, header)), stuck[0])
            for latch in self.loops[header][1]:
                succ[latch].append(EXIT)
            reaches = self.reaching_exit(succ)
        for label in self.order:
            if label not in reaches:
                succ[label].append(EXIT)
        reverse_preds = {label: [] for label in self.order}
        reverse_preds[EXIT] = []
        for label, targets in succ.items():
            for target in targets:
                reverse_preds.setdefault(target, [])
        # the reverse graph: EXIT first, edges backwards
        rsucc = {label: [] for label in list(self.order) + [EXIT]}
        rpred = {label: [] for label in list(self.order) + [EXIT]}
        for label, targets in succ.items():
            for target in targets:
                rsucc[target].append(label)
                rpred[label].append(target)
        order = []
        seen = {EXIT}
        stack = [(EXIT, iter(rsucc[EXIT]))]
        while stack:
            label, children = stack[-1]
            advanced = False
            for child in children:
                if child not in seen:
                    seen.add(child)
                    stack.append((child, iter(rsucc[child])))
                    advanced = True
                    break
            if not advanced:
                order.append(label)
                stack.pop()
        order.reverse()
        return dominators(order, rpred, EXIT)

    def reachable_from(self, start):
        found = {start}
        stack = [start]
        while stack:
            label = stack.pop()
            for successor in successors(self.blocks[label]):
                if successor not in found:
                    found.add(successor)
                    stack.append(successor)
        return found

    @staticmethod
    def reach_in(succ, start):
        """The labels 'succ' (label -> the labels after it) leads to from 'start', with it."""
        found = {start}
        stack = [start]
        while stack:
            for target in succ.get(stack.pop(), ()):
                if target not in found:
                    found.add(target)
                    stack.append(target)
        return found

    @staticmethod
    def reach_back(succ, end):
        """The labels from which 'succ' leads to 'end', with it."""
        reverse = {}
        for label, targets in succ.items():
            for target in targets:
                reverse.setdefault(target, []).append(label)
        return Graph.reach_in(reverse, end)

    def reaching_exit(self, succ):
        reverse = {}
        for label, targets in succ.items():
            for target in targets:
                reverse.setdefault(target, []).append(label)
        found = set()
        stack = [EXIT]
        while stack:
            label = stack.pop()
            for source in reverse.get(label, []):
                if source not in found:
                    found.add(source)
                    stack.append(source)
        return found


def split_loop_entries(function, notes, limit=None, rounds_limit=8):
    """A loop the code enters at two places (an unrolled loop whose odd count starts at its second half)
    has no header C can write. When one of the ways in is a small block whose only way on is the other
    one, the code that comes from outside gets a copy of that block: the loop then starts at the other
    block, and the copy runs before it. A loop entered at more than two places gets a copy of what those
    ways in lead to up to one block of it (split_entry_header). 'notes' as for split_small_exits; 'limit'
    (statements) for the piece split_entry_region copies, REGION_COPY when None."""
    blocks = function.blocks
    changed = True
    rounds = 0
    while changed and rounds < rounds_limit:
        changed = False
        rounds += 1
        graph = Graph(function)
        for component in strongly_connected(blocks, graph.order):
            if len(component) < 2:
                continue
            entries = {label for label in component
                       if label == function.entry or any(source not in component
                                                         for source in graph.predecessors.get(label, ()))}
            if len(entries) < 2 or function.entry in entries:
                continue
            if len(entries) > 2:
                changed = split_entry_header(function, component, entries, graph, notes, limit)
                if changed:
                    break
                continue
            for small in sorted(entries, key=str):
                other = (entries - {small}).pop()
                block = blocks[small]
                if len(block.statements) > SMALL_EXIT or not isinstance(block.end, ir.Goto) \
                        or block.end.target != other:
                    continue
                outside = [source for source in graph.predecessors[small] if source not in component]
                made = {}
                twin = copy.copy(block)
                twin.label = ("entry", small)
                twin.statements = [copy.deepcopy(statement, made) for statement in block.statements]
                twin.end = copy.deepcopy(block.end, made)
                for note in notes:
                    for key in [key for key in note if isinstance(key, int) and key in made]:
                        note[id(made[key])] = copied_note(note[key], made)
                    if ("end", id(block)) in note:
                        note[("end", id(twin))] = note[("end", id(block))]
                blocks[twin.label] = twin
                for source in outside:
                    retarget(blocks[source].end, small, twin.label)
                changed = True
                break
            if not changed:
                changed = split_entry_region(function, component, entries, graph, notes, limit)
            if changed:
                break


def split_entry_header(function, component, entries, graph, notes, limit=None):
    """split_loop_entries for a loop entered at more than two places (palette_fade_step's scan, which goes
    on into the loop of steps at the red, the green or the blue of the colour it stops at, by the way the
    colour's difference points): the block of the loop the copy is smallest for is its header, and the code
    that comes from outside gets a copy of what its ways in lead to up to that block (the rest of that
    colour). True if it split one."""
    blocks = function.blocks
    best = None
    for header in sorted(component, key=str):
        starts = entries - {header}
        piece = set(starts)
        stack = list(starts)
        while stack:
            label = stack.pop()
            for successor in successors(blocks[label]):
                if successor != header and successor in component and successor not in piece:
                    piece.add(successor)
                    stack.append(successor)
        size = sum(len(blocks[label].statements) + 1 for label in piece)
        if size > (limit or REGION_COPY) or not any(header in successors(blocks[label]) for label in piece):
            continue
        if best is None or size < best[0]:
            best = (size, header, starts, piece)
    if best is None:
        return False
    _, header, starts, piece = best
    made = {}
    twins = {}
    for label in sorted(piece, key=str):
        block = blocks[label]
        twin = copy.copy(block)
        twin.label = ("entries", header, label)
        twin.statements = [copy.deepcopy(statement, made) for statement in block.statements]
        twin.end = copy.deepcopy(block.end, made)
        twins[label] = twin
        for note in notes:
            if ("end", id(block)) in note:
                note[("end", id(twin))] = note[("end", id(block))]
    for note in notes:
        for key in [key for key in note if isinstance(key, int) and key in made]:
            note[id(made[key])] = copied_note(note[key], made)
    for label, twin in twins.items():
        blocks[twin.label] = twin
        for inner in piece:
            retarget(twin.end, inner, twins[inner].label)
    for start in starts:
        for source in [source for source in graph.predecessors[start] if source not in component]:
            retarget(blocks[source].end, start, twins[start].label)
    return True


def split_entry_region(function, component, entries, graph, notes, limit=None):
    """The other case of split_loop_entries: one way in leads, through a small piece of the loop (with loops
    of its own, and ways out of it, the line drawing's odd half), to the other one; the code that comes from
    outside gets a copy of that piece, and the loop starts at the other way in. True if it split one."""
    blocks = function.blocks
    for start in sorted(entries, key=str):
        other = (entries - {start}).pop()
        piece = {start}
        stack = [start]
        while stack:
            label = stack.pop()
            for successor in successors(blocks[label]):
                if successor != other and successor in component and successor not in piece:
                    piece.add(successor)
                    stack.append(successor)
        if sum(len(blocks[label].statements) + 1 for label in piece) > (limit or REGION_COPY) \
                or not any(other in successors(blocks[label]) for label in piece):
            continue
        made = {}
        twins = {}
        for label in sorted(piece, key=str):
            block = blocks[label]
            twin = copy.copy(block)
            twin.label = ("entry", start, label)
            twin.statements = [copy.deepcopy(statement, made) for statement in block.statements]
            twin.end = copy.deepcopy(block.end, made)
            twins[label] = twin
            for note in notes:
                if ("end", id(block)) in note:
                    note[("end", id(twin))] = note[("end", id(block))]
        for note in notes:
            for key in [key for key in note if isinstance(key, int) and key in made]:
                note[id(made[key])] = copied_note(note[key], made)
        for label, twin in twins.items():
            blocks[twin.label] = twin
            for inner in piece:
                retarget(twin.end, inner, twins[inner].label)
        for source in [source for source in graph.predecessors[start] if source not in component]:
            retarget(blocks[source].end, start, twins[start].label)
        return True
    return False


def retarget(end, old, new):
    """Makes a block end that jumps to 'old' jump to 'new' instead."""
    if isinstance(end, ir.Goto) and end.target == old:
        end.target = new
    elif isinstance(end, ir.Branch):
        if end.target == old:
            end.target = new
        if end.otherwise == old:
            end.otherwise = new
    elif isinstance(end, ir.Switch):
        end.cases = {case: new if target == old else target for case, target in end.cases.items()}


def strongly_connected(blocks, order):
    """The strongly connected components of the blocks reachable from the entry (Tarjan)."""
    index, low, on_stack, stack, found = {}, {}, set(), [], []
    counter = [0]

    def visit(start):
        work = [(start, iter(successors(blocks[start])))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            label, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(successors(blocks[child]))))
                    advanced = True
                    break
                if child in on_stack:
                    low[label] = min(low[label], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[label])
            if low[label] == index[label]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == label:
                        break
                found.append(component)

    for label in order:
        if label not in index:
            visit(label)
    return found


def copied_note(value, made):
    """A note about a node, for its copy: the nodes it names (a field's base register) are the copy's, also
    inside an expression the note puts together itself (a base the code adds up: screen + 2000 + R1)."""
    if isinstance(value, tuple):
        return tuple(copied_node(part, made) if not isinstance(part, (int, str, bool, type(None))) else part
                     for part in value)
    return value


def copied_node(node, made):
    """'node' in the copy of the code: the copy's own node, or an expression of them."""
    if id(node) in made:
        return made[id(node)]
    if isinstance(node, ir.Op):
        args = tuple(copied_node(arg, made) for arg in node.args)
        return node if all(new is old for new, old in zip(args, node.args)) else ir.Op(node.name, args, node.bits)
    if isinstance(node, ir.Load):
        address = copied_node(node.address, made)
        return node if address is node.address else ir.Load(address, node.bits)
    return node


def split_small_exits(function, notes):
    """A small block that ends the function and is jumped to from several places gets a copy of its
    own for each (they are written where they are jumped to anyway): each copy then knows which values
    reach it, so a result handed back as it came in is not copied back. 'notes' are the dicts keyed by
    the id of a statement or ("end", id of a block) (cleanup.py's liveness, pointers.py's fields),
    filled in for the copies."""
    thread_jumps(function)
    join_conditions(function)    # "if (a || b) return": one way in after all
    predecessors = {}
    for label, block in function.blocks.items():
        for successor in successors(block):
            predecessors.setdefault(successor, []).append(label)
    for label in list(function.blocks):
        block = function.blocks[label]
        sources = predecessors.get(label, [])
        if not isinstance(block.end, (ir.Return, ir.TailCall)) or len(block.statements) > SMALL_EXIT \
                or len(sources) < 2 or label == function.entry:
            continue
        for number, source in enumerate(dict.fromkeys(sources[1:]), 2):
            # a deep copy: the twin's expressions are its own nodes, so what is noted about a node (the
            # variable a register read finds, the field a load reads) can differ between the copies
            made = {}
            twin = copy.copy(block)
            twin.label = ("exit", label, number)
            twin.statements = [copy.deepcopy(statement, made) for statement in block.statements]
            twin.end = copy.deepcopy(block.end, made)
            for note in notes:
                for key in [key for key in note if isinstance(key, int) and key in made]:
                    note[id(made[key])] = copied_note(note[key], made)
                if ("end", id(block)) in note:
                    note[("end", id(twin))] = note[("end", id(block))]
            function.blocks[twin.label] = twin
            end = function.blocks[source].end
            if isinstance(end, ir.Goto) and end.target == label:
                end.target = twin.label
            elif isinstance(end, ir.Branch):
                if end.target == label:
                    end.target = twin.label
                if end.otherwise == label:
                    end.otherwise = twin.label
            elif isinstance(end, ir.Switch):
                end.cases = {case: twin.label if target == label else target for case, target in end.cases.items()}


def split_shared_blocks(function, notes, heads_only=False):
    """A small block (no more than SMALL_EXIT statements) that only jumps on, and that several blocks jump
    to, gets a copy for each of them: where it heads a loop around another one (init_screen's course
    length, added before the inner loop reads the next section), the inner loop is then the only one, left
    by break. With 'heads_only', only such a head of a loop whose body starts with another loop, the inner
    ones first (the loop around a loop it then heads is not one any more). Tried where the structure leaves
    control variables (decompile.py). 'notes' as for split_small_exits."""
    for _ in range(8 if heads_only else 1):
        loops = Graph(function).loops if heads_only else {}
        predecessors = {}
        for label, block in function.blocks.items():
            for successor in successors(block):
                predecessors.setdefault(successor, []).append(label)

        def shared(label):
            block = function.blocks[label]
            return isinstance(block.end, ir.Goto) and len(block.statements) <= SMALL_EXIT \
                and len(set(predecessors.get(label, []))) >= 2 and label != function.entry \
                and block.end.target != label \
                and (not heads_only or label in loops and block.end.target in loops)
        found = [label for label in function.blocks if shared(label)]
        if heads_only:
            found = [label for label in found if function.blocks[label].end.target not in found]
        if not found:
            return
        for label in found:
            block = function.blocks[label]
            for number, source in enumerate(list(dict.fromkeys(predecessors[label]))[1:], 2):
                made = {}
                twin = copy.copy(block)
                twin.label = ("shared", label, number)
                twin.statements = [copy.deepcopy(statement, made) for statement in block.statements]
                twin.end = copy.deepcopy(block.end, made)
                for note in notes:
                    for key in [key for key in note if isinstance(key, int) and key in made]:
                        note[id(made[key])] = copied_note(note[key], made)
                    if ("end", id(block)) in note:
                        note[("end", id(twin))] = note[("end", id(block))]
                function.blocks[twin.label] = twin
                retarget(function.blocks[source].end, label, twin.label)


def redispatch_states(function, notes, read, apply=True):
    """A jump into the code of a case of a switch over a state kept in memory, right after the number of
    that case was stored there, does what the switch does when it runs again: the jump goes to the switch
    instead, and the C writes continue in a loop around it (car_radio_update's state = 5, then on into
    state 5's code; the loops a state machine makes through its states then have one way in). Only where
    the switch's block reads nothing but memory (the state, tables in the ROM) and writes nothing live
    where the jump went, and not where a case goes on into the one after it (C's fall through). 'read'
    (address, bits) is the ROM's value there, None outside it; 'notes' as for split_small_exits,
    cleanup.py's liveness first. Returns how many jumps go to the switch (with 'apply' false, would go).
    decompile.py keeps it where the C is shorter (a case written once, not copied where the jump is)."""
    live = notes[0]
    found = 0
    predecessors = {}
    for label, block in function.blocks.items():
        for successor in successors(block):
            predecessors.setdefault(successor, []).append(label)
    idom = Graph(function).idom
    for label, block in list(function.blocks.items()):
        end = block.end
        if not isinstance(end, ir.Switch) or not end.cases or isinstance(end.address, ir.EntryPoint):
            continue
        # what runs again: the reads after the last thing the block does that is not a read (a store, a call)
        first = len(block.statements)
        while first > 0 and isinstance(block.statements[first - 1], (ir.SetTemp, ir.SetReg, ir.SetFlag)):
            first -= 1
        reads = block.statements[first:]
        bound = {statement.temp for statement in reads if isinstance(statement, ir.SetTemp)}
        if any(isinstance(node, ir.Temp) and node not in bound for statement in reads
               for node in walk_expression(statement.value)) \
                or any(isinstance(node, ir.Temp) and node not in bound for node in walk_expression(end.address)):
            continue    # (a value the block worked out before)
        values = {}
        for statement in reads:
            values[statement_key(statement)] = inline(statement.value, values)
        address = inline(end.address, values)

        def in_rom(load):
            place = evaluate(load.address, lambda _: None)
            return place is not None and read(place, load.bits) is not None
        states = {(load.address, load.bits) for load in loads_in(address) if not loads_in(load.address) and not in_rom(load)}
        if len(states) != 1:
            continue
        (state, bits), = states
        written = {key for key in values if not isinstance(key, ir.Temp)}
        by_target = {}
        for case, target in end.cases.items():
            by_target.setdefault(target, []).append(case)
        order = [target for target, _ in sorted(by_target.items(), key=lambda item: min(item[1]))]
        next_case = dict(zip(order, order[1:]))

        def case_of(source):
            # (the case whose start the code passes through to get there)
            while source not in by_target and source != label and idom.get(source, source) != source:
                source = idom[source]
            return source
        for target in by_target:
            for source in predecessors.get(target, []):
                source_block = function.blocks[source]
                if source == label or isinstance(source_block.end, ir.Switch) or next_case.get(case_of(source)) == target:
                    continue
                stored = stored_value(source_block, state, bits)
                if stored is None:
                    continue

                def load(node, stored=stored):
                    if node.address == state and node.bits == bits:
                        return stored
                    place = evaluate(node.address, load)
                    return None if place is None else read(place, node.bits)
                if end.cases.get(evaluate(address, load)) != target \
                        or any(variable[:2] in written for variable in live.get(("end", id(source_block)), ())):
                    continue
                found += 1
                if apply:
                    retarget(source_block.end, target, dispatch_block(function, label, first, live))
    return found


def dispatch_block(function, label, first, live):
    """The label of the block that only works out where the switch at the end of block 'label' goes (its
    statements from 'first' on): the block itself, or a new one split off it the first time."""
    block = function.blocks[label]
    if first == 0:
        return label
    new = ("dispatch", label)
    if new not in function.blocks:
        dispatch = copy.copy(block)
        dispatch.label = new
        dispatch.statements = block.statements[first:]
        dispatch.end = block.end
        if ("end", id(block)) in live:
            live[("end", id(dispatch))] = live[("end", id(block))]
            live[("end", id(block))] = live.get(id(block.statements[first - 1]), live[("end", id(block))])
        block.statements = block.statements[:first]
        block.end = ir.Goto(new)
        function.blocks[new] = dispatch
    return new


def walk_expression(expression):
    """The nodes of an expression."""
    yield expression
    if isinstance(expression, ir.Load):
        yield from walk_expression(expression.address)
    elif isinstance(expression, ir.Op):
        for arg in expression.args:
            yield from walk_expression(arg)


def statement_key(statement):
    """What a statement of a block sets, as inline() looks it up."""
    if isinstance(statement, ir.SetTemp):
        return statement.temp
    return ("r", statement.index) if isinstance(statement, ir.SetReg) else ("f", statement.name)


def inline(expression, values):
    """'expression' with the temporaries, registers and flags 'values' knows written as their values."""
    if isinstance(expression, ir.Temp):
        return values.get(expression, expression)
    if isinstance(expression, ir.Reg):
        value = values.get(("r", expression.index))
        if value is None:
            return expression
        return value if expression.bits == 32 else ir.Op("trunc", (value,), expression.bits)
    if isinstance(expression, ir.Flag):
        return values.get(("f", expression.name), expression)
    if isinstance(expression, ir.Load):
        return ir.Load(inline(expression.address, values), expression.bits)
    if isinstance(expression, ir.Op):
        return ir.Op(expression.name, tuple(inline(arg, values) for arg in expression.args), expression.bits)
    return expression


def loads_in(expression):
    """The memory reads in an expression."""
    if isinstance(expression, ir.Load):
        return [expression] + loads_in(expression.address)
    if isinstance(expression, ir.Op):
        return [load for arg in expression.args for load in loads_in(arg)]
    return []


def evaluate(expression, load):
    """The value of an expression of constants, with load(node) the value of a memory read; None when
    something in it is not known."""
    if isinstance(expression, ir.Const):
        return expression.value
    if isinstance(expression, ir.Load):
        return load(expression)
    if not isinstance(expression, ir.Op):
        return None
    args = [evaluate(arg, load) for arg in expression.args]
    if any(arg is None for arg in args):
        return None
    name, mask = expression.name, (1 << expression.bits) - 1
    if name in ("zext", "trunc"):
        return args[0] & mask
    if name == "sext":
        value = args[0] & ((1 << args[1]) - 1)
        return (value - (1 << args[1]) if value >> (args[1] - 1) else value) & mask
    operations = {"add": lambda a, b: a + b, "sub": lambda a, b: a - b, "mul": lambda a, b: a * b,
                  "and": lambda a, b: a & b, "or": lambda a, b: a | b, "xor": lambda a, b: a ^ b,
                  "shl": lambda a, b: a << (b & 31), "shr": lambda a, b: a >> (b & 31)}
    if name not in operations or len(args) != 2:
        return None
    return operations[name](*args) & mask


def stored_value(block, state, bits):
    """The constant 'block' stores last at the address 'state' ('bits' wide), when after that store it
    calls nothing and writes no memory but other fields of the same record (the state's object's step:
    the same base, places apart), and the registers the address is made of keep their values; else
    None."""
    values, touched, found = {}, set(), None
    for statement in block.statements:
        if isinstance(statement, ir.Store):
            place = inline(statement.address, values)
            shift = covers(place, statement.bits, state, bits)
            if shift is not None:
                # (the state is the store or a part of it: its bytes, low first)
                value = inline(statement.value, values)
                found = (value.value >> (8 * shift)) & ((1 << bits) - 1) if isinstance(value, ir.Const) else None
            elif found is not None and not apart(place, statement.bits, state, bits):
                return None    # (memory written after it: it may be the state)
        elif isinstance(statement, (ir.SetTemp, ir.SetReg, ir.SetFlag)):
            if isinstance(statement, ir.SetReg):
                touched.add(statement.index)
            values[statement_key(statement)] = inline(statement.value, values)
        elif found is not None:
            return None
        else:
            values, touched = {}, set()    # (a call: the registers hold what it left)
    return None if found is None or registers_in(state) & touched else found


def covers(place, bits, state, state_bits):
    """The byte of an access at 'place' ('bits' wide) where the state ('state_bits' at 'state') starts, when
    the access holds all of it; else None."""
    def parts(address):
        if isinstance(address, ir.Op) and address.name == "add" and isinstance(address.args[1], ir.Const):
            return address.args[0], address.args[1].value
        return address, 0
    base, offset = parts(place)
    state_base, state_offset = parts(state)
    if base != state_base or not offset <= state_offset <= offset + bits // 8 - state_bits // 8:
        return None
    return state_offset - offset


def apart(place, bits, other, other_bits):
    """True when two accesses are at constant places from the same base that do not overlap."""
    def parts(address):
        if isinstance(address, ir.Op) and address.name == "add" and isinstance(address.args[1], ir.Const):
            return address.args[0], address.args[1].value
        return address, 0
    base, offset = parts(place)
    other_base, other_offset = parts(other)
    return base == other_base and (offset + bits // 8 <= other_offset or other_offset + other_bits // 8 <= offset)


def registers_in(expression):
    """The registers an expression reads."""
    if isinstance(expression, ir.Reg):
        return {expression.index}
    if isinstance(expression, ir.Load):
        return registers_in(expression.address)
    if isinstance(expression, ir.Op):
        return set().union(*(registers_in(arg) for arg in expression.args))
    return set()


def join_exits(function, notes, tail_calls=False):
    """Every way back to the caller of a function that hands results back through pointers goes on to one
    block that ends it, written at the end: the C writes the results back once, there, rather than before
    every return. The ways out that come back to the code (ends of if, loops left by break) meet there as
    the structure lets them. With 'tail_calls', a jump on into another function ("br" to it) is that
    function called, then the same way back: what it hands back is written back at the end with the rest.
    'notes' as for split_small_exits."""
    thread_jumps(function)
    join_conditions(function)
    exits = [label for label, block in function.blocks.items() if isinstance(block.end, ir.Return)
             or tail_calls and isinstance(block.end, ir.TailCall) and block.end.entry is None]
    function.single_exit = bool(exits)
    if len(exits) < 2:
        return
    joined = copy.copy(function.blocks[exits[0]])
    joined.label = ("exit", "joined")
    joined.statements = []
    joined.end = ir.Return()
    function.blocks[joined.label] = joined
    for label in exits:
        block = function.blocks[label]
        if isinstance(block.end, ir.TailCall):
            call = ir.Call(target=block.end.target, return_address=None)
            if hasattr(block.end, "origin"):
                call.origin = block.end.origin    # (symbols.txt's local lines name what it hands back by the jump)
            block.statements = block.statements + [call]
            for note in notes:
                if ("end", id(block)) in note:
                    note[id(call)] = note[("end", id(block))]
        else:
            for note in notes:
                if ("end", id(block)) in note:
                    note[("end", id(joined))] = note[("end", id(block))]
        block.end = ir.Goto(joined.label)


# ---- building the tree ----

def thread_jumps(function):
    """A jump to a block that does nothing but jump on goes straight to where that one leads (the V60
    tables of "br" instructions are made of such blocks)."""
    blocks = function.blocks

    def final(label):
        seen = set()
        while label != function.entry and label not in seen:
            block = blocks[label]
            if block.statements or not isinstance(block.end, ir.Goto):
                break
            seen.add(label)
            label = block.end.target
        return label

    for block in blocks.values():
        end = block.end
        if isinstance(end, ir.Goto):
            end.target = final(end.target)
        elif isinstance(end, ir.Branch):
            end.target = final(end.target)
            end.otherwise = final(end.otherwise)
        elif isinstance(end, ir.Switch):
            end.cases = {case: final(target) for case, target in end.cases.items()}


def known_conditions(condition, outcome):
    """What taking the side 'outcome' (True: the condition held) of a branch on 'condition' says: the
    conditions known to hold there (each part of an && that held, of an || that did not)."""
    if outcome and isinstance(condition, ir.Op) and condition.name == "land":
        return known_conditions(condition.args[0], True) | known_conditions(condition.args[1], True)
    if not outcome and isinstance(condition, ir.Op) and condition.name == "lor":
        return known_conditions(condition.args[0], False) | known_conditions(condition.args[1], False)
    return {condition if outcome else negate(condition)}


def plain_condition(condition):
    """True for a condition made only of constants, registers and memory read through them: a temporary,
    a flag or a stack slot is set again by the statements that work it out (a loop's test of the byte it
    has just read), which a test made before says nothing of."""
    return all(isinstance(node, (ir.Const, ir.Reg, ir.Load, ir.Op)) for node in ir.walk(condition))


def keeps_condition(statements, condition):
    """True when 'statements' leave 'condition' as it was: they write none of the registers it reads, and
    no memory (no store), and call nothing (a call may change both, and the interrupts run in calls)."""
    registers = registers_in(condition)
    reads_memory = bool(loads_in(condition))
    for statement in statements:
        if isinstance(statement, ir.SetReg) and statement.index in registers:
            return False
        if isinstance(statement, (ir.Call, ir.Effect)) or isinstance(statement, ir.Store) and reads_memory:
            return False
    return True


def thread_known_tests(function, notes, steady, depth=4):
    """A branch on a condition the way into its block already tested, with nothing between that changes
    what it reads (race_car_draw tests the car's rival flag, then, past the segment's test, tests it again
    where the car is off the screen): that way goes straight to the side the condition takes, through a
    copy of the block's statements. A condition that reads memory is only taken as known where 'steady'
    (load) says each of its reads is of memory only the program's stores change, not a device's. 'notes'
    as for split_small_exits. True if it changed anything."""
    blocks = function.blocks
    changed = False
    predecessors = {}
    for label, block in blocks.items():
        for successor in successors(block):
            predecessors.setdefault(successor, []).append(label)
    for label in sorted(blocks, key=str):
        block = blocks.get(label)
        if block is None or not isinstance(block.end, ir.Branch) or block.end.target == block.end.otherwise \
                or label == function.entry:
            continue
        condition = block.end.condition
        if not plain_condition(condition) or not keeps_condition(block.statements, condition) \
                or not all(steady(node) for node in ir.walk(condition) if isinstance(node, ir.Load)):
            continue
        for source in sorted(set(predecessors.get(label, ())), key=str):
            if source == label:
                continue
            # what the branches on the way here tested, walking back through blocks that only one block
            # leads to and that keep the condition (their statements come after the branches before them)
            facts, edge_from, edge_to = set(), source, label
            for _ in range(depth):
                previous = blocks[edge_from]
                end = previous.end
                if isinstance(end, ir.Branch) and end.target != end.otherwise:
                    facts |= known_conditions(end.condition, end.target == edge_to)
                if condition in facts or negate(condition) in facts:
                    break
                ways_in = set(predecessors.get(edge_from, ()))
                if len(ways_in) != 1 or edge_from == function.entry or not keeps_condition(previous.statements, condition):
                    break
                edge_to, edge_from = edge_from, ways_in.pop()
            if condition in facts:
                known = block.end.target
            elif negate(condition) in facts:
                known = block.end.otherwise
            else:
                continue
            if block.statements:
                made = {}
                twin = copy.copy(block)
                twin.label = ("known", label, source)
                twin.statements = [copy.deepcopy(statement, made) for statement in block.statements]
                twin.end = ir.Goto(known)
                for note in notes:
                    for key in [key for key in note if isinstance(key, int) and key in made]:
                        note[id(made[key])] = copied_note(note[key], made)
                blocks[twin.label] = twin
                retarget(blocks[source].end, label, twin.label)
            else:
                retarget(blocks[source].end, label, known)
            changed = True
    return changed


def join_conditions(function):
    """"if (a) goto X; if (b) goto X; ..." is one test: if (a || b). A block that only branches, that only
    one block jumps to, and that shares a target with that block, goes into its condition (|| or &&, which
    evaluate in the same order and skip what the jump skipped)."""
    blocks = function.blocks
    changed = True
    while changed:
        changed = False
        predecessors = {}
        for label, block in blocks.items():
            for successor in block.successors():
                predecessors.setdefault(successor, []).append(label)
        for label, block in blocks.items():
            end = block.end
            if not isinstance(end, ir.Branch):
                continue
            for side in ("otherwise", "target"):
                inner_label = getattr(end, side)
                inner = blocks.get(inner_label)
                if inner is None or inner_label == label or inner.statements or not isinstance(inner.end, ir.Branch) \
                        or predecessors.get(inner_label) != [label] or inner_label == function.entry:
                    continue
                shared = end.target if side == "otherwise" else end.otherwise
                c1, c2 = end.condition, inner.end.condition
                if side == "otherwise":
                    # if (c1) X; else if (c2 ...) -> one of inner's targets is X
                    if inner.end.target == shared:
                        new = Branch_parts(logical("lor", c1, c2), shared, inner.end.otherwise)
                    elif inner.end.otherwise == shared:
                        new = Branch_parts(logical("lor", c1, negate(c2)), shared, inner.end.target)
                    else:
                        continue
                else:
                    # if (c1) { if (c2 ...) } else Y -> one of inner's targets is Y
                    if inner.end.otherwise == shared:
                        new = Branch_parts(logical("land", c1, c2), inner.end.target, shared)
                    elif inner.end.target == shared:
                        new = Branch_parts(logical("land", c1, negate(c2)), inner.end.otherwise, shared)
                    else:
                        continue
                end.condition, end.target, end.otherwise = new
                del blocks[inner_label]
                changed = True
                break
            if changed:
                break


def Branch_parts(condition, target, otherwise):
    return condition, target, otherwise


def logical(name, a, b):
    return ir.op(name, (a, b), 1)


@dataclass
class LoopContext:
    header: object
    body: set            # the natural loop
    region: set          # the body, and code only the loop leads to that never comes back: written inside
    follow: object       # where the loop goes on
    in_switch: bool = False
    switch_leaves: bool = False    # in a switch whose cases go round again or leave the loop: break leaves the
                                   # switch, and the break after it the loop

    def inside_switch(self, leaves=False):
        return LoopContext(self.header, self.body, self.region, self.follow, True, leaves)


class Structurer:
    def __init__(self, function):
        thread_jumps(function)
        join_conditions(function)
        self.function = function
        self.blocks = function.blocks
        self.graph = Graph(function)
        self.emitted = set()
        self.labels = set()        # blocks something jumps to with goto
        self.pending = []          # of those, the ones not written yet
        self.meeting_ends = set()  # loop headers whose loop ends where all its ways out meet (loop_follow)
        self.loop_ipdoms = {}      # loop header -> the post-dominators inside its loop (loop_ipdom)
        self.met_past = set()      # loop headers whose loop goes on where its ways out meet (meeting_past)

    def small_exit(self, label):
        block = self.blocks[label]
        if isinstance(block.end, ir.Return) and getattr(self.function, "single_exit", False):
            return False    # (the one way back, written at the end: join_exits)
        return (isinstance(block.end, (ir.Return, ir.TailCall, ir.InterruptReturn))
                and len(block.statements) <= SMALL_EXIT)

    def loop_follow(self, header):
        chosen = self.natural_follow(header)
        body, _ = self.graph.loops[header]
        if header in self.met_past:
            return chosen    # (its natural end is written inside it, the ways that end the function too)
        meet = self.graph.ipdom.get(header)
        if meet in (None, EXIT) or meet in body:
            return chosen
        if meet != chosen:
            ways = {successor for label in body for successor in successors(self.blocks[label])
                    if successor not in body and not self.small_exit(successor)}
            # a way out that does not run into the chosen end would need a goto there: the loop then ends
            # where all its ways out meet (its header's immediate post-dominator)
            if chosen is not None and all(self.post_dominates(chosen, way) for way in ways if way != chosen):
                return chosen
        # (each way out is then written in the loop up to there, ending in break)
        self.meeting_ends.add(header)
        return meet

    def post_dominates(self, later, label):
        """True if every way from 'label' to the end of the function passes through 'later'."""
        seen = set()
        while label not in (None, EXIT) and label not in seen:
            if label == later:
                return True
            seen.add(label)
            label = self.graph.ipdom.get(label)
        return False

    def natural_follow(self, header):
        body, latches = self.graph.loops[header]
        # where the step that goes round again leaves the loop ("blocks_left--; if (blocks_left != 0) go
        # round; else go on"): the loop's natural end, so that the jumps to that step read as a copy of it
        # ending in continue, and break
        ends = {target for latch in latches if isinstance(self.blocks[latch].end, ir.Branch)
                for target in successors(self.blocks[latch]) if target not in body}
        if MEET_FOLLOW and not ends:
            # (a latch that only goes round again, a block of its own for the Z80's extra cycles of a jump
            # taken: the branch before it is where the count runs out)
            ends = {target for latch in latches if isinstance(self.blocks[latch].end, ir.Goto)
                    and len(self.blocks[latch].statements) <= SMALL_EXIT
                    and len(self.graph.predecessors.get(latch, ())) == 1
                    for source in self.graph.predecessors[latch] if isinstance(self.blocks[source].end, ir.Branch)
                    for target in successors(self.blocks[source]) if target not in body}
        exits = {}
        for label in body:
            for successor in successors(self.blocks[label]):
                if successor not in body:
                    exits[successor] = exits.get(successor, 0) + 1
        if len(ends) == 1:
            end = next(iter(ends))
            meet = self.meeting_past(end, set(exits) - {end}, body) if MEET_FOLLOW else None
            if meet is not None:
                # the loop's other ways out all come to one place, and its natural end only goes there too or
                # ends the function: that place is where the loop goes on, and the natural end is written
                # inside it, where the count runs out (if (n == 0) { ...; break; })
                self.met_past.add(header)
                return meet
            if not self.small_exit(end):
                return end
        candidates = [label for label in exits if not self.small_exit(label)] or list(exits)
        if not candidates:
            return None
        # the exit that the most code after the loop hangs from: it reaches the most blocks
        return max(candidates, key=lambda label: (len(self.reachable(label)), exits[label], -self.graph.position[label]))

    def way_out_chain(self, target, loop):
        """The few blocks of a few statements each that go on, one into the next, from 'target' to where
        'loop' goes on (a way out that steps a pointer back twice before the code after the loop), as Code
        nodes to write where the jump is; None when 'target' is not such a chain."""
        nodes = []
        label = target
        while label != loop.follow:
            block = self.blocks[label]
            if len(nodes) >= 3 or not isinstance(block.end, ir.Goto) or len(block.statements) > SMALL_EXIT \
                    or label == self.function.entry or label in loop.region:
                return None
            nodes.append(Code(label, list(block.statements)))
            label = block.end.target
        return nodes or None

    def meeting_past(self, end, others, body):
        """Where 'others' (a loop's ways out besides its natural end 'end') all arrive, through a few
        statements that only go on, or else through code of their own that only the loop leads to and that
        has no loop of its own (palette_fade_step's scan stops at the colour's red, green or blue and goes on
        into the loop of steps there: the copy split_entry_header makes of the rest of that colour), when
        the code from 'end' never comes back to itself, only leads there or ends the function, and is either
        reached from the loop alone (written once, where the loop leaves) or small (written again); None
        otherwise."""
        def arrival(label):
            seen = set()
            while label not in seen:
                seen.add(label)
                block = self.blocks[label]
                if not isinstance(block.end, ir.Goto) or len(block.statements) > SMALL_EXIT \
                        or len(self.graph.predecessors.get(block.end.target, ())) < 2:
                    return label
                label = block.end.target
            return None
        meets = {arrival(label) for label in others}
        # (the place the short ways lead to, else the first one past all the ways out: take_voice_screen_1's
        # free voice, a block of five statements, and its search for the oldest both go on to start_voice)
        candidates = [next(iter(meets))] if len(meets) == 1 and None not in meets else []
        candidates.append(self.ways_meet(others, body))
        for meet in candidates:
            if meet is not None and self.natural_end_meets(end, meet, body):
                return meet
        return None

    def natural_end_meets(self, end, meet, body):
        """True when the code from a loop's natural end 'end' never comes back to itself, only leads to
        'meet' or ends the function, and is either reached from the loop alone or small (meeting_past)."""
        reach = self.reachable_avoiding(end, meet)
        if meet in reach or any(end in successors(self.blocks[label]) for label in reach):
            return False
        for label in reach:
            block = self.blocks[label]
            if not all(successor in reach or successor == meet for successor in successors(block)):
                return False
            if not successors(block) and not isinstance(block.end, (ir.Return, ir.TailCall)):
                return False
        alone = all(source in body or source in reach for label in reach
                    for source in self.graph.predecessors.get(label, ()))
        return alone or sum(len(self.blocks[label].statements) + 1 for label in reach) <= REGION_COPY

    def ways_meet(self, others, body):
        """The first block every way from 'others' (ways out of a loop) passes through, when the code on the
        ways up to there is reached from the loop alone, never comes back to the loop or to itself, and goes
        nowhere else; None otherwise."""
        others = sorted(others, key=str)
        if not others:
            return None
        chains = []
        for label in others:
            chain, seen = [], set()
            while label not in (None, EXIT) and label not in seen:
                seen.add(label)
                chain.append(label)
                label = self.graph.ipdom.get(label)
            chains.append(chain)
        meet = next((label for label in chains[0] if label not in others and all(label in chain for chain in chains[1:])),
                    None)
        if meet is None or meet in body:
            return None
        ways = set()
        for label in others:
            ways |= self.reachable_avoiding(label, meet)
        if ways & set(body) or meet in ways:
            return None
        for label in ways:
            if any(source not in body and source not in ways for source in self.graph.predecessors.get(label, ())):
                return None
        # (no loop of their own: each block of the ways comes after the blocks that lead to it)
        left = {label: sum(1 for source in self.graph.predecessors.get(label, ()) if source in ways)
                for label in ways}
        ready = [label for label, count in left.items() if not count]
        while ready:
            label = ready.pop()
            for successor in successors(self.blocks[label]):
                if successor in left:
                    left[successor] -= 1
                    if not left[successor]:
                        ready.append(successor)
        if any(left.values()):
            return None
        return meet

    def reachable(self, start):
        return self.graph.reachable_from(start)

    def reachable_avoiding(self, start, avoided):
        """The blocks 'start' leads to without passing through 'avoided'."""
        found = {start}
        stack = [start]
        while stack:
            label = stack.pop()
            for successor in successors(self.blocks[label]):
                if successor not in found and successor != avoided:
                    found.add(successor)
                    stack.append(successor)
        return found

    def build(self):
        tree = self.region(self.graph.entry, None, None)
        # code only gotos reach goes after the rest, each piece ending the function or jumping back
        while self.pending:
            label = self.pending.pop(0)
            if label in self.emitted:
                continue
            tree += self.region(label, None, None)
        return tree

    def copy_tail(self, target):
        """The code from 'target' on, written again: when all of it is small, never comes back to
        'target' and needs no goto of its own. None otherwise."""
        reach = self.reachable(target)
        if any(target in successors(self.blocks[label]) for label in reach):
            return None
        if sum(len(self.blocks[label].statements) + 1 for label in reach) > TAIL_COPY:
            return None
        if getattr(self.function, "meet_at_exit", False) \
                and any(isinstance(self.blocks[label].end, ir.Return) for label in reach):
            return None    # (the ways to the one way back meet there: none of them is written again)
        copy = Structurer.__new__(Structurer)
        copy.function, copy.blocks, copy.graph = self.function, self.blocks, self.graph
        copy.emitted, copy.labels, copy.pending = set(), set(), []
        copy.meeting_ends, copy.loop_ipdoms, copy.met_past = self.meeting_ends, self.loop_ipdoms, self.met_past
        for name in ("case_starts", "current_case", "case_follow", "case_loop", "case_order"):
            if hasattr(self, name):
                setattr(copy, name, getattr(self, name))
        nodes = copy.region(target, None, None)
        if copy.labels:
            return None
        return without_labels(nodes)

    def copy_until(self, target, stop, loop):
        """The code from 'target' up to 'stop' (where the code the jump comes from goes on), written again:
        when all of it is small, comes back neither to 'target' nor to code before it (but, with COPY_LOOPS,
        a whole loop that starts at 'target': road_surface_lines' copy of the points left, after each of
        three ways out of its loop over the segments), and needs no goto of its own (a jump into a branch
        already written, whose code then joins this one's). None otherwise."""
        if stop is None and loop is None:
            return None    # (a tail: copy_tail's)
        copying = getattr(self, "copying", frozenset())
        if target in copying:
            return None    # (already being written again, in the copy this one is part of)
        # a way out of the loop is copied up to where the loop goes on (break) and the ends of the function
        leaving = loop is not None and target not in loop.region
        reach = {target}
        stack = [target]
        while stack:
            label = stack.pop()
            for successor in successors(self.blocks[label]):
                if successor == stop or successor in reach:
                    continue
                if loop is not None and (successor == loop.header or successor == loop.follow
                                         or successor not in loop.region and not leaving):
                    continue    # inside a loop: continue, break, or a way out written as such in the copy
                if leaving and (self.small_exit(successor) or successor in loop.region):
                    continue
                reach.add(successor)
                stack.append(successor)
        if any(target in successors(self.blocks[label]) for label in reach) and not self.whole_loop(target, reach):
            return None
        if sum(len(self.blocks[label].statements) + 1 for label in reach) > REGION_COPY:
            return None
        copy = Structurer.__new__(Structurer)
        copy.function, copy.blocks, copy.graph = self.function, self.blocks, self.graph
        copy.emitted, copy.labels, copy.pending = set(), set(), []
        copy.meeting_ends, copy.loop_ipdoms, copy.met_past = self.meeting_ends, self.loop_ipdoms, self.met_past
        for name in ("case_starts", "current_case", "case_follow", "case_loop", "case_order"):
            if hasattr(self, name):
                setattr(copy, name, getattr(self, name))
        copy.copying = copying | {target}
        if leaving:
            # (written as if it were in the loop: its jumps to where the loop goes on are break)
            loop = copy_module.copy(loop)
            loop.region = set(loop.region) | reach
        nodes = copy.region(target, stop, loop)
        if copy.labels:
            return None
        return without_labels(nodes)

    def whole_loop(self, target, reach):
        """True, with COPY_LOOPS, when the jumps back to 'target' from 'reach' are those of a loop that starts
        at 'target' and lies all in 'reach': written again, it is a loop of its own."""
        if not COPY_LOOPS or target not in self.graph.loops:
            return False
        body, _ = self.graph.loops[target]
        return set(body) <= reach and all(label in body for label in reach
                                          if target in successors(self.blocks[label]))

    def goto(self, target, stop=None, loop=None):
        copied = self.copy_tail(target)
        if copied is not None:
            return copied
        copied = self.copy_until(target, stop, loop)
        if copied is not None:
            return copied
        self.labels.add(target)
        if target not in self.emitted:
            self.pending.append(target)
        return [Goto(target)]

    def jump(self, target, stop, loop):
        """What going to 'target' is from inside 'loop' with the region ending at 'stop': a list of nodes,
        and the block the walk goes on at (None when the jump ends the walk)."""
        if loop is not None:
            if target == loop.header:
                return [Continue()], None
            if target not in loop.region:
                if target == loop.follow:
                    # inside a switch, break would only leave the switch (unless a break after it leaves the loop)
                    return (self.goto(target) if loop.in_switch and not loop.switch_leaves else [Break()]), None
                if self.small_exit(target):
                    return self.copy_exit(target), None
                block = self.blocks[target]
                if not loop.in_switch and isinstance(block.end, ir.Goto) and block.end.target == loop.follow \
                        and len(block.statements) <= SMALL_EXIT and target != self.function.entry:
                    # a few statements on the way out of the loop: written where the jump is, then break
                    return [Code(target, list(block.statements)), Break()], None
                chain = self.way_out_chain(target, loop) if not loop.in_switch else None
                if chain is not None:
                    return chain + [Break()], None
                # (a copy of the way out, from inside the loop: its jumps to where the loop goes on are break)
                return self.goto(target, None, loop if not loop.in_switch else None), None
        if CASE_STOPS and (loop is None or loop.in_switch) and getattr(self, "current_case", None) is not None \
                and target == self.case_follow and target != stop:
            return [Break()], None    # (where the switch goes on, from inside one of its ifs)
        if target == stop:
            return [], None
        if CASE_STOPS and target in getattr(self, "case_starts", ()) and target != getattr(self, "current_case", None) \
                and target not in self.emitted:
            # another case of the switch being written: a goto to where it starts
            self.labels.add(target)
            return [Goto(target)], None
        if loop is not None and COPY_LATCHES:
            latch = self.copied_latch(target, loop)
            if latch is not None:
                return latch, None
        if target in self.emitted:
            if self.small_exit(target):
                return self.copy_exit(target), None
            return self.goto(target, stop, loop), None
        return [], target

    def copied_latch(self, label, loop):
        """The few statements before 'loop' goes round again (the next voice: the pointer moved on, the count
        down), written again where the body jumps to them, ending in continue (and break when the count runs
        out); None when 'label' is not such a block."""
        if loop.in_switch or label not in loop.body or label == loop.header:
            return None
        block = self.blocks[label]
        if len(block.statements) > SMALL_EXIT:
            return None
        end = block.end
        if isinstance(end, ir.Goto) and end.target == loop.header:
            return [Code(label, list(block.statements)), Continue()]

        def again(side):
            """The nodes for a side of the branch that goes round again ([continue], after the statements of
            a block of its own on the way, the Z80's extra cycles of a jump taken), or None."""
            if side == loop.header:
                return [Continue()]
            way = self.blocks.get(side)
            if way is not None and side in loop.body and isinstance(way.end, ir.Goto) \
                    and way.end.target == loop.header and len(way.statements) <= SMALL_EXIT:
                return [Code(side, list(way.statements)), Continue()]
            return None
        if isinstance(end, ir.Branch):
            for side, other, condition in ((end.target, end.otherwise, end.condition),
                                           (end.otherwise, end.target, negate(end.condition))):
                nodes = again(side)
                if nodes is not None and other == loop.follow:
                    return [Code(label, list(block.statements)), If(negate(condition), [Break()])] + nodes
        return None

    def copy_exit(self, label):
        block = self.blocks[label]
        return [Code(label, list(block.statements)), End(block.end)]

    def region(self, start, stop, loop):
        """Nodes for the code from 'start' up to 'stop' (not included) inside 'loop' (a LoopContext or
        None)."""
        out = []
        current = start
        while current is not None and current != stop:
            if (loop is not None and current not in loop.region) or current in self.emitted:
                nodes, _ = self.jump(current, stop, loop)
                out += nodes
                break
            if current in self.graph.loops and (loop is None or loop.header != current):
                current = self.loop(current, out, stop, loop)
                continue
            current = self.block(current, out, stop, loop)
        return out

    def loop(self, header, out, stop, outer):
        body, _ = self.graph.loops[header]
        follow = self.loop_follow(header)
        region = set(body)
        if header in self.meeting_ends:
            # where the ways out meet: the code on each way up to there (not what follows that, even when it
            # comes back round an outer loop) is the loop's
            after = self.reachable_avoiding(follow, header)
            region |= {label for label in self.graph.order
                       if label not in after and dominates(self.graph.idom, header, label)}
        else:
            after = self.reachable(follow) if follow is not None else set()
            for label in self.graph.order:
                if label in body or label in after or (outer is not None and label in outer.body):
                    continue
                if dominates(self.graph.idom, header, label):
                    region.add(label)
        inner = LoopContext(header, body, region, follow)
        nodes = []
        current = self.block(header, nodes, None, inner)
        if current is not None:
            nodes += self.region(current, None, inner)
        # code of the loop only gotos reach (a way out of an inner loop that comes back into this one) goes at
        # the end of its body, after the round's end, rather than after the whole function
        while True:
            label = next((label for label in self.pending if label in body and label not in self.emitted), None)
            if label is None:
                break
            self.pending.remove(label)
            if not ends_flow(nodes):
                nodes.append(Continue())
            nodes += self.region(label, None, inner)
        out.append(Loop(nodes))
        if follow is None or not breaks_out(nodes):
            # (a loop nothing leaves with a break: what follows it comes only from elsewhere, which writes it
            # or jumps to it)
            return None
        nodes, next_label = self.jump(follow, stop, outer)
        out += nodes
        return next_label

    def block(self, label, out, stop, loop):
        """Writes one block and its end; returns where the walk goes on."""
        self.emitted.add(label)
        block = self.blocks[label]
        out.append(Label(label))
        if block.statements:
            out.append(Code(label, list(block.statements)))
        end = block.end
        if isinstance(end, (ir.Return, ir.TailCall, ir.InterruptReturn)):
            out.append(End(end))
            return None
        if isinstance(end, ir.Goto):
            nodes, next_label = self.jump(end.target, stop, loop)
            out += nodes
            return next_label
        if isinstance(end, ir.Branch):
            return self.branch(label, end, out, stop, loop)
        if isinstance(end, ir.Switch):
            return self.switch(label, end, out, stop, loop)
        raise ValueError(f"block end {end!r}")

    def follow_of(self, label, stop, loop):
        """Where the two or more ways out of 'label' meet again inside the current region, or None."""
        if CASE_STOPS and (loop is None or loop.in_switch) and getattr(self, "current_case", None) is not None:
            # inside a case: its ways out (break, the start of another case, a small exit) do not meet the
            # others
            follow = self.case_ipdom().get(label)
            if follow == CASE_NEXT:
                # (the ways meet where the case falls through into the next one: a goto there, at its end)
                return self.case_nexts.get((self.current_case, self.case_follow, self.case_starts))
            if follow in (CASE_EXIT, None, label) or follow in self.emitted and follow != stop:
                return None
            return follow
        follow = self.graph.ipdom.get(label)
        if LEAVE_SHARED and loop is not None:
            # (where the ways inside the loop meet, its shared short ways out written where each is: not one
            # of those, even when every way of the function passes through it)
            inside = self.loop_ipdom(loop).get(label)
            if inside not in (LOOP_EXIT, None, label) and inside in loop.region and inside != loop.header:
                follow = inside
        if follow in (EXIT, None, label) or (loop is not None and (follow not in loop.region
                                                                   or follow == loop.header)):
            if loop is None:
                return None
            # inside a loop, the ways that leave it (continue, break) do not meet the others: where those meet
            follow = self.loop_ipdom(loop).get(label)
            if follow in (LOOP_EXIT, None, label) or follow not in loop.region or follow == loop.header:
                return None
        if follow != stop and follow in self.emitted:
            return None
        return follow

    def case_break(self, label):
        """True for a short way out of the case being written: where the switch goes on, or a few statements
        on to there (case_break_nodes writes it)."""
        if label == self.case_follow:
            return True
        block = self.blocks[label]
        return isinstance(block.end, ir.Goto) and block.end.target == self.case_follow \
            and len(block.statements) <= SMALL_EXIT and label not in self.emitted

    def case_break_nodes(self, label):
        """The nodes of a short way out of the case being written: its statements, then break."""
        if label == self.case_follow:
            return [Break()]
        self.emitted.add(label)
        return [Label(label), Code(label, list(self.blocks[label].statements)), Break()]

    def case_ipdom(self):
        """Immediate post-dominators inside the case being written (CASE_STOPS): its ways out (to where the
        switch goes on, to where another case starts, a small exit, continue) all end at CASE_EXIT, and are
        written where they are reached, so they are no place for the other ways to meet; nor is a way that
        only ends so (break, continue, return) when the other way goes on into another case: what meets is
        the code before that case."""
        key = (self.current_case, self.case_follow, self.case_starts)
        cache = self.__dict__.setdefault("case_ipdoms", {})
        if key in cache:
            return cache[key]
        start = self.current_case
        loop = self.case_loop

        def leaves(target):
            # (inside a loop: going round again or out of it, continue or a way out of the loop)
            return loop is not None and (target == loop.header or target not in loop.region)

        def onward(target):
            return target in self.case_starts and target != start and target != self.case_follow

        def out(target):
            return target == self.case_follow or onward(target) or self.small_exit(target) or leaves(target)
        region, stack = {start}, [start]
        while stack:
            for target in successors(self.blocks[stack.pop()]):
                if not out(target) and target not in region:
                    region.add(target)
                    stack.append(target)
        # the case it goes on into: the next one in the switch's order it can go on into, else the first
        targets = {target for label in region for target in successors(self.blocks[label]) if onward(target)}
        order = self.case_order
        later = [case for case in order[order.index(start) + 1:] if case in targets] if start in order else []
        main = later[0] if later else next((case for case in self.case_order if case in targets), None)
        # (the case right after it in the switch: the ways into it meet there, and C falls through into it)
        following = order[order.index(start) + 1] if start in order and order.index(start) + 1 < len(order) else None
        self.__dict__.setdefault("case_nexts", {})[key] = main if main == following else None

        def ends(target):
            # (a way out written where it is: break, a small exit, continue, a jump into another case)
            return target == self.case_follow or self.small_exit(target) or leaves(target) \
                or onward(target) and target != main

        def written_in_place(target):
            if ends(target):
                return True
            # (a few statements on the way to one of those)
            block = self.blocks[target]
            return isinstance(block.end, ir.Goto) and len(block.statements) <= SMALL_EXIT and ends(block.end.target)
        # the blocks from which the case can go on into the case it goes on into
        going_on = {label for label in region if main in successors(self.blocks[label])}
        stack = list(going_on)
        while stack:
            label = stack.pop()
            for other in region:
                if other not in going_on and label in successors(self.blocks[other]):
                    going_on.add(other)
                    stack.append(other)

        def kept(label):
            targets = [target for target in successors(self.blocks[label]) if not written_in_place(target)]
            if any(target in going_on or target == main for target in targets):
                # (a way that only ends, beside one that goes on into another case)
                targets = [target for target in targets if target in going_on or target == main]
            return [CASE_NEXT if target == main and main == following else CASE_EXIT if out(target) else target
                    for target in targets] or [CASE_EXIT]
        after = {label: kept(label) for label in region}
        after[CASE_NEXT] = [CASE_EXIT]
        reaching = Graph.reach_back(after, CASE_EXIT)
        for label in region:
            if label not in reaching:
                after[label].append(CASE_EXIT)    # (a loop inside the case nothing leaves)
        cache[key] = post_dominator_tree(after, CASE_EXIT)
        return cache[key]

    def loop_ipdom(self, loop):
        """Immediate post-dominators inside 'loop': its ways out (continue, break, a jump out of its region, a
        small exit) all end at LOOP_EXIT, and a block every way from which ends so (it is written where it is
        reached) is no place for the other ways to meet."""
        found = self.loop_ipdoms.get(loop.header)
        if found is not None:
            return found
        region = loop.region

        def out(target):
            return target == loop.header or target not in region or self.small_exit(target)
        after = {label: [LOOP_EXIT if out(target) else target for target in successors(self.blocks[label])]
                 or [LOOP_EXIT] for label in region}

        entries = {}
        for label, targets in after.items():
            for target in targets:
                entries.setdefault(target, set()).add(label)

        judged = {}

        def leaving(label):
            """A short way out only one block leads to: a few blocks every way from which leaves the loop
            without meeting other code ("if (count == 0) break; continue;"), written where it is reached. (The
            step the ways of the body meet at before going round again is no such thing, but with LEAVE_SHARED:
            then a short way out several blocks lead to, that runs into no code other blocks lead to, is written
            again where each is, a whole small loop of its own too, with COPY_LOOPS: road_surface_lines' step to
            the next segment and its copy of the points left, so that its ways meet where it draws the line.)"""
            if label not in judged:
                judged[label] = False    # (while it is judged: a way back to it is no way out)
                judged[label] = judge(label)
            return judged[label]

        def judge(label):
            shared = len(entries.get(label, ())) != 1
            if shared and not LEAVE_SHARED:
                return False
            whole_loop = LEAVE_SHARED and COPY_LOOPS and label in self.graph.loops
            reach, stack = {label}, [label]
            while stack:
                for target in after[stack.pop()]:
                    if target == LOOP_EXIT or target in reach:
                        continue
                    if target == label and not whole_loop or len(reach) >= WAY_OUT_BLOCKS:
                        return False
                    reach.add(target)
                    stack.append(target)
            if whole_loop and not set(self.graph.loops[label][0]) <= reach:
                return False
            own = self.graph.loops.get(label, ((),))[0]
            if LEAVE_SHARED and any(len(entries.get(inner, ())) != 1 and (shared or not leaving(inner))
                                    for inner in reach if inner != label and inner not in own):
                return False    # (a way out that runs into code other blocks lead to: where those ways meet)
            return all(all(target == LOOP_EXIT or target in reach for target in after[inner]) for inner in reach) \
                and (whole_loop or all(label not in after[inner] for inner in reach)) \
                and sum(len(self.blocks[inner].statements) + 1 for inner in reach) <= TAIL_COPY
        succ = {label: [target for target in targets if target != LOOP_EXIT and not leaving(target)]
                or [LOOP_EXIT] for label, targets in after.items()}
        reaching = Graph.reach_back(succ, LOOP_EXIT)
        for label in region:
            if label not in reaching:
                succ[label].append(LOOP_EXIT)    # (a loop inside whose ways out all end: it ends there)
        found = post_dominator_tree(succ, LOOP_EXIT)
        self.loop_ipdoms[loop.header] = found
        return found

    def branch(self, label, end, out, stop, loop):
        then_label, else_label = end.target, end.otherwise
        # an early way out: "if (x) { ...; return; }" and the walk goes on with the other side
        for exit_label, other, condition in ((then_label, else_label, end.condition),
                                            (else_label, then_label, negate(end.condition))):
            if loop is not None and exit_label == loop.follow:
                continue    # leaving the loop: a break, and the way out is written once after it
            if self.small_exit(exit_label) and not self.small_exit(other) and exit_label != stop \
                    and exit_label not in successors(self.blocks[other]):
                out.append(If(condition, self.copy_exit(exit_label)))
                nodes, next_label = self.jump(other, stop, loop)
                out += nodes
                return next_label
        if CASE_STOPS and (loop is None or loop.in_switch) and getattr(self, "current_case", None) is not None:
            # inside a case, a short way to where the switch goes on: "if (x) { ...; break; }" first
            for exit_label, other, condition in ((then_label, else_label, end.condition),
                                                (else_label, then_label, negate(end.condition))):
                if self.case_break(exit_label) and not self.case_break(other) and other != stop:
                    out.append(If(condition, self.case_break_nodes(exit_label)))
                    nodes, next_label = self.jump(other, stop, loop)
                    out += nodes
                    return next_label
        follow = self.follow_of(label, stop, loop)
        region_stop = follow if follow is not None else stop
        then_nodes = self.side(then_label, region_stop, loop)
        else_nodes = self.side(else_label, region_stop, loop)
        if not then_nodes and else_nodes:
            out.append(If(negate(end.condition), else_nodes))
        else:
            out.append(If(end.condition, then_nodes, else_nodes))
        if follow is None:
            return None
        nodes, next_label = self.jump(follow, stop, loop)
        out += nodes
        return next_label

    def side(self, label, stop, loop):
        """One side of an if: the walk from 'label' until 'stop'."""
        if label == stop:
            return []
        nodes, next_label = self.jump(label, stop, loop)
        if next_label is None:
            return nodes
        return nodes + self.region(next_label, stop, loop)

    def leaves_to(self, start, loop):
        """True if the code from 'start' gets to where 'loop' goes on, staying in it on the way."""
        seen, stack = {start}, [start]
        while stack:
            label = stack.pop()
            if label == loop.follow:
                return True
            if label == loop.header or label not in loop.region:
                continue
            for successor in successors(self.blocks[label]):
                if successor not in seen:
                    seen.add(successor)
                    stack.append(successor)
        return False

    def switch(self, label, end, out, stop, loop):
        follow = self.follow_of(label, stop, loop)
        by_target = {}
        for case, target in end.cases.items():
            by_target.setdefault(target, []).append(case)
        # a switch in a loop whose cases go round again, end, or leave the loop (init_screen's course walk):
        # the loop goes on where the switch does, and a break after the switch leaves it
        leaves = follow is None and loop is not None and not loop.in_switch and loop.follow is not None \
            and any(self.leaves_to(target, loop) for target in by_target)
        if leaves:
            follow = loop.follow
        region_stop = follow if follow is not None else stop
        cases = []
        inner = loop.inside_switch(leaves) if loop is not None else None
        saved = getattr(self, "case_starts", frozenset()), getattr(self, "current_case", None), \
            getattr(self, "case_follow", None), getattr(self, "case_loop", None), getattr(self, "case_order", [])
        self.case_starts, self.case_follow, self.case_loop = frozenset(by_target), region_stop, inner
        self.case_order = [target for target, _ in sorted(by_target.items(), key=lambda item: min(item[1]))]
        for target, values in sorted(by_target.items(), key=lambda item: min(item[1])):
            self.current_case = target
            body = self.side(target, region_stop, inner)
            if not ends_flow(body):
                body.append(Break())
            cases.append((sorted(values), body))
        self.case_starts, self.current_case, self.case_follow, self.case_loop, self.case_order = saved
        out.append(Switch(end.address, cases))
        if follow is None:
            return None
        nodes, next_label = self.jump(follow, stop, loop)
        out += nodes
        return next_label


def without_labels(nodes):
    """'nodes' with their labels taken out (a copy must not repeat them)."""
    found = []
    for node in nodes:
        if isinstance(node, Label):
            continue
        if isinstance(node, If):
            node = If(node.condition, without_labels(node.then), without_labels(node.otherwise))
        elif isinstance(node, Loop):
            node = Loop(without_labels(node.body), node.kind, node.condition)
        elif isinstance(node, Switch):
            node = Switch(node.value, [(values, without_labels(body)) for values, body in node.cases])
        found.append(node)
    return found


def ends_flow(nodes):
    """True if control never runs off the end of 'nodes'."""
    if not nodes:
        return False
    last = nodes[-1]
    if isinstance(last, (End, Goto, Break, Continue)):
        return True
    if isinstance(last, If):
        return bool(last.otherwise) and ends_flow(last.then) and ends_flow(last.otherwise)
    if isinstance(last, Loop):
        return last.kind == "forever" and not contains_break(last.body)
    return False


def contains_break(nodes):
    for node in nodes:
        if isinstance(node, Break):
            return True
        if isinstance(node, If) and (contains_break(node.then) or contains_break(node.otherwise)):
            return True
        if isinstance(node, Switch):
            continue    # a break there leaves the switch
    return False


# ---- conditions ----

NEGATED = {"eq": "ne", "ne": "eq"}


def breaks_out(nodes):
    """True when 'nodes' (a loop's body) hold a break that leaves that loop: not one of an inner loop, nor of
    a switch (where break leaves the switch)."""
    for node in nodes:
        if isinstance(node, Break):
            return True
        if isinstance(node, If) and (breaks_out(node.then) or breaks_out(node.otherwise)):
            return True
    return False


def negate(condition):
    """The opposite condition, said directly where it can be."""
    if isinstance(condition, ir.Op):
        name, args = condition.name, condition.args
        if floats.is_compare(condition):
            return floats.complement(condition)
        if name in NEGATED:
            if name == "eq" and args[1] == ir.Const(0, 1) and args[0].bits == 1:
                return args[0]
            return ir.op(NEGATED[name], args, 1)
        if name == "lt":
            return ir.op("le", (args[1], args[0]), 1)
        if name == "le":
            return ir.op("lt", (args[1], args[0]), 1)
        if name == "ult":
            return ir.op("ule", (args[1], args[0]), 1)
        if name == "ule":
            return ir.op("ult", (args[1], args[0]), 1)
        if name == "lor":
            return ir.op("land", (negate(args[0]), negate(args[1])), 1)
        if name == "land":
            return ir.op("lor", (negate(args[0]), negate(args[1])), 1)
    return ir.op("eq", (condition, ir.Const(0, 1)), 1)


# ---- tidying the tree ----

def tidy(nodes, labels, in_loop=False):
    """Labels nothing jumps to go, loops get their conditions (while, do-while), a continue that ends a
    loop body goes, and so on."""
    out = []
    for node in nodes:
        if isinstance(node, Label) and node.label not in labels:
            continue
        if isinstance(node, If):
            node.then = tidy(node.then, labels, in_loop)
            node.otherwise = tidy(node.otherwise, labels, in_loop)
            if not node.then and node.otherwise:
                node = If(negate(node.condition), node.otherwise)
            if not node.then and not node.otherwise:
                continue
            # no else after a side that does not come back: "if (x) return; rest"
            if node.otherwise and ends_flow(node.then):
                out.append(If(node.condition, node.then))
                out += node.otherwise
                continue
            if node.otherwise and ends_flow(node.otherwise) and not ends_flow(node.then):
                out.append(If(negate(node.condition), node.otherwise))
                out += node.then
                continue
        elif isinstance(node, Loop):
            node.body = tidy(node.body, labels, True)
            node = loop_shape(node)
        elif isinstance(node, Switch):
            node.cases = [(values, tidy(body, labels, in_loop)) for values, body in node.cases]
        out.append(node)
    return out


def loop_shape(loop):
    body = loop.body
    # the body ends "if (c) continue; break;" or "if (c) continue;" followed by nothing: do { } while (c)
    while body and isinstance(body[-1], Continue):
        body.pop()
    # the body ends "if (c) continue; a few statements; break;": the round goes on anyway, "if (!c) { ...;
    # break; }"
    if len(body) >= 3 and isinstance(body[-1], Break):
        for position in range(len(body) - 3, -1, -1):
            node = body[position]
            if isinstance(node, If) and not node.otherwise and len(node.then) == 1 \
                    and isinstance(node.then[0], Continue):
                return loop_shape(Loop(body[:position] + [If(negate(node.condition), body[position + 1:])],
                                       "forever"))
            if not isinstance(node, Code):
                break
    # "if (c) continue; else break;" at the end: do { } while (c)
    if body and isinstance(body[-1], If) and len(body[-1].then) == 1 and len(body[-1].otherwise) == 1 \
            and not uses_continue(body[:-1]):
        last = body[-1]
        if isinstance(last.then[0], Continue) and isinstance(last.otherwise[0], Break):
            return Loop(body[:-1], "do", last.condition)
        if isinstance(last.then[0], Break) and isinstance(last.otherwise[0], Continue):
            return Loop(body[:-1], "do", negate(last.condition))
    if len(body) >= 2 and isinstance(body[-1], Break) and isinstance(body[-2], If) \
            and body[-2].then and len(body[-2].then) == 1 and isinstance(body[-2].then[0], Continue) \
            and not body[-2].otherwise and not uses_continue(body[:-2]):
        condition = body[-2].condition
        return Loop(body[:-2], "do", condition)
    if body and isinstance(body[-1], If) and not body[-1].otherwise and len(body[-1].then) == 1 \
            and isinstance(body[-1].then[0], Break) and not uses_continue(body[:-1]):
        return Loop(body[:-1], "do", negate(body[-1].condition))
    # the body starts "if (c) break;": while (!c) { }
    if body and isinstance(body[0], If) and not body[0].otherwise and len(body[0].then) == 1 \
            and isinstance(body[0].then[0], Break):
        return Loop(body[1:], "while", negate(body[0].condition))
    # the body ends "if (c) { ...; continue; } break;": the way out in the middle, "if (!c) break; ..."
    if len(body) >= 2 and isinstance(body[-1], Break) and isinstance(body[-2], If) and not body[-2].otherwise \
            and len(body[-2].then) >= 2 and isinstance(body[-2].then[-1], Continue):
        last = body[-2]
        return loop_shape(Loop(body[:-2] + [If(negate(last.condition), [Break()])] + last.then[:-1], "forever"))
    return Loop(body, "forever")


def uses_continue(nodes):
    for node in nodes:
        if isinstance(node, Continue):
            return True
        if isinstance(node, If) and (uses_continue(node.then) or uses_continue(node.otherwise)):
            return True
        if isinstance(node, Switch) and any(uses_continue(body) for _, body in node.cases):
            return True
    return False


LARGER_COPIES = (40, 32, 8)    # (TAIL_COPY, REGION_COPY, WAY_OUT_BLOCKS) tried where the usual ones leave jumps a block cannot
                            # write (structured): copies that big where they take control variables away, whole small
                            # loops among them (COPY_LOOPS)
COPY_LOOPS = False          # a copy of the code a jump leads to may hold a whole loop that starts where it jumps
LEAVE_SHARED = False        # a short way out of a loop several blocks lead to is no place for the others to meet
MEET_FOLLOW = False         # (experiment) a loop whose other ways out meet past its natural end goes on there
CASE_STOPS = False          # a case of a switch stops where another case starts (a goto there, which falls through
                            # into it when it goes just before it: fall_into_cases), rather than writing that case's
                            # code as its own (structured tries it)
LARGER_GROWTH = 30          # statements a function may grow by for that


def structure(function, control_name=None, copies=None, meet_follow=False, case_stops=False, latches=True,
              leave_shared=False):
    """The tree for 'function', and the labels gotos still need: none when 'control_name' (label -> the name
    of a control variable for it) is given, as gotos.py then takes the gotos out. 'copies' are other
    (TAIL_COPY, REGION_COPY, WAY_OUT_BLOCKS); 'meet_follow' lets a loop go on where its other ways out meet past its
    natural end (natural_follow); 'case_stops' (CASE_STOPS) has a case stop where another starts; 'latches'
    (COPY_LATCHES) writes the few statements a loop goes round again after where each way jumps to them;
    'leave_shared' (LEAVE_SHARED) takes short ways out several blocks lead to as written where each is."""
    global TAIL_COPY, REGION_COPY, WAY_OUT_BLOCKS, MEET_FOLLOW, CASE_STOPS, COPY_LOOPS, COPY_LATCHES, LEAVE_SHARED
    usual = TAIL_COPY, REGION_COPY, WAY_OUT_BLOCKS, MEET_FOLLOW, CASE_STOPS, COPY_LOOPS, COPY_LATCHES, LEAVE_SHARED
    if copies is not None:
        TAIL_COPY, REGION_COPY, WAY_OUT_BLOCKS = copies
        COPY_LOOPS = True
    MEET_FOLLOW = meet_follow
    CASE_STOPS = case_stops
    COPY_LATCHES = latches
    LEAVE_SHARED = leave_shared
    try:
        structurer = Structurer(function)
        tree = tidy(structurer.build(), structurer.labels)
    finally:
        TAIL_COPY, REGION_COPY, WAY_OUT_BLOCKS, MEET_FOLLOW, CASE_STOPS, COPY_LOOPS, COPY_LATCHES, LEAVE_SHARED = usual
    fallthrough_cases(tree)
    fall_into_cases(tree)
    if case_stops:
        copy_case_gotos(tree, 2 * (copies or (TAIL_COPY, REGION_COPY))[1])    # (structured keeps it only where the function grows little)
    drop_fallthrough_gotos(tree)
    group_cases(tree)
    if control_name is not None and goto_targets(tree):
        from . import gotos    # (it rewrites this module's tree)
        tree = gotos.eliminate(tree, control_name)
    labels = goto_targets(tree)
    return drop_labels(tree, labels), labels


def structured(function, control_name):
    """structure() as usual, or, when that leaves jumps a block cannot write (each a control variable in the
    C), the way of the others (LARGER_COPIES, loops going on where their ways out meet, both, and those with
    a loop's last statements written once, where the ways to them meet, rather than at each: road_surface_lines'
    step to the next segment) that leaves the fewest of them, when it leaves fewer and the function grows by no
    more than LARGER_GROWTH statements."""
    tree, labels = structure(function)
    best, chosen = (len(labels), size(tree)), (None, False)
    if labels:
        for way in ((LARGER_COPIES, False), (None, True), (LARGER_COPIES, True), (None, False, True),
                    (LARGER_COPIES, False, True), (LARGER_COPIES, False, False, False),
                    (LARGER_COPIES, True, False, False), (LARGER_COPIES, True, False, True, True),
                    (LARGER_COPIES, False, False, True, True)):
            other, other_labels = structure(function, None, *way)
            found = (len(other_labels), size(other))
            if found[0] < best[0] and found[1] <= size(tree) + LARGER_GROWTH \
                    or found[0] == best[0] < len(labels) and found[1] < best[1]:
                best, chosen = found, way
    return structure(function, control_name, *chosen)


def size(nodes):
    """How many statements and block ends a tree writes."""
    found = 0
    for node in nodes:
        found += len(node.statements) if isinstance(node, Code) else 1
        if isinstance(node, If):
            found += size(node.then) + size(node.otherwise)
        elif isinstance(node, Loop):
            found += size(node.body)
        elif isinstance(node, Switch):
            found += sum(size(body) for _, body in node.cases)
    return found


def group_cases(nodes):
    """Cases that only go on in the same function (a table of jumps to the state code: "case 0: case 1:
    player_move(); return;") are one case with all their values."""
    for node in nodes:
        if isinstance(node, If):
            group_cases(node.then)
            group_cases(node.otherwise)
        elif isinstance(node, Loop):
            group_cases(node.body)
        elif isinstance(node, Switch):
            for _, body in node.cases:
                group_cases(body)
            grouped = []
            for values, body in node.cases:
                key = tail_target(body)
                same = next((index for index, (_, other) in enumerate(grouped)
                             if key is not None and tail_target(other) == key), None)
                if same is None:
                    grouped.append((list(values), body))
                else:
                    grouped[same] = (sorted(grouped[same][0] + list(values)), grouped[same][1])
            node.cases = grouped


def tail_target(body):
    """(target, entry) of a case body that is only a tail call, or None."""
    nodes = [node for node in body if not isinstance(node, Label)]
    if len(nodes) == 1 and isinstance(nodes[0], End) and isinstance(nodes[0].end, ir.TailCall):
        return nodes[0].end.target, nodes[0].end.entry
    return None


def fallthrough_cases(nodes):
    """A case that is only a goto into the middle of another case, where that one's code runs on into
    it (the original lays case 3's code just before case 2's), goes right after that case, which then
    falls through into it the way C cases do: "case 3: ...; case 2: ...; break;"."""
    for node in nodes:
        if isinstance(node, If):
            fallthrough_cases(node.then)
            fallthrough_cases(node.otherwise)
        elif isinstance(node, Loop):
            fallthrough_cases(node.body)
        elif isinstance(node, Switch):
            for _, body in node.cases:
                fallthrough_cases(body)
            changed = True
            while changed:
                changed = False
                for b_index, (b_values, b_body) in enumerate(node.cases):
                    if len(b_body) != 1 or not isinstance(b_body[0], Goto):
                        continue
                    label = b_body[0].label
                    for a_index, (a_values, a_body) in enumerate(node.cases):
                        position = next((index for index, inner in enumerate(a_body)
                                         if isinstance(inner, Label) and inner.label == label), None)
                        if a_index == b_index or position is None:
                            continue
                        head, tail = a_body[:position], a_body[position:]
                        node.cases[a_index] = (a_values, head)
                        moved = (b_values, tail)
                        del node.cases[b_index]
                        node.cases.insert(node.cases.index((a_values, head)) + 1, moved)
                        changed = True
                        break
                    if changed:
                        break


def fall_into_cases(nodes):
    """A case that ends going on to the start of another case (lap_timer_update's state 3 goes back to
    state 2's loop) goes just before that case and falls through into it, the way C cases do, where nothing
    else falls into either of them: no control variable to go round the switch again."""
    for node in nodes:
        if isinstance(node, If):
            fall_into_cases(node.then)
            fall_into_cases(node.otherwise)
        elif isinstance(node, Loop):
            fall_into_cases(node.body)
        elif isinstance(node, Switch):
            for _, body in node.cases:
                fall_into_cases(body)
            changed = True
            while changed:
                changed = False
                for b_index, (b_values, b_body) in enumerate(node.cases):
                    if not b_body or not isinstance(b_body[-1], Goto):
                        continue
                    label = b_body[-1].label
                    a_index = next((index for index, (_, body) in enumerate(node.cases)
                                    if index != b_index and starts_at(body, label)), None)
                    if a_index is None:
                        continue
                    if a_index == b_index + 1:
                        node.cases[b_index] = (b_values, b_body[:-1])    # (it is next: it falls into it)
                        changed = True
                        break
                    # (nothing may fall into the case that moves, nor into the one it goes before)
                    if b_index > 0 and not ends_flow(node.cases[b_index - 1][1]):
                        continue
                    if a_index > 0 and not ends_flow(node.cases[a_index - 1][1]):
                        continue
                    moved = (b_values, b_body[:-1])
                    del node.cases[b_index]
                    a_index = next(index for index, (_, body) in enumerate(node.cases) if starts_at(body, label))
                    node.cases.insert(a_index, moved)
                    changed = True
                    break


def starts_at(nodes, label):
    """True if 'nodes' start with the code at 'label': its label, its block, or a loop that starts there."""
    if not nodes:
        return False
    first = nodes[0]
    if isinstance(first, Label) or isinstance(first, Code):
        return first.label == label
    if isinstance(first, Loop) and first.kind != "while":
        return starts_at(first.body, label)
    return False


def copy_case_gotos(nodes, limit, targeted=None):
    """A goto to the start of another case of the same switch, where that case is small (no more than
    'limit' statements), ends (break, return) and has no gotos of its own nor labels a goto jumps to past
    its start: that case's code written again where the goto is (song_title_show's state 2 going on into
    state 4)."""
    if targeted is None:
        targeted = goto_targets(nodes)
    for node in nodes:
        if isinstance(node, If):
            copy_case_gotos(node.then, limit, targeted)
            copy_case_gotos(node.otherwise, limit, targeted)
        elif isinstance(node, Loop):
            copy_case_gotos(node.body, limit, targeted)
        elif isinstance(node, Switch):
            starts = {}
            for index, (_, body) in enumerate(node.cases):
                if body and isinstance(body[0], Label):
                    rest = list(body[1:])
                    # (a case that falls through into the next ones: those too, up to one that ends)
                    following = index + 1
                    while not ends_flow(rest) and following < len(node.cases):
                        after = node.cases[following][1]
                        rest += after[1:] if after and isinstance(after[0], Label) else after
                        following += 1
                    if ends_flow(rest) and size(rest) <= limit and not goto_targets(rest) \
                            and not any(isinstance(inner, Label) and inner.label in targeted
                                        for inner in walk_nodes(rest)):
                        starts[body[0].label] = rest
            for _, body in node.cases:
                replace_gotos(body, starts)
                copy_case_gotos(body, limit, targeted)


def walk_nodes(nodes):
    """Every node of 'nodes', the ones inside ifs, loops and switches too."""
    for node in nodes:
        yield node
        if isinstance(node, If):
            yield from walk_nodes(node.then)
            yield from walk_nodes(node.otherwise)
        elif isinstance(node, Loop):
            yield from walk_nodes(node.body)
        elif isinstance(node, Switch):
            for _, body in node.cases:
                yield from walk_nodes(body)


def replace_gotos(nodes, starts):
    """The gotos in 'nodes' (not inside loops or switches of their own, where break means another thing)
    to a label of 'starts' replaced by a copy of its nodes."""
    for index, node in enumerate(nodes):
        if isinstance(node, Goto) and node.label in starts:
            # (the same statements, as the other copies of code: what the decompiler noted about them holds)
            nodes[index:index + 1] = without_labels(starts[node.label])
            return replace_gotos(nodes, starts)
        if isinstance(node, If):
            replace_gotos(node.then, starts)
            replace_gotos(node.otherwise, starts)


def drop_labels(nodes, labels):
    """The tree without the labels no goto jumps to (and the ifs left with nothing to do)."""
    out = []
    for node in nodes:
        if isinstance(node, Label) and node.label not in labels:
            continue
        if isinstance(node, If):
            node.then = drop_labels(node.then, labels)
            node.otherwise = drop_labels(node.otherwise, labels)
            if not node.then and not node.otherwise:
                continue
            if not node.then:
                node = If(negate(node.condition), node.otherwise)
        elif isinstance(node, Loop):
            node.body = drop_labels(node.body, labels)
        elif isinstance(node, Switch):
            node.cases = [(values, drop_labels(body, labels)) for values, body in node.cases]
        out.append(node)
    return out


def drop_fallthrough_gotos(nodes, follow=frozenset()):
    """A goto that ends a branch, to a label the code reaches anyway when the branch ends (the label just
    after the if), goes. 'follow': the labels falling off the end of 'nodes' reaches."""
    for position, node in enumerate(nodes):
        after = set()
        rest = position + 1
        while rest < len(nodes) and isinstance(nodes[rest], Label):
            after.add(nodes[rest].label)
            rest += 1
        if rest == len(nodes):
            after |= follow
        if isinstance(node, If):
            drop_fallthrough_gotos(node.then, frozenset(after))
            drop_fallthrough_gotos(node.otherwise, frozenset(after))
        elif isinstance(node, Loop):
            drop_fallthrough_gotos(node.body)
        elif isinstance(node, Switch):
            for _, body in node.cases:
                drop_fallthrough_gotos(body)
    if nodes and isinstance(nodes[-1], Goto) and nodes[-1].label in follow:
        nodes.pop()


def goto_targets(nodes):
    """The labels the gotos of a tree jump to."""
    found = set()
    for node in nodes:
        if isinstance(node, Goto):
            found.add(node.label)
        elif isinstance(node, If):
            found |= goto_targets(node.then) | goto_targets(node.otherwise)
        elif isinstance(node, Loop):
            found |= goto_targets(node.body)
        elif isinstance(node, Switch):
            for _, body in node.cases:
                found |= goto_targets(body)
    return found
