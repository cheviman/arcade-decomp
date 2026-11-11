# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        emit.py
Description: writes a Konami CPU program's functions as C, through the decompiler's writer
Version:     0.1.0
Created:     06-11-2026
Modified:    07-11-2026
Author:      Sebastian Toyos

The decompiler's FunctionWriter does the work (decompiler/emit.py); this gives it the 053248's memory, its effects
and its helpers. use_memory_map() makes the board's map the one decompiler/memory.py and decompiler/pointers.py work
on, from the profile:
  - the work RAM (WORK_RAM_AREA, 4000-5FFF) holds the variables, read and written as lvalues (RAM8/16 of the
    game's support header). The 053248 is big endian: the board keeps its work RAM the other way round, as TMNT's
    keeps the 68000's, so that a word at any address is a plain lvalue of the host's (and an array's elements are
    NAME(index), a macro of the element's place);
  - the program's data is read through the bank the CPU has: a bank's code reads its own bank in the window
    (6000-7FFF), the fixed program (8000-FFFF) is everyone's. Its tables are named by their place (the region's
    offset, as symbols.txt and the assets have them), which the decompiler keys at PLACES + place so that no place
    is taken for an address of the work RAM: canonical() turns a CPU address into that key, with the bank of the
    function being written (current_bank); a window address the fixed program reads is named in the bank the
    static analysis finds the CPU surely has at the instruction (konami_cpu/function.py's banked_reads for an
    operand's address, origin_bank for one worked out), and in no bank where it chose the bank by a table (but for
    the profile's WINDOW_ELEMENTS). A table's NAME_ADDRESS is its CPU address (what the program holds as a value); the
    game loads it by its place (src/table.c's registry). A table read with the CPU's signed index (A,X) is the
    element the CPU picks, NAME[(int8_t)a], NAME[(int8_t)(a << 1) / 2] for words, NAME[(int8_t)a - 1] where the code
    counts from 1, or by its name and the bytes into it (TABLE8, TABLE16) where no element fits; the tables of
    records and what a table leads to are keyed by place too (pointers.DATA_KEY), so that a pointer read from a table
    is typed by symbols.txt;
  - the board's devices (0000-3FFF: the palette, the K052109 and its RAM, the sprite RAM and the RAM at 3000, which
    1FC2 swaps in and out, and the ports) are reached through the bus by name (io_read8, io_write8).
Effects: frame_wait(WAIT) where the program finds nothing to do until its next frame, take_interrupts() after it and
after what lets an interrupt in, firq_point() where the IRQ handler takes the sprite DMA's FIRQ, set_lines() (SETLN:
the bank), the interrupt mask's enable_irq()/disable_irq()/enable_firq()/disable_firq()/restore_mask(), lost_stack()
(a function that runs on a stack the code that reaches it pushed, which no path the program takes reaches as one).
"""
import re

from decompiler import cprint
from decompiler import emit
from decompiler import memory
from decompiler import pointers
from decompiler import structure
from v60 import ir

import game_profile

from . import analysis
from . import function as functions

WORK_RAM_AREA = getattr(game_profile, "WORK_RAM_AREA", (0x4000, 0x6000))
PLACES = 0x100000           # the key the decompiler names the program's data by: PLACES + its place
OUTSIDE = 0x200000          # the key of a window address whose bank is not known (no name)
PROGRAM_SIZE = analysis.CODE_END
current_bank = None         # the bank of the function being written (None: the fixed program's)
# in the fixed program, the bank the CPU surely has at the instruction being written (statement_bank), where it
# reads a window address it worked out (LDX #$6000 after the bank's SETLN, then LDX D,X)
origin_bank = None
# what gives that bank: (instruction place) -> bank or None (konami_cpu/decompile.py sets it from the analysis)
BANK_AT = None
# the window's data the fixed program reads in a bank it chose by a table, whatever the bank is (the profile's
# WINDOW_ELEMENTS: {function place: {window address: elements name}}): read as those elements at the window's start
# (BANK_WINDOW, game/support.h) plus the offset, NAME(BANK_WINDOW + i), since no one place has them
WINDOW_ELEMENTS = dict(getattr(game_profile, "WINDOW_ELEMENTS", {}))
current_entry = None        # the place of the function being written
# the names of the frame waits (the profile's FRAME_WAIT_NAMES: {CPU address: name})
FRAME_WAIT_NAMES = dict(getattr(game_profile, "FRAME_WAIT_NAMES", {}))
SHIFTS = ("lsrdi", "rordi", "asrdi", "asldi", "roldi")
SHIFT_PARTS = {"": "KONAMI_RESULT", "s": "KONAMI_N", "z": "KONAMI_Z", "ov": "KONAMI_V", "cy": "KONAMI_C"}
MASKS = {0x10: "irq", 0x40: "firq"}


def canonical(address):
    """The key the decompiler names data of the program at CPU address 'address' by, in current_bank"""
    if address >= PLACES:
        return address
    address &= 0xFFFF
    if address >= analysis.FIXED_CPU:
        return PLACES + analysis.place_of(None, address)
    if address >= analysis.WINDOW and current_bank is not None:
        return PLACES + analysis.place_of(current_bank, address)
    if address >= analysis.WINDOW and origin_bank is not None:
        return PLACES + analysis.place_of(origin_bank, address)
    return OUTSIDE + address


def statement_bank(statement):
    """Sets origin_bank for a statement of the fixed program's code: the bank the CPU surely has at its instruction"""
    global origin_bank
    origin = getattr(statement, "origin", None)
    origin_bank = BANK_AT(origin) if BANK_AT is not None and current_bank is None and isinstance(origin, int) \
        else None


def cpu_of_key(key):
    """The CPU address of a data key (PLACES + place)"""
    return analysis.cpu_of(key - PLACES)


def ram_element(name, index, bits):
    return f"{name}({index})"


def use_memory_map(names):
    """Makes the 053248's memory the one decompiler/memory.py and decompiler/pointers.py work on: 'names'
    (disassemble.Names, its program's addresses already keyed by canonical()) gives the structures"""
    memory.WORK_RAM = WORK_RAM_AREA
    # (the places first: the area memory.Memory bounds a table's reach by, the keys the tables are named by)
    memory.ROM_AREAS = ((PLACES, PLACES + PROGRAM_SIZE), (analysis.WINDOW, 0x10000))
    memory.ADDRESS_MASK = 0xFFFFFF
    memory.ADDRESS_DIGITS = 4
    memory.RAM_MACRO, memory.ROM_MACRO = "RAM", "DATA"
    memory.RAM_FUNCTION, memory.ROM_FUNCTION = "ram", "data_at"
    memory.LOADED_TABLES = True
    memory.COUNTED_FROM_ONE = False
    memory.NAMES_BEFORE_TABLES = 0
    memory.UNBOUNDED_READS = False
    memory.PAGED = False
    memory.PLAYER_FIELDS = None
    memory.CANONICAL = canonical
    memory.NAMED_COPIES = True
    memory.ROM_NAMES_FROM = analysis.WINDOW
    memory.VARIABLES = None
    memory.HARDWARE = list(getattr(game_profile, "HARDWARE", [(0x0000, 0x1F80, "VIDEO"), (0x1F80, 0x2000, "IO"),
                                                               (0x2000, 0x4000, "VIDEO")]))
    memory.PLAIN_HARDWARE = ()
    memory.BUS_ONLY = []
    memory.BYTE_INDEXES = True
    memory.BOUNDED_INDEXES = True
    memory.SIGNED_INDEXES = False
    memory.NAMED_BASES = {}
    memory.RAM_ELEMENT = ram_element
    memory.NOTES = dict(getattr(game_profile, "NOTES", {
        "variables": ["/* the game's variables in work RAM (4000-5FFF), which the board keeps the other way round */"],
        "scoped": ["/* work RAM as one family of functions uses it */"],
        "tables": ["/* the program's data the code reads by name, which the game loads from its assets (src/table.c) */"],
        "hardware": ["/* the board's registers (read and written through the bus) */"],
    }))
    emit.REGIONS = []
    # the register the game's handlers find their object in (the profile's OBJECT_REGISTER, The Simpsons' X: the
    # object lists' dispatchers hand it, 07CB62 "LDX #$4F80"): a function of a family reads that object's fields by
    # the family's names (symbols.txt scoped lines); none by default
    own = getattr(game_profile, "OBJECT_REGISTER", None)
    numbers = {name: number for number, name in functions.KONAMI.register_names.items()}
    emit.OWN_OBJECT_REGISTERS = (numbers[own.lower()],) if own else ()
    emit.BOARD_MEMORIES = {WORK_RAM_AREA[0]}
    # (the profile's naming settings, as the 68000's (m68000/emit.py), off by default: a constant stored into a field
    # typed as a number is a number; a parameter or result takes the name of the structure read through it, or of the
    # variable every caller hands it; a variable stored into, or compared with, one field only takes its name)
    emit.NUMBERS_BY_FIELD = getattr(game_profile, "NUMBERS_BY_FIELD", False)
    emit.PARAMS_BY_USE = getattr(game_profile, "PARAMS_BY_USE", False)
    emit.NAMES_BY_STORES = getattr(game_profile, "NAMES_BY_STORES", False)
    emit.PARAMS_FROM_CALLERS = getattr(game_profile, "PARAMS_FROM_CALLERS", False)
    emit.NAMES_BY_COMPARISONS = getattr(game_profile, "NAMES_BY_COMPARISONS", False)
    emit.UNREACHABLE_ENDS = True    # (a function whose every way out of its last block returns says its end is never reached)
    emit.NAMED_CODE_CASES = True
    emit.INSTANCE_FAMILIES_BY_FUNCTION = dict(getattr(game_profile, "INSTANCE_FAMILIES_BY_FUNCTION", {}))
    pointers.STRUCTS = dict(names.structs)
    pointers.PLAYERS = (0, 0, 0)
    pointers.VARIABLES = None
    pointers.CONTEXT_KINDS = {}
    pointers.OBJECT_REGISTERS = ()
    pointers.HANDLER_FIELD = None
    pointers.WORK_RAM = WORK_RAM_AREA
    pointers.DEVICE_AREAS = ((0x0000, 0x4000),)
    pointers.ADDRESS_MASK = 0xFFFF
    pointers.POINTER_BITS = 16
    pointers.PAGE_LINKS = False
    pointers.ROM_DATA = (analysis.WINDOW, 0x10000)
    # the game's structures that lie one after another at a fixed address (the profile's STRUCT_ARRAYS: name ->
    # (first, count); The Simpsons' objects), and the family a structure at a fixed address always is
    # (INSTANCE_FAMILIES); none by default
    pointers.ARRAYS = dict(getattr(game_profile, "STRUCT_ARRAYS", {}))
    pointers.INSTANCES = {first + number * names.structs[struct]: (struct, names.structs[struct])
                          for struct, (first, count) in pointers.ARRAYS.items() for number in range(count)}
    pointers.INSTANCE_FAMILIES = dict(getattr(game_profile, "INSTANCE_FAMILIES", {}))
    pointers.VARIABLE_BLOCKS = {}
    pointers.BASE_KINDS = {}
    pointers.PAGE_LINK_KINDS = {}
    pointers.NESTED = {}
    pointers.BELOW = {}
    # (the profile's OWN_OBJECT_POINTERS, off by default: the object register is the family's object in the pointer
    # analysis too, so a family's pointer fields type what is read through them)
    pointers.OWN_REGISTERS = emit.OWN_OBJECT_REGISTERS if getattr(game_profile, "OWN_OBJECT_POINTERS", False) else ()
    pointers.HINTS_OVER_FAMILY = True    # (a param line's type over the function's family: the holder in X)
    pointers.STRUCT_FUNCTION = "ram"
    pointers.ROM_STRUCTS = set(names.rom_structs)
    pointers.RUNS = set(names.structs)
    pointers.ELEMENTS = dict(names.arrays)
    pointers.RECORD_TABLES = dict(names.record_tables)
    pointers.RECORD_COUNTS = {address: names.counts[address] for address in names.record_tables
                              if address in getattr(names, "counts", {})}
    # (a structure laid out backwards has no array fields: its first element is the field's name, the others are
    # read by their offsets)
    pointers.FIELD_ARRAYS = {}
    pointers.SCOPED_ARRAYS = {}
    pointers.BOARD_STRUCTS = set(names.board_structs)
    pointers.ROM_FUNCTION = "data_record"
    pointers.CONSTANTS = True
    pointers.PAGED = False
    pointers.REVERSED = True
    pointers.RECORDS_LOADED_WHOLE = True
    # (the profile's ELEMENT_READS, off by default: the values a pointer walks, symbols.txt elements lines, read one at a
    # time at their address, NAME(p), as the records' copies run: data_record)
    pointers.ELEMENT_READS = getattr(game_profile, "ELEMENT_READS", False)
    if pointers.ELEMENT_READS:
        # (what a value of one is called where a local holds it: the elements' name, not "element")
        for struct, kind in names.arrays.items():
            if names.fields.get((struct, 0), ("",))[0] == "element":
                names.fields[(struct, 0)] = (struct, kind)
    # (the profile's NESTED_FAMILIES, off by default: a family's view takes the fields of the families its name extends)
    pointers.NESTED_FAMILIES = getattr(game_profile, "NESTED_FAMILIES", False)
    pointers.SIGNED_OFFSETS = False
    # (the tables of records and what the tables lead to are keyed by place: a constant of the code is the place it
    # names in the bank of the function being walked)
    pointers.DATA_KEY = data_key
    pointers.ENTERING = entering
    # (the profile's RECORDS_BEFORE, none by default: a base that many records before a table of records, where the
    # code counts its records from 1, still reads that table, team_moves[i - 1].field)
    pointers.RECORDS_BEFORE = getattr(game_profile, "RECORDS_BEFORE", 0)


def data_key(address):
    """The key of the program's data at CPU address 'address' in current_bank (pointers.DATA_KEY), or None"""
    key = canonical(address)
    return key if PLACES <= key < PLACES + PROGRAM_SIZE else None


def entering(entry):
    """The bank of the function the pointers' analysis walks (pointers.ENTERING)"""
    global current_bank
    current_bank = analysis.bank_of(entry) if isinstance(entry, int) else None


def signed_offset(rest):
    """The byte an indexed operand adds signed to its index register (A,X and B,X: konami.lst's INDEXED takes the
    accumulator as an int8_t), looked through the widening the names' pass gives an offset; None for any other
    offset"""
    node = rest
    while isinstance(node, ir.Op) and node.name in ("zext", "fits") and node.args[0].bits > 8:
        node = node.args[0]
    if isinstance(node, ir.Op) and node.name == "sext" and node.args[0].bits == 8:
        return node
    return None


def doubled(offset):
    """How many low bits of a signed byte offset (signed_offset) are known to be zero: 1 for an index the code
    doubled before it (ASLA: a table of words), 2 for one it multiplied by 4..."""
    return pointers.low_zeros(offset.args[0], {})


def offset_terms(rest):
    """The terms of an offset added up (looked through widenings)"""
    while isinstance(rest, ir.Op) and rest.name in ("zext", "fits") and rest.args[0].bits > 8:
        rest = rest.args[0]
    if isinstance(rest, ir.Op) and rest.name == "add":
        return [term for arg in rest.args for term in offset_terms(arg)]
    return [rest]


def signed_sum(rest):
    """True for an offset that adds the CPU's signed byte to something else (LEAX A,X then LDA B,X: a row and a
    column), or that is what DIVX worked out (a quotient, any number): a table read by its name and that offset"""
    terms = offset_terms(rest)
    if len(terms) == 1:
        return isinstance(terms[0], ir.Op) and terms[0].name.startswith("helper:divx")
    # (every other term an index, not a pointer: u + (int8_t)b - 3 is a record's byte, no table at FFFD)
    signed = [term for term in terms if signed_offset(term) is not None]
    return bool(signed) and all(term in signed or memory.small(term) for term in terms)


def wrapped(text):
    """'text' as one operand of a cast: in parentheses unless it is a name or a number, or in them already"""
    if re.fullmatch(r"[\w.]+", text):
        return text
    if text.startswith("("):
        depth = 0
        for position, character in enumerate(text):
            depth += {"(": 1, ")": -1}.get(character, 0)
            if not depth:
                if position == len(text) - 1:
                    return text
                break
    return f"({text})"


# the accumulators A, B and D: what the program counts and computes in, never an address
ACCUMULATORS = (functions.A, functions.B, functions.D)


class Memory(memory.Memory):
    """The decompiler's memory names, with the tables of the program's data keyed by canonical(): an unnamed table
    is named after its place, and its NAME_ADDRESS is its CPU address. A table read with the CPU's signed index (A,X)
    is that table's element by the index as the CPU takes it, NAME[(int8_t)a], or (the index doubled for a table of
    words) the element that many bytes on, NAME[(int8_t)(a << 1) / 2]; a read at another width, or into an element,
    by the table's name and the bytes, TABLE8(name, 0x1u + (int8_t)a)"""

    def __init__(self, names, accesses, data_names=None, places=None):
        self.data_names = dict(data_names or {})
        self.banks_of = dict(places or {})    # {key: bank or None} of the tables
        self.unnamed_names = set()            # the tables named here, after their piece or their place
        self.named_keys = set(names.tables)   # the tables symbols.txt names (their keys)
        # (a table read with a signed index is a table like one read with a small one: what the C names)
        small = ir.Load(ir.Const(0, 16), 8)
        widened = []
        for address, bits in accesses:
            parts = memory.based(address) if memory.constant_address(address) is None else None
            if parts is not None and PLACES <= parts[0] < PLACES + PROGRAM_SIZE \
                    and (signed_offset(parts[1]) is not None or signed_sum(parts[1])):
                address = ir.op("add", (ir.Const(parts[0], 32), ir.zext(small, 32)), 32)
            widened.append((address, bits))
        super().__init__(names, widened, data_names)
        # (the board's registers symbols.txt names: io lines; the others the decompiler names after their address)
        self.named_hardware = set(names.hardware)

    def access(self, address, bits, index_text, indexed=False):
        parts = memory.based(address) if memory.constant_address(address) is None else None
        window = WINDOW_ELEMENTS.get(current_entry, {}).get(parts[0] & 0xFFFF) if parts is not None else None
        if window is not None and memory.own_copy(parts[0]) >= OUTSIDE and window in pointers.ELEMENTS:
            # (the window's data in a bank the code chose by a table: those elements, from the window's start)
            element_bits, signed = memory.SIGNED[pointers.ELEMENTS[window]]
            if element_bits == bits:
                moved = parts[0] - analysis.WINDOW
                start = "BANK_WINDOW" if not moved else f"BANK_WINDOW + 0x{moved:X}u"
                return f"{window.upper()}({start} + {index_text(parts[1])})", signed, False
        offset = signed_offset(parts[1]) if parts is not None else None
        if offset is not None:
            found = self.signed_read(memory.own_copy(parts[0]), offset, bits, index_text)
            if found is not None:
                return found
        elif parts is not None and signed_sum(parts[1]):
            item = self.tables.get(memory.own_copy(parts[0]))
            if item is not None:
                if item.bits == bits and len(offset_terms(parts[1])) == 1:
                    # (the quotient DIVX worked out: an element by that number)
                    return self.indexed_text(self.tables, item, index_text(parts[1])), item.signed, False
                # (a row and a column, as the CPU adds them: the bytes into the table by its name)
                return f"TABLE{bits}({item.name}, {index_text(parts[1])})", False, False
        found = super().access(address, bits, index_text, indexed)
        if found is None and parts is not None:
            key = memory.own_copy(parts[0])
            item = self.tables.get(key)
            if item is not None and key in self.named_keys and not any(
                    not isinstance(term, (ir.Op, ir.Const)) and term.bits > 8 for term in offset_terms(parts[1])):
                # (a table symbols.txt names, read by a 16-bit number the code works out, D after ASLB; ROLA: by its
                # name and the bytes into it)
                return f"TABLE{bits}({item.name}, {index_text(parts[1])})", False, False
        return found

    def signed_read(self, base, offset, bits, index_text):
        """(C, signed, False) for a read of 'bits' at the program's data 'base' (a key) plus the CPU's signed byte
        'offset', by the name of the table it is in; None where no table has a name there"""
        if not PLACES <= base < PLACES + PROGRAM_SIZE:
            return None
        item, number, byte = self.tables.get(base), 0, 0
        if item is None:
            found = self.element(self.tables, base)
            if found is not None:
                item, number, byte = found
        if item is None:
            # (a few bytes into a table: the second byte of its words, 79A7 into 79A6's)
            start = next((start for start in range(base - 1, base - 4, -1) if start in self.tables), None)
            if start is not None and start + self.tables[start].bits // 8 > base:
                item, byte = self.tables[start], base - start
        before = 0
        if item is None:
            # (an element before a table: the code counts from 1, LDB <$57 the players; NAME[(int8_t)players - 1])
            found = self.table_just_after(base)
            if found is not None:
                item, before = found
        if item is None:
            return None
        size = item.bits // 8
        text = f"(int8_t){wrapped(index_text(offset.args[0]))}"
        if item.bits == bits and not byte and (size == 1 or doubled(offset) >= size.bit_length() - 1) \
                and not before % size:
            index = text if size == 1 else f"{text} / {size}"
            if number:
                index = f"{number} + {index}"
            if before:
                index = f"{index} - {before // size}"
            return self.indexed_text(self.tables, item, index), item.signed, False
        into = base - item.address
        if into < 0:
            return f"TABLE{bits}({item.name}, {text} - 0x{-into:X})", False, False
        return f"TABLE{bits}({item.name}, {f'0x{into:X}u + ' if into else ''}{text})", False, False

    def table_just_after(self, base):
        """(table, how many bytes after 'base' it starts) for a table symbols.txt names 1 or 2 bytes after 'base', a
        place no table or piece of the data starts at: the base the code indexes from where its index starts at 1"""
        if base in self.data_names or base in self.tables:
            return None
        for distance in (1, 2):
            item = self.tables.get(base + distance)
            if item is not None and item.name not in self.unnamed_names:
                return item, distance
        return None

    def add_unnamed(self, items, widths, name_of):
        if items is self.tables:
            # (a base just before a table symbols.txt names, where the code counts from 1: that table, no other)
            for address in [address for address in widths
                            if address not in items and self.table_just_after(address) is not None]:
                del widths[address]

            def name_of(address):
                name = self.data_names.get(address, f"rom_{address - PLACES:06X}")
                self.unnamed_names.add(name)
                return name
        super().add_unnamed(items, widths, name_of)

    def declarations(self):
        lines = super().declarations()
        # (the program's data: its NAME_ADDRESS the CPU's address, not the key the decompiler names it by)
        fixed = []
        for line in lines:
            if line.startswith("#define ") and line.endswith("_ADDRESS 0x" + line.split(" 0x")[-1]) \
                    and line.split()[1][:-len("_ADDRESS")] in self.table_names():
                value = int(line.split(" 0x")[-1].rstrip("u"), 16)
                if value >= PLACES:
                    place = value - PLACES
                    bank = analysis.bank_of(place)
                    where = "the fixed program" if bank is None else f"bank {bank:02X}"
                    line = f"#define {line.split()[1]} 0x{cpu_of_key(value):04X}u    /* {place:06X}: {where} */"
            fixed.append(line)
        return fixed

    def table_names(self):
        if not hasattr(self, "_table_names"):
            self._table_names = {item.name for item in self.tables.values()}
        return self._table_names

    def loaded_tables(self):
        return [(address, name) for address, name in super().loaded_tables()]


class Printer(emit.Printer):
    def emit(self, node):
        if isinstance(node, ir.Const) and PLACES <= node.value < PLACES + PROGRAM_SIZE:
            # a key the decompiler names the program's data by (a table of records the code adds an index to, read
            # where the pointers' analysis keyed it): the data's name, NAME_ADDRESS, whose value is the CPU's address;
            # else that address
            named = self.writer.emitter.address_text(node.value)
            if named is not None:
                return cprint.Value(named[0], named[1], True, "u32")
            return cprint.Value(f"0x{cpu_of_key(node.value):04X}u", cprint.PRIMARY, True, "u32")
        family = self.writer.global_family
        if isinstance(node, ir.Const) and family and id(node) not in self.writer.numbers():
            # the address of work RAM the function's family names its own way (a scoped global: the scratch bytes
            # insert_high_score hands on as the new entry's place), by that name
            item = self.writer.emitter.memory.scoped.get(family, {}).get(node.value)
            if item is not None:
                return cprint.Value(f"{item.name}_ADDRESS", cprint.PRIMARY, True, "u32")
        return super().emit(node)

    def helper(self, name, args, bits):
        values = [self.exact(arg).text for arg in args]
        base, _, part = name.partition("_")
        if base in SHIFTS:
            text = f"konami_shift(KONAMI_{base.upper()}, {SHIFT_PARTS[part]}, {', '.join(values)})"
            return cprint.Value(text, cprint.PRIMARY, True, "u32")
        if name in ("daa", "daa_cy", "divx", "divx_rem"):
            function = {"daa": "konami_daa", "daa_cy": "konami_daa_carry", "divx": "konami_divx",
                        "divx_rem": "konami_divx_remainder"}[name]
            return cprint.Value(f"{function}({', '.join(values)})", cprint.PRIMARY, True, "u32")
        if name == "interrupt_mask":
            return cprint.Value("interrupt_mask()", cprint.PRIMARY, True, "u32")
        return super().helper(name, args, bits)

    def operator(self, node):
        if node.name == "case_key":
            return self.any(node.args[0])
        return super().operator(node)


def signed_case(value):
    return str(value - 0x10000 if value >= 0x8000 else value)


class Writer(emit.FunctionWriter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.printer = Printer(self)

    def write(self):
        global current_bank, origin_bank, current_entry
        current_bank = analysis.bank_of(self.function.entry) if isinstance(self.function.entry, int) else None
        current_entry = self.function.entry
        origin_bank = None
        try:
            return super().write()
        finally:
            origin_bank = None

    def statement(self, statement):
        statement_bank(statement)    # (a window address the fixed program reads: in the bank the CPU has there)
        return super().statement(statement)

    def numbers(self):
        """emit.FunctionWriter's, and the constants the function puts in A, B or D, the accumulators, unless it reads or
        writes memory through that variable: a sound command (LDD #$5A00), an animation and its time (LDD #$8000), a
        speed (LDD #$FFC0) are numbers whatever address they equal; the pointers are in X, Y and U"""
        if hasattr(self, "_numbers"):
            return self._numbers
        found = super().numbers()
        pointing = self.address_variables()
        for block in self.function.blocks.values():
            for statement in block.statements:
                if isinstance(statement, ir.SetReg) and statement.index in ACCUMULATORS \
                        and isinstance(statement.value, ir.Const) \
                        and self.vars.def_name(statement, statement.index) not in pointing:
                    found.add(id(statement.value))
            # (and what an accumulator is compared with: CMPD #$4000, a zoom's limit, is no floor_map_ADDRESS)
            for statement in block.statements + [block.end]:
                for expression in ir.statement_expressions(statement):
                    for node in ir.walk(expression):
                        if isinstance(node, ir.Op) and node.name in emit.COMPARISONS \
                                and any(isinstance(arg, ir.Reg) and arg.index in ACCUMULATORS
                                        and self.vars.use_name(arg) not in pointing for arg in node.args):
                            found |= {id(arg) for arg in node.args if isinstance(arg, ir.Const)}
        return found

    def effect(self, statement):
        name = statement.name
        args = statement.args
        if name == "frame_wait":
            wait = args[0].value
            return [f"frame_wait({FRAME_WAIT_NAMES.get(wait, f'0x{wait:04X}u')});"]
        if name == "take_interrupts":
            return ["take_interrupts();"]
        if name == "firq_point":
            return ["firq_point();"]
        if name == "set_lines":
            return [f"set_lines({self.exact(args[0])});"]
        if name == "and_mask":
            cleared = ~args[0].value & 0xD0
            if cleared in MASKS:
                return [f"enable_{MASKS[cleared]}();"]
            return [f"and_mask(0x{args[0].value:02X}u);"]
        if name == "or_mask":
            if args[0].value in MASKS:
                return [f"disable_{MASKS[args[0].value]}();"]
            return [f"or_mask(0x{args[0].value:02X}u);"]
        if name == "restore_mask":
            return [f"restore_mask({self.exact(args[0])});"]
        if name == "lost_stack":
            return [f"lost_stack(0x{args[0].value:06X}u);"]
        if name == "illegal":
            return [f"illegal_opcode({self.exact(args[0])});"]
        if name == "invalid_jump":
            return [f"invalid_jump({self.exact(args[0])});"]
        return super().effect(statement)

    def case_lines(self, pad, key, signed, cases):
        """The lines of a switch on a case key: its subject, then each group of cases"""
        subject = key.args[0] if isinstance(key, ir.Op) and key.name == "case_key" else key
        value = self.printer.exact(subject)
        text = value.text
        if signed:
            # (the cast takes all of the value: (int8_t)(x >> 8), never (int8_t)x >> 8)
            inner = self.printer.wrap(value, cprint.UNARY)
            text = f"(int8_t){inner}" if subject.bits == 8 else f"(int16_t){inner}"
        lines = [pad + f"switch ({text})", pad + "{"]
        for values, body in cases:
            for value in sorted(values, key=lambda value: (value - 0x100 if signed and value >= 0x80 else value)
                                if isinstance(value, int) else 0):
                if signed:
                    shown = value - 0x100 if subject.bits == 8 and value >= 0x80 else value
                    shown = shown - 0x10000 if subject.bits == 16 and shown >= 0x8000 else shown
                    lines.append(pad + f"case {shown}:")
                else:
                    named = self.emitter.address_text(value) if subject.bits == 16 and value >= 0x4000 else None
                    lines.append(pad + f"case {named[0] if named is not None else f'0x{value:X}u'}:")
            lines += body
        return lines + emit.checked_default(pad, text) + [pad + "}"]

    def node(self, node, depth, last):
        if isinstance(node, structure.Switch) and isinstance(node.value, ir.Op) and node.value.name == "case_key":
            pad = "\t" * depth
            signed = bool(node.value.args[1].value) if len(node.value.args) > 1 else False
            cases = [(values, self.nodes(body, depth + 1)) for values, body in node.cases]
            return self.case_lines(pad, node.value, signed, cases)
        return super().node(node, depth, last)

    def call(self, statement):
        if statement.target is None and isinstance(statement.address, ir.Op) and statement.address.name == "case_key":
            cases = getattr(statement, "cases", {})
            signed = bool(statement.address.args[1].value) if len(statement.address.args) > 1 else False
            by_target = {}
            for value, target in cases.items():
                by_target.setdefault(target, []).append(value)
            groups = [(values, ["\t" + self.call_text(target, statement.return_address), "\tbreak;"])
                      for target, values in sorted(by_target.items(), key=lambda item: min(item[1]))]
            return self.case_lines("", statement.address, signed, groups)
        return super().call(statement)


class Emitter(emit.Emitter):
    """emit.Emitter with nothing of the V60 program's"""

    def address_text(self, value):
        """emit.Emitter's, but for a register of the board symbols.txt does not name: the 053248 sees its devices from
        0000 up (VIDEO_00C0), where the program's small numbers are (0xC0, a field's offset 0x3C, a sound 0x1D00), so a
        constant is such a register only by an io line's name"""
        if value in self.memory.hardware and value not in getattr(self.memory, "named_hardware", ()):
            return None
        return super().address_text(value)

    def restores_current(self, entry):
        return None, None
