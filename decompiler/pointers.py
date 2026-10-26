"""
Name:        pointers.py
Description: works out which registers point at which of the game's structures, so that a memory access
             through them reads as a field: OBJECT(r20)->unk_30 instead of mem_read32(r20 + 0x30u)
Version:     0.1.0
Created:     12-10-2026
Modified:    25-10-2026
Author:      Sebastian Toyos

The structures (STRUCTS) are OutRunners': the game objects (0x100 bytes, 124 of them below each screen's
block, run by the object loop with R20 pointing at each), the screen's car block (0x200 bytes at 1000 of
the screen's block, R23; its field 0 is the car's object) and the block of the screen a task runs (from
0x208000 or 0x218000, R25). A kind is (structure, offset): a pointer to a structure, or into it; or
("global", address): a pointer to a variable in work RAM, so that what is reached through it reads as
that variable.

Fields and variables symbols.txt says point at a structure (a struct name after their type) give
their kind to what is read from them: a u32 holds the address, a u16 the low half of it, which the code
puts in the work RAM page ("(r & 0xFFFF0000) | halfword"). An access through any expression of a known
kind reads as a field, not only through a register.

A kind may also say which family's structure it points into ((struct, offset, family): object.eggman_scene
in symbols.txt), so that what is read through it reads as that family's code reads its own object (its
scoped fields, emit.py). The family comes from what symbols.txt says a field, variable, parameter or local
points at, and from the function's own name: the object a function of a family runs on (R20 and R23 when
it starts) is that family's. Two paths, callers or returns that say two families of one structure say
the structure alone.

Pointers into the program ROM (REGIONS: "rom") have a kind of their own, without a structure: an address
from 0x1000 on in the ROM, plus any offset, or a word read from a field or variable symbols.txt says points
into the ROM ("rom" after its type). What is read through one reads the ROM itself (ROM16(p)) rather than
the board's bus; rom() stops the program if such an address ever falls outside the ROM.

A pointer may also say which record of the program ROM it points at (ROM_STRUCTS: "struct NAME SIZE rom"; an
array of plain values, ELEMENTS, is a record of one value), and so may a table of them at a fixed address
(RECORD_TABLES). Then a read through it plus a record's number times its size, or past the record it is in,
is a field of that record (indexed_access: STRUCT(p)[i].field, STRUCT(p)[1].field, name[i].field); a field
that is an array (FIELD_ARRAYS: TYPE[N] in the record, TYPE[] after its end) takes an element's number too
(array_access: STRUCT(p)->field[j]). The number is what the code adds, looked through widenings, chains of
constant factors and the Z80's i + i; an offset that is not whole records points into the ROM, no more.

Forward over each function's blocks, a register keeps the kind of what it was set to:
  - another register, or one plus a constant (a pointer into the same structure; into a structure
    inside it, NESTED, a pointer to that one: the screen's block plus 0x1000 is its car);
  - an address BASE_KINDS names (the blocks of the two screens, 0x208000 and 0x218000);
  - "(r & 0xFFFF0000) | halfword": the game keeps structures in the 0x20xxxx page and links them with
    16-bit addresses, so this is another structure of the same kind, or the one PAGE_LINK_KINDS says the
    page of r's leads to (a screen's objects are in the page of its block);
  - the first of a run of players (PLAYERS) plus a multiple of their size: a player (none in OutRunners);
  - a word read from a variable that holds a pointer (GLOBAL_KINDS: the ends of the object list);
  - the variables' base (VARIABLES) plus a word read from a table of offsets
    symbols.txt says lead to a structure.
Across functions: what a function finds in its registers when it starts is what its callers have there
(through calls of any kind), and what a call leaves in the registers it hands back is what the callee
has there when it returns. A caller that does not know says nothing; two that know different things make
it unknown. The context registers start as the game keeps them (CONTEXT_KINDS: R20 an object, R23 the
car, R25 the screen's block) unless the callers say otherwise. R20 holds an object wherever the game puts
it, except in the blocks of variables it walks (VARIABLE_BLOCKS: the test mode's settings), which read as
those variables. Inside a function, where paths meet, a kind holds only if it is the same on all of them.
Repeated until it settles.

This only decides how accesses are written, never what they do: the C reads and writes the same bytes.

The structures, places and rules above are the V60 program's. Another program sets its own in the module's
names below (tools/decompile_sound.py: the Z80's sound channels in its shared RAM, 16-bit pointers).
"""
from v60 import ir

STRUCTS = {}
VARIABLES = None          # the base of the game's variables (R24, which the code adds offsets to: the race
                              # records' copies from rom_00E0A0); R25 is the block of the screen each task runs
PLAYERS = (0, 0, 0)           # first, size, count of a run of player blocks (SegaSonic's); none
# (struct, offset) or ("global", address) -> (struct, bits): what a field or variable points at (Program)
TARGETS = {}
# (struct, family, offset) -> (what it points at, bits): a field one family of functions uses as a pointer;
# ("global", family, address) the same for a variable of the work RAM the family uses its own way
SCOPED_TARGETS = {}
GLOBAL_FAMILY = None          # the family whose own names of the work RAM the function being analysed uses
CONTEXT_KINDS = {}
OBJECT_REGISTERS = ()      # what the game keeps here it reads as objects, wherever it put them
OWN_REGISTERS = ()         # what points at the object a function runs on when it starts (the object loop's)
HANDLER_FIELD = None  # where an object keeps the code the object loop runs for it: set to a function of a
                               # family, the object is that family's
# blocks of the work RAM the object registers walk as variables, not as objects: start -> size (the test
# mode reads and writes the settings, 0x20E600-0x20E67F, through R20)
VARIABLE_BLOCKS = {}
REGIONS = {"rom"}             # kinds that are a memory, not a structure
OFFSET = ("offset", 0)        # not a pointer: a value below 0x10000 (a halfword read, a mask...), an offset
CONFLICT = ("conflict", 0)    # callers that know different things: nothing is known
UNKNOWN = ("unknown", 0)      # a caller that does not know
ROUNDS = 40
WORK_RAM = (0, 0)
ADDRESS_MASK = 0xFFFFFF
POINTER_BITS = 32              # a value read at this width holds a whole address
PAGE_LINKS = True              # structures linked by the low halves of their addresses, in the work RAM page
ROM_DATA = (0x1000, 0x200000)  # where the program ROM's data can be (the vectors and first code are below)
ARRAYS = {}                    # struct -> (first, count): structures one after another, so that a pointer
                               # moved by the size of one points at the next (PLAYERS has its own rules)
STRUCT_FUNCTION = "ram"        # what gives a pointer to a structure from its address (game/support.h)
ROM_STRUCTS = set()            # structures that are records of the program ROM: read through ROM_FUNCTION
ROM_FUNCTION = "rom"           # ... and what gives a pointer to one of those (const)
CONSTANTS = False              # registers keep the constants they are set to, so that a pointer moved by one
                               # (the Z80's "ld de,12h ... add iy,de") keeps its kind
PAGED = False                  # structures a page long, one per page: (p & FF00) | n is field n of p's (page_parts)
# addresses that are a structure of the game: the blocks of the two screens (R25 of each task)
BASE_KINDS = {}
# what the page of a structure's address plus a 16-bit link points at, where it is not the same structure:
# the objects of a screen are in the page of its block (0x200400-0x207FFF, 0x210400-0x217FFF)
PAGE_LINK_KINDS = {}
# structures inside others: struct -> (offset, size, inner struct); a pointer into one is a pointer to it
NESTED = {}
# structures right below others: struct -> (size, the structure below it): below each screen's block, its
# objects' area (the lists of the objects, their slots, the spare object and the 124 objects)
BELOW = {}
# structures at fixed addresses, whose fields the code also reaches by their own address: start -> (struct,
# how far into it): the blocks of the two screens (up to the game's variables at 0x20E000) and the race
# records (each screen's, and the copy of them each screen keeps)
INSTANCES = {}
RUNS = set()                   # structures laid one after another wherever a pointer starts (no fixed base,
                               # unlike ARRAYS): a pointer moved past one points into the next
ELEMENTS = {}                  # ROM structure -> element type: an array of one value per element (symbols.txt
                               # "array"), read as NAME(p)[i] rather than through a field
RECORD_TABLES = {}             # program ROM address -> (name, ROM structure): a table of records, name[i].field
FIELD_ARRAYS = {}              # (struct, offset) -> (bits, count): a field that is an array of 'count' elements
                               # of 'bits' (None: it goes on past the record's end), read STRUCT(p)->field[i]


BASES = {}                     # name -> address of the program's data, for "STRUCT@NAME" (decompile.py)


