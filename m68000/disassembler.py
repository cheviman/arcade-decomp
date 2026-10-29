# license:BSD-3-Clause
# copyright-holders:Karl Stenerud, Sebastian Toyos
"""
Name:        disassembler.py
Description: Motorola 68000 instruction decoder with structured operands
Version:     0.1.0
Created:     29-10-2026
Modified:    29-10-2026
Author:      Sebastian Toyos

Ported from MAME src/devices/cpu/m68000/m68kdasm.cpp (Musashi, by Karl Stenerud), for a 68000: the
disassembler MAME builds with TYPE_68000. The text of every instruction matches MAME's output exactly,
spacing included: konami/reference/compare_m68k_with_mame.py runs MAME's own disassembler and diffs
both, over the game's program and over every 16-bit opcode followed by several kinds of extension
words, at every even offset. On top of the text, every instruction carries its decoded operands and
its control flow, which is what the tools that analyze the program work with.

The port keeps MAME's whole opcode table (m_opcode_info: every entry, the 68010+, 68020+, FPU, MMU and
ColdFire ones too) and builds the 65536-entry handler table at import the way build_opcode_table does:
entries sorted by the number of bits in their mask, most bits first; for each opcode the first entry
whose mask and match fit, that passes the destination check of MOVE and whose allowed effective
address modes (valid_ea) take the opcode's; d68000_illegal when none does. MAME sorts with std::sort,
which does not keep the order of entries with the same number of mask bits. Here ties keep table order.
For a 68000 that changes nothing: the only opcodes two tied entries both take are $F27A-$F27C
(d68881_ftrap and d68020_cptrapcc_*, both 68020+ and so both 'opcode 1111' here) and $F048-$F04F
(d68020_cpdbcc and d68851_p001), which d68851_pdbcc takes first with more mask bits.

Handlers of later CPUs start with limit_cpu_types and, on a 68000, never get past it: they print
'dc.w    $xxxx; opcode 1111' for an opcode $Fxxx and 'dc.w    $xxxx; ILLEGAL' otherwise, and read no
extension word (none of them reads one before the check). A few MMU and ColdFire handlers have no such
check and MAME prints them for a 68000 too (pmove, pload, ptest, pflush, pb.., halt...): they are ported
as they are. In get_ea_mode_str, a 68000 takes a brief extension word with a scale, or a full extension
word (bit 8 set), as 'INVALID 30' (the mode and register in hex), after reading that extension word.

Instruction.text is MAME's text; mnemonic is its name without the size ('move', 'movea', 'addq',
'dc.w'), with the condition apart for the conditional ones ('bcc', 'dbcc', 'scc'); size is the suffix
MAME prints ('b', 'w', 'l'), None when it prints none (lea, moveq, scc, exg...). DBRA is 'dbcc' with
condition 'f' (it is DBF). Branch and call targets, and every value MAME prints from an address
computation, are 32-bit as MAME computes them; a 68000 drives 24 address lines, so a caller that wants
the bus address masks them with 0xFFFFFF.

Control flow:
- BRA is a jump and BSR a call, both with their target. On a 68000 the Bcc encodings with condition t
  and f are BRA and BSR, and MAME prints them so: no 'bt' or 'bf' ever comes out. A displacement byte
  of $FF is the 68020's 32-bit form, so MAME prints those three (BRA, BSR, Bcc) as 'dc.w ...; ILLEGAL'.
- Bcc is a branch with its target and condition (m_cc names: hi, ls, cc, cs, ne, eq, vc, vs, pl, mi,
  ge, lt, gt, le).
- DBcc is a branch with its target and condition. DBT never branches, and DBF (DBRA) loops until the
  counter runs out; both stay branches with their condition, 't' and 'f'.
- JMP is a jump and JSR a call. They carry a target only when the effective address is absolute (.w or
  .l) or PC-relative with a displacement; register-based and PC-indexed ones have none.
- RTS, RTE and RTR are returns.
- Lines MAME prints as data are invalid: 'dc.w ...; ILLEGAL' (unassigned opcodes and the ILLEGAL
  instruction $4AFC alike), 'opcode 1010', 'opcode 1111', and any instruction with an 'INVALID'
  effective address. So are the MMU and ColdFire instructions MAME prints without a CPU check (above):
  a 68000 takes the line 1111 or the illegal instruction exception on them instead. Their mnemonic is
  the first word of MAME's text ('pmove', 'ptestw', 'halt', 'mmu' for 'MMU 001 group'), but the 68851
  branches are 'pbcc' and 'pdbcc' with their m_mmucond condition, and none of them has a target.
- Everything else is normal, the traps included: TRAP, TRAPV, CHK, STOP, RESET, NOP.

Operand modes (Operand.mode and the fields it uses; register names are 'D0'-'D7' and 'A0'-'A7'):
    dreg, areg            a register ('reg')
    indirect              (An) ('reg')
    postinc, predec       (An)+ and -(An) ('reg')
    disp                  (d16,An), MOVEP's ($xxxx,An) too ('reg'; 'disp', signed)
    index                 (d8,An,Xn.s) ('reg'; 'disp', signed; 'index', the index register name;
                          'index_size', 'w' or 'l')
    abs_short, abs_long   $xxxx.w and $xxxxxxxx.l ('value': the address, .w sign-extended to 32 bits)
    pc_disp               (d16,PC) ('value': the address it reaches)
    pc_index              (d8,PC,Xn.s) ('base': the PC the displacement is added to, which is the
                          address of the extension word; 'disp', 'index', 'index_size')
    immediate             #data, and the opcode of a 'dc.w' line ('value': as printed, so signed
                          where MAME prints it signed: ADDI, SUBI, CMPI, LINK, STOP)
    quick                 data inside the opcode: ADDQ, SUBQ, the count of a shift, MOVEQ (signed),
                          the vector of TRAP ('value')
    address               the target of a Bcc, BRA, BSR or DBcc ('value')
    reglist               the register list of MOVEM ('mask': the word as written, whose bit order
                          is reversed for -(An); 'registers': the registers it names, D0-D7 then A0-A7)
    sr, ccr, usp          the status register, the condition codes, the user stack pointer
    invalid               an effective address MAME prints as 'INVALID xx' ('value': the 6-bit mode)
"""
from dataclasses import dataclass, field

# How execution continues after an instruction (the same values as tools/core/z80/disassembler.py)
FLOW_NORMAL = "normal"
FLOW_JUMP = "jump"            # unconditional, never falls through
FLOW_BRANCH = "branch"        # falls through or goes to the target
FLOW_CALL = "call"            # goes to the target and comes back to the next instruction
FLOW_RETURN = "return"
FLOW_INVALID = "invalid"

MASK32 = 0xFFFFFFFF
MASK24 = 0xFFFFFF

# CPU types (m68kdasm.h). This port is the 68000's: m_cpu_type is always TYPE_68000.
TYPE_68000 = 1
TYPE_68008 = 2
TYPE_68010 = 4
TYPE_68020 = 8
TYPE_68030 = 16
TYPE_68040 = 32
TYPE_CPU32 = 64
TYPE_COLDFIRE = 128

M68000_ONLY = TYPE_68000 | TYPE_68008
M68010_ONLY = TYPE_68010
M68010_LESS = TYPE_68000 | TYPE_68008 | TYPE_68010
M68010_PLUS = TYPE_68010 | TYPE_68020 | TYPE_68030 | TYPE_68040 | TYPE_CPU32 | TYPE_COLDFIRE
M68020_ONLY = TYPE_68020 | TYPE_CPU32
M68020_LESS = TYPE_68010 | TYPE_68020 | TYPE_CPU32
M68020_PLUS = TYPE_68020 | TYPE_68030 | TYPE_68040 | TYPE_CPU32 | TYPE_COLDFIRE
M68030_ONLY = TYPE_68030
M68030_LESS = TYPE_68010 | TYPE_68020 | TYPE_68030 | TYPE_CPU32
M68030_PLUS = TYPE_68030 | TYPE_68040
M68040_PLUS = TYPE_68040
COLDFIRE = TYPE_COLDFIRE

CPU_TYPE = TYPE_68000

# Used by ops like asr, ror, addq, etc (m_3bit_qdata_table)
QDATA_3BIT = (8, 1, 2, 3, 4, 5, 6, 7)

# m_cc: condition codes of Bcc, DBcc, Scc and TRAPcc
CONDITIONS = ("t", "f", "hi", "ls", "cc", "cs", "ne", "eq", "vc", "vs", "pl", "mi", "ge", "lt", "gt", "le")

# m_mmuregs and m_mmucond (68851)
MMU_REGISTERS = ("tc", "drp", "srp", "crp", "cal", "val", "sccr", "acr")
MMU_CONDITIONS = ("bs", "bc", "ls", "lc", "ss", "sc", "as", "ac",
                  "ws", "wc", "is", "ic", "gs", "gc", "cs", "cc")


@dataclass(slots=True)
class Operand:
    """A decoded operand; the module docstring lists the modes and the fields each one uses."""
    mode: str
    text: str
    reg: str = ""
    disp: int = 0
    index: str = ""
    index_size: str = ""
    value: int = 0
    base: int = 0
    mask: int = 0
    registers: tuple = ()


@dataclass(slots=True)
class Instruction:
    address: int
    length: int
    mnemonic: str
    size: str | None
    text: str
    flow: str
    operands: list = field(default_factory=list)
    target: int | None = None       # fixed target of a jump, branch or call
    condition: str | None = None    # Bcc, DBcc and Scc (m_cc names); PBcc, PDBcc (m_mmucond)


class FlatMemory:
    """Big-endian reads from a byte block, as the 68000 sees its program from address 0, and as
    oracle_m68k's buffer reads it: addresses wrapped to 24 bits, $FF past the end of the data."""

    def __init__(self, data):
        self.data = bytes(data)
        self.size = len(self.data)

    def r8(self, address):
        address &= MASK24
        return self.data[address] if address < self.size else 0xFF

    def r16(self, address):
        address &= MASK24
        if address + 1 < self.size:
            data = self.data
            return (data[address] << 8) | data[address + 1]
        return (self.r8(address) << 8) | self.r8(address + 1)

    def r32(self, address):
        return (self.r16(address) << 16) | self.r16(address + 2)


def _s8(value):
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


def _s16(value):
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _s32(value):
    value &= MASK32
    return value - 0x100000000 if value & 0x80000000 else value


def make_signed_hex_str_8(value):
    value &= 0xFF
    if value == 0x80:
        return "-$80"
    if value & 0x80:
        return "-$%x" % ((-value) & 0x7F)
    return "$%x" % (value & 0x7F)


def make_signed_hex_str_16(value):
    value &= 0xFFFF
    if value == 0x8000:
        return "-$8000"
    if value & 0x8000:
        return "-$%x" % ((-value) & 0x7FFF)
    return "$%x" % (value & 0x7FFF)


