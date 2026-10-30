"""
Name:        memory.py
Description: the game's variables, tables and hardware registers by name: work RAM variables as C
             lvalues laid over the original RAM, program ROM tables as arrays, the board's registers as
             named addresses
Version:     0.1.0
Created:     12-10-2026
Modified:    30-10-2026
Author:      Sebastian Toyos

Work RAM (0x200000-0x20FFFF) keeps the layout the board gives it, so the state compares byte for byte with
the reference. A variable is a macro for a typed lvalue at its address (game/support.h: RAM8, RAM16, RAM32):
"frames_to_wait = 1;", "unk_20E5A2 &= 0x7Fu;". One the code reads with an index ("[R0]") is an array:
"unk_20E590[r0]". With STATE (the game's profile), the work RAM is the game's state instead, one structure
the C reaches through a pointer: each variable a field of it at its place ("game->frames_to_wait = 1;"),
the blocks STATE_BLOCKS names between them (state_declaration); a place the code works out by its address
is still the byte there ("RAM8(settings_magic_ADDRESS + offset)"). The program ROM's data is read the same
way through rom(): "rom_10B6CC[r1]". The board's registers keep going through the bus (reading or writing
them does things), by name: "mem_write8(IO_PORT_D, r2)".

Names come from symbols.txt (var, io and table lines, disassemble.load_names). Whatever has none yet
gets one made of its address, with the width the code uses most: unk_ for work RAM, rom_ for tables,
and the board area for registers (VIDEO_, IO_...). An access with another width than the variable's,
or into the middle of it, says so (AS16(name), AT8(name, 1)). An index the analysis cannot show to be
small stays an address ("RAM16(0x20E562u + r3)"), so it wraps the way the V60's did.

Writes that do something besides storing (the protection device at 0x20E5C4, see machine.c) keep going
through the bus.

The program ROM shows up several times in the address space (game_profile.rom.PROGRAM_AREAS): code that runs in
a mirror reads its tables through that mirror. A table is named, and read, at its own copy.

The map above is the V60's. Another program sets its own in the module's names below (MAP and the rest;
tools/decompile_sound.py: the Z80's shared RAM, ROM, sample bank and RF5C68).
"""
import re
from collections import Counter

from v60 import ir

from . import cprint
from . import pointers

WORK_RAM = (0, 0)
ROM_AREAS = ((0x000000, 0x200000), (0xF00000, 0x1000000))    # the program ROM, the first area holding it whole
ADDRESS_MASK = 0xFFFFFF
ADDRESS_DIGITS = 6
RAM_MACRO, ROM_MACRO = "RAM", "ROM"          # the lvalue and rvalue macros (game/support.h) ...
RAM_FUNCTION, ROM_FUNCTION = "ram", "rom"    # ... and what gives a pointer into either
BOARD_MACRO, BOARD_FUNCTION = "BOARD", "board"    # ... and into the board's plain memories (PLAIN_HARDWARE)
# the areas of HARDWARE the code uses as plain memory (the video RAM, the sprite RAM, the RAM the sound
# program shares): what symbols.txt names there is a variable of the board, an lvalue through board();
# a name anywhere else is a register of a device, which the bus hands every read and write (io_read8...)
PLAIN_HARDWARE = ()
PAGED = True                                 # tables read with 16-bit arithmetic inside their 64 KB page
PLAYER_FIELDS = ()                           # reached by the player's number (pointers.py), not as tables
CANONICAL = lambda address: address      # the copy of the ROM tables are named by
NAMED_COPIES = True    # a constant in another copy of the ROM is named by its own copy's place (game_profile.py:
                       # False where such a constant may be no address)
ROM_NAMES_FROM = 0x1000                      # constants below are numbers, never ROM addresses (the vectors)
# the program's tables as data the game loads from its assets: each one a pointer of its own (NAME_data)
# that the runtime sets from the game's table registry (table.c: game_tables), read by name (NAME[i]),
# never through ROM_FUNCTION and the table's address
LOADED_TABLES = False
NOTES = {
    "variables": ["/* the game's variables in work RAM; SCREEN_1 and SCREEN_2 are the blocks of the two screens,",
                  "   which the code reaches through screen (R25 of the task that runs each) */"],
    "scoped": ["/* work RAM as one family of functions uses it (their scratch variables) */"],
    "tables": ["/* data in the program ROM the code reads */"],
    "hardware": ["/* the board's registers (read and written through the bus) */"],
}
TYPES = {8: "uint8_t", 16: "uint16_t", 32: "uint32_t"}
SIGNED = {"u8": (8, False), "s8": (8, True), "u16": (16, False), "s16": (16, True),
          "u32": (32, False), "s32": (32, True)}
