# license:BSD-3-Clause
# copyright-holders:Nathan Woods,Sean Riddle,Tim Lindner,Sebastian Toyos
"""
Name:        disassembler.py
Description: Konami CPU (the 052001 and the 053248: a 6809 with Konami's opcodes) instruction decoder with
             structured operands
Version:     0.1.0
Created:     05-11-2026
Modified:    05-11-2026
Author:      Sebastian Toyos

Ported from MAME src/devices/cpu/m6809/6x09dasm.cpp (konami_disassembler, on m6x09_base_disassembler). The
text of every instruction matches MAME's output exactly, spacing included:
konami/reference/compare_konami_with_mame.py runs MAME's own disassembler (oracle_konami) and diffs both, at
every offset of every program bank as the CPU sees it and of an image of every opcode with every postbyte. On top
of the text, every instruction carries its decoded operands and its control flow, which is what the tools that
analyze the program work with.

Konami's table has no pages (no $10/$11 prefixes): an opcode is one byte, and one MAME does not know comes out as
'FCB    $xx', one byte long. Its addressing modes are immediate (8 or 16 bits), relative (8 or 16 bits) and one
indexed form, a postbyte after the opcode, which covers the 6809's direct and extended modes too:
    postbyte bit 7 clear: bits 4-6 the register (2 X, 3 Y, 5 U, 6 S, 7 PC; MAME prints '?' for 0, 1, 4), bit 3
      indirect, bits 0-2: 0 ,R+  1 ,R++  2 ,-R  3 ,--R  4 a signed byte offset  5 a signed word offset  6 ,R
      7 an absolute address (the register unused)
    postbyte bit 7 set: bit 3 indirect, bits 0-2: 0 A,R  1 B,R  4 a direct page address (<$xx)  7 D,R
      (the others print '?,R')
Konami's mnemonics are MAME's: PUSHS/PULLS for PSHS/PULS, SETLN (the CPU's output lines), the shifts by a
count (LSRDI...), the word ones (LSRW, CLRW...), DBJNZ and DXJNZ, BMOVE, MOVE, BSET, BSETW, LMUL, DIV.

Instruction.text is MAME's text; mnemonic is its name in lower case without the operands MAME writes into some
names ('div' for 'DIV    X,B'), and the conditional branches are 'bcc' and 'lbcc' with the condition apart
(hi, cc, ne, vc, pl, ge, gt, ls, cs, eq, vs, mi, lt, le). Targets are 16-bit CPU addresses: the program's banks
are the caller's (the memory object gives the bytes of the bank the CPU sees).

Control flow:
- BRA and LBRA are jumps, BSR and LBSR calls, with their target; BRN and LBRN never branch (normal flow); the
  conditional ones, DBJNZ and DXJNZ (B or X counted down, a branch while not zero) are branches with their
  target.
- JMP is a jump and JSR a call. They carry a target only when the indexed operand gives one without a register's
  value or memory: an absolute address, or a PC-relative offset (the PC past the instruction plus the offset).
  Indirect, register, accumulator and direct page forms have none (the direct page is the DP register's).
- RTS and RTI are returns, and so are PULLS and PULLU when they pull the PC.
- EXG and TFR that write the PC are jumps with no target. The CPU tells the two apart by the postbyte's bit 7
  (set: a transfer from the low register to the high one; clear: an exchange), whatever the opcode, which only
  decides what MAME prints: a transfer writes the PC when the high register is it, an exchange when either is.
- 'FCB' lines are invalid. Everything else is normal (SETLN included: the caller knows what its lines do).

Operand modes (Operand.mode and the fields it uses):
    immediate   #$xx or #$xxxx ('value', 'size' 1 or 2)
    address     the target of a relative branch ('value')
    indexed     the postbyte's forms ('kind': 'postinc', 'postinc2', 'predec', 'predec2', 'offset', 'register',
                'extended', 'direct', 'accumulator', 'invalid'; 'reg' the index register, '?' where MAME prints
                it so; 'disp' the signed offset; 'accumulator' 'A', 'B' or 'D'; 'value' the address of an extended
                or direct form (direct: the low byte); 'indirect' for the bracketed forms; 'target' the address a
                PC-relative offset reaches)
    reglist     PUSHS, PUSHU, PULLS, PULLU ('registers': the names in MAME's order)
    registers   EXG and TFR ('source', 'destination' as the CPU takes them; 'exchange' True when bit 7 is clear)
    reg         a register MAME writes into the name (DIV X,B, BMOVE Y,X,U...) ('reg')
"""
from dataclasses import dataclass, field

