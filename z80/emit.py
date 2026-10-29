# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        emit.py
Description: writes the sound program's functions as C, through the decompiler's writer
Version:     0.1.0
Created:     13-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

The decompiler's FunctionWriter does the work (decompiler/emit.py); this gives it the Z80's memory and
effects. MEMORY_MAP is the Z80's for decompiler/memory.py and decompiler/pointers.py: the RAM it shares
with the main CPU (E000-FFFF) holds the variables and the structures sound_symbols.txt names, read and
written as lvalues over those bytes (SOUND_RAM8, VOICE(p)->field); the program ROM (0000-9FFF) the tables;
the banked page of the sound ROM (A000-BFFF) and the sound chip (C000-DFFF) are the board's, reached through
its bus by name (game_profile.SOUND_HARDWARE). Another board gives its own map in the profile: SOUND_RAM_AREA and
SOUND_ROM_AREA (TMNT: the Z80's own RAM at 8000-87FF, its program at 0000-7FFF, the chips at 9000-F000). A computed address no structure explains goes over the bus, z80_read8/16() and z80_write8/16()
(runtime/include/sound/support.h). The timing model's Effects (z80_block, z80_cycles), DI and EI, OUT and
the block moves print as calls of that header, the ports by the names sound_symbols.txt gives them.
"""
from decompiler import cprint
from decompiler import emit
from v60 import ir

SHARED_RAM = (0xE000, 0x10000)
PROGRAM_ROM = (0x0000, 0xA000)
BLOCK_FETCHES = True    # a block's z80_block() says its opcode fetches too, which count R up (the game's runtime
                        # keeps R: game_profile.SOUND_BLOCK_FETCHES)


def use_memory_map(names, arrays):
    """Makes the Z80's memory the one decompiler/memory.py and decompiler/pointers.py work on: 'names'
    (disassemble.Names) gives the structures, 'arrays' ({struct: (first, count)}) where they stand one
    after another."""
    import game_profile
    from decompiler import memory
    from decompiler import pointers
    global BLOCK_FETCHES, SHARED_RAM, PROGRAM_ROM
    BLOCK_FETCHES = getattr(game_profile, "SOUND_BLOCK_FETCHES", True)
    SHARED_RAM = getattr(game_profile, "SOUND_RAM_AREA", SHARED_RAM)
    PROGRAM_ROM = getattr(game_profile, "SOUND_ROM_AREA", PROGRAM_ROM)
    memory.WORK_RAM = SHARED_RAM
    memory.ROM_AREAS = (PROGRAM_ROM,)
    memory.ADDRESS_MASK = 0xFFFF
    memory.ADDRESS_DIGITS = 4
    memory.RAM_MACRO, memory.ROM_MACRO = "SOUND_RAM", "SOUND_ROM"
    memory.RAM_FUNCTION, memory.ROM_FUNCTION = "sound_ram_at", "sound_rom_at"
    memory.LOADED_TABLES = getattr(game_profile, "SOUND_LOADED_TABLES", False)
    memory.COUNTED_FROM_ONE = getattr(game_profile, "SOUND_COUNTED_FROM_ONE", False)
    memory.NAMES_BEFORE_TABLES = getattr(game_profile, "SOUND_NAMES_BEFORE_TABLES", 0)
    memory.PAGED = False
    memory.PLAYER_FIELDS = None
    memory.CANONICAL = lambda address: address
    memory.ROM_NAMES_FROM = 0x0100    # the restarts and the interrupt's code are below
    memory.VARIABLES = None
    memory.HARDWARE = list(game_profile.SOUND_HARDWARE)    # the board's: its banked window and its sound chip
    memory.BUS_ONLY = []
    memory.BYTE_INDEXES = True
    memory.NAMED_BASES = {}
    memory.NOTES = dict(game_profile.SOUND_NOTES)
    pointers.STRUCTS = dict(names.structs)
    pointers.PLAYERS = (0, 0, 0)
    pointers.VARIABLES = None
    pointers.CONTEXT_KINDS = {}
    pointers.OBJECT_REGISTERS = ()
    pointers.HANDLER_FIELD = None
    pointers.WORK_RAM = SHARED_RAM
    pointers.ADDRESS_MASK = 0xFFFF
    pointers.POINTER_BITS = 16
    pointers.PAGE_LINKS = False
    pointers.ROM_DATA = (0x0100, PROGRAM_ROM[1])
    pointers.ARRAYS = dict(arrays)
    pointers.INSTANCES = {}
    pointers.VARIABLE_BLOCKS = {}
    pointers.BASE_KINDS = {}
    pointers.PAGE_LINK_KINDS = {}
    pointers.NESTED = {}
    pointers.BELOW = {}
    pointers.OWN_REGISTERS = ()
    pointers.STRUCT_FUNCTION = "sound_ram_at"
    pointers.ROM_STRUCTS = set(names.rom_structs)    # records of the sound program's ROM: read only
    pointers.RUNS = set(names.rom_structs)           # ... one after another wherever a pointer starts
    pointers.ELEMENTS = dict(names.arrays)           # its values a pointer walks ("elements" lines)
    pointers.RECORD_TABLES = dict(names.record_tables)    # its tables of records, by name
    pointers.FIELD_ARRAYS = {key: (int(names.fields[key][1][1:]), count) for key, count in names.field_arrays.items()}
    pointers.ROM_FUNCTION = "sound_rom_at"
    pointers.CONSTANTS = True
    pointers.PAGED = True    # the sequence players, one per page from E900


class SoundNames(emit.Names):
    def memory(self, load):
        found = super().memory(load)
        if found is not None:
            return found
        # a computed address: over the board's bus
        return f"z80_read{load.bits}({self.writer.address(load.address)})", False


def fits8(node):
    """True if 'node' holds no more than 8 bits: a byte, a value cut or widened from one, a small number."""
    if isinstance(node, ir.Const):
        return node.value <= 0xFF
    if isinstance(node, ir.Op) and node.name in ("zext", "fits"):
        return getattr(node.args[0], "bits", 16) <= 8
    return getattr(node, "bits", 16) <= 8


class SoundPrinter(emit.Printer):
    def __init__(self, writer):
        super().__init__(writer)
        self.names = SoundNames(writer)
        self.addressing = False    # printing the address of a memory access (SoundWriter.address)

    def bitwise(self, node):
        name, args, bits = node.name, node.args, node.bits
        if name == "or" and bits == 16:
            for keep, low in (args, args[::-1]):
                if isinstance(keep, ir.Op) and keep.name == "and" and keep.args[1] == ir.Const(0xFF00, 16) \
                        and fits8(low) and (self.addressing or self.names.pointer(keep.args[0])):
                    # a pointer moved to another place of its 256-byte page (the MIDI queue's next byte,
                    # a field of the page's sequence player): IN_PAGE(pointer, low) in sound/support.h
                    return cprint.Value(f"IN_PAGE({self.any(keep.args[0]).text}, {self.any(low).text})",
                                        cprint.PRIMARY, True, "int")
        return super().bitwise(node)


class SoundWriter(emit.FunctionWriter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.printer = SoundPrinter(self)

    def statement(self, statement):
        if isinstance(statement, ir.Store):
            target = self.memory(statement.address, statement.bits, id(statement))
            if target is not None and target[2]:
                return [self.assignment(target[0], statement, target[1])]
            if target is not None:
                raise emit.EmitError(f"{self.function.name}: writes the program ROM")
            return [f"z80_write{statement.bits}({self.address(statement.address)}, {self.stored(statement.value, statement.bits)});"]
        return super().statement(statement)

    def address(self, node):
        """C for the address of a memory access over the bus (a pointer: SoundPrinter.bitwise)."""
        self.printer.addressing = True
        try:
            return self.exact(node)
        finally:
            self.printer.addressing = False

    def port(self, node):
        name = self.emitter.ports.get(node.value) if isinstance(node, ir.Const) else None
        return name or self.exact(node)

    def effect(self, statement):
        name = statement.name
        args = statement.args
        if name == "z80_block":
            if not BLOCK_FETCHES:
                return [f"z80_block({args[0].value});"]
            return [f"z80_block({args[0].value}, {args[1].value});"]
        if name == "refresh_read":
            return [f"{self.register_set(args[1].value)} = refresh_read({args[0].value});"]
        if name == "z80_cycles":
            return [f"z80_cycles({args[0].value});"]
        if name == "interrupts_off":
            return ["interrupts_off();"]
        if name == "interrupts_on":
            # EI as a block's last instruction: the interrupt waits for the block after the next start
            return ["interrupts_on_after_block();" if args[0].value else "interrupts_on();"]
        if name == "port_out":
            return [f"port_out({self.port(args[0])}, {self.exact(args[1])});"]
        if name == "move_block":
            source, destination, count, upward = (self.exact(arg) for arg in args)
            return [f"move_block({source}, {destination}, {count}, {upward});"]
        raise emit.EmitError(f"{self.function.name}: effect {name}")


class SoundEmitter(emit.Emitter):
    """emit.Emitter with nothing of the V60 program's: no context registers; the sound program's
    structures (pointers.Layouts) and ports."""

    def __init__(self, functions, signatures, names, memory_, always, layouts, ports):
        super().__init__(functions, signatures, names, (), set(), memory_, always, lambda address: 0)
        self.layouts = layouts
        self.ports = dict(ports)
        self.saved_locals = True
        self.reroll = True    # copy_sample_page's 256 LDI in a row
        self.flow = None

    def restores_current(self, entry):
        return None, None

    def port_declarations(self):
        return [f"#define {name} 0x{port:02X}u" for port, name in sorted(self.ports.items())]
