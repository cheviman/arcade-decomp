"""
Name:        typed.py
Description: the program ROM's tables as symbols.txt types them: one record a line under a layout line that
             names the columns, numbers as numbers and addresses as the names of what they reach
Version:     0.1.0
Created:     27-10-2026
Modified:    27-10-2026
Author:      Sebastian Toyos

A piece symbols.txt gives a type (a 'table NAME TYPE' line, or a table of records the code reads by index)
is written as its elements. The first line is the layout: 'layout TYPE' for values (u8, s8, u16, s16, u32,
s32), 'layout STRUCT SIZE name:type ...' for records, the columns in the order of their bytes (name:u16[4]
is four values one after the other; at_OO:u8 a byte at offset OO no field of symbols.txt names). Then the
elements, a record a line (values eight a line, four when they are addresses), each line after the number
of its first element in brackets:

    layout road_row_record 24 animation:u16 palette:u16 flags:u8 ...
    [0]  0x0123  0x0040  0x01 ...

Unsigned values are hex, signed ones decimal. A column of 32-bit values every one of which is the address
of a named place (or 0 or FFFFFFFF) is written as those names: the place's own, name+0xNN inside a named
data piece or a function's code, a variable's for a work RAM address. What follows the last whole element
goes as 'bytes' lines. Every piece comes back byte for byte: the layout line says how to read the rest, so
a reader needs nothing but the file and the names.
"""
import struct
from bisect import bisect_right

from . import common

VALUE_TYPES = {"u8": "B", "s8": "b", "u16": "H", "s16": "h", "u32": "I", "s32": "i"}
# the columns a layout line may have besides the value types: u24, three bytes (an address with a byte of
# something else above it: the 32-bit value split as u24 and u8); text, a quoted string the record holds
# closed by a 0 byte (a record with one is as long as its string: the layout's size is 0)
# rgb555 and rgb555alt, a 16-bit colour as #RRGGBB (five bits a component, scaled to eight) and a '+' when
# bit 15 is set: as the palette RAM holds it (blue, green and red from bit 10 down) or in the palette
# window's alternate format (the low bit of blue, green and red in bits 14-12, the rest 4:4:4)
SIZES = {"u8": 1, "s8": 1, "u16": 2, "s16": 2, "u24": 3, "u32": 4, "s32": 4, "rgb555": 2, "rgb555alt": 2}
COLUMN_TYPES = set(SIZES) | {"text"}
SENTINELS = (0, 0xFFFFFFFF)     # values a column of addresses holds besides them: none, an end
LOWEST_ADDRESS = 0x400          # below it, a value is a number (the vectors are no data the tables point at)
PER_LINE = {1: 8, 2: 8, 4: 4}

_TYPES = {}     # piece name -> its element type (a value type or a structure's name)
_LAYOUTS = {}   # structure name -> (size, [(column name, offset, value type)])
_CODE = []      # sorted addresses of the program's functions, for names inside their code


def _load():
    if _TYPES:
        return
    import disassemble
    names = disassemble.load_names(common.ROOT / "symbols.txt")
    for _, (name, kind) in names.tables.items():
        _TYPES[name] = kind
    for _, (name, kind) in names.record_tables.items():
        _TYPES[name] = kind
    for structure, size in names.structs.items():
        columns = _columns(names, structure, size)
        if columns is not None:
            _LAYOUTS[structure] = (size, columns)
    _CODE.extend(sorted(address for address, symbol in disassemble.load_symbols(common.ROOT / "symbols.txt").items()
                        if symbol.kind == "func"))


