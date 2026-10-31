# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        emit.py
Description: writes the 68000 program's functions as C, through the decompiler's writer
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

The decompiler's FunctionWriter does the work (decompiler/emit.py); this gives it the 68000's memory, its effects
and its helpers. use_memory_map() makes the board's map the one decompiler/memory.py and decompiler/pointers.py
work on, from the profile: the work RAM (WORK_RAM_AREA) holds the variables, read and written as lvalues
(RAM8/16/32 of the game's support header, over the work RAM as the board keeps it); the program ROM
(PROGRAM_AREA) the tables, which the game loads by name from its assets; the board's devices (HARDWARE) are
reached through the bus by name (mem_read8/16/32, mem_write8/16/32). A computed address no name explains goes
over the bus too.

The 68000 is big endian and its C runs on little-endian machines: the board keeps its work RAM with the bytes
the other way round (the byte at the work RAM's last address first), so that a value of any width at any
address is a plain C lvalue of the host's (support header: RAM16(address)), and a part of a variable is the
host's part of it: cprint.part() says the place from the host's end for a big-endian CPU. An array in work RAM
then runs the other way in the host's memory: its elements are written NAME(index) (memory.RAM_ELEMENT), a macro
of the element's place. A table the C reads by name is a copy the game makes in the host's byte order, element by
element (its registry gives the width); a read of it at another width reads the data as the program has it
(TABLE16(name, offset)).

The game's structures (symbols.txt's struct and field lines: the objects in work RAM, the records of the program's
data its pointers lead to) run the other way round in the host's memory too, so ram.h declares their fields from
their ends (decompiler/pointers.py, REVERSED): OBJECT(address) is ram(address, sizeof(struct object)), a record
of the program's data data_record(address, sizeof(...)), the host's place of the record's bytes the other way
round (game/support.h). A record past the one a pointer is in is reached by moving the pointer.

Effects: frame_wait() where the main loop finds nothing to do, take_interrupts() after it and after a write of
the status register (write_sr), return_from_exception() before RTE.
"""
import re

from decompiler import cprint
from decompiler import emit
from decompiler import memory
from decompiler import pointers
from v60 import ir

import game_profile

WORK_RAM_AREA = getattr(game_profile, "WORK_RAM_AREA", (0x060000, 0x064000))
PROGRAM_AREA = getattr(game_profile, "PROGRAM_AREA", (0x000000, 0x060000))
SHIFT_KINDS = ("asl", "asr", "lsl", "lsr", "rol", "ror", "roxl", "roxr")
# what the driver (m68000/decompile.py) gives the memory names: the bytes of the code, where the pieces of the
# program's data start (the assets' layout), and how far before a piece a base the code indexes from may be
CODE_BYTES = set()
PIECE_STARTS = []
BEFORE_TABLES = 0x20


def ram_element(name, index, bits):
    """An element of an array in work RAM: NAME(index), the macro of its place"""
    return f"{name}({index})"


def use_memory_map(names):
    """Makes the 68000's memory the one decompiler/memory.py and decompiler/pointers.py work on: 'names'
    (disassemble.Names) gives the structures"""
    memory.WORK_RAM = WORK_RAM_AREA
    memory.ROM_AREAS = (PROGRAM_AREA,)
    memory.ADDRESS_MASK = 0xFFFFFF
    memory.ADDRESS_DIGITS = 6
    memory.RAM_MACRO, memory.ROM_MACRO = "RAM", "DATA"
    memory.RAM_FUNCTION, memory.ROM_FUNCTION = "ram", "data_at"
    memory.LOADED_TABLES = True
    memory.COUNTED_FROM_ONE = False
    memory.NAMES_BEFORE_TABLES = 0
    memory.PAGED = False
    memory.PLAYER_FIELDS = None
    memory.CANONICAL = lambda address: address
    memory.NAMED_COPIES = True
    memory.ROM_NAMES_FROM = getattr(game_profile, "CODE_START", 0x1000)
    memory.VARIABLES = None
    memory.HARDWARE = list(getattr(game_profile, "HARDWARE", []))
    memory.PLAIN_HARDWARE = ()
    memory.BUS_ONLY = []
    memory.BYTE_INDEXES = True
    memory.BOUNDED_INDEXES = True
    # work RAM addresses with a name of their own (the profile's NAMED_BASES, none by default): the structures at a
    # fixed address the code reaches by name (LAYER(WORKING_LAYER)->x rather than LAYER(LAYER_ADDRESS(0))->x)
    memory.NAMED_BASES = dict(getattr(game_profile, "NAMED_BASES", {}))
    memory.RAM_ELEMENT = ram_element
    memory.NOTES = dict(getattr(game_profile, "NOTES", memory.NOTES))
    emit.REGIONS = list(getattr(game_profile, "REGIONS", []))
    # the address register the game's handlers find their object in (the profile's OBJECT_REGISTER, TMNT's A0):
    # a function of a family reads that object's fields by the family's names (symbols.txt scoped lines)
    own = getattr(game_profile, "OBJECT_REGISTER", None)
    emit.OWN_OBJECT_REGISTERS = (8 + int(own[1]),) if own and own[0] in "Aa" else ()
    emit.BOARD_MEMORIES = {WORK_RAM_AREA[0]} | {start for start, _, _ in emit.REGIONS}
    pointers.STRUCTS = dict(names.structs)
    pointers.PLAYERS = (0, 0, 0)
    pointers.VARIABLES = None
    pointers.CONTEXT_KINDS = {}
    pointers.OBJECT_REGISTERS = ()
    pointers.HANDLER_FIELD = None
    pointers.WORK_RAM = WORK_RAM_AREA
    # where the board's devices answer (the profile's DEVICE_AREAS, none by default): a pointer the code works out
    # from an address there (the tile RAM's place a text is drawn at) reaches the device, through the bus by name
    # (io_write8(p, value)), not memory of the program's
    pointers.DEVICE_AREAS = tuple(getattr(game_profile, "DEVICE_AREAS", ()))
    pointers.ADDRESS_MASK = 0xFFFFFF
    pointers.POINTER_BITS = 32
    # (a data register that holds an address takes a link of 16 bits into its low word: move.w (A1)+, D6)
    pointers.PAGE_LINKS = True
    pointers.ROM_DATA = (memory.ROM_NAMES_FROM, PROGRAM_AREA[1])
    # the game's structures that lie one after another at a fixed address (the profile's STRUCT_ARRAYS: name ->
    # (first, count)), and the ones whose fields the code also reaches by their own address (INSTANCES)
    pointers.ARRAYS = dict(getattr(game_profile, "STRUCT_ARRAYS", {}))
    pointers.INSTANCES = {first + number * names.structs[struct]: (struct, names.structs[struct])
                          for struct, (first, count) in pointers.ARRAYS.items() for number in range(count)}
    pointers.VARIABLE_BLOCKS = {}
    pointers.BASE_KINDS = {}
    pointers.PAGE_LINK_KINDS = {}
    pointers.NESTED = {}
    pointers.BELOW = {}
    pointers.OWN_REGISTERS = ()
    pointers.STRUCT_FUNCTION = "ram"
    pointers.ROM_STRUCTS = set(names.rom_structs)
    # (records of the program's data and the work RAM's structures alike: past one, the next)
    pointers.RUNS = set(names.structs)
    pointers.ELEMENTS = dict(names.arrays)
    pointers.RECORD_TABLES = dict(names.record_tables)
    pointers.FIELD_ARRAYS = {key: (int(names.fields[key][1][1:]), count) for key, count in names.field_arrays.items()}
    pointers.SCOPED_ARRAYS = {(struct, family, offset): (int(names.scoped[(struct, family)][offset][1][1:]), count)
                              for (struct, family, offset), count in names.scoped_arrays.items()}
    pointers.BOARD_STRUCTS = set(names.board_structs)
    pointers.ROM_FUNCTION = "data_record"
    pointers.CONSTANTS = True
    pointers.PAGED = False
    # the work RAM and the program's data run the other way round in the host's memory: so do the structures
    pointers.REVERSED = True
    # (the profile's NESTED_FAMILIES: enemy_walk's view takes the fields enemy names, decompiler/pointers.py)
    pointers.NESTED_FAMILIES = getattr(game_profile, "NESTED_FAMILIES", False)


class Memory(memory.Memory):
    """The decompiler's memory names, with the tables the game loads in the host's byte order: a table read at
    another width than its elements' is read as the program has it (TABLE16(name, offset)). A variable the C
    reads by index only through a pointer to it (pointers.py's "global" fields) turns out an array while the C
    is written: 'late' keeps their names, whose plain uses become NAME(0) (indexed_uses)"""

    def __init__(self, names, accesses, data_names=None, own=None):
        self.late = set()
        self.shifted = {}    # {base in the code: (the table's start, how far before it the base is)}
        self.data_names = dict(data_names or {})
        super().__init__(names, accesses, data_names, own)

    def add_unnamed(self, items, widths, name_of):
        if items is self.tables:
            # (a number below the code, added to a pointer, is a displacement: "(A3,D6.w)" with D6 = 0x40)
            for address in [address for address in widths if address < memory.ROM_NAMES_FROM]:
                del widths[address]
            # a base the code indexes from past its start ("lea table, A6" then "(-$10,A6,D2.w)" with D2 at least
            # 0x10): it falls in the code, and the table is the piece of data that starts after it
            for address in list(widths):
                if address not in CODE_BYTES or address in items:
                    continue
                following = next((start for start in PIECE_STARTS if address < start <= address + BEFORE_TABLES), None)
                if following is None:
                    continue
                counts = widths.pop(address)
                self.shifted[address] = (following, following - address)
                if following not in items and following not in widths:
                    widths[following] = counts
        super().add_unnamed(items, widths, name_of)

    def access(self, address, bits, index_text, indexed=False):
        parts = memory.based(address) if memory.constant_address(address) is None else None
        if parts is not None and memory.program_rom(parts[0]) and parts[0] < memory.ROM_NAMES_FROM:
            return None    # (a displacement from a pointer, not a table: the data where the pointer points)
        if parts is not None and memory.program_rom(parts[0]) and isinstance(parts[1], ir.Op)                 and parts[1].name == "sext" and parts[1].args[0].bits <= 16 and parts[0] not in self.shifted:
            # a table read with a signed word the code adds to its address ("(A0,D0.w)"), which may be any offset:
            # the data at that many bytes into it, by its name
            name = self.tables[parts[0]].name if parts[0] in self.tables else self.data_names.get(parts[0])
            if name is not None:
                return f"TABLE{bits}({name}, {index_text(parts[1])})", False, False
        if parts is not None and parts[0] in self.shifted:
            start, distance = self.shifted[parts[0]]
            item = self.tables.get(start)
            index = memory.element_index(parts[1], bits)
            size = bits // 8
            if item is not None and item.bits == bits and index is not None and not distance % size:
                return f"{item.name}[{index_text(index)} - {distance // size}u]", item.signed, False
            if item is not None:
                return f"TABLE{bits}({item.name}, {index_text(parts[1])} - 0x{distance:X}u)", False, False
        return super().access(address, bits, index_text, indexed)

    def indexed_text(self, items, item, index):
        if memory.RAM_ELEMENT is not None and items is not self.tables and not item.indexed:
            item.indexed = True
            self.late.add(item.name)
        return super().indexed_text(items, item, index)

    def indexed_uses(self, text):
        """'text' with the plain uses of the variables that turned out arrays as their first element"""
        for name in sorted(self.late):
            text = re.sub(rf"\b{name}\b(?!\()", f"{name}(0)", text)
        return text

    def scalar(self, items, address, bits):
        if items is self.tables:
            item = items.get(address)
            if item is not None and item.bits != bits:
                return f"TABLE{bits}({item.name}, 0)"
            outer = self.inside(items, address)
            if outer is not None:
                return f"TABLE{bits}({outer.name}, 0x{address - outer.address:X}u)"
            found = self.element(items, address)
            if found is not None and (found[2] or found[0].bits != bits):
                return f"TABLE{bits}({found[0].name}, 0x{address - found[0].address:X}u)"
        return super().scalar(items, address, bits)


class Printer(emit.Printer):
    def operator(self, node):
        if node.name in ("carry_add", "overflow_add", "carry_sub", "overflow_sub"):
            width = node.args[0].bits
            values = ", ".join(self.exact(arg).text for arg in node.args)
            return cprint.Value(f"m68k_{node.name}{width}({values})", cprint.PRIMARY, True, "u32")
        return super().operator(node)

    def helper(self, name, args, bits):
        values = [self.exact(arg).text for arg in args]
        base, _, part = name.partition("_")
        if base in SHIFT_KINDS:
            function = {"": "m68k_shift", "cy": "m68k_shift_carry", "ov": "m68k_shift_overflow"}[part]
            text = f"{function}(M68K_{base.upper()}, {values[0]}, {values[1]}, {values[2]}, {args[0].bits})"
            return cprint.Value(text, cprint.PRIMARY, True, "u32")
        if base in ("divu", "divs", "abcd", "sbcd", "chk"):
            return cprint.Value(f"m68k_{name}({', '.join(values)})", cprint.PRIMARY, True, "u32")
        return super().helper(name, args, bits)


class Writer(emit.FunctionWriter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.printer = Printer(self)

    def effect(self, statement):
        name = statement.name
        if name == "frame_wait":
            return ["frame_wait();"]
        if name == "write_sr":
            return [f"write_status({self.exact(statement.args[0])});"]
        if name == "return_from_exception":
            return ["return_from_exception();"]
        if name == "stop":
            return [f"stop({self.exact(statement.args[0])});"]
        if name == "reset":
            return ["reset_devices();"]
        if name == "read":
            # a read of a device that does something when read, whose value nothing uses
            return [f"(void){self.exact(statement.args[0])};"]
        if name == "lost_stack":
            return [f"lost_stack(0x{statement.args[0].value:06X}u);"]
        return super().effect(statement)


class Emitter(emit.Emitter):
    """emit.Emitter with nothing of the V60 program's: the 68000 program's code addresses and tables"""

    def restores_current(self, entry):
        return None, None
