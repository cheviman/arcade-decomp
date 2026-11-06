# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        emit.py
Description: writes a Konami CPU program's functions as C, through the decompiler's writer
Version:     0.1.0
Created:     06-11-2026
Modified:    06-11-2026
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
    function being written (current_bank); a window address read from the fixed program, whose bank only the
    running program knows, has no name and goes through the bus. A table's NAME_ADDRESS is its CPU address (what
    the program holds as a value); the game loads it by its place (src/table.c's registry);
  - the board's devices (0000-3FFF: the palette, the K052109 and its RAM, the sprite RAM and the RAM at 3000, which
    1FC2 swaps in and out, and the ports) are reached through the bus by name (io_read8, io_write8).
Effects: frame_wait(WAIT) where the program finds nothing to do until its next frame, take_interrupts() after it and
after what lets an interrupt in, firq_point() where the IRQ handler takes the sprite DMA's FIRQ, set_lines() (SETLN:
the bank), the interrupt mask's enable_irq()/disable_irq()/enable_firq()/disable_firq()/restore_mask(), lost_stack()
(a function that runs on a stack the code that reaches it pushed, which no path the program takes reaches as one).
"""
from decompiler import cprint
from decompiler import emit
from decompiler import memory
from decompiler import pointers
from decompiler import structure
from v60 import ir

import game_profile

from . import analysis

WORK_RAM_AREA = getattr(game_profile, "WORK_RAM_AREA", (0x4000, 0x6000))
PLACES = 0x100000           # the key the decompiler names the program's data by: PLACES + its place
OUTSIDE = 0x200000          # the key of a window address whose bank is not known (no name)
PROGRAM_SIZE = analysis.CODE_END
current_bank = None         # the bank of the function being written (None: the fixed program's)
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
    return OUTSIDE + address


def cpu_of_key(key):
    """The CPU address of a data key (PLACES + place)"""
    return analysis.cpu_of(key - PLACES)


def ram_element(name, index, bits):
    return f"{name}({index})"


def use_memory_map(names):
    """Makes the 053248's memory the one decompiler/memory.py and decompiler/pointers.py work on: 'names'
    (disassemble.Names, its program's addresses already keyed by canonical()) gives the structures"""
    memory.WORK_RAM = WORK_RAM_AREA
    memory.ROM_AREAS = ((analysis.WINDOW, 0x10000), (PLACES, PLACES + PROGRAM_SIZE))
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
    emit.OWN_OBJECT_REGISTERS = ()
    emit.BOARD_MEMORIES = {WORK_RAM_AREA[0]}
    emit.NUMBERS_BY_FIELD = False
    emit.PARAMS_BY_USE = False
    emit.NAMES_BY_STORES = False
    emit.PARAMS_FROM_CALLERS = False
    emit.NAMES_BY_COMPARISONS = False
    emit.UNREACHABLE_ENDS = True    # (a function whose every way out of its last block returns says its end is never reached)
    emit.NAMED_CODE_CASES = True
    emit.INSTANCE_FAMILIES_BY_FUNCTION = {}
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
    pointers.ARRAYS = {}
    pointers.INSTANCES = {}
    pointers.INSTANCE_FAMILIES = {}
    pointers.VARIABLE_BLOCKS = {}
    pointers.BASE_KINDS = {}
    pointers.PAGE_LINK_KINDS = {}
    pointers.NESTED = {}
    pointers.BELOW = {}
    pointers.OWN_REGISTERS = ()
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
    pointers.ELEMENT_READS = False
    pointers.NESTED_FAMILIES = False
    pointers.SIGNED_OFFSETS = False


class Memory(memory.Memory):
    """The decompiler's memory names, with the tables of the program's data keyed by canonical(): an unnamed table
    is named after its place, and its NAME_ADDRESS is its CPU address"""

    def __init__(self, names, accesses, data_names=None, places=None):
        self.data_names = dict(data_names or {})
        self.banks_of = dict(places or {})    # {key: bank or None} of the tables
        super().__init__(names, accesses, data_names)

    def add_unnamed(self, items, widths, name_of):
        if items is self.tables:
            def name_of(address):
                return self.data_names.get(address, f"rom_{address - PLACES:06X}")
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
        global current_bank
        current_bank = analysis.bank_of(self.function.entry) if isinstance(self.function.entry, int) else None
        return super().write()

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
        text = self.exact(subject)
        if signed:
            text = f"(int8_t){text}" if subject.bits == 8 else f"(int16_t){text}"
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

    def restores_current(self, entry):
        return None, None