FLOW_NORMAL = "normal"
FLOW_JUMP = "jump"            # unconditional, never falls through
FLOW_BRANCH = "branch"        # falls through or goes to the target
FLOW_CALL = "call"            # goes to the target and comes back to the next instruction
FLOW_RETURN = "return"
FLOW_INVALID = "invalid"

INH, PSHS, PSHU, PULS, PULU, IND, REL, LREL, IMM, IMM_RR = range(10)

# MAME's konami_opcodes: opcode -> (operand length, name, mode)
OPCODES = {
    0x08: (1, "LEAX", IND), 0x09: (1, "LEAY", IND), 0x0A: (1, "LEAU", IND), 0x0B: (1, "LEAS", IND),
    0x0C: (1, "PUSHS", PSHS), 0x0D: (1, "PUSHU", PSHU), 0x0E: (1, "PULLS", PULS), 0x0F: (1, "PULLU", PULU),

    0x10: (1, "LDA", IMM), 0x11: (1, "LDB", IMM), 0x12: (1, "LDA", IND), 0x13: (1, "LDB", IND),
    0x14: (1, "ADDA", IMM), 0x15: (1, "ADDB", IMM), 0x16: (1, "ADDA", IND), 0x17: (1, "ADDB", IND),
    0x18: (1, "ADCA", IMM), 0x19: (1, "ADCB", IMM), 0x1A: (1, "ADCA", IND), 0x1B: (1, "ADCB", IND),
    0x1C: (1, "SUBA", IMM), 0x1D: (1, "SUBB", IMM), 0x1E: (1, "SUBA", IND), 0x1F: (1, "SUBB", IND),

    0x20: (1, "SBCA", IMM), 0x21: (1, "SBCB", IMM), 0x22: (1, "SBCA", IND), 0x23: (1, "SBCB", IND),
    0x24: (1, "ANDA", IMM), 0x25: (1, "ANDB", IMM), 0x26: (1, "ANDA", IND), 0x27: (1, "ANDB", IND),
    0x28: (1, "BITA", IMM), 0x29: (1, "BITB", IMM), 0x2A: (1, "BITA", IND), 0x2B: (1, "BITB", IND),
    0x2C: (1, "EORA", IMM), 0x2D: (1, "EORB", IMM), 0x2E: (1, "EORA", IND), 0x2F: (1, "EORB", IND),

    0x30: (1, "ORA", IMM), 0x31: (1, "ORB", IMM), 0x32: (1, "ORA", IND), 0x33: (1, "ORB", IND),
    0x34: (1, "CMPA", IMM), 0x35: (1, "CMPB", IMM), 0x36: (1, "CMPA", IND), 0x37: (1, "CMPB", IND),
    0x38: (1, "SETLN", IMM), 0x39: (1, "SETLN", IND), 0x3A: (1, "STA", IND), 0x3B: (1, "STB", IND),
    0x3C: (1, "ANDCC", IMM), 0x3D: (1, "ORCC", IMM), 0x3E: (1, "EXG", IMM_RR), 0x3F: (1, "TFR", IMM_RR),

    0x40: (2, "LDD", IMM), 0x41: (1, "LDD", IND), 0x42: (2, "LDX", IMM), 0x43: (1, "LDX", IND),
    0x44: (2, "LDY", IMM), 0x45: (1, "LDY", IND), 0x46: (2, "LDU", IMM), 0x47: (1, "LDU", IND),
    0x48: (2, "LDS", IMM), 0x49: (1, "LDS", IND), 0x4A: (2, "CMPD", IMM), 0x4B: (1, "CMPD", IND),
    0x4C: (2, "CMPX", IMM), 0x4D: (1, "CMPX", IND), 0x4E: (2, "CMPY", IMM), 0x4F: (1, "CMPY", IND),

    0x50: (2, "CMPU", IMM), 0x51: (1, "CMPU", IND), 0x52: (2, "CMPS", IMM), 0x53: (1, "CMPS", IND),
    0x54: (2, "ADDD", IMM), 0x55: (1, "ADDD", IND), 0x56: (2, "SUBD", IMM), 0x57: (1, "SUBD", IND),
    0x58: (1, "STD", IND), 0x59: (1, "STX", IND), 0x5A: (1, "STY", IND), 0x5B: (1, "STU", IND),
    0x5C: (1, "STS", IND),

    0x60: (1, "BRA", REL), 0x61: (1, "BHI", REL), 0x62: (1, "BCC", REL), 0x63: (1, "BNE", REL),
    0x64: (1, "BVC", REL), 0x65: (1, "BPL", REL), 0x66: (1, "BGE", REL), 0x67: (1, "BGT", REL),
    0x68: (2, "LBRA", LREL), 0x69: (2, "LBHI", LREL), 0x6A: (2, "LBCC", LREL), 0x6B: (2, "LBNE", LREL),
    0x6C: (2, "LBVC", LREL), 0x6D: (2, "LBPL", LREL), 0x6E: (2, "LBGE", LREL), 0x6F: (2, "LBGT", LREL),

    0x70: (1, "BRN", REL), 0x71: (1, "BLS", REL), 0x72: (1, "BCS", REL), 0x73: (1, "BEQ", REL),
    0x74: (1, "BVS", REL), 0x75: (1, "BMI", REL), 0x76: (1, "BLT", REL), 0x77: (1, "BLE", REL),
    0x78: (2, "LBRN", LREL), 0x79: (2, "LBLS", LREL), 0x7A: (2, "LBCS", LREL), 0x7B: (2, "LBEQ", LREL),
    0x7C: (2, "LBVS", LREL), 0x7D: (2, "LBMI", LREL), 0x7E: (2, "LBLT", LREL), 0x7F: (2, "LBLE", LREL),

    0x80: (0, "CLRA", INH), 0x81: (0, "CLRB", INH), 0x82: (1, "CLR", IND), 0x83: (0, "COMA", INH),
    0x84: (0, "COMB", INH), 0x85: (1, "COM", IND), 0x86: (0, "NEGA", INH), 0x87: (0, "NEGB", INH),
    0x88: (1, "NEG", IND), 0x89: (0, "INCA", INH), 0x8A: (0, "INCB", INH), 0x8B: (1, "INC", IND),
    0x8C: (0, "DECA", INH), 0x8D: (0, "DECB", INH), 0x8E: (1, "DEC", IND), 0x8F: (0, "RTS", INH),

    0x90: (0, "TSTA", INH), 0x91: (0, "TSTB", INH), 0x92: (1, "TST", IND), 0x93: (0, "LSRA", INH),
    0x94: (0, "LSRB", INH), 0x95: (1, "LSR", IND), 0x96: (0, "RORA", INH), 0x97: (0, "RORB", INH),
    0x98: (1, "ROR", IND), 0x99: (0, "ASRA", INH), 0x9A: (0, "ASRB", INH), 0x9B: (1, "ASR", IND),
    0x9C: (0, "ASLA", INH), 0x9D: (0, "ASLB", INH), 0x9E: (1, "ASL", IND), 0x9F: (0, "RTI", INH),

    0xA0: (0, "ROLA", INH), 0xA1: (0, "ROLB", INH), 0xA2: (1, "ROL", IND), 0xA3: (1, "LSRW", IND),
    0xA4: (1, "RORW", IND), 0xA5: (1, "ASRW", IND), 0xA6: (1, "ASLW", IND), 0xA7: (1, "ROLW", IND),
    0xA8: (1, "JMP", IND), 0xA9: (1, "JSR", IND), 0xAA: (1, "BSR", REL), 0xAB: (2, "LBSR", LREL),
    0xAC: (1, "DBJNZ", REL), 0xAD: (1, "DXJNZ", REL), 0xAE: (0, "NOP", INH),

    0xB0: (0, "ABX", INH), 0xB1: (0, "DAA", INH), 0xB2: (0, "SEX", INH), 0xB3: (0, "MUL", INH),
    0xB4: (0, "LMUL", INH), 0xB5: (0, "DIV    X,B", INH), 0xB6: (0, "BMOVE  Y,X,U", INH),
    0xB7: (0, "MOVE   Y,X,U", INH), 0xB8: (1, "LSRDI", IMM), 0xB9: (1, "LSRWA", IND), 0xBA: (1, "RORDI", IMM),
    0xBB: (1, "RORWA", IND), 0xBC: (1, "ASRDI", IMM), 0xBD: (1, "ASRWA", IND), 0xBE: (1, "ASLDI", IMM),
    0xBF: (1, "ASLWA", IND),

    0xC0: (1, "ROLDI", IMM), 0xC1: (1, "ROLWA", IND), 0xC2: (0, "CLRD", INH), 0xC3: (1, "CLRW", IND),
    0xC4: (0, "NEGD", INH), 0xC5: (1, "NEGW", IND), 0xC6: (0, "INCD", INH), 0xC7: (1, "INCW", IND),
    0xC8: (0, "DECD", INH), 0xC9: (1, "DECW", IND), 0xCA: (0, "TSTD", INH), 0xCB: (1, "TSTW", IND),
    0xCC: (0, "ABSA", INH), 0xCD: (0, "ABSB", INH), 0xCE: (0, "ABSD", INH), 0xCF: (0, "BSET   A,X,U", INH),

    0xD0: (0, "BSETW  D,X,U", INH),
}

