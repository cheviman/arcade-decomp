"""
Name:        data_text.py
Description: a program's data pieces as text by name, and back byte for byte
Version:     0.1.0
Created:     27-11-2026
Modified:    27-11-2026
Author:      Sebastian Toyos

A folder holds layout.txt, a line a piece (NAME PLACE LENGTH FAMILY, PLACE hex, LENGTH decimal), and a FAMILY.txt a
family, its pieces as sections: [NAME]  # KIND, then their lines indented two spaces. A line is an optional label
(a word that is not a type, not ref nor a value) and items, and what follows # is a comment. The types set the type
of the values after them on that line: u8, s8, u16, s16 and u32, a value 0x hex after an unsigned type and a
decimal integer after a signed one, both must fit their type, and VALUE*COUNT is a run of COUNT of it (the fill).
ref NAME[+0xN] is the address of a piece or a function, in the format's byte order. What a file says wrong stops with SystemExit and a message that says the
file, the section and the line. sound_data_text.py is the Z80's format over this (little endian, 16-bit refs, 4
hex digits); the 68000's is Format("big", 4, 6, ...) (Sunset Riders' tools/data_assets.py).
"""
import re
from bisect import bisect_right
from pathlib import Path

TYPES = {"u8": (1, False), "s8": (1, True), "u16": (2, False), "s16": (2, True), "u32": (4, False)}  # bytes, signed
# a value: 0x hex or a decimal integer, and *COUNT for a run of it
VALUE = re.compile(r"(0[xX][0-9A-Fa-f]+|-?\d+)(?:\*(\d+))?")


class Names:
    """The pieces' names ({start: (stop, name)}) and the functions' ({address: name}), both ways"""

    def __init__(self, pieces, functions):
        self.pieces = pieces
        self.functions = functions
        self.starts = sorted(pieces)
        self.addresses = {name: start for start, (_, name) in pieces.items()}
        self.addresses.update({name: address for address, name in functions.items()})

    def name(self, value):
        """A function's name, NAME or NAME+0xN of the piece that holds the address, or None"""
        if value in self.functions:
            return self.functions[value]
        index = bisect_right(self.starts, value) - 1
        if index >= 0:
            start = self.starts[index]
            stop, name = self.pieces[start]
            if start <= value < stop:
                return name if value == start else f"{name}+0x{value - start:X}"
        return None

    def start(self, value):
        """The name of the piece that starts at the address, or None"""
        if value in self.pieces:
            return self.pieces[value][1]
        return None

    def value(self, text, where):
        """The address NAME or NAME+0xN names"""
        base, _, offset = text.partition("+")
        if base not in self.addresses:
            raise SystemExit(f"{where}: no piece or function named {base}")
        if offset:
            try:
                return self.addresses[base] + int(offset, 16)
            except ValueError:
                raise SystemExit(f"{where}: bad offset in {text}")
        return self.addresses[base]