def make_signed_hex_str_32(value):
    value &= MASK32
    if value == 0x80000000:
        return "-$80000000"
    if value & 0x80000000:
        return "-$%x" % ((-value) & 0x7FFFFFFF)
    return "$%x" % (value & 0x7FFFFFFF)


class _State:
    """m_cpu_pc, m_cpu_ir and the buffer of one disassemble() call."""
    __slots__ = ("mem", "pc", "ir")

    def __init__(self, mem, pc):
        self.mem = mem
        self.pc = pc
        self.ir = 0

    def read_imm_8(self):
        """The low byte of the next word; advances a whole word, to keep the alignment."""
        value = self.mem.r8(self.pc + 1)
        self.pc += 2
        return value

    def read_imm_16(self):
        value = self.mem.r16(self.pc)
        self.pc += 2
        return value

    def read_imm_32(self):
        value = self.mem.r32(self.pc)
        self.pc += 4
        return value


# ----------------------------------------------------------------------------------------------------
# Operands
# ----------------------------------------------------------------------------------------------------

_D_NAMES = tuple("D%d" % n for n in range(8))
_A_NAMES = tuple("A%d" % n for n in range(8))


def _dreg(number):
    name = _D_NAMES[number]
    return Operand("dreg", name, reg=name)


def _areg(number):
    name = _A_NAMES[number]
    return Operand("areg", name, reg=name)


def _predec(number):
    return Operand("predec", "-(A%d)" % number, reg=_A_NAMES[number])


def _postinc(number):
    return Operand("postinc", "(A%d)+" % number, reg=_A_NAMES[number])


def _address(target):
    return Operand("address", "$%x" % target, value=target)


def _quick(text, value):
    return Operand("quick", text, value=value)


def _special(name):
    return Operand(name.lower(), name)


def get_imm_str_s(s, size):
    """#data printed signed; size 0, 1, 2 for byte, word, long."""
    if size == 0:
        raw = s.read_imm_8()
        return Operand("immediate", "#" + make_signed_hex_str_8(raw), value=_s8(raw))
    if size == 1:
        raw = s.read_imm_16()
        return Operand("immediate", "#" + make_signed_hex_str_16(raw), value=_s16(raw))
    raw = s.read_imm_32()
    return Operand("immediate", "#" + make_signed_hex_str_32(raw), value=_s32(raw))


def get_imm_str_u(s, size):
    """#data printed unsigned."""
    if size == 0:
        raw = s.read_imm_8()
    elif size == 1:
        raw = s.read_imm_16()
    else:
        raw = s.read_imm_32()
    return Operand("immediate", "#$%x" % raw, value=raw)


def _invalid(mode):
    return Operand("invalid", "INVALID %x" % mode, value=mode)


def _index_register(extension):
    """The index register of a brief extension word: ('D3', 'w')."""
    name = ("A%d" if extension & 0x8000 else "D%d") % ((extension >> 12) & 7)
    return name, "l" if extension & 0x800 else "w"


def get_ea_mode_str(s, instruction, size):
    """The effective address in the low 6 bits of 'instruction', reading its extension words.

    A 68000 (M68010_LESS) breaks out on a brief extension word with a scale and on a full extension
    word, once the word is read: those come back as 'INVALID xx', with the word counted in the length.
    MAME's code for the full format (68020+) is never reached here and is not ported."""
    mode = instruction & 0x3F
    number = instruction & 7
    group = mode >> 3
    if group == 0:
        # data register direct
        return _dreg(number)
    if group == 1:
        # address register direct
        return _areg(number)
    if group == 2:
        # address register indirect
        return Operand("indirect", "(A%d)" % number, reg=_A_NAMES[number])
    if group == 3:
        # address register indirect with postincrement
        return _postinc(number)
    if group == 4:
        # address register indirect with predecrement
        return _predec(number)
    if group == 5:
        # address register indirect with displacement
        disp = s.read_imm_16()
        return Operand("disp", "(%s,A%d)" % (make_signed_hex_str_16(disp), number), reg=_A_NAMES[number],
                       disp=_s16(disp))
    if group == 6:
        # address register indirect with index
        extension = s.read_imm_16()
        if extension & 0x700:       # a scale (bits 9-10), or the full format (bit 8): not on a 68000
            return _invalid(mode)
        index, index_size = _index_register(extension)
        if extension & 0xFF:
            text = "(%s,A%d,%s.%s)" % (make_signed_hex_str_8(extension), number, index, index_size)
        else:
            text = "(A%d,%s.%s)" % (number, index, index_size)
        return Operand("index", text, reg=_A_NAMES[number], disp=_s8(extension), index=index,
                       index_size=index_size)
    if mode == 0x38:
        # absolute short address
        value = s.read_imm_16()
        return Operand("abs_short", "$%x.w" % value, value=_s16(value) & MASK32)
    if mode == 0x39:
        # absolute long address
        value = s.read_imm_32()
        return Operand("abs_long", "$%x.l" % value, value=value)
    if mode == 0x3A:
        # program counter with displacement
        temp_value = s.read_imm_16()
        value = (_s16(temp_value) + s.pc - 2) & MASK32
        return Operand("pc_disp", "($%x,PC)" % value, value=value)
    if mode == 0x3B:
        # program counter with index
        extension = s.read_imm_16()
        if extension & 0x700:
            return _invalid(mode)
        index, index_size = _index_register(extension)
        if extension & 0xFF:
            text = "(%s,PC,%s.%s)" % (make_signed_hex_str_8(extension), index, index_size)
        else:
            text = "(PC,%s.%s)" % (index, index_size)
        return Operand("pc_index", text, base=(s.pc - 2) & MASK32, disp=_s8(extension), index=index,
                       index_size=index_size)
    if mode == 0x3C:
        # immediate
        return get_imm_str_u(s, size)
    return _invalid(mode)


# ----------------------------------------------------------------------------------------------------
# Handlers. Each returns (text, mnemonic, size, operands, flow, target, condition).
# ----------------------------------------------------------------------------------------------------

def _name(fmt):
    """'add.b   %s, D%d' -> ('add', 'b'); 'lea     %s, A%d' -> ('lea', None)."""
    token = fmt.split(" ", 1)[0]
    if "." in token:
        mnemonic, size = token.split(".")
        return mnemonic, size
    return token, None


def _out(text, mnemonic, size, operands, flow=FLOW_NORMAL, target=None, condition=None):
    return text, mnemonic, size, operands, flow, target, condition


def _fixed_target(operand):
    """The target of JMP and JSR, when the effective address gives one."""
    if operand.mode in ("abs_short", "abs_long", "pc_disp"):
        return operand.value
    return None


def _dc_w(text, ir):
    return _out(text, "dc.w", None, [Operand("immediate", "$%04x" % ir, value=ir)], FLOW_INVALID)


def d68000_illegal(s):
    return _dc_w("dc.w    $%04x; ILLEGAL" % s.ir, s.ir)


def d68000_1010(s):
    return _dc_w("dc.w    $%04x; opcode 1010" % s.ir, s.ir)


def d68000_1111(s):
    return _dc_w("dc.w    $%04x; opcode 1111" % s.ir, s.ir)


def _limit_cpu_types(s):
    """limit_cpu_types for a CPU that is not allowed: the result replaces the instruction."""
    if (s.ir & 0xF000) == 0xF000:
        return d68000_1111(s)
    return d68000_illegal(s)


def _limited(allowed):
    """A handler that starts with limit_cpu_types(allowed). For a 68000 it never gets past the check,
    and that check comes before any read in every such handler, so this is all of it a 68000 runs."""
    if CPU_TYPE & allowed:
        raise NotImplementedError("only the 68000 path of the handlers of later CPUs is ported")
    return _limit_cpu_types


def _ea_to_dreg(fmt, size):
    """'op.s   <ea>, Dn' with Dn in bits 9-11."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        ea = get_ea_mode_str(s, s.ir, size)
        number = (s.ir >> 9) & 7
        return _out(fmt % (ea.text, number), mnemonic, suffix, [ea, _dreg(number)])
    return handler


def _ea_to_areg(fmt, size):
    """'op.s   <ea>, An' with An in bits 9-11."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        ea = get_ea_mode_str(s, s.ir, size)
        number = (s.ir >> 9) & 7
        return _out(fmt % (ea.text, number), mnemonic, suffix, [ea, _areg(number)])
    return handler


def _dreg_to_ea(fmt, size):
    """'op.s   Dn, <ea>' with Dn in bits 9-11."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        number = (s.ir >> 9) & 7
        ea = get_ea_mode_str(s, s.ir, size)
        return _out(fmt % (number, ea.text), mnemonic, suffix, [_dreg(number), ea])
    return handler


def _imm_to_ea(fmt, size, signed):
    """'opi.s  #data, <ea>': the immediate is read first (MAME keeps it in a local before the call)."""
    mnemonic, suffix = _name(fmt)
    get_imm = get_imm_str_s if signed else get_imm_str_u

    def handler(s):
        immediate = get_imm(s, size)
        ea = get_ea_mode_str(s, s.ir, size)
        return _out(fmt % (immediate.text, ea.text), mnemonic, suffix, [immediate, ea])
    return handler


def _ea_only(fmt, size):
    """'op.s   <ea>'."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        ea = get_ea_mode_str(s, s.ir, size)
        return _out(fmt % ea.text, mnemonic, suffix, [ea])
    return handler


def _quick_to_ea(fmt, size):
    """'addq.s #q, <ea>' with q in bits 9-11 (0 is 8)."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        data = QDATA_3BIT[(s.ir >> 9) & 7]
        ea = get_ea_mode_str(s, s.ir, size)
        return _out(fmt % (data, ea.text), mnemonic, suffix, [_quick("#%d" % data, data), ea])
    return handler


def _shift_quick(fmt):
    """'asr.s  #q, Dy': count in bits 9-11 (0 is 8), register in bits 0-2."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        data = QDATA_3BIT[(s.ir >> 9) & 7]
        number = s.ir & 7
        return _out(fmt % (data, number), mnemonic, suffix, [_quick("#%d" % data, data), _dreg(number)])
    return handler


def _shift_register(fmt):
    """'asr.s  Dx, Dy': count register in bits 9-11, register in bits 0-2; also EXG Dx, Dy."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        first, second = (s.ir >> 9) & 7, s.ir & 7
        return _out(fmt % (first, second), mnemonic, suffix, [_dreg(first), _dreg(second)])
    return handler


def _source_first(fmt, operand):
    """'abcd   Dy, Dx', '-(Ay), -(Ax)' or '(Ay)+, (Ax)+': source in bits 0-2, destination in bits 9-11."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        source, destination = s.ir & 7, (s.ir >> 9) & 7
        return _out(fmt % (source, destination), mnemonic, suffix, [operand(source), operand(destination)])
    return handler