CONDITIONS = {"HI", "CC", "NE", "VC", "PL", "GE", "GT", "LS", "CS", "EQ", "VS", "MI", "LT", "LE"}
INDEX_REGISTERS = ("?", "?", "X", "Y", "?", "U", "S", "PC")
EXCHANGE_REGISTERS = ("A", "B", "X", "Y", "DP", "U", "S", "PC")


@dataclass(slots=True)
class Operand:
    """A decoded operand; the module docstring lists the modes and the fields each one uses."""
    mode: str
    text: str
    kind: str = ""
    reg: str = ""
    disp: int = 0
    accumulator: str = ""
    value: int = 0
    size: int = 0
    indirect: bool = False
    target: int | None = None
    registers: tuple = ()
    source: str = ""
    destination: str = ""
    exchange: bool = False


@dataclass(slots=True)
class Instruction:
    address: int
    length: int
    mnemonic: str
    text: str
    flow: str
    operands: list = field(default_factory=list)
    target: int | None = None       # fixed target of a jump, branch or call
    condition: str | None = None    # bcc and lbcc


class FlatMemory:
    """Reads from a byte block as the CPU's 64 KB, and as oracle_konami's buffer reads it: addresses wrapped
    to 16 bits, $FF past the end of the data."""

    def __init__(self, data):
        self.data = bytes(data)
        self.size = len(self.data)

    def r8(self, address):
        address &= 0xFFFF
        return self.data[address] if address < self.size else 0xFF

    def r16(self, address):
        return (self.r8(address) << 8) | self.r8(address + 1)