def link_kind(link, offset=0):
    """The kind of a pointer to what 'link' names (symbols.txt): STRUCT, STRUCT.FAMILY or a region;
    STRUCT+OFFSET (hexadecimal) that many bytes into one; STRUCT@BASE (BASE a name of the program's data or an
    address, then maybe + a hexadecimal offset) no pointer but a byte offset from BASE to one: ("to", base,
    STRUCT)."""
    if "@" in link:
        struct, _, base = link.partition("@")
        if not base:
            return "rel", struct    # (a byte offset from the table it is read from: STRUCT@)
        name, plus, extra = base.partition("+")
        address = BASES[name] if name in BASES else int(name, 16)
        extra = int(extra, 16) if plus else 0
        return "to", address + extra, struct, address, extra
    struct, plus, extra = link.partition("+")
    if plus:
        offset += int(extra, 16)
    struct, _, family = struct.partition(".")
    return (struct, offset, family) if family else (struct, offset)


def family_of(kind):
    """The family of the structure a kind points into, or None."""
    return kind[2] if kind is not None and len(kind) > 2 else None


def paged(kind):
    """True for a pointer into a structure a page long whose array starts on a page (the Z80's sequence
    players): its high byte picks the structure, its low byte the field."""
    return PAGED and kind is not None and kind[0] in STRUCTS and STRUCTS[kind[0]] == 0x100 \
        and kind[0] in ARRAYS and ARRAYS[kind[0]][0] & 0xFF == 0


def is_page_mask(node):
    return isinstance(node, ir.Op) and node.name == "and" and isinstance(node.args[1], ir.Const) \
        and node.args[1].value == 0xFF00 and node.bits == 16


def page_parts(node, state, zeros):
    """(structure, field offset, the structure's address) for a pointer into a paged structure set a byte
    at a time: p & FF00 (the structure), (p & FF00) | n ("ld l,n"), (p & FF00) | (p + n) & FF ("inc l"), or
    q | n with q a structure's own address; None otherwise."""
    if is_page_mask(node):
        inner = kind_of(node.args[0], state, zeros)
        return (inner[0], 0, node) if paged(inner) else None
    if isinstance(node, ir.Op) and node.name == "shl" and node.bits == 16 and isinstance(node.args[1], ir.Const) \
            and node.args[1].value == 8:
        # the next page's structure ("inc h; ld l,0"): ((p >> 8) + n) << 8
        page = node.args[0]
        while isinstance(page, ir.Op) and page.name in ("zext", "fits", "trunc"):
            page = page.args[0]
        if isinstance(page, ir.Op) and page.name == "add" and isinstance(page.args[1], ir.Const):
            high = page.args[0]
            while isinstance(high, ir.Op) and high.name in ("zext", "fits", "trunc"):
                high = high.args[0]
            if isinstance(high, ir.Op) and high.name == "shr" and isinstance(high.args[1], ir.Const) \
                    and high.args[1].value == 8:
                inner = kind_of(high.args[0], state, zeros)
                if paged(inner):
                    return inner[0], 0, node
        return None
    if not (isinstance(node, ir.Op) and node.name == "or" and node.bits == 16):
        return None
    for kept, low in (node.args, tuple(reversed(node.args))):
        if is_page_mask(kept):
            inner = kind_of(kept.args[0], state, zeros)
            if not paged(inner):
                continue
            if isinstance(low, ir.Const) and low.value < 0x100:
                return inner[0], low.value, kept
            byte = low
            while isinstance(byte, ir.Op) and byte.name in ("zext", "fits", "trunc"):
                byte = byte.args[0]
            if isinstance(byte, ir.Op) and byte.name == "add" and isinstance(byte.args[1], ir.Const):
                moved = byte.args[0]
                while isinstance(moved, ir.Op) and moved.name in ("zext", "fits", "trunc"):
                    moved = moved.args[0]
                if moved == kept.args[0]:
                    return inner[0], (inner[1] + byte.args[1].value) & 0xFF, kept
        elif isinstance(low, ir.Const) and low.value < 0x100:
            inner = kind_of(kept, state, zeros)
            if paged(inner) and inner[1] == 0:
                return inner[0], low.value, kept
    return None


def below(struct, offset):
    """(struct, offset) of a place before the start of 'struct' that BELOW says is another structure (or one
    inside that, NESTED), or None."""
    if struct not in BELOW or offset >= 0:
        return None
    size, under = BELOW[struct]
    if offset < -size or under not in STRUCTS:
        return None
    offset += size
    for start, length, nested in NESTED.get(under, ()):
        if start <= offset < start + length and nested in STRUCTS:
            return nested, offset - start
    return under, offset


def instance_of(value):
    """(start, struct, offset) when 'value' is the address of a field of a structure at a fixed address
    (INSTANCES), or None; a structure inside it (NESTED) is left to the pointers that reach it."""
    for start, (struct, extent) in INSTANCES.items():
        if start <= value < start + extent:
            offset = value - start
            if any(inner <= offset < inner + size for inner, size, _ in NESTED.get(struct, ())):
                return None
            return start, struct, offset
    return None


def player_constant(value):
    first, size, count = PLAYERS
    if first <= value < first + size * count:
        return "player", (value - first) % size
    for struct, (start, number) in ARRAYS.items():
        size = STRUCTS[struct]
        if start <= value < start + size * number:
            return struct, (value - start) % size
    return None


def in_work_ram(value):
    return WORK_RAM[0] <= value < WORK_RAM[1]


def record_table(value):
    """(structure, offset into its first record) for an address at the start of a table of records, or into
    its first record (a column of the table: hud_gauge_positions_3p + 2, the y's); None otherwise."""
    for address, (_, struct) in RECORD_TABLES.items():
        if 0 <= value - address < STRUCTS[struct]:
            return struct, value - address
    return None


def record_table_name(value):
    """(name, structure, offset) of the table of records whose first record 'value' is in, or None."""
    for address, (name, struct) in RECORD_TABLES.items():
        if 0 <= value - address < STRUCTS[struct]:
            return name, struct, value - address
    return None


def upper_bound(node):
    """The largest value 'node' can have, from its masks, constant factors and widths."""
    if isinstance(node, ir.Const):
        return node.value
    if isinstance(node, ir.Op):
        name, args = node.name, node.args
        if name == "and":
            return min(upper_bound(arg) for arg in args)
        if name in ("zext", "fits"):
            return upper_bound(args[0])
        if name == "mul" and isinstance(args[1], ir.Const):
            return upper_bound(args[0]) * args[1].value
        if name == "shl" and isinstance(args[1], ir.Const):
            return upper_bound(args[0]) << args[1].value
        if name in ("shr", "sar") and isinstance(args[1], ir.Const) and name == "shr":
            return upper_bound(args[0]) >> args[1].value
    return (1 << node.bits) - 1