BUS_ONLY = []    # writes a device on the bus watches in work RAM (SegaSonic: its protection)
VARIABLES = None    # where the game's variables start, as a constant base (R24: GAME_VARIABLES)
# work RAM addresses with a name of their own: the blocks of the two screens (R25 of each task) and the race
# records (each screen's, and the copy of them each screen keeps)
NAMED_BASES = {}
# The game's state as one structure the C reaches through a pointer of this name (game->frame_count): the
# work RAM, its variables at their places and its blocks (STATE_BLOCKS) between them, laid out as the board
# has it so that each frame's state compares byte for byte with the original's. None: each variable a macro
# over its address (RAM16(NAME_ADDRESS))
STATE = None
# (address, member, structure or None, count of structures or bytes) of the blocks of STATE's structure
STATE_BLOCKS = ()
HARDWARE = [(0x300000, 0x320000, "VIDEO"), (0x400000, 0x420000, "SPRITE_RAM"), (0x500000, 0x500010, "SPRITE_CONTROL"),
            (0x600000, 0x610000, "PALETTE"), (0x610000, 0x620000, "MIXER"), (0x700000, 0x702000, "SOUND"),
            (0xC00000, 0xC00080, "IO"), (0xD00000, 0xD00010, "IRQ"), (0xD80000, 0xD80010, "RANDOM")]


def work_ram(address):
    return WORK_RAM[0] <= address < WORK_RAM[1]


def program_rom(address):
    return any(start <= address < end for start, end in ROM_AREAS)


def own_copy(address):
    """A ROM address at the copy the program's tables are named by; any other address as it is."""
    if program_rom(address):
        found = CANONICAL(address)
        return found if found is not None else address
    return address


def hardware_area(address):
    for start, end, name in HARDWARE:
        if start <= address < end:
            return name
    return None


def plain_hardware(address):
    """True for an address in a memory of the board the code uses as plain memory (PLAIN_HARDWARE)."""
    return hardware_area(address) in PLAIN_HARDWARE


def bus_only(address, bits):
    end = address + bits // 8
    return any(address < stop and start < end for start, stop in BUS_ONLY)


def constant_address(expression):
    """The address an expression always gives (24 bits), or None."""
    if isinstance(expression, ir.Const):
        return expression.value & ADDRESS_MASK
    return None


def based(expression):
    """(constant base, rest) for "base + rest", or None. A table of the program ROM can be in the middle of
    the sum: "a + table + b" is the table and a + b ((uint16_t)(stage x 0xC) + player_palette_lists + kind x 4)."""
    if isinstance(expression, ir.Op) and expression.name == "add":
        left, right = expression.args
        if isinstance(left, ir.Const) and not isinstance(right, ir.Const):
            return left.value & ADDRESS_MASK, right
        if isinstance(right, ir.Const) and not isinstance(left, ir.Const):
            return right.value & ADDRESS_MASK, left
        for inner, other in ((left, right), (right, left)):
            parts = based(inner) if isinstance(inner, ir.Op) and inner.bits == expression.bits else None
            if parts is not None and program_rom(parts[0]) and not isinstance(other, ir.Const):
                return parts[0], ir.op("add", (parts[1], other), expression.bits)
    return None


def paged(expression):
    """(base, rest) for "page | (uint16_t)(low + rest)": a table read with 16-bit arithmetic in the 64 KB page
    it is in (the code adds the index to the low half of a register that holds the page), or None."""
    if not PAGED or not (isinstance(expression, ir.Op) and expression.name == "or"):
        return None
    for page, low in (expression.args, reversed(expression.args)):
        if not isinstance(page, ir.Const) or page.value & 0xFFFF:
            continue
        narrow = False
        while isinstance(low, ir.Op) and low.name in ("zext", "fits", "trunc"):
            narrow = narrow or (low.args[0].bits if low.name == "zext" else low.bits) <= 16
            low = low.args[0]
        if isinstance(low, ir.Op) and low.name == "add" and (narrow or low.bits <= 16):
            parts = based(low)
            if parts is not None:
                return (page.value | parts[0]) & 0xFFFFFF, parts[1]
    return None