def _s8(value):
    return value - 0x100 if value & 0x80 else value


def _s16(value):
    return value - 0x10000 if value & 0x8000 else value


def _indexed(mem, mode, p):
    """konami_disassembler::indexed: the postbyte 'mode', its extra bytes from 'p' on; (operand, p after them)"""
    reg = INDEX_REGISTERS[(mode >> 4) & 7]
    kind_bits = mode & 0x0F
    indirect = bool(kind_bits & 8)
    operand = Operand("indexed", "", reg=reg, indirect=indirect)
    if mode & 0x80:
        kind = kind_bits & 7
        if kind in (0, 1, 7):
            accumulator = "ABD"[(0, 1, 7).index(kind)]
            operand.kind, operand.accumulator, inner = "accumulator", accumulator, f"{accumulator},{reg}"
        elif kind == 4:
            value = mem.r8(p)
            p += 1
            operand.kind, operand.value, inner = "direct", value, f"<${value:02X}"
        else:
            operand.kind, inner = "invalid", f"?,{reg}"
    else:
        kind = kind_bits & 7
        if kind == 0:
            operand.kind, inner = "postinc", f",{reg}+"
        elif kind == 1:
            operand.kind, inner = "postinc2", f",{reg}++"
        elif kind == 2:
            operand.kind, inner = "predec", f",-{reg}"
        elif kind == 3:
            operand.kind, inner = "predec2", f",--{reg}"
        elif kind == 4:
            value = mem.r8(p)
            p += 1
            disp = _s8(value)
            operand.kind, operand.disp = "offset", disp
            inner = f"#$-{0x100 - value:02X},{reg}" if value & 0x80 else f"#${value:02X},{reg}"
        elif kind == 5:
            value = mem.r16(p)
            p += 2
            disp = _s16(value)
            operand.kind, operand.disp = "offset", disp
            inner = f"#$-{0x10000 - value:04X},{reg}" if value & 0x8000 else f"#${value:04X},{reg}"
        elif kind == 6:
            operand.kind, inner = "register", f",{reg}"
        else:
            value = mem.r16(p)
            p += 2
            operand.kind, operand.value, operand.reg, inner = "extended", value, "", f"${value:04X}"
    operand.text = f"[{inner}]" if indirect else inner
    if operand.kind == "offset" and reg == "PC":
        operand.target = (p + operand.disp) & 0xFFFF
    return operand, p