def _imm_to_register(fmt, size, register):
    """'andi    #data, CCR' and the like."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        immediate = get_imm_str_u(s, size)
        return _out(fmt % immediate.text, mnemonic, suffix, [immediate, _special(register)])
    return handler


def _fixed(fmt, flow=FLOW_NORMAL):
    """An instruction with no operand."""
    def handler(s):
        return _out(fmt, fmt, None, [], flow)
    return handler


d68000_abcd_rr = _source_first("abcd    D%d, D%d", _dreg)
d68000_abcd_mm = _source_first("abcd    -(A%d), -(A%d)", _predec)
d68000_add_er_8 = _ea_to_dreg("add.b   %s, D%d", 0)
d68000_add_er_16 = _ea_to_dreg("add.w   %s, D%d", 1)
d68000_add_er_32 = _ea_to_dreg("add.l   %s, D%d", 2)
d68000_add_re_8 = _dreg_to_ea("add.b   D%d, %s", 0)
d68000_add_re_16 = _dreg_to_ea("add.w   D%d, %s", 1)
d68000_add_re_32 = _dreg_to_ea("add.l   D%d, %s", 2)
d68000_adda_16 = _ea_to_areg("adda.w  %s, A%d", 1)
d68000_adda_32 = _ea_to_areg("adda.l  %s, A%d", 2)
d68000_addi_8 = _imm_to_ea("addi.b  %s, %s", 0, True)
d68000_addi_16 = _imm_to_ea("addi.w  %s, %s", 1, True)
d68000_addi_32 = _imm_to_ea("addi.l  %s, %s", 2, True)
d68000_addq_8 = _quick_to_ea("addq.b  #%d, %s", 0)
d68000_addq_16 = _quick_to_ea("addq.w  #%d, %s", 1)
d68000_addq_32 = _quick_to_ea("addq.l  #%d, %s", 2)
d68000_addx_rr_8 = _source_first("addx.b  D%d, D%d", _dreg)
d68000_addx_rr_16 = _source_first("addx.w  D%d, D%d", _dreg)
d68000_addx_rr_32 = _source_first("addx.l  D%d, D%d", _dreg)
d68000_addx_mm_8 = _source_first("addx.b  -(A%d), -(A%d)", _predec)
d68000_addx_mm_16 = _source_first("addx.w  -(A%d), -(A%d)", _predec)
d68000_addx_mm_32 = _source_first("addx.l  -(A%d), -(A%d)", _predec)
d68000_and_er_8 = _ea_to_dreg("and.b   %s, D%d", 0)
d68000_and_er_16 = _ea_to_dreg("and.w   %s, D%d", 1)
d68000_and_er_32 = _ea_to_dreg("and.l   %s, D%d", 2)
d68000_and_re_8 = _dreg_to_ea("and.b   D%d, %s", 0)
d68000_and_re_16 = _dreg_to_ea("and.w   D%d, %s", 1)
d68000_and_re_32 = _dreg_to_ea("and.l   D%d, %s", 2)
d68000_andi_8 = _imm_to_ea("andi.b  %s, %s", 0, False)
d68000_andi_16 = _imm_to_ea("andi.w  %s, %s", 1, False)
d68000_andi_32 = _imm_to_ea("andi.l  %s, %s", 2, False)
d68000_andi_to_ccr = _imm_to_register("andi    %s, CCR", 0, "CCR")
d68000_andi_to_sr = _imm_to_register("andi    %s, SR", 1, "SR")
d68000_asr_s_8 = _shift_quick("asr.b   #%d, D%d")
d68000_asr_s_16 = _shift_quick("asr.w   #%d, D%d")
d68000_asr_s_32 = _shift_quick("asr.l   #%d, D%d")
d68000_asr_r_8 = _shift_register("asr.b   D%d, D%d")
d68000_asr_r_16 = _shift_register("asr.w   D%d, D%d")
d68000_asr_r_32 = _shift_register("asr.l   D%d, D%d")
d68000_asr_ea = _ea_only("asr.w   %s", 1)
d68000_asl_s_8 = _shift_quick("asl.b   #%d, D%d")
d68000_asl_s_16 = _shift_quick("asl.w   #%d, D%d")
d68000_asl_s_32 = _shift_quick("asl.l   #%d, D%d")
d68000_asl_r_8 = _shift_register("asl.b   D%d, D%d")
d68000_asl_r_16 = _shift_register("asl.w   D%d, D%d")
d68000_asl_r_32 = _shift_register("asl.l   D%d, D%d")
d68000_asl_ea = _ea_only("asl.w   %s", 1)


def d68000_bcc_8(s):
    temp_pc = s.pc
    condition = CONDITIONS[(s.ir >> 8) & 0xF]
    target = (temp_pc + _s8(s.ir)) & MASK32
    return _out("b%-2s     $%x" % (condition, target), "bcc", None, [_address(target)], FLOW_BRANCH, target,
                condition)


def d68000_bcc_16(s):
    temp_pc = s.pc
    condition = CONDITIONS[(s.ir >> 8) & 0xF]
    target = (temp_pc + _s16(s.read_imm_16())) & MASK32
    return _out("b%-2s     $%x" % (condition, target), "bcc", None, [_address(target)], FLOW_BRANCH, target,
                condition)


d68020_bcc_32 = _limited(M68020_PLUS)
d68000_bchg_r = _dreg_to_ea("bchg    D%d, %s", 0)
d68000_bchg_s = _imm_to_ea("bchg    %s, %s", 0, False)
d68000_bclr_r = _dreg_to_ea("bclr    D%d, %s", 0)
d68000_bclr_s = _imm_to_ea("bclr    %s, %s", 0, False)
d68010_bkpt = _limited(M68010_PLUS)
d68020_bfchg = _limited(M68020_PLUS)
d68020_bfclr = _limited(M68020_PLUS)
d68020_bfexts = _limited(M68020_PLUS)
d68020_bfextu = _limited(M68020_PLUS)
d68020_bfffo = _limited(M68020_PLUS)
d68020_bfins = _limited(M68020_PLUS)
d68020_bfset = _limited(M68020_PLUS)
d68020_bftst = _limited(M68020_PLUS)


def d68000_bra_8(s):
    temp_pc = s.pc
    target = (temp_pc + _s8(s.ir)) & MASK32
    return _out("bra     $%x" % target, "bra", None, [_address(target)], FLOW_JUMP, target)


def d68000_bra_16(s):
    temp_pc = s.pc
    target = (temp_pc + _s16(s.read_imm_16())) & MASK32
    return _out("bra     $%x" % target, "bra", None, [_address(target)], FLOW_JUMP, target)


d68020_bra_32 = _limited(M68020_PLUS)
d68000_bset_r = _dreg_to_ea("bset    D%d, %s", 0)
d68000_bset_s = _imm_to_ea("bset    %s, %s", 0, False)


def d68000_bsr_8(s):
    temp_pc = s.pc
    target = (temp_pc + _s8(s.ir)) & MASK32
    return _out("bsr     $%x" % target, "bsr", None, [_address(target)], FLOW_CALL, target)


def d68000_bsr_16(s):
    temp_pc = s.pc
    target = (temp_pc + _s16(s.read_imm_16())) & MASK32
    return _out("bsr     $%x" % target, "bsr", None, [_address(target)], FLOW_CALL, target)


d68020_bsr_32 = _limited(M68020_PLUS)
d68000_btst_r = _dreg_to_ea("btst    D%d, %s", 0)
d68000_btst_s = _imm_to_ea("btst    %s, %s", 0, False)
d68020_callm = _limited(M68020_ONLY)
d68020_cas_8 = _limited(M68020_PLUS)
d68020_cas_16 = _limited(M68020_PLUS)
d68020_cas_32 = _limited(M68020_PLUS)
d68020_cas2_16 = _limited(M68020_PLUS)
d68020_cas2_32 = _limited(M68020_PLUS)
d68000_chk_16 = _ea_to_dreg("chk.w   %s, D%d", 1)
d68020_chk_32 = _limited(M68020_PLUS)
d68020_chk2_cmp2_8 = _limited(M68020_PLUS)
d68020_chk2_cmp2_16 = _limited(M68020_PLUS)
d68020_chk2_cmp2_32 = _limited(M68020_PLUS)
d68040_cinv = _limited(M68040_PLUS)
d68000_clr_8 = _ea_only("clr.b   %s", 0)
d68000_clr_16 = _ea_only("clr.w   %s", 1)
d68000_clr_32 = _ea_only("clr.l   %s", 2)
d68000_cmp_8 = _ea_to_dreg("cmp.b   %s, D%d", 0)
d68000_cmp_16 = _ea_to_dreg("cmp.w   %s, D%d", 1)
d68000_cmp_32 = _ea_to_dreg("cmp.l   %s, D%d", 2)
d68000_cmpa_16 = _ea_to_areg("cmpa.w  %s, A%d", 1)
d68000_cmpa_32 = _ea_to_areg("cmpa.l  %s, A%d", 2)
d68000_cmpi_8 = _imm_to_ea("cmpi.b  %s, %s", 0, True)
d68020_cmpi_pcdi_8 = _limited(M68010_PLUS)
d68020_cmpi_pcix_8 = _limited(M68010_PLUS)
d68000_cmpi_16 = _imm_to_ea("cmpi.w  %s, %s", 1, True)
d68020_cmpi_pcdi_16 = _limited(M68010_PLUS)
d68020_cmpi_pcix_16 = _limited(M68010_PLUS)
d68000_cmpi_32 = _imm_to_ea("cmpi.l  %s, %s", 2, True)
d68020_cmpi_pcdi_32 = _limited(M68010_PLUS)
d68020_cmpi_pcix_32 = _limited(M68010_PLUS)
d68000_cmpm_8 = _source_first("cmpm.b  (A%d)+, (A%d)+", _postinc)
d68000_cmpm_16 = _source_first("cmpm.w  (A%d)+, (A%d)+", _postinc)
d68000_cmpm_32 = _source_first("cmpm.l  (A%d)+, (A%d)+", _postinc)
d68020_cpbcc_16 = _limited(M68020_PLUS)
d68020_cpbcc_32 = _limited(M68020_PLUS)
d68020_cpdbcc = _limited(M68020_PLUS)
d68020_cpgen = _limited(M68020_PLUS)
d68020_cprestore = _limited(M68020_PLUS)
d68020_cpsave = _limited(M68020_PLUS)
d68020_cpscc = _limited(M68020_PLUS)
d68020_cptrapcc_0 = _limited(M68020_PLUS)
d68020_cptrapcc_16 = _limited(M68020_PLUS)
d68020_cptrapcc_32 = _limited(M68020_PLUS)
d68040_cpush = _limited(M68040_PLUS)


def d68000_dbra(s):
    temp_pc = s.pc
    number = s.ir & 7
    target = (temp_pc + _s16(s.read_imm_16())) & MASK32
    return _out("dbra    D%d, $%x" % (number, target), "dbcc", None, [_dreg(number), _address(target)],
                FLOW_BRANCH, target, "f")


def d68000_dbcc(s):
    temp_pc = s.pc
    condition = CONDITIONS[(s.ir >> 8) & 0xF]
    number = s.ir & 7
    target = (temp_pc + _s16(s.read_imm_16())) & MASK32
    return _out("db%-2s    D%d, $%x" % (condition, number, target), "dbcc", None,
                [_dreg(number), _address(target)], FLOW_BRANCH, target, condition)


d68000_divs = _ea_to_dreg("divs.w  %s, D%d", 1)
d68000_divu = _ea_to_dreg("divu.w  %s, D%d", 1)
d68020_divl = _limited(M68020_PLUS)
d68000_eor_8 = _dreg_to_ea("eor.b   D%d, %s", 0)
d68000_eor_16 = _dreg_to_ea("eor.w   D%d, %s", 1)
d68000_eor_32 = _dreg_to_ea("eor.l   D%d, %s", 2)
d68000_eori_8 = _imm_to_ea("eori.b  %s, %s", 0, False)
d68000_eori_16 = _imm_to_ea("eori.w  %s, %s", 1, False)
d68000_eori_32 = _imm_to_ea("eori.l  %s, %s", 2, False)
d68000_eori_to_ccr = _imm_to_register("eori    %s, CCR", 0, "CCR")
d68000_eori_to_sr = _imm_to_register("eori    %s, SR", 1, "SR")
d68000_exg_dd = _shift_register("exg     D%d, D%d")


def d68000_exg_aa(s):
    first, second = (s.ir >> 9) & 7, s.ir & 7
    return _out("exg     A%d, A%d" % (first, second), "exg", None, [_areg(first), _areg(second)])


def d68000_exg_da(s):
    first, second = (s.ir >> 9) & 7, s.ir & 7
    return _out("exg     D%d, A%d" % (first, second), "exg", None, [_dreg(first), _areg(second)])


def d68000_ext_16(s):
    number = s.ir & 7
    return _out("ext.w   D%d" % number, "ext", "w", [_dreg(number)])


def d68000_ext_32(s):
    number = s.ir & 7
    return _out("ext.l   D%d" % number, "ext", "l", [_dreg(number)])


d68020_extb_32 = _limited(M68020_PLUS)
d68881_ftrap = _limited(M68020_PLUS)
d68040_fpu = _limited(M68020_PLUS)


def dcoldfire_halt(s):
    # No CPU check in MAME: a 68000 gets 'halt' too, though it takes $4AC8 (TAS An) as illegal
    return _out("halt", "halt", None, [], FLOW_INVALID)


def d68000_jmp(s):
    ea = get_ea_mode_str(s, s.ir, 2)
    return _out("jmp     %s" % ea.text, "jmp", None, [ea], FLOW_JUMP, _fixed_target(ea))


def d68000_jsr(s):
    ea = get_ea_mode_str(s, s.ir, 2)
    return _out("jsr     %s" % ea.text, "jsr", None, [ea], FLOW_CALL, _fixed_target(ea))


d68000_lea = _ea_to_areg("lea     %s, A%d", 2)


def d68000_link_16(s):
    number = s.ir & 7
    immediate = get_imm_str_s(s, 1)
    return _out("link    A%d, %s" % (number, immediate.text), "link", None, [_areg(number), immediate])


d68020_link_32 = _limited(M68020_PLUS)
d68000_lsr_s_8 = _shift_quick("lsr.b   #%d, D%d")
d68000_lsr_s_16 = _shift_quick("lsr.w   #%d, D%d")
d68000_lsr_s_32 = _shift_quick("lsr.l   #%d, D%d")
d68000_lsr_r_8 = _shift_register("lsr.b   D%d, D%d")
d68000_lsr_r_16 = _shift_register("lsr.w   D%d, D%d")
d68000_lsr_r_32 = _shift_register("lsr.l   D%d, D%d")
d68000_lsr_ea = _ea_only("lsr.w   %s", 2)
d68000_lsl_s_8 = _shift_quick("lsl.b   #%d, D%d")
d68000_lsl_s_16 = _shift_quick("lsl.w   #%d, D%d")
d68000_lsl_s_32 = _shift_quick("lsl.l   #%d, D%d")
d68000_lsl_r_8 = _shift_register("lsl.b   D%d, D%d")
d68000_lsl_r_16 = _shift_register("lsl.w   D%d, D%d")
d68000_lsl_r_32 = _shift_register("lsl.l   D%d, D%d")
d68000_lsl_ea = _ea_only("lsl.w   %s", 2)


def _move(fmt, size):
    """MOVE: the source is read first (MAME keeps it in a local), then the destination, whose mode and
    register are swapped in bits 6-11."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        source = get_ea_mode_str(s, s.ir, size)
        destination = get_ea_mode_str(s, ((s.ir >> 9) & 7) | ((s.ir >> 3) & 0x38), size)
        return _out(fmt % (source.text, destination.text), mnemonic, suffix, [source, destination])
    return handler