def table_index(rest, bits):
    """True if "base + rest" reads an element of a table or array at 'base': the index is small, or scaled by
    the size of the elements."""
    index = element_index(rest, bits)
    if index is not None and bits > 8 and index is not rest:
        return True
    return small(index if index is not None else rest)


def element_index(rest, bits):
    """The element number when 'rest' is a byte offset into an array of 'bits'-wide elements, or None: a
    multiple of the size, or a sum of them (the sum of their quotients: (uint16_t)(stage x 0xC) / 4 + kind
    in a table of longs, the cut keeping it a multiple of 4)."""
    size = bits // 8
    if size == 1:
        return rest
    if isinstance(rest, ir.Op) and rest.name == "add":
        parts = [element_index(arg, bits) for arg in rest.args]
        if all(part is not None for part in parts):
            return ir.op("add", tuple(parts), rest.bits)
        return None
    found = pointers.narrow_count(rest, size) or whole_elements(rest, bits)
    if found is not None:
        return found
    if not isinstance(rest, ir.Const) and size & (size - 1) == 0 \
            and pointers.low_zeros(rest, {}) >= size.bit_length() - 1 and small(rest):
        # a small value known to be a multiple of the size (a cut product: (uint16_t)(x * 0xC) of longs)
        return ir.op("udiv", (rest, ir.Const(size, rest.bits)), rest.bits)
    return None