def _columns(names, structure, size, prefix="", base=0, depth=0):
    """[(name, offset, value type)] that tile a structure of 'size' bytes: its fields in the order of their
    offsets (the first of fields that overlap), arrays as as many columns as they hold, nested structures as
    their own columns, and the bytes no field covers as at_OO:u8; None when a field cannot be laid out."""
    fields = sorted((offset, field) for (owner, offset), field in names.fields.items() if owner == structure)
    columns, at = [], 0
    for offset, (name, kind) in fields:
        if offset < at:
            continue    # a field over another one (a narrower view of the same bytes)
        count = names.field_arrays.get((structure, offset), 1) or 1
        if kind in VALUE_TYPES:
            width = struct.calcsize(VALUE_TYPES[kind])
            if offset + width * count > size:
                continue
            columns += [(f"at_{base + at + gap:02X}", base + at + gap, "u8") for gap in range(offset - at)]
            columns += [(f"{prefix}{name}" + (f"[{index}]" if count > 1 else ""), base + offset + index * width, kind)
                        for index in range(count)]
            at = offset + width * count
        elif kind in names.structs and depth < 4:
            inner_size = names.structs[kind]
            if offset + inner_size * count > size:
                continue
            columns += [(f"at_{base + at + gap:02X}", base + at + gap, "u8") for gap in range(offset - at)]
            for index in range(count):
                inner = _columns(names, kind, inner_size, f"{prefix}{name}" + (f"[{index}]" if count > 1 else "") + ".",
                                 base + offset + index * inner_size, depth + 1)
                if inner is None:
                    return None
                columns += inner
            at = offset + inner_size * count
        else:
            return None
    columns += [(f"at_{base + at + gap:02X}", base + at + gap, "u8") for gap in range(size - at)]
    return columns


# ---- addresses as names ----

def _resolve(token):
    """The address a name (name, name+0xNN) stands for, or None."""
    name, _, offset = token.partition("+")
    found = common._names()["name"].get(name)
    if found is None:
        return None
    try:
        return found + (int(offset, 16) if offset else 0)
    except ValueError:
        return None


_RESOLVER = [_resolve]    # what reads a name while from_text() runs: these names, or another program's


def name_for(value):
    """The name of address 'value' that reads back as exactly this value, or None."""
    if value in SENTINELS or value < LOWEST_ADDRESS:
        return None
    name = common.name_of(value)
    if name is None and value < 0x200000:
        from data_formats import _v60_instructions
        if value in _v60_instructions:
            index = bisect_right(_CODE, value) - 1
            if index >= 0:
                name = f"{common._names()['address'][_CODE[index]]}+0x{value - _CODE[index]:X}"
    return name if name is not None and _resolve(name) == value else None


def _addresses(values):
    """Whether a column of 32-bit values reads as addresses: every one a named place, or a sentinel."""
    seen = False
    for value in values:
        if value in SENTINELS:
            continue
        if name_for(value) is None:
            return False
        seen = True
    return seen


# ---- text ----

def _value_text(value, kind, as_name):
    if as_name:
        return name_for(value) or f"0x{value:08X}"
    if kind[0] == "s":
        return str(value)
    return f"0x{value:0{struct.calcsize(VALUE_TYPES[kind]) * 2}X}"


def color_text(value, kind):
    """A 16-bit colour (rgb555 or rgb555alt) as #RRGGBB, with '+' when bit 15 is set."""
    if kind == "rgb555alt":
        red = ((value >> 12) & 1) | ((value << 1) & 0x1E)
        green = ((value >> 13) & 1) | ((value >> 3) & 0x1E)
        blue = ((value >> 14) & 1) | ((value >> 7) & 0x1E)
    else:
        red, green, blue = value & 0x1F, (value >> 5) & 0x1F, (value >> 10) & 0x1F
    eight = [(part << 3) | (part >> 2) for part in (red, green, blue)]
    return "#" + "".join(f"{part:02X}" for part in eight) + ("+" if value & 0x8000 else "")


def color_value(token, kind, where):
    """The 16-bit colour of '#RRGGBB' ('+': bit 15)."""
    if not (token.startswith("#") and len(token.rstrip("+")) == 7):
        raise ValueError(f"{where}: {token} is not a colour (#RRGGBB)")
    red, green, blue = (int(token[at:at + 2], 16) >> 3 for at in (1, 3, 5))
    if kind == "rgb555alt":
        value = (red & 1) << 12 | (green & 1) << 13 | (blue & 1) << 14 | (red >> 1) | (green >> 1) << 4 | (blue >> 1) << 8
    else:
        value = red | green << 5 | blue << 10
    return value | (0x8000 if token.endswith("+") else 0)