d68000_move_8 = _move("move.b  %s, %s", 0)
d68000_move_16 = _move("move.w  %s, %s", 1)
d68000_move_32 = _move("move.l  %s, %s", 2)
d68000_movea_16 = _ea_to_areg("movea.w %s, A%d", 1)
d68000_movea_32 = _ea_to_areg("movea.l %s, A%d", 2)


def d68000_move_to_ccr(s):
    ea = get_ea_mode_str(s, s.ir, 0)
    return _out("move    %s, CCR" % ea.text, "move", None, [ea, _special("CCR")])


d68010_move_fr_ccr = _limited(M68010_PLUS)


def d68000_move_fr_sr(s):
    ea = get_ea_mode_str(s, s.ir, 1)
    return _out("move    SR, %s" % ea.text, "move", None, [_special("SR"), ea])


def d68000_move_to_sr(s):
    ea = get_ea_mode_str(s, s.ir, 1)
    return _out("move    %s, SR" % ea.text, "move", None, [ea, _special("SR")])


def d68000_move_fr_usp(s):
    number = s.ir & 7
    return _out("move    USP, A%d" % number, "move", None, [_special("USP"), _areg(number)])


def d68000_move_to_usp(s):
    number = s.ir & 7
    return _out("move    A%d, USP" % number, "move", None, [_areg(number), _special("USP")])


d68010_movec = _limited(M68010_PLUS)


def _register_runs(bits, prefix):
    """MAME's register list loop for one bank: 'bits' says, for registers 0 to 7, whether each is in the
    list; consecutive ones are written as a range. Returns the pieces and the register names."""
    pieces = []
    names = []
    i = 0
    while i < 8:
        if bits[i]:
            first = i
            run_length = 0
            while i < 7 and bits[i + 1]:
                i += 1
                run_length += 1
            piece = "%s%d" % (prefix, first)
            if run_length > 0:
                piece += "-%s%d" % (prefix, first + run_length)
            pieces.append(piece)
            names.extend("%s%d" % (prefix, n) for n in range(first, first + run_length + 1))
        i += 1
    return pieces, names


def _register_list(data, predecrement):
    """The register list of MOVEM. With -(An) bit 15 is D0 and bit 0 is A7; otherwise bit 0 is D0."""
    if predecrement:
        data_bits = [bool(data & (1 << (15 - i))) for i in range(8)]
        address_bits = [bool(data & (1 << (7 - i))) for i in range(8)]
    else:
        data_bits = [bool(data & (1 << i)) for i in range(8)]
        address_bits = [bool(data & (1 << (i + 8))) for i in range(8)]
    data_pieces, data_names = _register_runs(data_bits, "D")
    address_pieces, address_names = _register_runs(address_bits, "A")
    text = "/".join(data_pieces + address_pieces)
    return Operand("reglist", text, mask=data, registers=tuple(data_names + address_names))


def _movem_to_ea(fmt, size, predecrement):
    """MOVEM registers to memory: the mask is read before the effective address."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        registers = _register_list(s.read_imm_16(), predecrement)
        ea = get_ea_mode_str(s, s.ir, size)
        return _out(fmt % (registers.text, ea.text), mnemonic, suffix, [registers, ea])
    return handler


def _movem_to_registers(fmt, size):
    """MOVEM memory to registers: the mask is read before the effective address."""
    mnemonic, suffix = _name(fmt)

    def handler(s):
        registers = _register_list(s.read_imm_16(), False)
        ea = get_ea_mode_str(s, s.ir, size)
        return _out(fmt % (ea.text, registers.text), mnemonic, suffix, [ea, registers])
    return handler


d68000_movem_pd_16 = _movem_to_ea("movem.w %s, %s", 1, True)
d68000_movem_pd_32 = _movem_to_ea("movem.l %s, %s", 2, True)
d68000_movem_er_16 = _movem_to_registers("movem.w %s, %s", 1)
d68000_movem_er_32 = _movem_to_registers("movem.l %s, %s", 2)
d68000_movem_re_16 = _movem_to_ea("movem.w %s, %s", 1, False)
d68000_movem_re_32 = _movem_to_ea("movem.l %s, %s", 2, False)


def _movep_operand(disp, number):
    """MOVEP's memory operand; MAME prints its displacement unsigned."""
    return Operand("disp", "($%x,A%d)" % (disp, number), reg=_A_NAMES[number], disp=_s16(disp))


def _movep_to_memory(fmt):
    mnemonic, suffix = _name(fmt)

    def handler(s):
        data = (s.ir >> 9) & 7
        disp = s.read_imm_16()
        number = s.ir & 7
        return _out(fmt % (data, disp, number), mnemonic, suffix, [_dreg(data), _movep_operand(disp, number)])
    return handler


def _movep_to_register(fmt):
    mnemonic, suffix = _name(fmt)

    def handler(s):
        disp = s.read_imm_16()
        number = s.ir & 7
        data = (s.ir >> 9) & 7
        return _out(fmt % (disp, number, data), mnemonic, suffix, [_movep_operand(disp, number), _dreg(data)])
    return handler


d68000_movep_re_16 = _movep_to_memory("movep.w D%d, ($%x,A%d)")
d68000_movep_re_32 = _movep_to_memory("movep.l D%d, ($%x,A%d)")
d68000_movep_er_16 = _movep_to_register("movep.w ($%x,A%d), D%d")
d68000_movep_er_32 = _movep_to_register("movep.l ($%x,A%d), D%d")
d68010_moves_8 = _limited(M68010_PLUS)
d68010_moves_16 = _limited(M68010_PLUS)
d68010_moves_32 = _limited(M68010_PLUS)


def d68000_moveq(s):
    data = make_signed_hex_str_8(s.ir)
    number = (s.ir >> 9) & 7
    return _out("moveq   #%s, D%d" % (data, number), "moveq", None,
                [_quick("#" + data, _s8(s.ir)), _dreg(number)])