def whole_elements(rest, bits):
    """The element number of a byte offset worked out as so many elements times their size, or None."""
    size = bits // 8
    if isinstance(rest, ir.Op) and rest.name == "mul":
        for value, other in ((rest.args[1], rest.args[0]), (rest.args[0], rest.args[1])):
            if isinstance(value, ir.Const) and value.value == size:
                return other
            if isinstance(value, ir.Const) and value.value > size and value.value % size == 0:
                # (so many elements of a row: index * 8 bytes in a table of longs is element index * 2)
                return ir.op("mul", (other, ir.Const(value.value // size, other.bits)), rest.bits)
    if isinstance(rest, ir.Op) and rest.name == "shl" and isinstance(rest.args[1], ir.Const) \
            and 1 << rest.args[1].value == size:
        return rest.args[0]
    # (a chain of constant factors: x * 2 * 2 is element x of a table of longs)
    factor, base = 1, rest
    while isinstance(base, ir.Op) and base.bits == rest.bits and base.name == "mul" \
            and isinstance(base.args[1], ir.Const):
        factor, base = factor * base.args[1].value, base.args[0]
    return base if factor == size and base is not rest else None


def signed_number(index):
    """True for an index that is a signed byte or halfword, widened: a number, not an address."""
    return isinstance(index, ir.Op) and index.name == "sext" and index.args[0].bits <= 16


def small(index):
    """True for an index that cannot be negative or huge: a zero-extended byte or halfword."""
    if isinstance(index, ir.Const):
        return index.value < 0x10000
    if isinstance(index, ir.Op):
        if index.name in ("zext", "fits") and index.args[0].bits <= 16:
            return True
        if index.name == "fits" and index.bits <= 16:
            return True
        if index.name == "and" and any(isinstance(arg, ir.Const) and arg.value < 0x10000 for arg in index.args):
            return True
    if isinstance(index, ir.Load) and index.bits <= 16:
        return True
    if isinstance(index, ir.Op) and index.name in ("add", "mul", "udiv") and all(small(arg) for arg in index.args):
        return True    # (two such values added, or one scaled by a constant: still no address)
    if isinstance(index, ir.Op) and index.name == "trunc" and index.bits <= 16:
        return True
    return False


class Item:
    def __init__(self, address, name, bits, signed=False):
        self.address = address
        self.name = name
        self.bits = bits
        self.signed = signed
        self.indexed = False
        self.count = None     # an array's number of elements (symbols.txt: var NAME[COUNT])
        self.unnamed = False  # a name the decompiler made up for an address symbols.txt names nothing at
        self.state = False    # a variable of the game's state, a field of STATE's structure

    @property
    def c(self):
        """The item as the C names it: a variable of the game's state as its field (game->frame_count),
        anything else by its name."""
        return f"{STATE}->{self.name}" if self.state else self.name


class Memory:
    def __init__(self, names, accesses, data_names=None, own=None):
        """'names': disassemble.Names; 'accesses': (address expression, bits) of every read and write;
        'data_names': {address: name} of the data labels symbols.txt has (tables of code addresses), which
        name the tables that start there; 'own': {family: accesses} of the reads and writes a family of
        functions makes of work RAM it names its own way (scoped global): an indexed one makes the family's
        variable an array, and leaves alone the variable the rest of the program knows there."""
        self.variables = {}
        self.tables = {}
        self.hardware = {}
        # where a list walk starts: a structure there would have the list's head as one of its fields
        self.anchors = dict(getattr(names, "anchors", {}))
        self.overlays = {}    # (address, bits) -> Item: a narrower name for a variable's first bytes
        self.scoped = {}      # family -> {address: Item}: work RAM a family of functions uses its own way
        self.scoped_overlays = {}    # family -> {(address, bits): Item}: a narrower name for its first bytes
        for (struct, family), found in names.scoped.items():
            if struct == "global":
                self.scoped[family] = {address: Item(address, name, *SIGNED[kind])
                                       for address, (name, kind) in found.items()}
        for (struct, family), found in getattr(names, "scoped_overlays", {}).items():
            if struct == "global":
                self.scoped_overlays[family] = {(address, bits): Item(address, name, *SIGNED[kind])
                                                for (address, bits), (name, kind) in found.items()}
        for (struct, address, bits), (name, kind) in names.overlays.items():
            if struct == "global":
                self.overlays[(address, bits)] = Item(address, name, *SIGNED[kind])
        for address, (name, kind) in names.variables.items():
            bits, signed = SIGNED[kind]
            self.variables[address] = Item(address, name, bits, signed)
            count = getattr(names, "counts", {}).get(address)
            if count is not None:
                self.variables[address].count = count
                self.variables[address].indexed = True
        for address, (name, kind) in names.tables.items():
            bits, signed = SIGNED[kind]
            self.tables[address] = Item(address, name, bits, signed)
            count = getattr(names, "counts", {}).get(address)
            if count is not None:
                self.tables[address].count = count
                self.tables[address].indexed = True
        for address, (name, kind) in names.hardware.items():
            self.hardware[address] = Item(address, name, SIGNED[kind][0])

        widths = {"variables": {}, "tables": {}, "hardware": {}}
        indexed = set()
        board_indexed = set()
        # the tables of records (pointers.py reads them by name and field): no plain table of their own
        records = [(address, address + names.structs[struct])
                   for address, (_, struct) in getattr(names, "record_tables", {}).items()]
        for address_expression, bits in accesses:
            if bits not in TYPES:
                continue
            value = constant_address(address_expression)
            parts = (based(address_expression) or paged(address_expression)) if value is None else None
            if parts is not None:
                if not table_index(parts[1], bits):
                    continue    # maybe a pointer and a displacement (access() keeps it on the bus)
                if PLAYER_FIELDS and PLAYER_FIELDS[0] <= parts[0] < PLAYER_FIELDS[1]:
                    continue    # a field of the player blocks, reached by the player's number (pointers.py)
                value = parts[0]
            if value is None:
                continue
            value = own_copy(value)
            if value not in self.tables and any(start <= value < end for start, end in records):
                continue    # (a column of a table of records gets no table without a name of its own)
            where = "variables" if work_ram(value) else "tables" if program_rom(value) else \
                "hardware" if hardware_area(value) else None
            if where == "hardware" and parts is not None and plain_hardware(value):
                board_indexed.add(value)    # (a variable of the board's plain memories, by a number)
                continue
            if where is None or (where == "hardware" and parts is not None):
                continue
            widths[where].setdefault(value, Counter())[bits] += 1
            if parts is not None:
                indexed.add(value)
        self.add_unnamed(self.variables, widths["variables"], lambda address: f"unk_{address:0{ADDRESS_DIGITS}X}")
        data_names = data_names or {}
        self.add_unnamed(self.tables, widths["tables"],
                         lambda address: data_names.get(address, f"rom_{address:0{ADDRESS_DIGITS}X}"))
        self.add_unnamed(self.hardware, widths["hardware"],
                         lambda address: f"{hardware_area(address)}_{address:0{ADDRESS_DIGITS}X}")
        # the board's plain memories: variables of the board, not registers
        self.board = {address: self.hardware.pop(address) for address in list(self.hardware)
                      if plain_hardware(address)}
        for address in board_indexed & set(self.board):
            self.board[address].indexed = True
        for address in indexed:
            for items in (self.variables, self.tables):
                if address in items:
                    items[address].indexed = True
        for family, found in (own or {}).items():
            items = self.scoped.get(family, {})
            for address_expression, bits in found:
                parts = based(address_expression)
                if parts is not None and parts[0] in items and table_index(parts[1], bits):
                    items[parts[0]].indexed = True
        if STATE is not None:
            for item in self.state_items():
                item.state = True

    def state_items(self):
        """The work RAM's variables under every name: the program's, the narrower ones over their first
        bytes, and the ones a family of functions gives the places it uses its own way."""
        found = list(self.variables.values()) + list(self.overlays.values())
        for items in list(self.scoped.values()) + list(self.scoped_overlays.values()):
            found += list(items.values())
        return [item for item in found if work_ram(item.address)]

    def add_unnamed(self, items, widths, name_of):
        for address, counts in sorted(widths.items()):
            if address in items or self.inside(items, address) is not None or self.element(items, address):
                continue
            bits = max(counts, key=lambda width: (counts[width], -width))
            items[address] = Item(address, name_of(address), bits)
            items[address].unnamed = True

    def drop_unread(self, texts):
        """Forgets the variables and tables with a made-up name (unk_208001, rom_000001) that none of the C
        'texts' reads: their accesses turned out to be something else, a field a pointer reaches (the screen's
        block at 208000) or a table and an index into it, and nothing is left for the name to declare, nor
        for the runtime to load."""
        words = set()
        for text in texts:
            words.update(re.findall(r"\w+", text))
        for items in (self.variables, self.tables):
            for address in [address for address, item in items.items()
                            if item.unnamed and item.name not in words and f"{item.name}_ADDRESS" not in words]:
                del items[address]

    @staticmethod
    def element(items, address):
        """(array, element number, byte into it) when 'address' falls in an array symbols.txt gives the number
        of elements of (var NAME[COUNT]), past its first element's start; or None."""
        for item in items.values():
            if item.count is not None and item.address < address < item.address + item.count * (item.bits // 8):
                return (item,) + divmod(address - item.address, item.bits // 8)
        return None

    @staticmethod
    def inside(items, address):
        """An item that 'address' falls inside of (not at its start), or None."""
        for start in range(address - 1, address - 4, -1):
            item = items.get(start)
            if item is not None and not item.indexed and start + item.bits // 8 > address:
                return item
        return None

    # ---- what the printer asks ----

    def scalar(self, items, address, bits):
        item = items.get(address)
        if item is not None:
            name = f"{item.c}[0]" if item.indexed else item.c
            overlay = self.overlays.get((address, bits)) if items is self.variables else None
            if item.bits != bits and overlay is not None:
                return overlay.c
            return name if item.bits == bits else cprint.part(bits, name, 0, item.bits)
        outer = self.inside(items, address)
        if outer is not None:
            return cprint.part(bits, outer.c, address - outer.address, outer.bits)
        found = self.element(items, address)
        if found is not None:
            array, number, byte = found
            name = f"{array.c}[{number}]"
            return name if byte == 0 and bits == array.bits else cprint.part(bits, name, byte, array.bits)
        return None

    def access(self, address, bits, index_text, indexed=False, signed_text=None):
        """C for an access of 'bits' at 'address' (an IR expression): (text, signed, is RAM) or None. The
        text is an lvalue for work RAM and an rvalue for the ROM. 'index_text' prints an index, 'signed_text'
        a signed one as the number it is. With 'indexed', what is added to the constant is an index whatever
        its size (a pointer to a variable moved by it: no pointer of its own)."""
        if bits not in TYPES:
            return None
        value = constant_address(address)
        if value is not None:
            value = own_copy(value)
            if plain_hardware(value):
                text = self.scalar(self.board, value, bits)
                if text is None:
                    return None
                item = self.board.get(value)
                return text, item is not None and item.signed and item.bits == bits, True
            for items, ram in ((self.variables, True), (self.tables, False)):
                if (work_ram(value) if ram else program_rom(value)):
                    text = self.scalar(items, value, bits)
                    if text is None:
                        return None
                    item = items.get(value)
                    overlay = self.overlays.get((value, bits)) if ram else None
                    if item is not None and item.bits != bits and overlay is not None:
                        return text, overlay.signed, ram
                    return text, item is not None and item.signed and item.bits == bits, ram
            return None
        parts = based(address) or paged(address)
        if parts is None:
            return None
        base, rest = parts
        base = own_copy(base)
        # "constant + register" is a table and an index only when the index is small, or scaled by the size
        # of the table's elements; a register that may hold anything could be a pointer with a
        # displacement, and that read stays on the bus
        index = element_index(rest, bits)
        centered = not indexed and not table_index(rest, bits)
        if centered and not signed_number(index):
            return None
        ram = work_ram(base)
        board = plain_hardware(base) and base in self.board
        if not ram and not board and (not program_rom(base) or base + index_limit(rest) * (bits // 8) >= ROM_AREAS[0][1]):
            return None
        items = self.variables if ram else self.board if board else self.tables
        lvalue = ram or board
        ram_macro = BOARD_MACRO if board else RAM_MACRO
        loaded = rest
        while isinstance(loaded, ir.Op) and loaded.name in ("zext", "fits"):
            loaded = loaded.args[0]
        if base == VARIABLES and isinstance(loaded, ir.Load):
            # R25 plus an offset the code reads from a table (direction_to_player_offsets): the variable
            # that many bytes into the variables
            return f"RAM{bits}(GAME_VARIABLES + {index_text(rest)})", False, True
        if centered and (base in items or self.element(items, base) is None):
            return None    # (a signed number only from an element past an array's first, below)
        if base not in items:
            found = self.element(items, base)
            following = items.get(base + bits // 8)
            if found is None and following is not None and following.bits == bits and index is not None:
                # one element before an array, where the code counts from 1 (car_numbers[players_left - 1])
                before = ir.op("sub", (index, ir.Const(1, index.bits)), index.bits)
                return f"{following.c}[{index_text(before)}]", following.signed, lvalue
            if found is None:
                return None
            # an index from an element of an array past its first (the scrolls of the second tile layer), a
            # signed one too from its middle (steer_frames_by_steering[64 + steering])
            array, number, byte = found
            if byte == 0 and index is not None and array.bits == bits:
                text = (signed_text or index_text)(index) if centered else index_text(index)
                return f"{array.c}[{number} + {text}]", array.signed, lvalue
            if centered:
                return None
            narrow = element_index(rest, array.bits) if byte == 0 and array.bits < bits and lvalue else None
            if narrow is not None:
                # several elements at once, as the original's one wider access (AS32(video_registers[32 +
                # index * 2u]): two halfword registers cleared together)
                return cprint.part(bits, f"{array.c}[{number} + {index_text(narrow)}]", 0, array.bits), False, lvalue
            if LOADED_TABLES and not lvalue:
                return f"TABLE{bits}({array.name}, 0x{base - array.address:X}u + {index_text(rest)})", False, False
            macro = (ram_macro if lvalue else ROM_MACRO) + str(bits)
            return f"{macro}({array.name}_ADDRESS + 0x{base - array.address:X}u + {index_text(rest)})", False, lvalue
        item = items[base]
        if base == VARIABLES:
            macro = (ram_macro if lvalue else ROM_MACRO) + str(bits)
            return f"{macro}(GAME_VARIABLES + {index_text(rest)})", False, lvalue
        if index is not None and item.bits == bits:
            return f"{item.c}[{index_text(index)}]", item.signed, lvalue
        if LOADED_TABLES and not lvalue:
            # a loaded table read at another width than its elements': by its name and the offset in bytes
            return f"TABLE{bits}({item.name}, {index_text(rest)})", False, False
        macro = (ram_macro if lvalue else ROM_MACRO) + str(bits)
        start = "GAME_VARIABLES" if base == VARIABLES else f"{item.name}_ADDRESS"
        return f"{macro}({start} + {index_text(rest)})", False, lvalue

    def register(self, value):
        """Name of a hardware register at 'value' (NAME_ADDRESS for a variable of the board's plain
        memories), or None."""
        item = self.hardware.get(value)
        if item is not None:
            return item.name
        item = self.board.get(value)
        return f"{item.name}_ADDRESS" if item is not None else None

    def device(self, value):
        """Name of the register of a device at 'value' (none of the plain memories'), or None."""
        item = self.hardware.get(value)
        return item.name if item is not None else None

    # ---- the header ----

    def declarations(self):
        lines = list(NOTES["variables"])
        if VARIABLES is not None:
            lines.append(f"#define GAME_VARIABLES 0x{VARIABLES:06X}u")
        for address, name in sorted(NAMED_BASES.items()):
            lines.append(f"#define {name} 0x{address:06X}u")
        for address, item in sorted(self.variables.items()):
            lines += self.declare(item, RAM_MACRO, RAM_FUNCTION)
            for (start, bits), overlay in sorted(self.overlays.items()):
                if start == address:
                    lines += self.declare(overlay, RAM_MACRO, RAM_FUNCTION)
        if self.scoped:
            lines += [""] + NOTES["scoped"]
            for family, items in sorted(self.scoped.items()):
                for address, item in sorted(items.items()):
                    lines += self.declare(item, RAM_MACRO, RAM_FUNCTION)
                    for (start, _), overlay in sorted(self.scoped_overlays.get(family, {}).items()):
                        if start == address:
                            lines += self.declare(overlay, RAM_MACRO, RAM_FUNCTION)
        lines += [""] + NOTES["tables"]
        for address, item in sorted(self.tables.items()):
            lines += self.declare(item, ROM_MACRO, ROM_FUNCTION)
        lines += [""] + NOTES["hardware"]
        for address, item in sorted(self.hardware.items()):
            lines.append(f"#define {item.name} 0x{address:0{ADDRESS_DIGITS}X}u")
        if self.board:
            lines += [""] + NOTES.get("board", ["/* the board's plain memories, by name */"])
            for address, item in sorted(self.board.items()):
                lines += self.declare(item, BOARD_MACRO, BOARD_FUNCTION)
        return lines

    @staticmethod
    def declare(item, macro, function):
        kind = TYPES[item.bits]
        if item.signed:
            kind = kind[1:]
        constness = "const " if macro == ROM_MACRO else ""
        lines = [f"#define {item.name}_ADDRESS 0x{item.address:0{ADDRESS_DIGITS}X}u"]
        if item.state:
            return lines    # (a field of the game's state: state_declaration())
        if macro == ROM_MACRO and LOADED_TABLES:
            lines.append(f"extern const void *{item.name}_data;")
            if item.indexed:
                lines.append(f"#define {item.name} ((const {kind} *){item.name}_data)")
            else:
                lines.append(f"#define {item.name} (*(const {kind} *){item.name}_data)")
            return lines
        if item.indexed:
            lines.append(f"#define {item.name} (({constness}{kind} *){function}({item.name}_ADDRESS))")
        else:
            suffix = "S" if item.signed else ""
            lines.append(f"#define {item.name} {macro}{item.bits}{suffix}({item.name}_ADDRESS)")
        return lines

    def state_declaration(self):
        """C lines for the game's state (STATE): a structure that is the work RAM, its blocks (STATE_BLOCKS)
        and its variables at their places, as the board has them. Names that share bytes (a narrower name
        over a variable's first bytes, a family's own name for a place, a variable inside a block) share a
        union, each at its place in it. An array the code indexes with no count of its own runs as far as
        the next name, or the end of the block it is in."""
        if STATE is None:
            return []
        start, end = WORK_RAM
        blocks = []
        for address, member, struct, count in STATE_BLOCKS:
            if struct is None:
                blocks.append((address, count, f"uint8_t {member}[0x{count:X}];", member))
            else:
                blocks.append((address, pointers.STRUCTS[struct] * count,
                               f"struct {struct} {member}" + (f"[{count}]" if count > 1 else "") + ";", member))
        items = self.state_items()
        starts = sorted({block[0] for block in blocks} | {item.address for item in items})
        members = list(blocks)
        for item in items:
            kind = TYPES[item.bits][1:] if item.signed else TYPES[item.bits]
            count = item.count
            if item.indexed and count is None:
                limit = next((address for address in starts if address > item.address), end)
                for address, size, _, _ in blocks:
                    if address <= item.address < address + size:
                        limit = min(limit, address + size)
                count = max((limit - item.address) // (item.bits // 8), 1)
            members.append((item.address, item.bits // 8 * (count if item.indexed else 1),
                            f"{kind} {item.name}" + (f"[0x{count:X}]" if item.indexed else "") + ";", item.name))
        names = [member[3] for member in members]
        if len(set(names)) != len(names):
            raise SystemExit("the game's state has two places of one name: " +
                             ", ".join(sorted({name for name in names if names.count(name) > 1})))
        clusters = []    # [first, end, members]: places that share bytes
        for member in sorted(members, key=lambda member: (member[0], -member[1])):
            if clusters and member[0] < clusters[-1][1]:
                clusters[-1][1] = max(clusters[-1][1], member[0] + member[1])
                clusters[-1][2].append(member)
            else:
                clusters.append([member[0], member[0] + member[1], [member]])
        lines = ["#pragma pack(push, 1)", f"struct {STATE}", "{"]
        position = start
        for first, last, found in clusters:
            if first > position:
                lines.append(f"\tuint8_t pad_{position:0{ADDRESS_DIGITS}X}[0x{first - position:X}];")
            if len(found) == 1:
                lines.append("\t" + found[0][2])
            else:
                # the union's ways of reading those bytes: each the places that follow one another without
                # sharing any (an array, and beside it the names of its bytes one after another)
                ways = []
                for member in found:
                    way = next((way for way in ways if member[0] >= way[-1][0] + way[-1][1]), None)
                    if way is None:
                        ways.append([member])
                    else:
                        way.append(member)
                lines += ["\tunion", "\t{"]
                for way in ways:
                    if len(way) == 1 and way[0][0] == first:
                        lines.append("\t\t" + way[0][2])
                        continue
                    lines += ["\t\tstruct", "\t\t{"]
                    place = first
                    for address, size, declaration, name in way:
                        if address > place:
                            lines.append(f"\t\t\tuint8_t pad_to_{name}[0x{address - place:X}];")
                        lines.append("\t\t\t" + declaration)
                        place = address + size
                    lines.append("\t\t};")
                lines.append("\t};")
            position = last
        if position < end:
            lines.append(f"\tuint8_t pad_{position:0{ADDRESS_DIGITS}X}[0x{end - position:X}];")
        lines += ["};", "#pragma pack(pop)",
                  f"_Static_assert(sizeof(struct {STATE}) == 0x{end - start:X}, \"struct {STATE} is the work RAM\");"]
        lines += [f"_Static_assert(offsetof(struct {STATE}, {member}) == 0x{address - start:X}, "
                  f"\"{member} at its place in the work RAM\");" for address, _, _, member in blocks]
        return lines + [f"extern struct {STATE} *{STATE};"]

    def loaded_tables(self):
        """[(address, name)] of the tables the game loads (LOADED_TABLES): the plain ones and the tables of
        records."""
        if not LOADED_TABLES:
            return []
        found = [(address, item.name) for address, item in self.tables.items()]
        found += [(address, name) for address, (name, _) in pointers.RECORD_TABLES.items()]
        return sorted(found)


def table_registry(tables, digits, prefix="game"):
    """The C of the table registry (LOADED_TABLES): each table's pointer and what sets it, the table's
    name and its address, which say which piece of the assets it is ('prefix': game_tables, of type
    game_table; sound_tables for the sound program)."""
    lines = [f"const void *{name}_data;" for _, name in tables]
    lines += ["", "/* the tables the game loads from its assets: their names, addresses and pointers */",
              f"const {prefix}_table {prefix}_tables[] =", "{"]
    lines += [f"\t{{ \"{name}\", 0x{address:0{digits}X}u, &{name}_data }}," for address, name in tables]
    lines += ["};", "", f"const size_t {prefix}_table_count = {len(tables)};"]
    return lines


def index_limit(rest):
    """The largest index 'rest' can be: a byte's when it is one (on a map that says so), else a halfword's."""
    while BYTE_INDEXES and isinstance(rest, ir.Op) and rest.name in ("zext", "fits"):
        if rest.args[0].bits <= 8:
            return 0xFF
        rest = rest.args[0]
    return 0xFFFF


BYTE_INDEXES = False    # whether index_limit() looks at the index (the V60's program never needed it)


def accesses_of(functions):
    """(address expression, bits) of every memory read and write of the functions."""
    found = []
    for function in functions:
        for block in function.blocks.values():
            for statement in block.statements + [block.end]:
                if isinstance(statement, ir.Store):
                    found.append((statement.address, statement.bits))
                for expression in ir.statement_expressions(statement):
                    for node in ir.walk(expression):
                        if isinstance(node, ir.Load):
                            found.append((node.address, node.bits))
    return found
