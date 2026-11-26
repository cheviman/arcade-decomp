# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        versions.py
Description: what the versions matcher (tools/core/versions.py) needs of the Z80: its places (the addresses of the
             sound program), the operands that name the program and the instructions' shapes
Version:     0.1.0
Created:     25-11-2026
Modified:    25-11-2026
Author:      Sebastian Toyos

Usage: python tools/match_version.py VERSION_ROM [--base BASE_ROM] [--out DIR]   (the game's shim; the matcher's main)

A place is an address of the sound program (0000-7FFF: game_profile.SOUND_CODE_END; the banks at 8000-BFFF hold only
data, which no symbols line names). The program has no code banks: every place is in the one area 'program'. The
analysis is z80/analysis.py's; a place in sound_symbols.txt is four hex digits, and so is an address of the Z80's RAM
or of the board's (F000 on), which no place is (is_place) and whose lines are of kinds neither code nor data.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import game_profile  # noqa: E402  (the game's: its root, its ROM set, its sound program's settings)
from z80 import analysis as z80_analysis  # noqa: E402
from z80 import disassembler as dis  # noqa: E402

SOUND_CODE_END = getattr(game_profile, "SOUND_CODE_END", 0x8000)
PAIRS = ("hl", "de", "bc", "ix", "iy", "sp")   # (the registers an address of the program is loaded in)
VIEW = 0x10000                  # what the Z80 sees: the region's first 64 KB


class Z80Cpu:
    TOKEN = re.compile(r"(?<![0-9A-Za-z_])[0-9A-F]{4}(?![0-9A-Za-z_])")
    FOUR = re.compile(r"(?!)")  # (an address that is no place starts its line as a place does: kept by its kind)
    CODE_KINDS = ("func", "code", "file", "loop", "via")
    DATA_KINDS = ("data", "table", "jumps", "fill")
    CODE_TABLE_KINDS = ("jumps",)    # (tables of code the analysis follows only by their lines: the second match's)
    FLOW_JUMP = dis.FLOW_JUMP
    FLOW_RETURN = dis.FLOW_RETURN

    @staticmethod
    def analyse(image, symbols):
        """The sound program from the region's first 64 KB (a shorter one followed by zeros, as load_program
        does), analysed: its instructions, functions and the places each instruction names"""
        image = bytes(image[:VIEW]) + bytes(max(0, VIEW - len(image)))
        # (the version's own symbols are few until the carry gives it the rest: its tables of code have no length)
        z80_analysis.LENIENT = True
        try:
            program = z80_analysis.Program(image, symbols)
        finally:
            z80_analysis.LENIENT = False
        instructions = program.analysis.instructions
        named_by = {}
        for address, instruction in instructions.items():
            places = {operand.value for operand in instruction.operands
                      if operand.mode != "address" and Z80Cpu.names_program(instruction, operand)}
            if places:
                named_by[address] = places
        return type("Z80Analysis", (), {"instructions": instructions, "functions": program.entries,
                                        "mem": program.mem, "named_by": named_by})()

    @staticmethod
    def decode(analysis, place):
        """The instruction at a place its analysis did not reach"""
        return dis.decode(analysis.mem, place)

    @staticmethod
    def functions(analysis):
        return analysis.functions

    @staticmethod
    def load_symbols(path):
        return z80_analysis.load_symbols(path)

    @staticmethod
    def is_place(value):
        """Whether a TOKEN of a line is a place (an address of the program, not of the RAM or the board)"""
        return value < SOUND_CODE_END

    @staticmethod
    def place_text(place):
        return format(place, "04X")

    @staticmethod
    def area_of(place):
        """The program has no code banks: every place is in the one area"""
        return "program"

    @staticmethod
    def names_program(instruction, operand):
        """Whether an operand's value is an address of the program (one that moves between versions): a jump's or a
        call's target (not RST's, a fixed vector), memory at a fixed address of the program, an address loaded in a
        register pair (LD HL,nnnn)"""
        if operand.mode == "address":
            return instruction.mnemonic != "rst"
        if operand.mode == "direct":
            return operand.value < SOUND_CODE_END
        destination = instruction.operands[0] if instruction.operands else None
        return operand.mode == "immediate" and instruction.mnemonic == "ld" and destination is not None \
            and destination.mode == "reg" and destination.reg in PAIRS and operand.value < SOUND_CODE_END

    def shape(self, instruction):
        """An instruction without the operands that name the program: what two versions of the same code share"""
        parts = [instruction.mnemonic, instruction.condition]
        for operand in instruction.operands:
            if self.names_program(instruction, operand):
                parts.append((operand.mode, "place"))
            else:
                parts.append((operand.mode, operand.reg, operand.disp, operand.value))
        return tuple(parts)

    @staticmethod
    def kind(instruction):
        """An instruction's kind alone: the same in two versions that give it other numbers (a record's field too)"""
        return (instruction.mnemonic, instruction.condition,
                tuple((operand.mode, operand.reg) for operand in instruction.operands))

    def kept(self, instruction):
        """Which of an instruction's bytes do not move between versions: all but the operands that name the program
        (a relative jump's offset, its last byte; an address, its last two)"""
        keep = [True] * instruction.length
        for operand in instruction.operands:
            if self.names_program(instruction, operand):
                width = 1 if instruction.mnemonic in ("jr", "djnz") else 2
                for index in range(instruction.length - width, instruction.length):
                    keep[index] = False
        return keep

    @staticmethod
    def target_place(place, instruction):
        """The place a jump or call with a fixed target goes to, or None"""
        target = instruction.target
        return target if target is not None and target < SOUND_CODE_END else None

    @staticmethod
    def place_from(bank, cpu):
        """The place of a Z80 address (no banks of code: the same number), None past the program"""
        return cpu if cpu < SOUND_CODE_END else None

    @staticmethod
    def operand_value(operand):
        return operand.value

    @staticmethod
    def cpu_of(place):
        return place

    @staticmethod
    def bank_of(place):
        return None


CPU = Z80Cpu()


if __name__ == "__main__":
    import versions  # noqa: E402  (the matcher, tools/core/versions.py)
    versions.main()