class Format:
    """The format: order "little" or "big", ref_bytes 2 or 4, place_digits, program, module"""

    def __init__(self, order, ref_bytes, place_digits, program, module):
        if order not in ("little", "big"):
            raise SystemExit(f"Format: no order {order}")
        if ref_bytes not in (2, 4):
            raise SystemExit(f"Format: no ref_bytes {ref_bytes}")
        self.order = order
        self.ref_bytes = ref_bytes
        self.place_digits = place_digits
        self.program = program
        self.module = module

    def byte_lines(self, data):
        """u8 lines, 16 bytes a line"""
        lines = []
        for at in range(0, len(data), 16):
            lines.append("u8 " + " ".join(f"0x{byte:02X}" for byte in data[at:at + 16]))
        return lines

    def word_lines(self, data, names=None):
        """u16 lines, 8 words a line; with 'names', a line a word, a ref when the word is a piece's start"""
        if names is not None and self.ref_bytes != 2:
            raise SystemExit("word_lines: names with a 4-byte ref format")
        lines = []
        if names is None:
            values = []
            for at in range(0, len(data) - 1, 2):
                value = data[at] | data[at + 1] << 8 if self.order == "little" else data[at] << 8 | data[at + 1]
                values.append(f"0x{value:04X}")
            for at in range(0, len(values), 8):
                lines.append("u16 " + " ".join(values[at:at + 8]))
        else:
            for at in range(0, len(data) - 1, 2):
                value = data[at] | data[at + 1] << 8 if self.order == "little" else data[at] << 8 | data[at + 1]
                name = names.start(value)
                lines.append(f"ref {name}" if name else f"u16 0x{value:04X}")
        if len(data) % 2:
            lines.append(f"u8 0x{data[-1]:02X}")
        return lines

    def long_lines(self, data, names=None):
        """u32 lines, 4 longs a line; with 'names', a line a long, a ref when names.name gives a name"""
        lines = []
        if names is None:
            values = []
            for at in range(0, len(data) - 3, 4):
                value = int.from_bytes(data[at:at + 4], self.order)
                values.append(f"0x{value:08X}")
            for at in range(0, len(values), 4):
                lines.append("u32 " + " ".join(values[at:at + 4]))
        else:
            for at in range(0, len(data) - 3, 4):
                value = int.from_bytes(data[at:at + 4], self.order)
                name = names.name(value)
                lines.append(f"ref {name}" if name else f"u32 0x{value:08X}")
        if len(data) % 4:
            rest = data[-(len(data) % 4):]
            lines.append("u8 " + " ".join(f"0x{byte:02X}" for byte in rest))
        return lines

    def run_lines(self, data, least=16):
        """byte_lines, with each run of 'least' or more of the same byte as one item, 0xVV*COUNT, on a line of its own"""
        lines = []
        at = loose = 0
        while at < len(data):
            stop = at
            while stop < len(data) and data[stop] == data[at]:
                stop += 1
            if stop - at >= least:
                lines += self.byte_lines(data[loose:at])
                lines.append(f"u8 0x{data[at]:02X}*{stop - at}")
                loose = stop
            at = stop
        return lines + self.byte_lines(data[loose:])

    def value_text(self, kind, value):
        """A value as a line writes it after its type: hex for the unsigned types, decimal for the signed"""
        size, signed = TYPES[kind]
        return str(value) if signed else f"0x{value:0{2 * size}X}"

    def record_lines(self, data, fields, names=None):
        """A line a field ([(offset, name, type)]: u8, s8, u16, s16, u32 or ref), the bytes no field covers as u8
        lines in their place; a ref field holds a ref when 'names' names its value, else its value"""
        lines = []
        pos = 0
        for offset, name, kind in sorted(fields, key=lambda field: field[0]):
            if offset < pos:
                raise SystemExit(f"record_lines: field {name} overlaps the previous fields")
            if kind != "ref" and kind not in TYPES:
                raise SystemExit(f"record_lines: no type {kind}")
            size = self.ref_bytes if kind == "ref" else TYPES[kind][0]
            if offset + size > len(data):
                raise SystemExit(f"record_lines: field {name} is past the end of the data")
            if offset > pos:
                lines += self.byte_lines(data[pos:offset])
            if kind == "ref":
                value = int.from_bytes(data[offset:offset + size], self.order)
                target = names.name(value) if names is not None else None
                kind = f"u{8 * size}"
                lines.append(f"{name} ref {target}" if target else f"{name} {kind} {self.value_text(kind, value)}")
            else:
                value = int.from_bytes(data[offset:offset + size], self.order, signed=TYPES[kind][1])
                lines.append(f"{name} {kind} {self.value_text(kind, value)}")
            pos = offset + size
        if pos < len(data):
            lines += self.byte_lines(data[pos:])
        return lines

    def read_lines(self, lines, names, where):
        """The bytes the lines give, in order"""
        out = bytearray()
        for number, line in enumerate(lines, 1):
            here = f"{where}, line {number}"
            tokens = line.split("#", 1)[0].strip().split()
            if not tokens:
                continue
            if not (tokens[0] in TYPES or tokens[0] == "ref" or VALUE.fullmatch(tokens[0])):
                tokens = tokens[1:]
            kind = None
            at = 0
            while at < len(tokens):
                token = tokens[at]
                found = VALUE.fullmatch(token)
                if token in TYPES:
                    kind = token
                elif token == "ref":
                    at += 1
                    if at >= len(tokens):
                        raise SystemExit(f"{here}: ref with no name")
                    out += names.value(tokens[at], here).to_bytes(self.ref_bytes, self.order)
                elif found:
                    if kind is None:
                        raise SystemExit(f"{here}: value {token} with no type")
                    size, signed = TYPES[kind]
                    if signed == found.group(1).lower().startswith("0x"):
                        raise SystemExit(f"{here}: {token}: a {kind} value is "
                                         + ("decimal" if signed else "0x hex"))
                    value = int(found.group(1), 0 if not signed else 10)
                    low, high = (-(1 << (8 * size - 1)), (1 << (8 * size - 1)) - 1) if signed else (0, (1 << (8 * size)) - 1)
                    if not low <= value <= high:
                        raise SystemExit(f"{here}: {token} does not fit {kind}")
                    out += value.to_bytes(size, self.order, signed=signed) * int(found.group(2) or 1)
                else:
                    raise SystemExit(f"{here}: unknown word {token}")
                at += 1
        return bytes(out)

    def write(self, out, sections):
        """The folder 'out': a FAMILY.txt a family and layout.txt, from [(family, name, kind, place, length, lines)]"""
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        families = {}
        layout = []
        for family, name, kind, place, length, lines in sections:
            if family not in families:
                families[family] = [
                    f"# {self.program}'s {family}: each piece's [NAME]  # KIND, then its lines "
                    f"({self.module})"
                ]
            families[family].append(f"[{name}]  # {kind}")
            families[family] += [f"  {line}" for line in lines]
            layout.append((place, name, length, family))
        for family, lines in families.items():
            (out / f"{family}.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
        layout_lines = [f"# NAME PLACE LENGTH FAMILY: each piece, where {self.program} has it "
                        "(PLACE hex, LENGTH decimal)"]
        layout_lines += [f"{name} {place:0{self.place_digits}X} {length} {family}"
                         for place, name, length, family in sorted(layout)]
        (out / "layout.txt").write_text("\n".join(layout_lines) + "\n", encoding="ascii")

    def read(self, out, functions):
        """{name: (place, bytes)} of the pieces the folder 'out' gives ('functions': {address: name}, for the refs)"""
        out = Path(out)
        layout_path = out / "layout.txt"
        if not layout_path.exists():
            raise SystemExit(f"{layout_path}: missing")
        layout = {}
        for number, line in enumerate(layout_path.read_text(encoding="ascii").splitlines(), 1):
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 4 or not re.fullmatch(r"[0-9A-Fa-f]+", parts[1]) or not parts[2].isdigit():
                raise SystemExit(f"{layout_path}, line {number}: expected 'NAME PLACE LENGTH FAMILY'")
            if parts[0] in layout:
                raise SystemExit(f"{layout_path}, line {number}: {parts[0]} again")
            layout[parts[0]] = (int(parts[1], 16), int(parts[2]), parts[3])
        names = Names({place: (place + length, name) for name, (place, length, _) in layout.items()}, functions)
        families = {}
        for name, (_, _, family) in layout.items():
            families.setdefault(family, set()).add(name)
        sections = {}
        for family in families:
            path = out / f"{family}.txt"
            if not path.exists():
                raise SystemExit(f"{path}: missing")
            current = None
            for line in path.read_text(encoding="ascii").splitlines():
                if line.startswith("["):
                    end = line.find("]")
                    if end == -1:
                        raise SystemExit(f"{path}: bad section line {line}")
                    current = line[1:end]
                    if current in sections.get(family, {}):
                        raise SystemExit(f"{path}: section {current} again")
                    sections.setdefault(family, {})[current] = []
                elif line.startswith("  ") and current is not None:
                    sections[family][current].append(line.strip())
        for name, (_, _, family) in layout.items():
            if name not in sections.get(family, {}):
                raise SystemExit(f"{family}.txt: no section for {name}")
        for family, sections_by_name in sections.items():
            for name in sections_by_name:
                if name not in layout:
                    raise SystemExit(f"{family}.txt: section {name} has no layout line")
        pieces = {}
        for name, (place, length, family) in layout.items():
            where = f"{family}.txt [{name}]"
            data = self.read_lines(sections[family][name], names, where)
            if len(data) != length:
                raise SystemExit(f"{where}: {len(data)} bytes, the layout says {length}")
            pieces[name] = (place, data)
        return pieces

    def compare(self, image, pieces, wanted):
        """What does not come back: the pieces whose bytes differ from 'image', the pieces that share bytes, and the
        bytes of 'wanted' ([(first, stop)]) no piece gives; at most 20, in their places' order"""
        problems = []
        ordered = sorted(pieces, key=lambda name: (pieces[name][0], name))
        for name in ordered:
            place, data = pieces[name]
            chunk = image[place:place + len(data)]
            differ = [at for at in range(len(data)) if at >= len(chunk) or chunk[at] != data[at]]
            if differ:
                problems.append(f"{name} ({place:0{self.place_digits}X}): differs from the program at "
                                f"{place + differ[0]:0{self.place_digits}X}, "
                                f"and {len(differ) - 1} bytes more")
        reach, holder = None, None
        for name in ordered:
            place, data = pieces[name]
            if reach is not None and place < reach:
                problems.append(f"{holder} and {name} share {place:0{self.place_digits}X}")
            if reach is None or place + len(data) > reach:
                reach, holder = place + len(data), name
        covered = set()
        for place, data in pieces.values():
            covered.update(range(place, place + len(data)))
        intervals = []
        for first, stop in wanted:
            if stop <= first:
                continue
            if intervals and first <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(intervals[-1][1], stop))
            else:
                intervals.append((first, stop))
        for first, stop in intervals:
            at = first
            while at < stop:
                if at in covered:
                    at += 1
                    continue
                start = at
                while at < stop and at not in covered:
                    at += 1
                problems.append(f"{start:0{self.place_digits}X}: no piece gives this byte, "
                                f"nor {at - start - 1} after it")
        return problems[:20]