def _value_bytes(token, kind, where):
    if kind in ("rgb555", "rgb555alt"):
        return color_value(token, kind, where).to_bytes(2, "little")
    if kind == "text":
        if not (len(token) >= 2 and token[0] == '"' and token[-1] == '"'):
            raise ValueError(f"{where}: {token} is not a quoted string")
        return string_bytes(token) + b"\0"
    if token[0].isdigit() or token[0] in "+-":
        value = int(token, 0)
    else:
        value = _RESOLVER[0](token)
        if value is None:
            raise ValueError(f"{where}: unknown name '{token}'")
    bits = SIZES[kind] * 8
    if not -(1 << (bits - 1)) <= value < (1 << bits):
        raise ValueError(f"{where}: {token} does not fit in {bits} bits")
    return (value & ((1 << bits) - 1)).to_bytes(bits // 8, "little")


def string_text(data):
    """Bytes as a quoted string: ASCII 20-7E as they are (a quote and a backslash after a backslash), the rest
    as backslash, x and two hex digits."""
    out = []
    for byte in data:
        if byte in (0x22, 0x5C):
            out.append("\\" + chr(byte))
        elif 0x20 <= byte <= 0x7E:
            out.append(chr(byte))
        else:
            out.append(f"\\x{byte:02X}")
    return '"' + "".join(out) + '"'


def string_bytes(token):
    """The bytes of a quoted string string_text() wrote."""
    out = bytearray()
    at = 1
    while at < len(token) - 1:
        if token[at] == "\\":
            if token[at + 1] == "x":
                out.append(int(token[at + 2:at + 4], 16))
                at += 4
            else:
                out.append(ord(token[at + 1]))
                at += 2
        else:
            out.append(ord(token[at]))
            at += 1
    return bytes(out)


def _rest(data):
    return [f"bytes {' '.join(f'{byte:02X}' for byte in data[at:at + 16])}" for at in range(0, len(data), 16)]


def _aligned(rows):
    """Rows of cells in columns: numbers to the right, names to the left."""
    if not rows:
        return []
    widths = [max(len(row[column]) for row in rows if column < len(row)) for column in range(max(map(len, rows)))]
    lines = []
    for row in rows:
        cells = []
        for column, cell in enumerate(row):
            last = column == len(row) - 1
            number = cell[0].isdigit() or cell[0] in "+-"
            cells.append(cell.rjust(widths[column]) if number or column == 0 else (cell if last else cell.ljust(widths[column])))
        lines.append("  ".join(cells).rstrip())
    return lines


def values_to_text(data, kind):
    code = VALUE_TYPES[kind]
    width = struct.calcsize(code)
    count = len(data) // width
    values = list(struct.unpack_from(f"<{count}{code}", data)) if count else []
    as_names = kind == "u32" and _addresses(values)
    per_line = 4 if as_names else PER_LINE[width]
    rows = [[f"[{at}]"] + [_value_text(value, kind, as_names) for value in values[at:at + per_line]]
            for at in range(0, count, per_line)]
    return "\n".join([f"layout {kind}"] + _aligned(rows) + _rest(data[count * width:])) + "\n"


def records_to_text(data, structure):
    size, columns = _LAYOUTS[structure]
    count = len(data) // size
    named = {}
    for name, offset, kind in columns:
        if kind == "u32":
            named[name] = _addresses(struct.unpack_from("<I", data, at * size + offset)[0] for at in range(count))
    header = f"layout {structure} {size} " + " ".join(f"{name}:{kind}" for name, _, kind in columns)
    rows = []
    for at in range(count):
        row = [f"[{at}]"]
        for name, offset, kind in columns:
            value = struct.unpack_from("<" + VALUE_TYPES[kind], data, at * size + offset)[0]
            row.append(_value_text(value, kind, named.get(name, False)))
        rows.append(row)
    return "\n".join([header] + _aligned(rows) + _rest(data[count * size:])) + "\n"


# ---- columns: name:type, name:type:N (split at bit N: written HIGH:LOW in hex), and =VALUE after either (a
# fixed value the record always holds there, which the rows do not write) ----

class Column:
    def __init__(self, spec, where="layout"):
        spec, equals, fixed = spec.partition("=")
        parts = spec.split(":")
        if len(parts) not in (2, 3) or parts[1] not in COLUMN_TYPES:
            raise ValueError(f"{where}: '{spec}' is no column this format reads")
        self.name, self.type = parts[0], parts[1]
        self.split = int(parts[2]) if len(parts) == 3 else None
        self.fixed = _value_bytes(fixed, self.type, where) if equals else None

    def text(self):
        return f"{self.name}:{self.type}" + (f":{self.split}" if self.split is not None else "")

    def pack(self, token, where):
        if self.split is not None:
            high, colon, low = token.partition(":")
            if not colon:
                raise ValueError(f"{where}: {self.name} {token} is not HIGH:LOW")
            token = str((int(high, 16) << self.split) | int(low, 16))
        return _value_bytes(token, self.type, where)


def column(name, kind, split=None, fixed=None):
    """A column's text for a layout or record line."""
    text = f"{name}:{kind}" + (f":{split}" if split is not None else "")
    if fixed is not None:
        text += "=" + (str(fixed) if kind[0] == "s" else f"0x{fixed:X}")
    return text


def split_text(value, split):
    """A value split at bit 'split' as HIGH:LOW in hex (the low part as wide as its bits)."""
    return f"{value >> split:X}:{value & ((1 << split) - 1):0{(split + 3) // 4}X}"


def _record_bytes(columns, values, size, where):
    written = [column for column in columns if column.fixed is None]
    if len(values) != len(written):
        raise ValueError(f"{where}: {len(values)} values for {len(written)} columns")
    out = bytearray()
    given = iter(values)
    for column_ in columns:
        out += column_.fixed if column_.fixed is not None else column_.pack(next(given), where)
    if size and len(out) != size:
        raise ValueError(f"{where}: the columns make {len(out)} bytes, not {size}")
    return bytes(out)


HEX_DIGITS = set("0123456789ABCDEFabcdef")


def tokens_of(line):
    """The tokens of a line up to its comment: words, and quoted strings kept whole (with their quotes). A '#'
    starts the comment, unless a hex digit follows it (a colour, #RRGGBB)."""
    tokens = []
    at = 0
    while at < len(line):
        char = line[at]
        if char.isspace():
            at += 1
            continue
        if char == "#" and not (at + 1 < len(line) and line[at + 1] in HEX_DIGITS):
            break
        if char == '"':
            end = at + 1
            while end < len(line) and line[end] != '"':
                end += 2 if line[end] == "\\" else 1
            tokens.append(line[at:end + 1])
            at = end + 1
            continue
        end = at
        while end < len(line) and not line[end].isspace():
            end += 1
        tokens.append(line[at:end])
        at = end
    return tokens


def from_text(text, resolve=None):
    """The bytes of a piece in this format: 'layout TYPE' (values of TYPE), 'layout NAME SIZE COLUMN...'
    (records, a line each), 'record KEYWORD SIZE COLUMN...' (records of a kind, each line starting with its
    keyword, wherever they come), 'bytes XX...'; a record of SIZE 0 is as long as its columns (a text).
    'resolve' (a name -> its address, or None) reads the names of another program than the V70's (the sound
    program's: its own names)."""
    _RESOLVER[0] = resolve or _resolve
    try:
        return _from_text(text)
    finally:
        _RESOLVER[0] = _resolve


def _from_text(text):
    out = bytearray()
    current = None
    kinds = {}
    for line_number, line in enumerate(text.split("\n"), 1):
        tokens = tokens_of(line)
        if not tokens:
            continue
        where = f"line {line_number}"
        if tokens[0] in ("layout", "record"):
            if tokens[0] == "layout" and len(tokens) == 2 and tokens[1] in SIZES:
                current = None, tokens[1]
                continue
            if len(tokens) < 4:
                raise ValueError(f"{where}: a {tokens[0]} line without its columns")
            shape = int(tokens[2], 0), [Column(token, where) for token in tokens[3:]]
            if tokens[0] == "layout":
                current = shape
            else:
                kinds[tokens[1]] = shape
            continue
        if tokens[0] == "bytes":
            out += bytes(int(token, 16) for token in tokens[1:])
            continue
        if tokens[0] in kinds:
            size, columns = kinds[tokens[0]]
            out += _record_bytes(columns, tokens[1:], size, where)
            continue
        if current is None:
            raise ValueError(f"{where}: values before the layout line")
        values = tokens[1:] if tokens[0].startswith("[") else tokens
        size, columns = current
        if size is None:
            out += b"".join(_value_bytes(token, columns, where) for token in values)
            continue
        out += _record_bytes(columns, values, size, where)
    return bytes(out)


class Writer:
    """Writes a piece in this format, for the format modules: layout and record lines, rows of values (each
    a list of cells: numbers, names, quoted strings) lined up in columns under the line before them, comments,
    and the bytes no record explains."""

    def __init__(self):
        self.blocks = []    # [lines that start a block, [(cells, comment)]]

    def line(self, text):
        self.blocks.append([[text], []])

    def comment(self, text):
        self.line(f"# {text}" if text else "#")

    def layout(self, kind, name=None, size=None, columns=()):
        self.line(f"layout {kind}" if name is None else f"layout {name} {size} " + " ".join(columns))

    def record(self, keyword, size, columns):
        self.line(f"record {keyword} {size} " + " ".join(columns))

    def row(self, cells, comment=None):
        if not self.blocks:
            self.blocks.append([[], []])
        self.blocks[-1][1].append((list(cells), comment))

    def raw(self, data):
        for at in range(0, len(data), 16):
            self.line("bytes " + " ".join(f"{byte:02X}" for byte in data[at:at + 16]))

    def text(self):
        lines = []
        for head, rows in self.blocks:
            lines += head
            # (rows of the same keyword, or of the layout's values, and as many cells, lined up together)
            def key(cells):
                return (cells[0] if cells[0][:1].isalpha() else "", len(cells))
            groups = []
            for cells, comment in rows:
                if groups and key(groups[-1][0][0]) == key(cells):
                    groups[-1].append((cells, comment))
                else:
                    groups.append([(cells, comment)])
            for group in groups:
                aligned = _aligned([cells for cells, _ in group])
                width = max(map(len, aligned)) if aligned else 0
                lines += [text.ljust(width) + f"  # {comment}" if comment else text
                          for text, (_, comment) in zip(aligned, group)]
        return "".join(line.rstrip() + "\n" for line in lines)


def element_size(kind):
    """The size of an element of 'kind' (a value type or a structure), or None."""
    if kind in VALUE_TYPES:
        return struct.calcsize(VALUE_TYPES[kind])
    return _LAYOUTS[kind][0] if kind in _LAYOUTS else None


def inner_places(places, crossings=()):
    """The places of 'places' ([(address, name)]) that are names inside a table another place starts, which
    no piece may start at (the table is one piece; the code reaches them inside it, and reads the table
    across them): a place inside the first record of a table of records (a field the code also reads as a
    table of its own); the place right after a table of records of the same structure, on a record's
    boundary (an element the code names, sprite_animations' fork_branch_piece_animation); and the places
    'crossings' gives ([(address, name)]: where the game's reads run on from the piece before)."""
    _load()
    inner = set(crossings)
    ordered = sorted(places)
    for index, (address, name) in enumerate(ordered):
        kind = _TYPES.get(name)
        if kind not in _LAYOUTS:
            continue
        size = _LAYOUTS[kind][0]
        for other, other_name in ordered[index + 1:]:
            if other >= address + size:
                break
            if other > address:
                inner.add((other, other_name))
        following = [(other, other_name) for other, other_name in ordered[index + 1:] if other >= address + size]
        if following:
            other, other_name = following[0]
            if _TYPES.get(other_name) == kind and (other - address) % size == 0:
                inner.add((other, other_name))
    # (a table whose start is a name inside another one is inside it too, and so is what follows it alike)
    changed = True
    while changed:
        changed = False
        for index, (address, name) in enumerate(ordered):
            if (address, name) not in inner or index + 1 >= len(ordered):
                continue
            kind = _TYPES.get(name)
            other, other_name = ordered[index + 1]
            size = element_size(kind)
            if kind in _LAYOUTS and _TYPES.get(other_name) == kind and (other - address) % size == 0 \
                    and (other, other_name) not in inner:
                inner.add((other, other_name))
                changed = True
    return inner


def format_for(name):
    """The format of the piece 'name' (its key in FORMATS), when symbols.txt types it; else None."""
    _load()
    kind = _TYPES.get(name)
    if kind in VALUE_TYPES:
        return f"typed {kind}"
    if kind in _LAYOUTS:
        return f"typed {kind}"
    return None


def _formats():
    _load()
    found = {f"typed {kind}": (".txt", lambda data, kind=kind: values_to_text(data, kind), from_text)
             for kind in VALUE_TYPES}
    found.update({f"typed {structure}": (".txt", lambda data, structure=structure: records_to_text(data, structure),
                                         from_text)
                  for structure in _LAYOUTS})
    return found


FORMATS = _formats()
