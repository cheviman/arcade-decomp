# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        versions.py
Description: what the versions matcher (tools/core/versions.py) needs of the Konami CPU: its places (the banks of the
             program region and the fixed program), the operands that name the program and the instructions' shapes
Version:     0.1.0
Created:     25-11-2026
Modified:    25-11-2026
Author:      Sebastian Toyos

Usage: python tools/match_version.py VERSION_ROM [--base BASE_ROM] [--out DIR]   (the game's shim; the matcher's main)

A place is an offset of the 512 KB program region: the banks seen at 6000-7FFF, and the fixed program at 8000-FFFF
(the region's last 32 KB). The analysis is konami_cpu/analysis.py's (the banks followed); a place in symbols.txt is
six hex digits, an address that is no place (the work RAM, the board's) four.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import game_profile  # noqa: E402  (the game's: its root, its ROM set)
import disassemble  # noqa: E402  (symbols.txt, in the format all the games share)
from konami_cpu import analysis as konami_analysis  # noqa: E402
from konami_cpu import disassembler as dis  # noqa: E402

rom = game_profile.rom
FIXED_START = rom.FIXED_PROGRAM[0]
BANK_SIZE = rom.PROGRAM_BANK_SIZE
PROGRAM_START = 0x6000          # the lowest CPU address the program is seen at (the bank's window)
NAMING = ("ldx", "ldy", "ldu", "lds", "cmpx", "cmpy", "cmpu")


class KonamiCpu:
    TOKEN = re.compile(r"(?<![0-9A-Za-z_])[0-9A-F]{6}(?![0-9A-Za-z_])")
    FOUR = re.compile(r"(?<![0-9A-Za-z_])[0-9A-F]{4}(?![0-9A-Za-z_])")
    CODE_KINDS = ("func", "code", "file")
    DATA_KINDS = ("data", "table", "fill")
    FLOW_JUMP = dis.FLOW_JUMP
    FLOW_RETURN = dis.FLOW_RETURN

    @staticmethod
    def analyse(image, symbols):
        analysis = konami_analysis.Analysis(image, symbols)
        analysis.run()
        return analysis

    def decode(self, analysis, place):
        """The instruction at a place its analysis did not reach"""
        return analysis.decode(self.bank_of(place), self.cpu_of(place))

    @staticmethod
    def functions(analysis):
        return analysis.functions

    @staticmethod
    def load_symbols(path):
        return disassemble.load_symbols(path)

    @staticmethod
    def is_place(value):
        """Whether a TOKEN of a line is a place (all six digits ones are)"""
        return True

    @staticmethod
    def place_text(place):
        return format(place, "06X")

    @staticmethod
    def area_of(place):
        """The bank of a place, the fixed program as one area"""
        return "fixed" if place >= FIXED_START else place // BANK_SIZE

    @staticmethod
    def names_program(instruction, operand):
        """Whether an operand's value is an address of the program the CPU sees (one that moves between versions)"""
        if operand.mode == "address":
            return True
        if operand.mode == "immediate":
            return operand.size == 2 and operand.value >= PROGRAM_START and instruction.mnemonic in NAMING
        if operand.mode == "indexed" and operand.kind == "extended":
            return operand.value >= PROGRAM_START
        return operand.mode == "indexed" and operand.target is not None

    def shape(self, instruction):
        """An instruction without the operands that name the program: what two versions of the same code share"""
        parts = [instruction.mnemonic, instruction.condition]
        for operand in instruction.operands:
            if self.names_program(instruction, operand):
                parts.append((operand.mode, operand.kind, "place"))
            else:
                parts.append((operand.mode, operand.kind, operand.reg, operand.accumulator, operand.indirect,
                              operand.value, operand.disp, operand.size, operand.registers, operand.source,
                              operand.destination, operand.exchange))
        return tuple(parts)

    @staticmethod
    def kind(instruction):
        """An instruction's kind alone: the same in two versions that give it other numbers"""
        return (instruction.mnemonic, instruction.condition,
                tuple((operand.mode, operand.kind, operand.reg, operand.accumulator, operand.indirect)
                      for operand in instruction.operands))

    def kept(self, instruction):
        """Which of an instruction's bytes do not move between versions: all but the operands that name the program
        (an address of two bytes at the instruction's end, a branch's offset of one or two)"""
        keep = [True] * instruction.length
        for operand in instruction.operands:
            if self.names_program(instruction, operand):
                width = 2 if instruction.length >= 3 and operand.mode != "indexed" or operand.kind == "extended" \
                    else 1
                for index in range(instruction.length - width, instruction.length):
                    keep[index] = False
        return keep

    def target_place(self, place, instruction):
        """The place a branch, jump or call with a fixed target goes to (in its own bank from a bank's code), or
        None"""
        target = instruction.target
        if target is None or target < PROGRAM_START:
            return None
        if target >= 0x8000:
            return FIXED_START + target - 0x8000
        bank = self.bank_of(place)
        return None if bank is None else bank * BANK_SIZE + target - PROGRAM_START

    @staticmethod
    def place_from(bank, cpu):
        """The place of a CPU address with a bank in the window (None: the fixed program only)"""
        if cpu >= 0x8000:
            return FIXED_START + cpu - 0x8000
        if bank is None or cpu < PROGRAM_START:
            return None
        return bank * BANK_SIZE + cpu - PROGRAM_START

    @staticmethod
    def operand_value(operand):
        return operand.target if operand.mode == "indexed" and operand.target is not None else operand.value

    @staticmethod
    def cpu_of(place):
        return 0x8000 + place - FIXED_START if place >= FIXED_START else PROGRAM_START + place % BANK_SIZE

    @staticmethod
    def bank_of(place):
        return None if place >= FIXED_START else place // BANK_SIZE


CPU = KonamiCpu()


if __name__ == "__main__":
    import versions  # noqa: E402  (the matcher, tools/core/versions.py)
    versions.main()