d68040_move16_pi_pi = _limited(M68040_PLUS)
d68040_move16_pi_al = _limited(M68040_PLUS)
d68040_move16_al_pi = _limited(M68040_PLUS)
d68040_move16_ai_al = _limited(M68040_PLUS)
d68040_move16_al_ai = _limited(M68040_PLUS)
d68000_muls = _ea_to_dreg("muls.w  %s, D%d", 1)
d68000_mulu = _ea_to_dreg("mulu.w  %s, D%d", 1)
d68020_mull = _limited(M68020_PLUS)
d68000_nbcd = _ea_only("nbcd    %s", 0)
d68000_neg_8 = _ea_only("neg.b   %s", 0)
d68000_neg_16 = _ea_only("neg.w   %s", 1)
d68000_neg_32 = _ea_only("neg.l   %s", 2)
d68000_negx_8 = _ea_only("negx.b  %s", 0)
d68000_negx_16 = _ea_only("negx.w  %s", 1)
d68000_negx_32 = _ea_only("negx.l  %s", 2)
d68000_nop = _fixed("nop")
d68000_not_8 = _ea_only("not.b   %s", 0)
d68000_not_16 = _ea_only("not.w   %s", 1)
d68000_not_32 = _ea_only("not.l   %s", 2)
d68000_or_er_8 = _ea_to_dreg("or.b    %s, D%d", 0)
d68000_or_er_16 = _ea_to_dreg("or.w    %s, D%d", 1)
d68000_or_er_32 = _ea_to_dreg("or.l    %s, D%d", 2)
d68000_or_re_8 = _dreg_to_ea("or.b    D%d, %s", 0)
d68000_or_re_16 = _dreg_to_ea("or.w    D%d, %s", 1)
d68000_or_re_32 = _dreg_to_ea("or.l    D%d, %s", 2)
d68000_ori_8 = _imm_to_ea("ori.b   %s, %s", 0, False)
d68000_ori_16 = _imm_to_ea("ori.w   %s, %s", 1, False)
d68000_ori_32 = _imm_to_ea("ori.l   %s, %s", 2, False)
d68000_ori_to_ccr = _imm_to_register("ori     %s, CCR", 0, "CCR")
d68000_ori_to_sr = _imm_to_register("ori     %s, SR", 1, "SR")
d68020_pack_rr = _limited(M68020_PLUS)
d68020_pack_mm = _limited(M68020_PLUS)
d68000_pea = _ea_only("pea     %s", 2)
d68000_reset = _fixed("reset")
d68000_ror_s_8 = _shift_quick("ror.b   #%d, D%d")
d68000_ror_s_16 = _shift_quick("ror.w   #%d, D%d")
d68000_ror_s_32 = _shift_quick("ror.l   #%d, D%d")
d68000_ror_r_8 = _shift_register("ror.b   D%d, D%d")
d68000_ror_r_16 = _shift_register("ror.w   D%d, D%d")
d68000_ror_r_32 = _shift_register("ror.l   D%d, D%d")
d68000_ror_ea = _ea_only("ror.w   %s", 2)
d68000_rol_s_8 = _shift_quick("rol.b   #%d, D%d")
d68000_rol_s_16 = _shift_quick("rol.w   #%d, D%d")
d68000_rol_s_32 = _shift_quick("rol.l   #%d, D%d")
d68000_rol_r_8 = _shift_register("rol.b   D%d, D%d")
d68000_rol_r_16 = _shift_register("rol.w   D%d, D%d")
d68000_rol_r_32 = _shift_register("rol.l   D%d, D%d")
d68000_rol_ea = _ea_only("rol.w   %s", 2)
d68000_roxr_s_8 = _shift_quick("roxr.b  #%d, D%d")
d68000_roxr_s_16 = _shift_quick("roxr.w  #%d, D%d")
d68000_roxr_s_32 = _shift_quick("roxr.l  #%d, D%d")
d68000_roxr_r_8 = _shift_register("roxr.b  D%d, D%d")
d68000_roxr_r_16 = _shift_register("roxr.w  D%d, D%d")
d68000_roxr_r_32 = _shift_register("roxr.l  D%d, D%d")
d68000_roxr_ea = _ea_only("roxr.w  %s", 2)
d68000_roxl_s_8 = _shift_quick("roxl.b  #%d, D%d")
d68000_roxl_s_16 = _shift_quick("roxl.w  #%d, D%d")
d68000_roxl_s_32 = _shift_quick("roxl.l  #%d, D%d")
d68000_roxl_r_8 = _shift_register("roxl.b  D%d, D%d")
d68000_roxl_r_16 = _shift_register("roxl.w  D%d, D%d")
d68000_roxl_r_32 = _shift_register("roxl.l  D%d, D%d")
d68000_roxl_ea = _ea_only("roxl.w  %s", 2)
d68010_rtd = _limited(M68010_PLUS)
d68000_rte = _fixed("rte", FLOW_RETURN)
d68020_rtm = _limited(M68020_ONLY)
d68000_rtr = _fixed("rtr", FLOW_RETURN)
d68000_rts = _fixed("rts", FLOW_RETURN)
d68000_sbcd_rr = _source_first("sbcd    D%d, D%d", _dreg)
d68000_sbcd_mm = _source_first("sbcd    -(A%d), -(A%d)", _predec)


def d68000_scc(s):
    condition = CONDITIONS[(s.ir >> 8) & 0xF]
    ea = get_ea_mode_str(s, s.ir, 0)
    return _out("s%-2s     %s" % (condition, ea.text), "scc", None, [ea], FLOW_NORMAL, None, condition)


def d68000_stop(s):
    immediate = get_imm_str_s(s, 1)
    return _out("stop    %s" % immediate.text, "stop", None, [immediate])


d68000_sub_er_8 = _ea_to_dreg("sub.b   %s, D%d", 0)
d68000_sub_er_16 = _ea_to_dreg("sub.w   %s, D%d", 1)
d68000_sub_er_32 = _ea_to_dreg("sub.l   %s, D%d", 2)
d68000_sub_re_8 = _dreg_to_ea("sub.b   D%d, %s", 0)
d68000_sub_re_16 = _dreg_to_ea("sub.w   D%d, %s", 1)
d68000_sub_re_32 = _dreg_to_ea("sub.l   D%d, %s", 2)
d68000_suba_16 = _ea_to_areg("suba.w  %s, A%d", 1)
d68000_suba_32 = _ea_to_areg("suba.l  %s, A%d", 2)
d68000_subi_8 = _imm_to_ea("subi.b  %s, %s", 0, True)
d68000_subi_16 = _imm_to_ea("subi.w  %s, %s", 1, True)
d68000_subi_32 = _imm_to_ea("subi.l  %s, %s", 2, True)
d68000_subq_8 = _quick_to_ea("subq.b  #%d, %s", 0)
d68000_subq_16 = _quick_to_ea("subq.w  #%d, %s", 1)
d68000_subq_32 = _quick_to_ea("subq.l  #%d, %s", 2)
d68000_subx_rr_8 = _source_first("subx.b  D%d, D%d", _dreg)
d68000_subx_rr_16 = _source_first("subx.w  D%d, D%d", _dreg)
d68000_subx_rr_32 = _source_first("subx.l  D%d, D%d", _dreg)
d68000_subx_mm_8 = _source_first("subx.b  -(A%d), -(A%d)", _predec)
d68000_subx_mm_16 = _source_first("subx.w  -(A%d), -(A%d)", _predec)
d68000_subx_mm_32 = _source_first("subx.l  -(A%d), -(A%d)", _predec)


def d68000_swap(s):
    number = s.ir & 7
    return _out("swap    D%d" % number, "swap", None, [_dreg(number)])


d68000_tas = _ea_only("tas     %s", 0)


def d68000_trap(s):
    vector = s.ir & 0xF
    return _out("trap    #$%x" % vector, "trap", None, [_quick("#$%x" % vector, vector)])


d68020_trapcc_0 = _limited(M68020_PLUS)
d68020_trapcc_16 = _limited(M68020_PLUS)
d68020_trapcc_32 = _limited(M68020_PLUS)
d68000_trapv = _fixed("trapv")
d68000_tst_8 = _ea_only("tst.b   %s", 0)
d68020_tst_pcdi_8 = _limited(M68020_PLUS)
d68020_tst_pcix_8 = _limited(M68020_PLUS)
d68020_tst_i_8 = _limited(M68020_PLUS)
d68000_tst_16 = _ea_only("tst.w   %s", 1)
d68020_tst_a_16 = _limited(M68020_PLUS)
d68020_tst_pcdi_16 = _limited(M68020_PLUS)
d68020_tst_pcix_16 = _limited(M68020_PLUS)
d68020_tst_i_16 = _limited(M68020_PLUS)
d68000_tst_32 = _ea_only("tst.l   %s", 2)
d68020_tst_a_32 = _limited(M68020_PLUS)
d68020_tst_pcdi_32 = _limited(M68020_PLUS)
d68020_tst_pcix_32 = _limited(M68020_PLUS)
d68020_tst_i_32 = _limited(M68020_PLUS)


def d68000_unlk(s):
    number = s.ir & 7
    return _out("unlk    A%d" % number, "unlk", None, [_areg(number)])


d68020_unpk_rr = _limited(M68020_PLUS)
d68020_unpk_mm = _limited(M68020_PLUS)


# The MMU handlers below have no CPU check in MAME, so a 68000 gets their text too. A 68000 takes a
# line 1111 exception on all of them: they come back as FLOW_INVALID, with the effective address as their
# only operand when they have one. Their mnemonic is the first word of the text ('pmove', 'ptestw',
# 'mmu' for 'MMU 001 group', 'unknown'), except for the branches: 'pbcc' and 'pdbcc', with the condition.

def _unexecutable(text, operands=(), mnemonic=None, condition=None):
    if mnemonic is None:
        mnemonic = text.split(" ", 1)[0].lower()
    return _out(text, mnemonic, None, list(operands), FLOW_INVALID, None, condition)


def fc_to_string(modes):
    fc = modes & 0x1F
    if fc == 0:
        return "%sfc"
    if fc == 1:
        return "%dfc"
    if (fc >> 3) == 1:
        return "D%d" % (fc & 7)
    if (fc >> 3) == 2:
        return "#%d" % (fc & 7)
    return "unknown fc %x" % fc


def d68040_p000(s):
    ir = s.ir
    if (ir & 0xFFD8) == 0xF548:     # 68040 PTEST
        return _unexecutable("ptest%s  (A%d)" % ("r" if ir & 0x20 else "w", ir & 7))
    if (ir & 0xFFE0) == 0xF500:     # 68040 PFLUSH
        kind = (ir >> 3) & 3
        if kind == 0:
            return _unexecutable("pflushn (A%d)" % (ir & 7))
        if kind == 1:
            return _unexecutable("pflush  (A%d)" % (ir & 7))
        if kind == 2:
            return _unexecutable("pflushan")
        return _unexecutable("pflusha")
    return _unexecutable("unknown instruction: %04x" % ir)