def narrow_count(node, size):
    """The number of elements of 'size' bytes in an offset worked out in fewer bits than an address: a power
    of two times x in a halfword (shl.h, or a product cut to 16 bits) is x with its bits past the halfword
    dropped: (uint16_t)(x << 1) is element x & 0x7FFF of a table of words, exactly. None otherwise."""
    while isinstance(node, ir.Op) and node.name in ("zext", "fits") and node.args[0].bits < node.bits:
        node = node.args[0]
    width = node.bits
    if isinstance(node, ir.Op) and node.name == "trunc":
        node = node.args[0]
    elif not (isinstance(node, ir.Op) and width < POINTER_BITS):
        return None
    factor, base = 1, node
    while isinstance(base, ir.Op) and base.name in ("shl", "mul") and isinstance(base.args[1], ir.Const) and (
            base.name == "shl" or base.args[1].value > 0 and base.args[1].value & (base.args[1].value - 1) == 0):
        factor *= 1 << base.args[1].value if base.name == "shl" else base.args[1].value
        base = base.args[0]
    if base is node or size & (size - 1) or not size <= factor < 1 << width or factor % size:
        return None
    kept = base if upper_bound(base) * factor < 1 << width else \
        ir.op("and", (base, ir.Const((1 << (width - (factor.bit_length() - 1))) - 1, base.bits)), base.bits)
    if factor == size:
        return kept
    if kept.bits < POINTER_BITS:
        kept = ir.op("zext", (kept,), POINTER_BITS)
    return ir.op("mul", (kept, ir.Const(factor // size, POINTER_BITS)), POINTER_BITS)


def scaled_count(node, size, zeros=None):
    """The number of whole records of 'size' bytes 'node' is an offset of (x for "x * size", "x << n" with
    size 2^n, a constant's quotient), or None. Widenings are looked through, and a cut to fewer bits that
    keeps every bit the value can have (the Z80's byte arithmetic): an index cut for real wraps where a
    record number would not."""
    narrow = narrow_count(node, size)
    if narrow is not None:
        return narrow
    while isinstance(node, ir.Op) and (node.name in ("zext", "fits")
                                       or node.name == "trunc" and upper_bound(node.args[0]) < 1 << node.bits):
        node = node.args[0]
    if isinstance(node, ir.Op) and node.name == "trunc" and size & (size - 1) == 0:
        # an index the code works out in fewer bits (shl.h): a power of two times x, cut to them, is a number
        # of records cut to so many bits fewer: (uint16_t)(x << 1) is element x & 0x7FFF of a table of words
        width = node.bits
        factor, base = 1, node.args[0]
        while isinstance(base, ir.Op) and (base.name == "shl" and isinstance(base.args[1], ir.Const)
                                           or base.name == "mul" and isinstance(base.args[1], ir.Const)
                                           and base.args[1].value & (base.args[1].value - 1) == 0):
            factor *= 1 << base.args[1].value if base.name == "shl" else base.args[1].value
            base = base.args[0]
        if size <= factor < 1 << width:
            kept = ir.op("and", (base, ir.Const((1 << (width - (factor.bit_length() - 1))) - 1, base.bits)), base.bits)
            return kept if factor == size else ir.op("mul", (kept, ir.Const(factor // size, base.bits)), base.bits)
        return None
    if isinstance(node, ir.Const):
        value = node.value if node.value < 0x80000000 else node.value - 0x100000000
        return ir.Const(value // size & 0xFFFFFFFF, 32) if value % size == 0 else None
    if size == 1:
        return node
    if isinstance(node, ir.Reg) and zeros is not None and size & (size - 1) == 0 \
            and zeros.get(node.index, 0) >= size.bit_length() - 1:
        # a register known to hold a multiple of the size (stage x 2, kept in a register): its quotient
        return ir.op("udiv", (node, ir.Const(size, node.bits)), node.bits)
    # (a chain of constant factors and shifts: x * 2 * 2 is four times x)
    factor, base = 1, node
    while isinstance(base, ir.Op) and base.bits == node.bits and (
            base.name == "mul" and any(isinstance(arg, ir.Const) for arg in base.args)
            or base.name == "shl" and isinstance(base.args[1], ir.Const)):
        if base.name == "shl":
            factor, base = factor << base.args[1].value, base.args[0]
        else:
            constant, other = (base.args[1], base.args[0]) if isinstance(base.args[1], ir.Const) else base.args
            factor, base = factor * constant.value, other
    if factor == size:
        return base
    if factor > size and factor % size == 0:
        return ir.op("mul", (base, ir.Const(factor // size, base.bits)), base.bits)
    return None


def records_kind(kind):
    """True for the kind of a pointer to (or into) a record of the program ROM, which may move by whole
    records: by an index, or past the end of one into the next."""
    return kind is not None and kind[0] in ROM_STRUCTS and kind[0] in STRUCTS


def in_variable_block(kind):
    """True for a pointer to a variable of one of VARIABLE_BLOCKS."""
    return kind is not None and kind[0] == "global" and \
        any(start <= kind[1] < start + size for start, size in VARIABLE_BLOCKS.items())


def walks_variable_block(node, state):
    """True when 'node' is worked out from a register that points into one of VARIABLE_BLOCKS (the test
    mode picks a setting by the low byte of R20: what it reaches is no variable known in advance)."""
    if isinstance(node, ir.Reg):
        return in_variable_block(state.get(node.index))
    return isinstance(node, ir.Op) and any(walks_variable_block(arg, state) for arg in node.args)


def merge(a, b):
    """Kinds two paths agree on. A pointer to a variable in the work RAM on one and to a structure on the
    other points at that structure: a list's head is reached as the link field of a structure that is not
    there (check_zones starts its walk of hitter_list at 0x20F016, whose field 68, hit_list_next, is
    hitter_list), and the same register then walks the structures it links."""
    found = {}
    for index, kind in a.items():
        other = b.get(index)
        if other == kind:
            found[index] = kind
        elif other is not None and kind[:2] == other[:2] and kind[0] in STRUCTS:
            found[index] = kind[:2]    # one structure, of two families (or of one and of none): of none
        elif other is not None and {kind[0], other[0]} & {"global"}:
            pointer, structure = (kind, other) if kind[0] == "global" else (other, kind)
            if structure[0] in STRUCTS and structure[1] == 0 and in_work_ram(pointer[1]):
                found[index] = structure
        elif other is not None and all(records_kind(either) or either == ("rom", 0) for either in (kind, other)):
            found[index] = ("rom", 0)    # into the program ROM either way: two kinds of record, or one and other data
    return found


def gather(a, b):
    """What two callers (or returns) say together: CONFLICT where they know different things. One that
    does not know leaves what the other knows about a structure (a guess that only names fields), but not
    about a variable's address, which the C would put in place of the register: that has to be certain."""
    found = dict(a)
    for index, kind in b.items():
        old = found.get(index)
        if old is None or old == CONFLICT:
            if old is None:
                found[index] = kind
            continue
        if old == kind:
            continue
        if UNKNOWN in (old, kind):
            other = kind if old == UNKNOWN else old
            found[index] = CONFLICT if other[0] == "global" else other
        elif old[:2] == kind[:2] and old[0] in STRUCTS:
            found[index] = old[:2]    # one structure, of two families: of none
        else:
            found[index] = CONFLICT
    return found


def outward(kind):
    """What a function tells its callees and callers about a register: a pointer into the ROM, or an offset,
    stays inside the function (a constant that happens to be a ROM address is not news to anyone)."""
    return UNKNOWN if kind[0] in REGIONS or kind == OFFSET or kind[0] == "const" else kind


def held_constant(node, state):
    """A constant 'node' is or a register holds (CONSTANTS), as a Const; else 'node'."""
    if CONSTANTS and isinstance(node, ir.Reg) and state is not None:
        kind = state.get(node.index)
        if kind is not None and kind[0] == "const":
            return ir.Const(kind[1], node.bits)
    return node


def known(kinds):
    return {index: kind for index, kind in kinds.items() if kind not in (CONFLICT, UNKNOWN)}


def signed(offset):
    offset &= (1 << POINTER_BITS) - 1
    return offset - (1 << POINTER_BITS) if offset >= 1 << (POINTER_BITS - 1) else offset


def low_zeros(node, zeros):
    """How many low bits of 'node' are known to be zero ('zeros': the same for registers)."""
    if isinstance(node, ir.Const):
        value = node.value
        return (value & -value).bit_length() - 1 if value else node.bits
    if isinstance(node, ir.Reg):
        return zeros.get(node.index, 0)
    if not isinstance(node, ir.Op):
        return 0
    name, args = node.name, node.args
    if name == "shl" and isinstance(args[1], ir.Const):
        return min(node.bits, low_zeros(args[0], zeros) + args[1].value)
    if name == "mul":
        return min(node.bits, low_zeros(args[0], zeros) + low_zeros(args[1], zeros))
    if name == "and":
        return max(low_zeros(args[0], zeros), low_zeros(args[1], zeros))
    if name == "add" and args[0] == args[1]:
        return min(node.bits, low_zeros(args[0], zeros) + 1)    # (x + x: twice x, "add.h R5, R5")
    if name in ("or", "xor", "add", "sub"):
        return min(low_zeros(args[0], zeros), low_zeros(args[1], zeros))
    if name in ("zext", "trunc", "fits"):
        return min(node.bits, low_zeros(args[0], zeros))
    return 0


def kind_of(node, state, zeros):
    """The kind of the value 'node' computes, or None."""
    if isinstance(node, ir.Reg):
        return state.get(node.index)
    if isinstance(node, ir.Temp):
        return state.get(("temp", node.name))    # (PAGED only: the Z80's EX DE,HL goes through one)
    if isinstance(node, ir.Const):
        value = node.value & ADDRESS_MASK
        found = BASE_KINDS.get(value) or player_constant(value)
        if found is None:
            instance = instance_of(value)
            if instance is not None:
                found = instance[1], instance[2]    # a place in a structure at a fixed address (INSTANCES)
        if found is None and in_work_ram(value):
            found = ("global", value)
        if found is None and rom_address(value):
            found = record_table(value) or ("rom", 0)
        return found
    if isinstance(node, ir.Load):
        if node.bits == POINTER_BITS:
            target = target_of(node.address, state, POINTER_BITS)
            return link_kind(target) if target is not None else None
        # (a narrower value symbols.txt says is a byte offset to a record: STRUCT@BASE, STRUCT@)
        target = target_of(node.address, state, node.bits)
        return link_kind(target) if target is not None and "@" in target else None
    if not isinstance(node, ir.Op):
        return None
    name, args = node.name, node.args
    if name in ("zext", "fits") and isinstance(args[0], ir.Load):
        found = kind_of(args[0], state, zeros)
        if found is not None and found[0] in ("to", "rel"):
            return found
    if name in ("zext", "fits"):
        found = kind_of(args[0], state, zeros)
        if records_kind(found):
            return found    # (a pointer to a record in the ROM's first 64 KB, known to fit 16 bits)
    if PAGED and name in ("and", "or", "shl"):
        found = page_parts(node, state, zeros)
        if found is not None:
            return found[0], found[1]
    if CONSTANTS and name == "add":
        args = tuple(held_constant(arg, state) for arg in args)
    if name == "add" and node.bits == POINTER_BITS and not all(isinstance(arg, ir.Const) for arg in args):
        # a record of the program ROM picked by its number (a pointer, or a table of records, plus the number
        # times the size of a record), or past the one a pointer is in: another record of the same kind
        found = indexed_access(ir.op("add", args, node.bits) if CONSTANTS else node, state, in_fields=False)
        if found is not None:
            return (found[0], found[1]) + ((found[4],) if found[4] is not None else ())
    if name in ("add", "sub"):
        # a pointer into the ROM moved by an offset: something that cannot be an address itself (below
        # 0x10000), so that a RAM pointer plus a displacement that happens to be a ROM address stays unknown.
        # A pointer to a record moved by something other than whole records points into the ROM, no more
        kinds = [kind_of(arg, state, zeros) if not isinstance(arg, ir.Const) or name == "add" else None
                 for arg in args]
        # (a byte offset to a record, read where it leads nowhere symbols.txt says: an offset like any other)
        kinds = [OFFSET if kind is not None and kind[0] in ("to", "rel") else kind for kind in kinds]
        if not any(isinstance(arg, ir.Const) for arg in args):
            kinds = [("rom", 0) if records_kind(kind) else kind for kind in kinds]
        elif name == "add":
            # (a table of records, by its address, plus something other than a number of records)
            kinds = [("rom", 0) if isinstance(arg, ir.Const) and records_kind(kind) else kind
                     for kind, arg in zip(kinds, args)]
        if kinds.count(("rom", 0)) == 1 and all(kind in (("rom", 0), OFFSET) or (kind is None and offset_sized(arg, state))
                                                 for kind, arg in zip(kinds, args)):
            return "rom", 0
    if name in ("add", "sub") and isinstance(args[1], ir.Const):
        delta = args[1].value if name == "add" else -args[1].value
        inner = kind_of(args[0], state, zeros)
        if inner is not None and inner[0] == "global":
            value = (inner[1] + delta) & ADDRESS_MASK
            return ("global", value) if in_work_ram(value) else None
        if inner is not None and inner[0] in STRUCTS:
            offset = signed(inner[1] + delta)
            if 0 <= offset < STRUCTS[inner[0]]:
                for start, size, nested in NESTED.get(inner[0], ()):
                    if start <= offset < start + size:
                        return nested, offset - start
                return (inner[0], offset) + tuple(inner[2:])
            found = below(inner[0], offset)
            if found is not None:
                return found
            if inner[0] in RUNS or (inner[0] in ARRAYS and offset >= 0):
                return inner[0], offset % STRUCTS[inner[0]]    # into the next (or the one before) of the array
            return None
    if name == "add" and isinstance(args[0], ir.Const) and args[0].value & ADDRESS_MASK == VARIABLES \
            and isinstance(args[1], ir.Load) and args[1].bits == 32:
        # the variables' base (R25) plus an offset read from a table symbols.txt says leads to a structure
        # (player_block_offsets: 0x400, 0x600, 0x800)
        target = target_of(args[1].address, state, 32)
        if target is not None and link_kind(target)[0] in STRUCTS:
            return link_kind(target)
    if name == "add" and isinstance(args[0], ir.Const):
        # a player's block, or a place in it: the first one's plus a multiple of the block size
        found = player_constant(args[0].value & ADDRESS_MASK)
        if found is not None and found[0] == "player" and args[0].value & ADDRESS_MASK < PLAYERS[0] + PLAYERS[1] \
                and low_zeros(args[1], zeros) >= PLAYERS[1].bit_length() - 1:
            return found
        return None
    if name == "or" and PAGE_LINKS:
        kept, low = args
        page = isinstance(kept, ir.Const) and (kept.value & 0xFFFF) == 0 and in_work_ram(kept.value)
        masked = isinstance(kept, ir.Op) and kept.name == "and" and isinstance(kept.args[1], ir.Const) \
            and kept.args[1].value == 0xFFFF0000
        if page or masked:
            # a link read from a field or variable that holds one: what it links, whatever the register
            # held before (the player's object read into the player register)
            link = low
            while isinstance(link, ir.Op) and link.name in ("zext", "fits"):
                link = link.args[0]
            if isinstance(link, ir.Load) and link.bits == 16:
                target = target_of(link.address, state, 16)
                if target is not None:
                    return link_kind(target)
        if masked and isinstance(kept.args[0], ir.Reg):
            inner = state.get(kept.args[0].index)
            if inner is not None and inner[0] in PAGE_LINK_KINDS:
                return PAGE_LINK_KINDS[inner[0]]
            if inner is not None and inner[0] in STRUCTS:
                return inner[0], 0
            if inner == ("rom", 0):
                return inner    # a pointer into the ROM moved within its page
    return None


def offset_sized(node, state=None):
    """True for a value that is surely below 0x10000: a constant, a byte or halfword (read, extended or
    cut), one masked to 16 bits, a register that holds one (OFFSET), or such a value times a small
    constant."""
    if isinstance(node, ir.Const):
        return node.value < 0x10000 or node.value >= 0xFFFF0000    # a small displacement either way
    if isinstance(node, ir.Reg):
        kind = state.get(node.index) if state is not None else None
        return kind == OFFSET or (kind is not None and kind[0] == "const")
    if isinstance(node, ir.Load):
        return node.bits <= 16
    if not isinstance(node, ir.Op):
        return False
    name, args = node.name, node.args
    if name in ("zext", "fits", "trunc"):
        return (args[0].bits if name == "zext" else node.bits) <= 16 or offset_sized(args[0], state)
    if name == "and":
        return any(isinstance(arg, ir.Const) and arg.value < 0x10000 for arg in args)
    if name in ("mul", "shl") and isinstance(args[1], ir.Const) and args[1].value <= (8 if name == "mul" else 3):
        return offset_sized(args[0], state)
    if name == "or":
        return all(offset_sized(arg, state) for arg in args)
    if name in ("shr", "sar") and isinstance(args[1], ir.Const):
        return args[1].value >= 16 or offset_sized(args[0], state)
    if name in ("add", "sub"):
        return all(offset_sized(arg, state) for arg in args)    # an offset into a record or a table, either way
    return node.bits <= 16


def rom_address(value):
    """An address in the program ROM that can be data (the vectors and the first code are below 0x1000)."""
    return ROM_DATA[0] <= value < ROM_DATA[1]


def constant_base(address):
    """The constant of "constant + rest", or of a constant address."""
    if isinstance(address, ir.Const):
        return address.value & ADDRESS_MASK
    if isinstance(address, ir.Op) and address.name == "add":
        for arg in address.args:
            if isinstance(arg, ir.Const):
                return arg.value & ADDRESS_MASK
    return None


def target_of(address, state, bits):
    """What a value of 'bits' read at 'address' points at (TARGETS, SCOPED_TARGETS: STRUCT, STRUCT.FAMILY
    or a region), or None."""
    hit = access(address, state)
    if hit is not None and hit[0] in REGIONS:
        hit = None    # a table in the ROM: what symbols.txt says its elements lead to, by its address
    if hit is not None and hit[0] != "global":
        key = (hit[0], hit[1])
        scoped = SCOPED_TARGETS.get((hit[0], hit[4], hit[1])) if hit[4] is not None else None
        if scoped is not None:
            return scoped[0] if scoped[1] == bits else None    # the family's own use of the field
    else:
        base = constant_base(hit[1] if hit is not None else address)
        key = ("global", base) if base is not None else None
        scoped = SCOPED_TARGETS.get(("global", GLOBAL_FAMILY, base)) if GLOBAL_FAMILY is not None else None
        if scoped is not None:
            return scoped[0] if scoped[1] == bits else None    # the family's own use of the variable
    found = TARGETS.get(key) if key is not None else None
    return found[0] if found is not None and found[1] == bits else None


def access(address, state, zeros=None):
    """(struct, offset of the field, base register, how far into the struct the base points, the family of
    the structure or None) for a memory access at 'address', or None. Through a pointer to a variable:
    ("global", the address with the variable's address in place of the register)."""
    globals_ = {index: kind[1] for index, kind in state.items() if kind[0] == "global"}
    if globals_:
        def change(node):
            if isinstance(node, ir.Reg) and node.index in globals_:
                return ir.Const(globals_[node.index])
            return None
        rebuilt = ir.rebuild(address, change)
        if rebuilt != address:
            folded = ir.rebuild(rebuilt, fold)
            return ("global", folded)
    # a record by its number, or an element of a field that is an array, before "into the ROM, no more": a
    # table of records plus a row and a column is both
    found = indexed_access(address, state, zeros=zeros)
    if found is not None:
        return found
    whole = kind_of(address, state, {})
    if whole is not None and whole[0] in REGIONS:
        return whole[0], 0, address, 0, None
    if PAGED:
        found = page_parts(address, state, {})
        if found is not None:
            return found[0], found[1], found[2], 0, None
    if isinstance(address, ir.Reg):
        kind = state.get(address.index)
        return (kind[0], kind[1], address, kind[1], family_of(kind)) if kind is not None and kind[0] in STRUCTS \
            else None
    if isinstance(address, ir.Const):
        found = instance_of(address.value & ADDRESS_MASK)
        return (found[1], found[2], ir.Const(found[0], 32), 0, None) if found is not None else None
    base, displacement = address, 0
    if isinstance(address, ir.Op) and address.name in ("add", "sub") and isinstance(address.args[1], ir.Const):
        base = address.args[0]
        displacement = address.args[1].value if address.name == "add" else -address.args[1].value
    while isinstance(base, ir.Op) and base.name in ("zext", "fits") and records_kind(kind_of(base.args[0], state, {})):
        base = base.args[0]    # (a pointer to a record in the ROM's first 64 KB, known to fit 16 bits: the pointer)
    if isinstance(base, ir.Reg):
        kind = state.get(base.index)
    elif isinstance(base, ir.Op) and (base.name == "or" or base.name == "add" and isinstance(base.args[0], ir.Const)):
        kind = kind_of(base, state, {})
    elif isinstance(base, ir.Load):
        kind = kind_of(base, state, {})    # a pointer read where it is used (22[4[R20]])
    else:
        kind = None
    if kind is None or kind[0] in ("global", "const", "to", "rel") or kind[0] in REGIONS or kind == OFFSET:
        return None
    offset = signed(kind[1] + displacement)
    if 0 <= offset < STRUCTS[kind[0]]:
        return kind[0], offset, base, kind[1], family_of(kind)
    found = below(kind[0], offset)
    if found is not None:
        # the base points that far past the start of the structure below (screen - 0x8000u: its objects' area)
        return found[0], found[1], base, found[1] - (offset - kind[1]), None
    return None


def array_access(kind, pointer, constant, rest, zeros=None):
    """(struct, field offset, pointer, how far into its record the pointer points, family, record number or None,
    element number) for an access to an element of a field that is an array (FIELD_ARRAYS): the pointer's
    record (or the one so many records on) plus the field's offset plus the element's; None otherwise. The
    terms 'rest' are a record's number times its size and an element's number times the element's."""
    size = STRUCTS[kind[0]]
    for (struct, start), (bits, count) in FIELD_ARRAYS.items():
        if struct != kind[0]:
            continue
        width = bits // 8
        records, element_terms = [], []
        for term in rest:
            # (an array past the record's end takes no record number: what follows is the array)
            by_record = scaled_count(term, size, zeros) if count is not None else None
            by_element = scaled_count(term, width, zeros)
            if by_record is not None and not records:
                records.append(by_record)
            elif by_element is not None and not element_terms:
                element_terms.append(by_element)
            else:
                break
        else:
            offset = kind[1] + constant
            record, inside = divmod(offset, size) if count is not None else (0, offset)
            within = inside - start
            if within < 0 or within % width or (count is not None and within >= count * width):
                continue
            element = ir.Const(within // width, 32)
            if element_terms:
                element = element_terms[0] if not within else ir.op("add", (element_terms[0], element), 32)
            if records:
                number = records[0] if not record else ir.op("add", (records[0], ir.Const(record & 0xFFFFFFFF, 32)), 32)
            else:
                number = ir.Const(record & 0xFFFFFFFF, 32) if record or isinstance(pointer, ir.Const) else None
            return kind[0], start, pointer, kind[1], family_of(kind), number, element
    return None


def indexed_access(address, state, in_fields=True, zeros=None):
    """(struct, field offset, pointer, how far into its record the pointer points, family, index) for an
    access to a record of the program ROM by its number: a pointer to one (or a table of them) plus the
    number times the record's size, plus a displacement; or a displacement past the record the pointer is in
    (the next record's field). The index is an IR expression (a constant for the records after or before).
    With 'in_fields', an element of a field that is an array too (array_access). None for any other access,
    and for a field of the record the pointer is in (access() names those)."""
    terms, constant = [], 0

    def gather(node, sign):
        nonlocal constant
        if isinstance(node, ir.Const):
            constant += sign * node.value
        elif isinstance(node, ir.Op) and node.name == "add" and node.bits == POINTER_BITS:
            gather(node.args[0], sign)
            gather(node.args[1], sign)
        elif isinstance(node, ir.Op) and node.name == "sub" and node.bits == POINTER_BITS \
                and isinstance(node.args[1], ir.Const):
            gather(node.args[0], sign)
            constant -= sign * node.args[1].value
        else:
            terms.append(node)
    gather(address, 1)
    width = 1 << POINTER_BITS
    constant %= width
    if constant >= width // 2:
        constant -= width    # (a displacement back: the pointer's arithmetic wraps at its width)
    pointer = kind = None
    if terms == [address]:
        return None    # (nothing to take apart: an address of another width)
    kinds = [state.get(term.index) if isinstance(term, ir.Reg) else kind_of(term, state, {}) for term in terms]
    relative = [index for index, found in enumerate(kinds) if found is not None and found[0] == "rel"]
    if len(relative) == 1:
        # a byte offset read from a table, from the table's own start, plus a pointer to the table: what it
        # leads to, STRUCT(table + offset)
        bases = [index for index, found in enumerate(kinds) if index not in relative and records_kind(found)]
        if bases:
            offset_term, base_term = terms[relative[0]], terms[bases[0]]
            pointer = ir.op("add", (base_term, offset_term), POINTER_BITS)
            kind = (kinds[relative[0]][1], 0)
            terms = [pointer] + [term for term in terms if term is not offset_term and term is not base_term]
    for term, found in zip(terms, kinds) if pointer is None else ():
        if found is not None and found[0] == "to" and len(terms) == 1:
            # a byte offset from a table to a record in it plus the table's address (and a field's place):
            # that record's field, STRUCT(table_ADDRESS + offset)->field; [-1] the record before
            size = STRUCTS[found[2]]
            element, within = divmod(constant - found[1], size)
            if abs(element) > 1:
                continue
            named = ir.Const(found[3], POINTER_BITS)
            base = named if not found[4] else ir.op("add", (named, ir.Const(found[4], POINTER_BITS)), POINTER_BITS)
            at = ir.op("add", (base, term), POINTER_BITS)
            if not element:
                return found[2], within, at, 0, None
            return found[2], within, at, 0, None, ir.Const(element & 0xFFFFFFFF, 32)
        if records_kind(found):
            pointer, kind = term, found
            break
    if pointer is None:
        if any(found is not None and (found[0] in STRUCTS or found[0] == "global") for found in kinds):
            return None    # (a field of a structure a register points at: the constant is its offset, no table)
        table = record_table(constant & ADDRESS_MASK)
        if table is None:
            return None
        pointer, kind, constant = ir.Const(constant - table[1] & ADDRESS_MASK, 32), (table[0], 0), table[1]
    rest = [term for term in terms if term is not pointer]
    if len(rest) > 1 and all(term == rest[0] for term in rest):
        rest = [ir.op("mul", (rest[0], ir.Const(len(rest), rest[0].bits)), rest[0].bits)]    # (i + i: twice i)
    if in_fields and len(rest) <= 2:
        found = array_access(kind, pointer, constant, rest, zeros)
        if found is not None:
            return found
    if len(rest) > 1:
        return None
    size = STRUCTS[kind[0]]
    count = scaled_count(rest[0], size, zeros) if rest else None
    if rest and count is None:
        return None
    element, offset = divmod(kind[1] + constant, size)
    if count is None and element == 0 and not isinstance(pointer, ir.Const):
        return None
    if count is None:
        index = ir.Const(element & 0xFFFFFFFF, 32)
    elif element:
        index = ir.op("add", (count, ir.Const(element & 0xFFFFFFFF, 32)), 32)
    else:
        index = count
    return kind[0], offset, pointer, kind[1], family_of(kind), index


def table_element(address, zeros):
    """("table_index", table address, register, the power of two it is a multiple of) for "table + register"
    with a register whose low bits are known to be zero (stage x 2 kept in a register): emit.py reads it as
    the table's element (register / size) when the table's elements fit; None otherwise. The table can be
    added with 16-bit arithmetic in its 64 KB page ("page | (uint16_t)(low + register)", add.h)."""
    if isinstance(address, ir.Op) and address.name == "or" and len(address.args) == 2:
        for page, low in (address.args, tuple(reversed(address.args))):
            while isinstance(low, ir.Op) and low.name in ("zext", "fits", "trunc"):
                low = low.args[0]
            if isinstance(page, ir.Const) and page.value & 0xFFFF == 0 and isinstance(low, ir.Op) \
                    and low.name == "add" and low.bits <= 16:
                for table, register in (low.args, tuple(reversed(low.args))):
                    if isinstance(table, ir.Const):
                        found = table_element(ir.op("add", (ir.Const(page.value | table.value, POINTER_BITS),
                                                            register), POINTER_BITS), zeros)
                        if found is not None:
                            return found
        return None
    if not (isinstance(address, ir.Op) and address.name == "add" and address.bits == POINTER_BITS):
        return None
    for table, register in (address.args, tuple(reversed(address.args))):
        inner = register
        while isinstance(inner, ir.Op) and inner.name in ("zext", "fits", "trunc"):
            inner = inner.args[0]
        if isinstance(table, ir.Const) and rom_address(table.value & ADDRESS_MASK) and isinstance(inner, ir.Reg) \
                and low_zeros(register, zeros) > 0:
            return "table_index", table.value & ADDRESS_MASK, register, 1 << low_zeros(register, zeros)
    return None


def fold(node):
    """Constant additions in an address."""
    if isinstance(node, ir.Op) and node.name == "add":
        left, right = node.args
        if isinstance(left, ir.Const) and isinstance(right, ir.Const):
            return ir.Const((left.value + right.value) & 0xFFFFFFFF, node.bits)
        if isinstance(left, ir.Op) and left.name == "add" and isinstance(right, ir.Const) \
                and isinstance(left.args[0], ir.Const):
            return ir.op("add", (ir.Const((left.args[0].value + right.value) & 0xFFFFFFFF), left.args[1]), node.bits)
    return None


class Program:
    """The kinds of every function's registers, across calls."""

    def __init__(self, functions, flow, signatures, call_results, context, targets=None, hints=None, site_hints=None,
                 scoped_targets=None, families=None, global_families=None):
        TARGETS.clear()
        TARGETS.update(targets or {})
        SCOPED_TARGETS.clear()
        SCOPED_TARGETS.update(scoped_targets or {})
        self.families = families or {}       # entry -> the family of the object the function runs on (by its name)
        self.global_families = global_families or {}    # entry -> the family of its own names of the work RAM
        self.hints = hints or {}             # entry -> {register: kind}: what symbols.txt says a parameter is
        self.site_hints = site_hints or {}   # (instruction address, register) -> kind: what symbols.txt says a
                                             # local variable written there is
        self.functions = functions
        self.flow = flow
        self.signatures = signatures
        self.call_results = call_results     # entry -> {id(call statement): registers it hands back}
        self.context = set(context)
        self.entry_kinds = {}                # entry -> kinds every caller agrees on
        self.exit_kinds = {}                 # entry -> kinds of what it hands back, at every return
        self.changed = set()

    # ---- across functions ----

    def run(self):
        pending = set(self.functions)
        for _ in range(ROUNDS):
            if not pending:
                break
            self.changed = set()
            for entry in sorted(pending):
                self.analyse(entry, record=None)
            pending = self.changed
        fields, accesses = {}, []
        for entry in self.functions:
            found = {}
            self.analyse(entry, record=(found, accesses))
            fields[entry] = found
        return fields, accesses

    def tell_callee(self, target, state):
        state = {index: outward(kind) for index, kind in state.items()}
        merged = gather(self.entry_kinds.get(target, {}), state)
        if merged != self.entry_kinds.get(target):
            self.entry_kinds[target] = merged
            self.changed.add(target)

    def tell_callers(self, entry, kinds):
        kinds = {index: outward(kind) for index, kind in kinds.items()}
        merged = gather(self.exit_kinds.get(entry, {}), kinds)
        if merged == self.exit_kinds.get(entry):
            return
        self.exit_kinds[entry] = merged
        # every function that calls it has to look again
        self.changed |= self.callers.get(entry, set())

    @property
    def callers(self):
        if not hasattr(self, "_callers"):
            found = {}
            for entry, function in self.functions.items():
                for block in function.blocks.values():
                    for statement in block.statements:
                        if isinstance(statement, ir.Call):
                            for target in self.flow.call_targets(statement):
                                found.setdefault(target, set()).add(entry)
                    if isinstance(block.end, ir.TailCall):
                        found.setdefault(block.end.target, set()).add(entry)
            self._callers = found
        return self._callers

    # ---- one function ----

    def told(self, target, state):
        """What a caller tells a callee about its registers: the parameters and the context registers, as it
        knows them. A parameter the callee only hands back as it found it (its data flow summary's base, and
        what it works out its other results from, do not read it) tells only of the structures the program
        keeps everywhere (CONTEXT_KINDS: objects, players): a record one caller leaves in R11 is not what
        allocate_object finds there when no object is free, called from anywhere else."""
        summary = self.flow.summaries.get(target)
        common = {kind[0] for kind in CONTEXT_KINDS.values()}
        found = {index: state.get(index, UNKNOWN) for index in self.context}
        for value in self.signatures[target].params:
            if value[0] != "r":
                continue
            kind = state.get(value[1], UNKNOWN)
            if summary is not None and kind[0] in STRUCTS and kind[0] not in common:
                reads = set(summary.base)
                for key, passed in summary.passes.items():
                    if key != value:
                        reads |= passed
                if not any(variable[0] == "r" and variable[1] == value[1] for variable in reads):
                    kind = UNKNOWN
            found[value[1]] = kind
        return found

    def handed_back(self, entry):
        """Registers a function changes for its caller: its results, and the context registers it writes."""
        results = {value[1] for value in self.signatures[entry].results if value[0] == "r"}
        written = {variable[1] for variable in self.flow.summaries[entry].writes if variable[0] == "r"}
        return results | (written & self.context)

    def keeps(self, entry, index):
        """True when the function gives its caller back register 'index' as it came in on every way out:
        it never writes it, or puts back what it saved (a context register it saves on the stack while
        what it calls changes it). Then the caller's kind for it holds after the call."""
        if not hasattr(self, "_keeps"):
            self._keeps = {}
        key = (entry, index)
        if key in self._keeps:
            return self._keeps[key]
        self._keeps[key] = False    # (a call back into itself: not known)
        function = self.functions.get(entry)
        if function is None:
            return False
        start = {function.entry: True}    # block -> the register holds what it came in with
        pending = [function.entry]
        kept = True
        while pending and kept:
            label = pending.pop()
            same = start[label]
            saved = {}
            block = function.blocks[label]
            for statement in block.statements:
                if isinstance(statement, ir.SetStack) and statement.value == ir.Reg(index):
                    saved[statement.offset] = same
                elif isinstance(statement, ir.SetReg) and statement.index == index:
                    value = statement.value
                    same = isinstance(value, ir.Stack) and saved.get(value.offset, False)
                elif isinstance(statement, (ir.Call, ir.Effect)):
                    written = {variable[1] for variable in self.flow.statement_writes(statement) if variable[0] == "r"}
                    if index in written:
                        targets = self.flow.call_targets(statement) if isinstance(statement, ir.Call) else []
                        same = same and bool(targets) and all(self.keeps(target, index) for target in targets)
            if isinstance(block.end, (ir.Return, ir.InterruptReturn)):
                kept = kept and same
            elif isinstance(block.end, ir.TailCall):
                kept = kept and same and self.keeps(block.end.target, index)
            for successor in block.successors():
                if successor not in start:
                    start[successor] = same
                    pending.append(successor)
                elif start[successor] and not same:
                    start[successor] = False
                    pending.append(successor)
        self._keeps[key] = kept
        return kept

    def after_call(self, targets, state, context_only=False):
        """The kinds after a call that may go to any of 'targets': a register a target writes has the kind
        that target leaves in it, one it does not write keeps the caller's, and so does an object register
        every target puts back as it came in (keeps). Through a slot only the context registers come back."""
        affected = set()
        for target in targets:
            affected |= self.handed_back(target)
        if context_only:
            affected &= self.context
        affected -= {index for index in affected & set(OBJECT_REGISTERS)
                     if index in state and targets and all(self.keeps(target, index) for target in targets)}
        for index in affected:
            said = {}
            for target in targets:
                if index in self.handed_back(target):
                    kind = self.exit_kinds.get(target, {}).get(index, UNKNOWN)
                else:
                    kind = state.get(index, UNKNOWN)
                said = gather(said, {index: kind})
            kind = known(said).get(index)
            if kind is None and index in OBJECT_REGISTERS:
                kind = ("object", 0)    # the object registers hold objects after a call, whatever the callee did
            if kind is None:
                state.pop(index, None)
            else:
                state[index] = kind

    def step(self, statement, state, zeros, record):
        if PAGED and isinstance(statement, ir.SetTemp):
            kind = kind_of(statement.value, state, zeros)
            if kind is None:
                state.pop(("temp", statement.temp.name), None)
            else:
                state[("temp", statement.temp.name)] = kind
            return
        if isinstance(statement, ir.SetReg):
            kind = kind_of(statement.value, state, zeros)
            if CONSTANTS and isinstance(statement.value, ir.Const) and kind is None:
                kind = ("const", statement.value.value & ADDRESS_MASK)
            if kind is None and offset_sized(statement.value, state):
                kind = OFFSET
            hinted = self.site_hints.get((getattr(statement, "origin", None), statement.index))
            if hinted is not None:
                kind = hinted
            if statement.index in OBJECT_REGISTERS and (kind is None or kind[0] == "global") \
                    and not (kind is None and isinstance(statement.value, ir.Const)) \
                    and not in_variable_block(kind) and not walks_variable_block(statement.value, state):
                # whatever the object registers are set to is used as an object, except an address outside
                # the work RAM (test_bookkeeping walks the bookkeeping block at 701F02 with R20) and the
                # blocks of variables they walk (VARIABLE_BLOCKS)
                kind = ("object", 0)
            zeros[statement.index] = low_zeros(statement.value, zeros)
            if kind is None:
                state.pop(statement.index, None)
            else:
                state[statement.index] = kind
        elif isinstance(statement, ir.Call):
            targets = self.flow.call_targets(statement)
            if record is None:
                for target in targets:
                    # a call through a slot passes nothing but the context registers
                    told = {index: state.get(index, UNKNOWN) for index in self.context} if statement.slot is not None \
                        else self.told(target, state)
                    self.tell_callee(target, told)
            self.after_call(targets, state, context_only=statement.slot is not None)
            # what symbols.txt says a call hands back points at ("local" lines name it by the call)
            origin = getattr(statement, "origin", None)
            if origin is not None and statement.slot is None:
                for target in targets:
                    for index in self.handed_back(target):
                        hinted = self.site_hints.get((origin, index))
                        if hinted is not None:
                            state[index] = hinted
            for index in list(zeros):
                if index not in state:
                    zeros.pop(index, None)
        elif isinstance(statement, ir.Effect):
            written = {variable[1] for variable in self.flow.statement_writes(statement) if variable[0] == "r"}
            for index in written:
                state.pop(index, None)
                zeros.pop(index, None)
        elif isinstance(statement, ir.Store) and isinstance(statement.value, ir.Const) and HANDLER_FIELD is not None:
            # an object whose handler is set to a function of a family is that family's: what start_object
            # hands back, once its handler is set, reads as the family's code reads it (not the function's own
            # object: a handler that hands over to another family's goes on with its own fields)
            family = self.families.get(statement.value.value & ADDRESS_MASK)
            hit = access(statement.address, state) if family is not None else None
            if hit is not None and hit[:2] == HANDLER_FIELD and hit[3] == 0 and isinstance(hit[2], ir.Reg) \
                    and hit[2].index not in OWN_REGISTERS:
                state[hit[2].index] = (hit[0], 0, family)

    def note(self, statement, state, record, zeros=None):
        found, accesses = record
        zeros = zeros if zeros is not None else {}
        if isinstance(statement, ir.Store):
            hit = access(statement.address, state, zeros)
            if hit is not None and hit[0] in REGIONS:
                hit = None    # nothing writes the ROM: whatever this is, it stays as it was
            if hit is not None:
                if hit[0] == "global":
                    hit = hit + (statement.bits,)
                else:
                    accesses.append((hit[0], hit[1], statement.bits))
                found[id(statement)] = hit
        for expression in ir.statement_expressions(statement):
            for node in ir.walk(expression):
                if isinstance(node, ir.Load):
                    hit = access(node.address, state, zeros)
                    element = table_element(node.address, zeros) if hit is None or hit[0] in REGIONS else None
                    if element is not None or hit is not None and hit[0] in REGIONS:
                        found[id(node)] = element or hit
                        continue
                    if hit is not None:
                        if hit[0] == "global":
                            hit = hit + (node.bits,)
                        else:
                            accesses.append((hit[0], hit[1], node.bits))
                        found[id(node)] = hit

    def analyse(self, entry, record):
        global GLOBAL_FAMILY
        GLOBAL_FAMILY = self.global_families.get(entry)
        function = self.functions[entry]
        blocks = function.blocks
        told = self.entry_kinds.get(entry, {})
        # the object registers stay objects when callers disagree (one passes a block a sprite lives in)
        state = {index: kind for index, kind in CONTEXT_KINDS.items()
                 if told.get(index) != CONFLICT or index in OBJECT_REGISTERS}
        state.update(known(told))
        state.update(self.hints.get(entry, {}))
        own = self.families.get(entry)
        if own is not None:
            # the object the function runs on is its family's, whatever its callers say
            for index in OWN_REGISTERS:
                kind = state.get(index)
                if kind is not None and kind[:2] == ("object", 0):
                    state[index] = ("object", 0, own)
        start = {entry: state}
        pending = [entry]
        while pending:
            label = pending.pop()
            state = dict(start[label])
            zeros = {}
            for statement in blocks[label].statements:
                self.step(statement, state, zeros, None)
            for successor in blocks[label].successors():
                if successor not in start:
                    start[successor] = dict(state)
                    pending.append(successor)
                    continue
                merged = merge(start[successor], state)
                if merged != start[successor]:
                    start[successor] = merged
                    pending.append(successor)
        exits = None
        for label, state in start.items():
            state = dict(state)
            zeros = {}
            block = blocks[label]
            for statement in block.statements:
                if record is not None:
                    self.note(statement, state, record, zeros)
                self.step(statement, state, zeros, record)
            end = block.end
            if record is not None:
                self.note(end, state, record, zeros)
            if isinstance(end, ir.TailCall):
                if record is None:
                    self.tell_callee(end.target, self.told(end.target, state))
                self.after_call([end.target], state)
            if isinstance(end, (ir.Return, ir.TailCall, ir.InterruptReturn)):
                kinds = {index: state.get(index, UNKNOWN) for index in self.handed_back(entry)}
                exits = kinds if exits is None else gather(exits, kinds)
        if record is None and exits is not None:
            self.tell_callers(entry, exits)


class Layouts:
    """The fields of each structure: those symbols.txt names (with their types), then one for every other
    offset the code reads or writes, with the width it uses most (unk_XX). An access with another width,
    or inside a wider field, says so in the C. symbols.txt may name a part of a wider field as a field of
    its own (the whole part of a 16.16 position): the struct holds both in a union."""

    TYPES = {"u8": (8, False), "s8": (8, True), "u16": (16, False), "s16": (16, True),
             "u32": (32, False), "s32": (32, True)}

    def __init__(self, accesses, names=None):
        """'accesses': iterable of (struct, offset, bits); 'names': disassemble.Names."""
        declared = {}
        if names is not None:
            for (struct, offset), (name, kind) in names.fields.items():
                declared.setdefault(struct, {})[offset] = (name, *self.TYPES[kind])
        counts = {}
        for struct, offset, bits in accesses:
            counts.setdefault(struct, {}).setdefault(offset, {}).setdefault(bits, 0)
            counts[struct][offset][bits] += 1
        self.fields = {}
        self.names = {}
        self.signed = {}
        self.scoped = {}    # struct -> family -> {offset: (name, bits, signed)}
        self.views = {}     # struct -> family -> the member that holds the family's view
        self.overlays = {}  # struct -> {(offset, bits): (name, signed)}: a narrower name for a field's first bytes
        self.scoped_overlays = {}    # struct -> family -> {(offset, bits): (name, signed)}: the same in a family's view
        if names is not None:
            for (struct, offset, bits), (name, kind) in names.overlays.items():
                if struct != "global":
                    self.overlays.setdefault(struct, {})[(offset, bits)] = (name, self.TYPES[kind][1])
            for (struct, family), found in names.scoped_overlays.items():
                self.scoped_overlays.setdefault(struct, {})[family] = {
                    key: (name, self.TYPES[kind][1]) for key, (name, kind) in found.items()}
        if names is not None:
            for (struct, family), found in names.scoped.items():
                self.scoped.setdefault(struct, {})[family] = {offset: (name, *self.TYPES[kind])
                                                              for offset, (name, kind) in found.items()}
        for struct in set(counts) | set(declared):
            fields, names_, signed = {}, {}, {}
            for offset, (name, bits, is_signed) in declared.get(struct, {}).items():
                fields[offset] = bits
                names_[offset] = name
                signed[offset] = is_signed
            covered = {offset + byte for offset, bits in fields.items()
                       for byte in range(bits // 8 * (self.array_count(struct, offset) or 1))}
            past = [offset for (array_struct, offset), (_, count) in FIELD_ARRAYS.items()
                    if array_struct == struct and count is None]
            offsets = counts.get(struct, {})
            for offset in sorted(offsets):
                if offset in covered or any(offset >= start for start in past):
                    continue    # a named field, or inside a wider field before it, or an array's
                widths = offsets[offset]
                bits = max(widths, key=lambda width: (widths[width], width))
                while bits > 8 and any(offset + byte in covered for byte in range(bits // 8)):
                    bits //= 2
                if offset + bits // 8 > STRUCTS[struct]:
                    bits = 8
                fields[offset] = bits
                names_[offset] = f"unk_{offset:02X}"
                signed[offset] = False
                covered |= {offset + byte for byte in range(bits // 8)}
            self.fields[struct] = fields
            self.names[struct] = names_
            self.signed[struct] = signed
        # names the preprocessor would take (the variables, tables and registers are macros)
        macros = set()
        if names is not None:
            for found in (names.variables, names.tables, names.hardware, names.overlays):
                macros |= {name for name, _ in found.values()}
        for struct, families in self.scoped.items():
            taken = set(self.names.get(struct, {}).values()) | macros
            self.views[struct] = {family: family if family not in taken else f"{family}_view" for family in families}

    @staticmethod
    def array_count(struct, offset):
        """How many elements the field at 'offset' holds when it is an array (0 for one past the record's
        end), else None."""
        found = FIELD_ARRAYS.get((struct, offset))
        return None if found is None else (found[1] or 0)

    def scoped_field(self, struct, family, offset):
        """(view name, field name, field offset, field bits, signed) of the family's field holding 'offset',
        or None."""
        fields = self.scoped.get(struct, {}).get(family, {})
        for start in range(offset, offset - 4, -1):
            found = fields.get(start)
            if found is not None and start + found[1] // 8 > offset:
                return self.views[struct][family], found[0], start, found[1], found[2]
        return None

    def overlay(self, struct, offset, bits):
        """(name, signed) of the overlay of 'bits' at 'offset', or None."""
        return self.overlays.get(struct, {}).get((offset, bits))

    def scoped_overlay(self, struct, family, offset, bits):
        """(name, signed) of the family's overlay of 'bits' at 'offset', or None."""
        return self.scoped_overlays.get(struct, {}).get(family, {}).get((offset, bits))

    def families(self, struct):
        return sorted(self.scoped.get(struct, {}))

    def region(self, struct):
        """Where the families' views start in the structure (at the start of the field the first of them
        falls in), or None."""
        offsets = [offset for fields in self.scoped.get(struct, {}).values() for offset in fields]
        if not offsets:
            return None
        start = min(offsets)
        for offset, bits, _ in self.members(struct):
            if offset < start < offset + bits // 8:
                return offset
        return start

    def lookup(self, struct, offset):
        """(field offset, field bits) of the narrowest field holding 'offset', or None."""
        fields = self.fields.get(struct, {})
        for start in range(offset, offset - 4, -1):
            bits = fields.get(start)
            if bits is not None and start + bits // 8 > offset:
                return start, bits
        return None

    def members(self, struct, fields=None):
        """The fields ({offset: bits}, the structure's by default) as C members: (offset, bits, [parts]) for
        each outermost field, the parts being the fields inside it, as members of their own (a part may
        have parts). A field that starts inside another and runs past its end has no place in C."""
        fields = self.fields.get(struct, {}) if fields is None else fields
        outer = []
        holding = []    # the fields the next one may lie inside, the outermost first
        for offset in sorted(fields, key=lambda start: (start, -fields[start])):
            bits = fields[offset]
            member = (offset, bits, [])
            while holding and offset >= holding[-1][0] + holding[-1][1] // 8:
                holding.pop()
            if holding:
                if offset + bits // 8 > holding[-1][0] + holding[-1][1] // 8:
                    raise ValueError(f"{struct}: the field at {offset:X} runs past the end of the one at "
                                     f"{holding[-1][0]:X}")
                holding[-1][2].append(member)
            else:
                outer.append(member)
            holding.append(member)
        return outer

    def member_lines(self, struct, members, start, end, pad, names=None, signed=None, overlays=None):
        """C members for the fields 'members' (see members()), from offset 'start' to 'end'; 'names',
        'signed' ({offset: ...}) and 'overlays' ({(offset, bits): (name, signed)}) are the structure's by
        default."""
        generic = names is None
        names = self.names[struct] if names is None else names
        signed = self.signed[struct] if signed is None else signed
        if overlays is None:
            overlays = self.overlays.get(struct, {}) if generic else {}

        def kind(offset, bits):
            return f"int{bits}_t" if signed[offset] else f"uint{bits}_t"

        def lay(members, start, end, pad):
            lines = []
            position = start
            for offset, bits, parts in members:
                if offset > position:
                    lines.append(f"{pad}uint8_t pad_{position:02X}[0x{offset - position:X}];")
                own = [(found_bits, found) for (found_offset, found_bits), found in sorted(overlays.items())
                       if found_offset == offset and found_bits < bits]
                count = self.array_count(struct, offset) if generic else None
                if count is not None:
                    # an array: of so many, or (count 0) the values after the record
                    lines.append(f"{pad}{kind(offset, bits)} {names[offset]}[{f'0x{count:X}' if count else ''}];")
                    position = offset + bits // 8 * count
                    continue
                if not parts and not own:
                    lines.append(f"{pad}{kind(offset, bits)} {names[offset]};")
                else:
                    # a field with parts or overlays of its own: views of the same bytes
                    lines += [f"{pad}union", f"{pad}{{", f"{pad}\t{kind(offset, bits)} {names[offset]};"]
                    for overlay_bits, (overlay_name, overlay_signed) in own:
                        overlay_kind = f"int{overlay_bits}_t" if overlay_signed else f"uint{overlay_bits}_t"
                        lines.append(f"{pad}\t{overlay_kind} {overlay_name};")
                    if parts:
                        lines += [f"{pad}\tstruct", f"{pad}\t{{"]
                        lines += lay(parts, offset, offset, pad + "\t\t")
                        lines.append(f"{pad}\t}};")
                    lines.append(f"{pad}}};")
                position = offset + bits // 8
            if position < end:
                lines.append(f"{pad}uint8_t pad_{position:02X}[0x{end - position:X}];")
            return lines
        return lay(members, start, end, pad)

    def pointer_type(self, struct):
        """The cast and function that give a pointer to one of 'struct' from its address: "(const struct
        floor_block *)rom", "(const uint16_t *)rom" for an array of plain values, "(struct object *)ram"."""
        if struct in ELEMENTS:
            bits, signed = self.TYPES[ELEMENTS[struct]]
            return f"(const {'' if signed else 'u'}int{bits}_t *){ROM_FUNCTION}"
        if struct in ROM_STRUCTS:
            return f"(const struct {struct} *){ROM_FUNCTION}"
        return f"(struct {struct} *){STRUCT_FUNCTION}"

    def member_type(self, struct, offset, bits):
        return f"int{bits}_t" if self.signed[struct][offset] else f"uint{bits}_t"

    def declarations(self):
        lines = ["#pragma pack(push, 1)"]
        for struct, size in STRUCTS.items():
            if struct in ELEMENTS:
                continue    # (an array of plain values: no structure)
            fields = self.fields.get(struct, {})
            if struct not in self.names:
                self.names[struct], self.signed[struct] = {}, {}
            lines.append(f"struct {struct}")
            lines.append("{")
            members = self.members(struct)
            region = self.region(struct)
            if region is None:
                lines += self.member_lines(struct, members, 0, size, "\t")
            else:
                if any(offset < region < offset + bits // 8 for offset, bits, _ in members):
                    raise ValueError(f"{struct}: a field runs into the families' views at {region:X}")
                lines += self.member_lines(struct, [m for m in members if m[0] < region], 0, region, "\t")
                # the object types' own variables: what every object keeps there, and each family's view
                lines += ["\tunion", "\t{", "\t\tstruct", "\t\t{"]
                lines += self.member_lines(struct, [m for m in members if m[0] >= region], region, size, "\t\t\t")
                lines.append("\t\t};")
                for family in self.families(struct):
                    fields = self.scoped[struct][family]
                    widths = {offset: bits for offset, (_, bits, _) in fields.items()}
                    view = self.members(struct, widths)
                    if max(o + b // 8 for o, b in widths.items()) > size:
                        raise ValueError(f"{struct} {family}: fields run past the end")
                    lines += ["\t\tstruct", "\t\t{"]
                    lines += self.member_lines(struct, view, region, region, "\t\t\t",
                                               {offset: found[0] for offset, found in fields.items()},
                                               {offset: found[2] for offset, found in fields.items()},
                                               self.scoped_overlays.get(struct, {}).get(family, {}))
                    lines.append(f"\t\t}} {self.views[struct][family]};")
                lines.append("\t};")
            lines.append("};")
            lines.append("")
        lines.append("#pragma pack(pop)")
        lines.append("")
        for struct, size in STRUCTS.items():
            if struct in ELEMENTS:
                continue
            what = "the program ROM's records" if struct in ROM_STRUCTS else "the game's blocks"
            lines.append(f"_Static_assert(sizeof(struct {struct}) == 0x{size:X}, "
                         f"\"struct {struct} has the size of {what}\");")
        lines.append("")
        for struct in STRUCTS:
            lines.append(f"#define {struct.upper()}(address) ({self.pointer_type(struct)}(address))")
        if RECORD_TABLES:
            lines += ["", "/* the program ROM's tables of records, by name: name[i].field */"]
            digits = 4 if ADDRESS_MASK <= 0xFFFF else 6
            for address, (name, struct) in sorted(RECORD_TABLES.items()):
                lines.append(f"#define {name}_ADDRESS 0x{address:0{digits}X}u")
                lines.append(f"#define {name} ({self.pointer_type(struct)}({name}_ADDRESS))")
        for struct, (first, count) in ARRAYS.items():
            lines.append(f"#define {struct.upper()}_ADDRESS(n) (0x{first:X}u + (n) * 0x{STRUCTS[struct]:X}u)    "
                         f"/* the {count} of them, from 0 */")
        return lines