def _registers(pb, pull, user):
    """PSHS/PSHU (pull False) and PULS/PULU: the registers of the postbyte, in MAME's order"""
    other = "S" if user else "U"
    if not pull:
        names = []
        if pb & 0x80: names.append("PC")
        if pb & 0x40: names.append(other)
        if pb & 0x20: names.append("Y")
        if pb & 0x10: names.append("X")
        if pb & 0x08: names.append("DP")
        if pb & 0x04: names.append("D" if pb & 0x02 else "B")
        elif pb & 0x02: names.append("A")
        if pb & 0x01: names.append("CC")
        return names
    names = []
    if pb & 0x01: names.append("CC")
    if pb & 0x02: names.append("D" if pb & 0x04 else "A")
    elif pb & 0x04: names.append("B")
    if pb & 0x08: names.append("DP")
    if pb & 0x10: names.append("X")
    if pb & 0x20: names.append("Y")
    if pb & 0x40: names.append(other)
    if pb & 0x80: names.append("PC")
    return names


def decode(mem, address):
    """Decode the instruction at 'address'. Never raises: an opcode MAME does not know comes back as FLOW_INVALID.

    The bytes are read from mem (r8, r16 big endian) at address, address + 1 and so on, unmasked: the memory
    object maps them (FlatMemory wraps them to 16 bits, as the oracle does)."""
    opcode = mem.r8(address)
    entry = OPCODES.get(opcode)
    if entry is None:
        text = f"{'FCB':<7}${opcode:02X}"
        return Instruction(address, 1, "fcb", text, FLOW_INVALID,
                           [Operand("immediate", f"${opcode:02X}", value=opcode, size=1)])
    length, name, mode = entry
    ppc = address + 1
    p = ppc + length
    words = name.split()
    mnemonic = words[0].lower()
    operands = []
    flow, target, condition = FLOW_NORMAL, None, None

    if mode == INH:
        text = name
        if len(words) > 1:
            operands = [Operand("reg", reg, reg=reg) for reg in words[1].split(",")]
        if mnemonic in ("rts", "rti"):
            flow = FLOW_RETURN
        return Instruction(address, p - address, mnemonic, text, flow, operands)

    text = f"{name:<7}"
    if mode in (PSHS, PSHU, PULS, PULU):
        pb = mem.r8(ppc)
        names = _registers(pb, mode in (PULS, PULU), mode in (PSHU, PULU))
        operand_text = ",".join(names)
        operands = [Operand("reglist", operand_text, registers=tuple(names), value=pb)]
        if mode in (PULS, PULU) and pb & 0x80:
            flow = FLOW_RETURN
    elif mode in (REL, LREL):
        offset = _s8(mem.r8(ppc)) if mode == REL else _s16(mem.r16(ppc))
        target = (p + offset) & 0xFFFF
        operand_text = f"${target:04X}"
        operands = [Operand("address", operand_text, value=target)]
        base = words[0][1:] if words[0].startswith("L") else words[0]
        if base == "BRA":
            flow = FLOW_JUMP
        elif base == "BSR":
            flow = FLOW_CALL
        elif base == "BRN":
            flow, target = FLOW_NORMAL, None
        elif base in ("DBJNZ", "DXJNZ"):
            flow = FLOW_BRANCH
        else:
            flow, condition = FLOW_BRANCH, base[1:].lower()
            mnemonic = "lbcc" if words[0].startswith("L") else "bcc"
    elif mode == IND:
        operand, p = _indexed(mem, mem.r8(ppc), p)
        operand_text = operand.text
        operands = [operand]
        if mnemonic in ("jmp", "jsr"):
            flow = FLOW_JUMP if mnemonic == "jmp" else FLOW_CALL
            if not operand.indirect and operand.kind == "extended":
                target = operand.value
            elif not operand.indirect and operand.target is not None:
                target = operand.target
    elif mode == IMM:
        value = mem.r16(ppc) if length == 2 else mem.r8(ppc)
        operand_text = f"#${value:04X}" if length == 2 else f"#${value:02X}"
        operands = [Operand("immediate", operand_text, value=value, size=length)]
    else:    # IMM_RR: EXG and TFR, as the CPU takes them (konami.lst EXGTFR)
        pb = mem.r8(ppc)
        source, destination = EXCHANGE_REGISTERS[pb & 7], EXCHANGE_REGISTERS[(pb >> 4) & 7]
        operand_text = f"{source},{destination}"
        exchange = not pb & 0x80
        operands = [Operand("registers", operand_text, source=source, destination=destination, exchange=exchange)]
        if destination == "PC" or (exchange and source == "PC"):
            flow = FLOW_JUMP
    return Instruction(address, p - address, mnemonic, text + operand_text, flow, operands, target, condition)