def d68851_p000(s):
    modes = s.read_imm_16()
    # (MAME reads the effective address after the second word, so a third word follows it)
    ea = get_ea_mode_str(s, s.ir, 2)
    string = ea.text

    if (modes & 0xFDE0) == 0x2000:      # PLOAD
        if modes & 0x0200:
            return _unexecutable("pload   #%d, %s" % ((modes >> 10) & 7, string), [ea])
        return _unexecutable("pload   %s, #%d" % (string, (modes >> 10) & 7), [ea])

    if (modes & 0xE200) == 0x2000:      # PFLUSH
        return _unexecutable("pflushr %x, %x, %s" % (modes & 0x1F, (modes >> 5) & 0xF, string), [ea])

    if modes == 0xA000:                 # PFLUSHR
        return _unexecutable("pflushr %s" % string, [ea])

    if modes == 0x2800:                 # PVALID (FORMAT 1)
        return _unexecutable("pvalid  VAL, %s" % string, [ea])

    if (modes & 0xFFF8) == 0x2C00:      # PVALID (FORMAT 2)
        return _unexecutable("pvalid  A%d, %s" % (modes & 0xF, string), [ea])

    if (modes & 0xE000) == 0x8000:      # PTEST
        kind = "r" if modes & 0x200 else "w"
        fc = fc_to_string(modes)
        if modes & 0x100:
            text = "ptest%s  %s, %s, %d, @A%d" % (kind, fc, string, (modes >> 10) & 7, (modes >> 5) & 7)
            return _unexecutable(text, [ea])
        return _unexecutable("ptest%s  %s, %s, %d" % (kind, fc, string, (modes >> 10) & 7), [ea])

    # The register selected by the P-REG field depends on which of the three PMOVE forms this is
    preg = (modes >> 10) & 7
    regname = None
    has_fd = True
    form = (modes >> 13) & 7
    if form == 0:       # MC68030/040 transparent translation registers
        if preg == 2:
            regname = "tt0"
        elif preg == 3:
            regname = "tt1"
    elif form == 2:     # MC68851 registers (also TC/SRP/CRP on the MC68030/040)
        regname = MMU_REGISTERS[preg]
    elif form == 3:     # MC68030 to/from status reg
        regname = "mmusr"
        has_fd = False

    if regname is None:
        return _unexecutable("pmove [unknown form] %s" % string, [ea])

    opname = "pmovefd" if has_fd and (modes & 0x0100) else "pmove  "
    if modes & 0x0200:
        return _unexecutable("%s %s, %s" % (opname, regname, string), [ea])
    return _unexecutable("%s %s, %s" % (opname, string, regname), [ea])


def d68851_pbcc16(s):
    temp_pc = s.pc
    condition = MMU_CONDITIONS[s.ir & 0xF]
    target = (temp_pc + _s16(s.read_imm_16())) & MASK32
    return _unexecutable("pb%s %x" % (condition, target), (), "pbcc", condition)


def d68851_pbcc32(s):
    temp_pc = s.pc
    condition = MMU_CONDITIONS[s.ir & 0xF]
    target = (temp_pc + _s32(s.read_imm_32())) & MASK32
    return _unexecutable("pb%s %x" % (condition, target), (), "pbcc", condition)


def d68851_pdbcc(s):
    # (MAME prints it 'pb' too)
    temp_pc = s.pc
    modes = s.read_imm_16()
    condition = MMU_CONDITIONS[modes & 0xF]
    target = (temp_pc + _s16(s.read_imm_16())) & MASK32
    return _unexecutable("pb%s %x" % (condition, target), (), "pdbcc", condition)


def d68851_p001(s):
    return _unexecutable("MMU 001 group")


d68040_fbcc_16 = _limited(M68030_PLUS)
d68040_fbcc_32 = _limited(M68030_PLUS)


# ----------------------------------------------------------------------------------------------------
# Instruction table builder
# ----------------------------------------------------------------------------------------------------

# m_opcode_info, in MAME's order: handler, mask, match, allowed effective address modes. EA masks:
# 800 data register direct, 400 address register direct, 200 address register indirect, 100 ARI
# postincrement, 80 ARI predecrement, 40 ARI displacement, 20 ARI index, 10 absolute short,
# 8 absolute long, 4 immediate / sr, 2 pc displacement, 1 pc index
OPCODE_INFO = (
    ("d68000_1010", 0xf000, 0xa000, 0x000),
    ("d68000_1111", 0xf000, 0xf000, 0x000),
    ("d68000_abcd_rr", 0xf1f8, 0xc100, 0x000),
    ("d68000_abcd_mm", 0xf1f8, 0xc108, 0x000),
    ("d68000_add_er_8", 0xf1c0, 0xd000, 0xbff),
    ("d68000_add_er_16", 0xf1c0, 0xd040, 0xfff),
    ("d68000_add_er_32", 0xf1c0, 0xd080, 0xfff),
    ("d68000_add_re_8", 0xf1c0, 0xd100, 0x3f8),
    ("d68000_add_re_16", 0xf1c0, 0xd140, 0x3f8),
    ("d68000_add_re_32", 0xf1c0, 0xd180, 0x3f8),
    ("d68000_adda_16", 0xf1c0, 0xd0c0, 0xfff),
    ("d68000_adda_32", 0xf1c0, 0xd1c0, 0xfff),
    ("d68000_addi_8", 0xffc0, 0x0600, 0xbf8),
    ("d68000_addi_16", 0xffc0, 0x0640, 0xbf8),
    ("d68000_addi_32", 0xffc0, 0x0680, 0xbf8),
    ("d68000_addq_8", 0xf1c0, 0x5000, 0xbf8),
    ("d68000_addq_16", 0xf1c0, 0x5040, 0xff8),
    ("d68000_addq_32", 0xf1c0, 0x5080, 0xff8),
    ("d68000_addx_rr_8", 0xf1f8, 0xd100, 0x000),
    ("d68000_addx_rr_16", 0xf1f8, 0xd140, 0x000),
    ("d68000_addx_rr_32", 0xf1f8, 0xd180, 0x000),
    ("d68000_addx_mm_8", 0xf1f8, 0xd108, 0x000),
    ("d68000_addx_mm_16", 0xf1f8, 0xd148, 0x000),
    ("d68000_addx_mm_32", 0xf1f8, 0xd188, 0x000),
    ("d68000_and_er_8", 0xf1c0, 0xc000, 0xbff),
    ("d68000_and_er_16", 0xf1c0, 0xc040, 0xbff),
    ("d68000_and_er_32", 0xf1c0, 0xc080, 0xbff),
    ("d68000_and_re_8", 0xf1c0, 0xc100, 0x3f8),
    ("d68000_and_re_16", 0xf1c0, 0xc140, 0x3f8),
    ("d68000_and_re_32", 0xf1c0, 0xc180, 0x3f8),
    ("d68000_andi_to_ccr", 0xffff, 0x023c, 0x000),
    ("d68000_andi_to_sr", 0xffff, 0x027c, 0x000),
    ("d68000_andi_8", 0xffc0, 0x0200, 0xbf8),
    ("d68000_andi_16", 0xffc0, 0x0240, 0xbf8),
    ("d68000_andi_32", 0xffc0, 0x0280, 0xbf8),
    ("d68000_asr_s_8", 0xf1f8, 0xe000, 0x000),
    ("d68000_asr_s_16", 0xf1f8, 0xe040, 0x000),
    ("d68000_asr_s_32", 0xf1f8, 0xe080, 0x000),
    ("d68000_asr_r_8", 0xf1f8, 0xe020, 0x000),
    ("d68000_asr_r_16", 0xf1f8, 0xe060, 0x000),
    ("d68000_asr_r_32", 0xf1f8, 0xe0a0, 0x000),
    ("d68000_asr_ea", 0xffc0, 0xe0c0, 0x3f8),
    ("d68000_asl_s_8", 0xf1f8, 0xe100, 0x000),
    ("d68000_asl_s_16", 0xf1f8, 0xe140, 0x000),
    ("d68000_asl_s_32", 0xf1f8, 0xe180, 0x000),
    ("d68000_asl_r_8", 0xf1f8, 0xe120, 0x000),
    ("d68000_asl_r_16", 0xf1f8, 0xe160, 0x000),
    ("d68000_asl_r_32", 0xf1f8, 0xe1a0, 0x000),
    ("d68000_asl_ea", 0xffc0, 0xe1c0, 0x3f8),
    ("d68000_bcc_8", 0xf000, 0x6000, 0x000),
    ("d68000_bcc_16", 0xf0ff, 0x6000, 0x000),
    ("d68020_bcc_32", 0xf0ff, 0x60ff, 0x000),
    ("d68000_bchg_r", 0xf1c0, 0x0140, 0xbf8),
    ("d68000_bchg_s", 0xffc0, 0x0840, 0xbf8),
    ("d68000_bclr_r", 0xf1c0, 0x0180, 0xbf8),
    ("d68000_bclr_s", 0xffc0, 0x0880, 0xbf8),
    ("d68020_bfchg", 0xffc0, 0xeac0, 0xa78),
    ("d68020_bfclr", 0xffc0, 0xecc0, 0xa78),
    ("d68020_bfexts", 0xffc0, 0xebc0, 0xa7b),
    ("d68020_bfextu", 0xffc0, 0xe9c0, 0xa7b),
    ("d68020_bfffo", 0xffc0, 0xedc0, 0xa7b),
    ("d68020_bfins", 0xffc0, 0xefc0, 0xa78),
    ("d68020_bfset", 0xffc0, 0xeec0, 0xa78),
    ("d68020_bftst", 0xffc0, 0xe8c0, 0xa7b),
    ("d68881_ftrap", 0xfff8, 0xf278, 0x000),
    ("d68010_bkpt", 0xfff8, 0x4848, 0x000),
    ("d68000_bra_8", 0xff00, 0x6000, 0x000),
    ("d68000_bra_16", 0xffff, 0x6000, 0x000),
    ("d68020_bra_32", 0xffff, 0x60ff, 0x000),
    ("d68000_bset_r", 0xf1c0, 0x01c0, 0xbf8),
    ("d68000_bset_s", 0xffc0, 0x08c0, 0xbf8),
    ("d68000_bsr_8", 0xff00, 0x6100, 0x000),
    ("d68000_bsr_16", 0xffff, 0x6100, 0x000),
    ("d68020_bsr_32", 0xffff, 0x61ff, 0x000),
    ("d68000_btst_r", 0xf1c0, 0x0100, 0xbff),
    ("d68000_btst_s", 0xffc0, 0x0800, 0xbfb),
    ("d68020_callm", 0xffc0, 0x06c0, 0x27b),
    ("d68020_cas_8", 0xffc0, 0x0ac0, 0x3f8),
    ("d68020_cas_16", 0xffc0, 0x0cc0, 0x3f8),
    ("d68020_cas_32", 0xffc0, 0x0ec0, 0x3f8),
    ("d68020_cas2_16", 0xffff, 0x0cfc, 0x000),
    ("d68020_cas2_32", 0xffff, 0x0efc, 0x000),
    ("d68000_chk_16", 0xf1c0, 0x4180, 0xbff),
    ("d68020_chk_32", 0xf1c0, 0x4100, 0xbff),
    ("d68020_chk2_cmp2_8", 0xffc0, 0x00c0, 0x27b),
    ("d68020_chk2_cmp2_16", 0xffc0, 0x02c0, 0x27b),
    ("d68020_chk2_cmp2_32", 0xffc0, 0x04c0, 0x27b),
    ("d68040_cinv", 0xff20, 0xf400, 0x000),
    ("d68000_clr_8", 0xffc0, 0x4200, 0xbf8),
    ("d68000_clr_16", 0xffc0, 0x4240, 0xbf8),
    ("d68000_clr_32", 0xffc0, 0x4280, 0xbf8),
    ("d68000_cmp_8", 0xf1c0, 0xb000, 0xbff),
    ("d68000_cmp_16", 0xf1c0, 0xb040, 0xfff),
    ("d68000_cmp_32", 0xf1c0, 0xb080, 0xfff),
    ("d68000_cmpa_16", 0xf1c0, 0xb0c0, 0xfff),
    ("d68000_cmpa_32", 0xf1c0, 0xb1c0, 0xfff),
    ("d68000_cmpi_8", 0xffc0, 0x0c00, 0xbf8),
    ("d68020_cmpi_pcdi_8", 0xffff, 0x0c3a, 0x000),
    ("d68020_cmpi_pcix_8", 0xffff, 0x0c3b, 0x000),
    ("d68000_cmpi_16", 0xffc0, 0x0c40, 0xbf8),
    ("d68020_cmpi_pcdi_16", 0xffff, 0x0c7a, 0x000),
    ("d68020_cmpi_pcix_16", 0xffff, 0x0c7b, 0x000),
    ("d68000_cmpi_32", 0xffc0, 0x0c80, 0xbf8),
    ("d68020_cmpi_pcdi_32", 0xffff, 0x0cba, 0x000),
    ("d68020_cmpi_pcix_32", 0xffff, 0x0cbb, 0x000),
    ("d68000_cmpm_8", 0xf1f8, 0xb108, 0x000),
    ("d68000_cmpm_16", 0xf1f8, 0xb148, 0x000),
    ("d68000_cmpm_32", 0xf1f8, 0xb188, 0x000),
    ("d68020_cpbcc_16", 0xf1c0, 0xf080, 0x000),
    ("d68020_cpbcc_32", 0xf1c0, 0xf0c0, 0x000),
    ("d68020_cpdbcc", 0xf1f8, 0xf048, 0x000),
    ("d68020_cpgen", 0xf1c0, 0xf000, 0x000),
    ("d68020_cprestore", 0xf1c0, 0xf140, 0x37f),
    ("d68020_cpsave", 0xf1c0, 0xf100, 0x2f8),
    ("d68020_cpscc", 0xf1c0, 0xf040, 0xbf8),
    ("d68020_cptrapcc_0", 0xf1ff, 0xf07c, 0x000),
    ("d68020_cptrapcc_16", 0xf1ff, 0xf07a, 0x000),
    ("d68020_cptrapcc_32", 0xf1ff, 0xf07b, 0x000),
    ("d68040_cpush", 0xff20, 0xf420, 0x000),
    ("d68000_dbcc", 0xf0f8, 0x50c8, 0x000),
    ("d68000_dbra", 0xfff8, 0x51c8, 0x000),
    ("d68000_divs", 0xf1c0, 0x81c0, 0xbff),
    ("d68000_divu", 0xf1c0, 0x80c0, 0xbff),
    ("d68020_divl", 0xffc0, 0x4c40, 0xbff),
    ("d68000_eor_8", 0xf1c0, 0xb100, 0xbf8),
    ("d68000_eor_16", 0xf1c0, 0xb140, 0xbf8),
    ("d68000_eor_32", 0xf1c0, 0xb180, 0xbf8),
    ("d68000_eori_to_ccr", 0xffff, 0x0a3c, 0x000),
    ("d68000_eori_to_sr", 0xffff, 0x0a7c, 0x000),
    ("d68000_eori_8", 0xffc0, 0x0a00, 0xbf8),
    ("d68000_eori_16", 0xffc0, 0x0a40, 0xbf8),
    ("d68000_eori_32", 0xffc0, 0x0a80, 0xbf8),
    ("d68000_exg_dd", 0xf1f8, 0xc140, 0x000),
    ("d68000_exg_aa", 0xf1f8, 0xc148, 0x000),
    ("d68000_exg_da", 0xf1f8, 0xc188, 0x000),
    ("d68020_extb_32", 0xfff8, 0x49c0, 0x000),
    ("d68000_ext_16", 0xfff8, 0x4880, 0x000),
    ("d68000_ext_32", 0xfff8, 0x48c0, 0x000),
    ("d68040_fpu", 0xffc0, 0xf200, 0x000),
    ("dcoldfire_halt", 0xffff, 0x4ac8, 0x000),
    ("d68000_illegal", 0xffff, 0x4afc, 0x000),
    ("d68000_jmp", 0xffc0, 0x4ec0, 0x27b),
    ("d68000_jsr", 0xffc0, 0x4e80, 0x27b),
    ("d68000_lea", 0xf1c0, 0x41c0, 0x27b),
    ("d68000_link_16", 0xfff8, 0x4e50, 0x000),
    ("d68020_link_32", 0xfff8, 0x4808, 0x000),
    ("d68000_lsr_s_8", 0xf1f8, 0xe008, 0x000),
    ("d68000_lsr_s_16", 0xf1f8, 0xe048, 0x000),
    ("d68000_lsr_s_32", 0xf1f8, 0xe088, 0x000),
    ("d68000_lsr_r_8", 0xf1f8, 0xe028, 0x000),
    ("d68000_lsr_r_16", 0xf1f8, 0xe068, 0x000),
    ("d68000_lsr_r_32", 0xf1f8, 0xe0a8, 0x000),
    ("d68000_lsr_ea", 0xffc0, 0xe2c0, 0x3f8),
    ("d68000_lsl_s_8", 0xf1f8, 0xe108, 0x000),
    ("d68000_lsl_s_16", 0xf1f8, 0xe148, 0x000),
    ("d68000_lsl_s_32", 0xf1f8, 0xe188, 0x000),
    ("d68000_lsl_r_8", 0xf1f8, 0xe128, 0x000),
    ("d68000_lsl_r_16", 0xf1f8, 0xe168, 0x000),
    ("d68000_lsl_r_32", 0xf1f8, 0xe1a8, 0x000),
    ("d68000_lsl_ea", 0xffc0, 0xe3c0, 0x3f8),
    ("d68000_move_8", 0xf000, 0x1000, 0xbff),
    ("d68000_move_16", 0xf000, 0x3000, 0xfff),
    ("d68000_move_32", 0xf000, 0x2000, 0xfff),
    ("d68000_movea_16", 0xf1c0, 0x3040, 0xfff),
    ("d68000_movea_32", 0xf1c0, 0x2040, 0xfff),
    ("d68000_move_to_ccr", 0xffc0, 0x44c0, 0xbff),
    ("d68010_move_fr_ccr", 0xffc0, 0x42c0, 0xbf8),
    ("d68000_move_to_sr", 0xffc0, 0x46c0, 0xbff),
    ("d68000_move_fr_sr", 0xffc0, 0x40c0, 0xbf8),
    ("d68000_move_to_usp", 0xfff8, 0x4e60, 0x000),
    ("d68000_move_fr_usp", 0xfff8, 0x4e68, 0x000),
    ("d68010_movec", 0xfffe, 0x4e7a, 0x000),
    ("d68000_movem_pd_16", 0xfff8, 0x48a0, 0x000),
    ("d68000_movem_pd_32", 0xfff8, 0x48e0, 0x000),
    ("d68000_movem_re_16", 0xffc0, 0x4880, 0x2f8),
    ("d68000_movem_re_32", 0xffc0, 0x48c0, 0x2f8),
    ("d68000_movem_er_16", 0xffc0, 0x4c80, 0x37b),
    ("d68000_movem_er_32", 0xffc0, 0x4cc0, 0x37b),
    ("d68000_movep_er_16", 0xf1f8, 0x0108, 0x000),
    ("d68000_movep_er_32", 0xf1f8, 0x0148, 0x000),
    ("d68000_movep_re_16", 0xf1f8, 0x0188, 0x000),
    ("d68000_movep_re_32", 0xf1f8, 0x01c8, 0x000),
    ("d68010_moves_8", 0xffc0, 0x0e00, 0x3f8),
    ("d68010_moves_16", 0xffc0, 0x0e40, 0x3f8),
    ("d68010_moves_32", 0xffc0, 0x0e80, 0x3f8),
    ("d68000_moveq", 0xf100, 0x7000, 0x000),
    ("d68040_move16_pi_pi", 0xfff8, 0xf620, 0x000),
    ("d68040_move16_pi_al", 0xfff8, 0xf600, 0x000),
    ("d68040_move16_al_pi", 0xfff8, 0xf608, 0x000),
    ("d68040_move16_ai_al", 0xfff8, 0xf610, 0x000),
    ("d68040_move16_al_ai", 0xfff8, 0xf618, 0x000),
    ("d68000_muls", 0xf1c0, 0xc1c0, 0xbff),
    ("d68000_mulu", 0xf1c0, 0xc0c0, 0xbff),
    ("d68020_mull", 0xffc0, 0x4c00, 0xbff),
    ("d68000_nbcd", 0xffc0, 0x4800, 0xbf8),
    ("d68000_neg_8", 0xffc0, 0x4400, 0xbf8),
    ("d68000_neg_16", 0xffc0, 0x4440, 0xbf8),
    ("d68000_neg_32", 0xffc0, 0x4480, 0xbf8),
    ("d68000_negx_8", 0xffc0, 0x4000, 0xbf8),
    ("d68000_negx_16", 0xffc0, 0x4040, 0xbf8),
    ("d68000_negx_32", 0xffc0, 0x4080, 0xbf8),
    ("d68000_nop", 0xffff, 0x4e71, 0x000),
    ("d68000_not_8", 0xffc0, 0x4600, 0xbf8),
    ("d68000_not_16", 0xffc0, 0x4640, 0xbf8),
    ("d68000_not_32", 0xffc0, 0x4680, 0xbf8),
    ("d68000_or_er_8", 0xf1c0, 0x8000, 0xbff),
    ("d68000_or_er_16", 0xf1c0, 0x8040, 0xbff),
    ("d68000_or_er_32", 0xf1c0, 0x8080, 0xbff),
    ("d68000_or_re_8", 0xf1c0, 0x8100, 0x3f8),
    ("d68000_or_re_16", 0xf1c0, 0x8140, 0x3f8),
    ("d68000_or_re_32", 0xf1c0, 0x8180, 0x3f8),
    ("d68000_ori_to_ccr", 0xffff, 0x003c, 0x000),
    ("d68000_ori_to_sr", 0xffff, 0x007c, 0x000),
    ("d68000_ori_8", 0xffc0, 0x0000, 0xbf8),
    ("d68000_ori_16", 0xffc0, 0x0040, 0xbf8),
    ("d68000_ori_32", 0xffc0, 0x0080, 0xbf8),
    ("d68020_pack_rr", 0xf1f8, 0x8140, 0x000),
    ("d68020_pack_mm", 0xf1f8, 0x8148, 0x000),
    ("d68000_pea", 0xffc0, 0x4840, 0x27b),
    ("d68040_p000", 0xff80, 0xf500, 0x000),
    ("d68000_reset", 0xffff, 0x4e70, 0x000),
    ("d68000_ror_s_8", 0xf1f8, 0xe018, 0x000),
    ("d68000_ror_s_16", 0xf1f8, 0xe058, 0x000),
    ("d68000_ror_s_32", 0xf1f8, 0xe098, 0x000),
    ("d68000_ror_r_8", 0xf1f8, 0xe038, 0x000),
    ("d68000_ror_r_16", 0xf1f8, 0xe078, 0x000),
    ("d68000_ror_r_32", 0xf1f8, 0xe0b8, 0x000),
    ("d68000_ror_ea", 0xffc0, 0xe6c0, 0x3f8),
    ("d68000_rol_s_8", 0xf1f8, 0xe118, 0x000),
    ("d68000_rol_s_16", 0xf1f8, 0xe158, 0x000),
    ("d68000_rol_s_32", 0xf1f8, 0xe198, 0x000),
    ("d68000_rol_r_8", 0xf1f8, 0xe138, 0x000),
    ("d68000_rol_r_16", 0xf1f8, 0xe178, 0x000),
    ("d68000_rol_r_32", 0xf1f8, 0xe1b8, 0x000),
    ("d68000_rol_ea", 0xffc0, 0xe7c0, 0x3f8),
    ("d68000_roxr_s_8", 0xf1f8, 0xe010, 0x000),
    ("d68000_roxr_s_16", 0xf1f8, 0xe050, 0x000),
    ("d68000_roxr_s_32", 0xf1f8, 0xe090, 0x000),
    ("d68000_roxr_r_8", 0xf1f8, 0xe030, 0x000),
    ("d68000_roxr_r_16", 0xf1f8, 0xe070, 0x000),
    ("d68000_roxr_r_32", 0xf1f8, 0xe0b0, 0x000),
    ("d68000_roxr_ea", 0xffc0, 0xe4c0, 0x3f8),
    ("d68000_roxl_s_8", 0xf1f8, 0xe110, 0x000),
    ("d68000_roxl_s_16", 0xf1f8, 0xe150, 0x000),
    ("d68000_roxl_s_32", 0xf1f8, 0xe190, 0x000),
    ("d68000_roxl_r_8", 0xf1f8, 0xe130, 0x000),
    ("d68000_roxl_r_16", 0xf1f8, 0xe170, 0x000),
    ("d68000_roxl_r_32", 0xf1f8, 0xe1b0, 0x000),
    ("d68000_roxl_ea", 0xffc0, 0xe5c0, 0x3f8),
    ("d68010_rtd", 0xffff, 0x4e74, 0x000),
    ("d68000_rte", 0xffff, 0x4e73, 0x000),
    ("d68020_rtm", 0xfff0, 0x06c0, 0x000),
    ("d68000_rtr", 0xffff, 0x4e77, 0x000),
    ("d68000_rts", 0xffff, 0x4e75, 0x000),
    ("d68000_sbcd_rr", 0xf1f8, 0x8100, 0x000),
    ("d68000_sbcd_mm", 0xf1f8, 0x8108, 0x000),
    ("d68000_scc", 0xf0c0, 0x50c0, 0xbf8),
    ("d68000_stop", 0xffff, 0x4e72, 0x000),
    ("d68000_sub_er_8", 0xf1c0, 0x9000, 0xbff),
    ("d68000_sub_er_16", 0xf1c0, 0x9040, 0xfff),
    ("d68000_sub_er_32", 0xf1c0, 0x9080, 0xfff),
    ("d68000_sub_re_8", 0xf1c0, 0x9100, 0x3f8),
    ("d68000_sub_re_16", 0xf1c0, 0x9140, 0x3f8),
    ("d68000_sub_re_32", 0xf1c0, 0x9180, 0x3f8),
    ("d68000_suba_16", 0xf1c0, 0x90c0, 0xfff),
    ("d68000_suba_32", 0xf1c0, 0x91c0, 0xfff),
    ("d68000_subi_8", 0xffc0, 0x0400, 0xbf8),
    ("d68000_subi_16", 0xffc0, 0x0440, 0xbf8),
    ("d68000_subi_32", 0xffc0, 0x0480, 0xbf8),
    ("d68000_subq_8", 0xf1c0, 0x5100, 0xbf8),
    ("d68000_subq_16", 0xf1c0, 0x5140, 0xff8),
    ("d68000_subq_32", 0xf1c0, 0x5180, 0xff8),
    ("d68000_subx_rr_8", 0xf1f8, 0x9100, 0x000),
    ("d68000_subx_rr_16", 0xf1f8, 0x9140, 0x000),
    ("d68000_subx_rr_32", 0xf1f8, 0x9180, 0x000),
    ("d68000_subx_mm_8", 0xf1f8, 0x9108, 0x000),
    ("d68000_subx_mm_16", 0xf1f8, 0x9148, 0x000),
    ("d68000_subx_mm_32", 0xf1f8, 0x9188, 0x000),
    ("d68000_swap", 0xfff8, 0x4840, 0x000),
    ("d68000_tas", 0xffc0, 0x4ac0, 0xbf8),
    ("d68000_trap", 0xfff0, 0x4e40, 0x000),
    ("d68020_trapcc_0", 0xf0ff, 0x50fc, 0x000),
    ("d68020_trapcc_16", 0xf0ff, 0x50fa, 0x000),
    ("d68020_trapcc_32", 0xf0ff, 0x50fb, 0x000),
    ("d68000_trapv", 0xffff, 0x4e76, 0x000),
    ("d68000_tst_8", 0xffc0, 0x4a00, 0xbf8),
    ("d68020_tst_pcdi_8", 0xffff, 0x4a3a, 0x000),
    ("d68020_tst_pcix_8", 0xffff, 0x4a3b, 0x000),
    ("d68020_tst_i_8", 0xffff, 0x4a3c, 0x000),
    ("d68000_tst_16", 0xffc0, 0x4a40, 0xbf8),
    ("d68020_tst_a_16", 0xfff8, 0x4a48, 0x000),
    ("d68020_tst_pcdi_16", 0xffff, 0x4a7a, 0x000),
    ("d68020_tst_pcix_16", 0xffff, 0x4a7b, 0x000),
    ("d68020_tst_i_16", 0xffff, 0x4a7c, 0x000),
    ("d68000_tst_32", 0xffc0, 0x4a80, 0xbf8),
    ("d68020_tst_a_32", 0xfff8, 0x4a88, 0x000),
    ("d68020_tst_pcdi_32", 0xffff, 0x4aba, 0x000),
    ("d68020_tst_pcix_32", 0xffff, 0x4abb, 0x000),
    ("d68020_tst_i_32", 0xffff, 0x4abc, 0x000),
    ("d68000_unlk", 0xfff8, 0x4e58, 0x000),
    ("d68020_unpk_rr", 0xf1f8, 0x8180, 0x000),
    ("d68020_unpk_mm", 0xf1f8, 0x8188, 0x000),
    ("d68851_p000", 0xffc0, 0xf000, 0x000),
    ("d68851_pbcc16", 0xffc0, 0xf080, 0x000),
    ("d68851_pbcc32", 0xffc0, 0xf0c0, 0x000),
    ("d68851_pdbcc", 0xfff8, 0xf048, 0x000),
    ("d68851_p001", 0xffc0, 0xf040, 0x000),
    ("d68040_fbcc_16", 0xffc0, 0xf280, 0x000),
    ("d68040_fbcc_32", 0xffc0, 0xf2c0, 0x000),
)

# valid_ea: the bit of the EA mask for each value of the low 6 bits of the opcode
_EA_MODE_BITS = tuple([0x800] * 8 + [0x400] * 8 + [0x200] * 8 + [0x100] * 8 + [0x080] * 8 + [0x040] * 8
                      + [0x020] * 8 + [0x010, 0x008, 0x002, 0x001, 0x004, 0, 0, 0])
_MOVES = ("d68000_move_8", "d68000_move_16", "d68000_move_32")


def valid_ea(opcode, mask):
    """Whether the effective address in the low 6 bits of 'opcode' is one of the modes in 'mask'."""
    if mask == 0:
        return True
    return (mask & _EA_MODE_BITS[opcode & 0x3F]) != 0


def compare_nof_true_bits(entry):
    """Sort key: the number of bits in the mask, so that sorting gives the most specific entry first."""
    return -bin(entry[1]).count("1")


def build_opcode_table():
    """The handler of every opcode, as build_opcode_table builds m_instruction_table: the first entry,
    most mask bits first, whose mask and match fit, whose destination passes for MOVE, and whose EA mask
    takes the opcode; d68000_illegal when none does. Returns (handlers, handler names), 65536 each.

    MAME loops over the opcodes and, for each, over the sorted entries. This loops the other way, which
    gives the same table: each entry, in sorted order, takes the opcodes it fits that no entry before it
    took. Entries with the same number of mask bits keep MAME's table order (see the module docstring)."""
    namespace = globals()
    names = [None] * 0x10000
    for entry in sorted(OPCODE_INFO, key=compare_nof_true_bits):
        name, mask, match, ea_mask = entry
        if match & ~mask & 0xFFFF:
            continue                    # (could never match)
        free = ~mask & 0xFFFF
        is_move = name in _MOVES
        variable = free
        while True:
            # every opcode that fits: the match with any combination of the bits outside the mask
            opcode = match | variable
            if names[opcode] is None:
                if (not is_move or valid_ea(((opcode >> 9) & 7) | ((opcode >> 3) & 0x38), 0xbf8)) \
                        and valid_ea(opcode, ea_mask):
                    names[opcode] = name
            if variable == 0:
                break
            variable = (variable - 1) & free
    names = [name or "d68000_illegal" for name in names]
    handlers = {name: namespace[name] for name, _mask, _match, _ea_mask in OPCODE_INFO}
    handlers["d68000_illegal"] = d68000_illegal
    return [handlers[name] for name in names], names


_TABLE, _TABLE_NAMES = build_opcode_table()


def handler_name(opcode):
    """MAME's handler for an opcode, e.g. 'd68000_move_16'."""
    return _TABLE_NAMES[opcode & 0xFFFF]


def decode(mem, address):
    """Decode the instruction at 'address'. Never raises: undecodable words come back as FLOW_INVALID.

    The words are read from mem (r8/r16/r32, big endian) at address, address + 2 and so on, unmasked:
    the memory object maps them (FlatMemory wraps them to 24 bits, as the oracle does)."""
    s = _State(mem, address)
    s.ir = s.read_imm_16()
    text, mnemonic, size, operands, flow, target, condition = _TABLE[s.ir](s)
    for operand in operands:
        if operand.mode == "invalid":
            flow = FLOW_INVALID
            target = None
            break
    return Instruction(address, (s.pc - address) & MASK32, mnemonic, size, text, flow, operands, target,
                       condition)
