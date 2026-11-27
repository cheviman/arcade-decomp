r"""
Name:        data_text.py
Description: a program's data pieces as text by name, and back byte for byte
Version:     0.4.0
Created:     27-11-2026
Modified:    27-11-2026
Author:      Sebastian Toyos

A folder holds layout.txt, a line a piece (NAME PLACE LENGTH FAMILY, PLACE hex, LENGTH decimal), and a FAMILY.txt a
family, its pieces as sections: [NAME]  # KIND, then their lines indented two spaces. A line is an optional label
(a word that is not a type, not ref nor a value) and items, and what follows # is a comment. The types set the type
of the values after them on that line: u8, s8, u16, s16 and u32, a value 0x hex after an unsigned type and a
decimal integer after a signed one, both must fit their type, and VALUE*COUNT is a run of COUNT of it (the fill).
ref NAME[+0xN] is the address of a piece or a function, in the format's byte order. A format with a colour (COLOURS,
the board's colour words) has rgb, a type whose values are colours, 0xRRGGBB, a word each (each part's 5 bits
widened to 8, and back to the nearest); one with characters ({byte: character}) has text "...", a byte a
character and \xNN for any byte (a # or a " in a text is \x23 or \x22). What a file says wrong stops with
SystemExit and a message that says the file, the section and the line. sound_data_text.py is the Z80's format over
this (little endian, 16-bit refs, 4 hex digits); the 68000's is Format("big", 4, 6, ...) (Sunset Riders'
tools/data_assets.py, with its colours and characters).

A kind whose lines say what its bytes give rather than the bytes (a packed map's cells) has a packer: read's
packers, {KIND: pack(data, lines, where)}, the bytes of a section whose [NAME] line's comment is that KIND from the
bytes its lines give and its lines (for a hint a line holds).

A family whose pieces another tool writes and gives back (a sound program's music, as MIDI files) is elsewhere:
write's elsewhere, {FAMILY: where}, gives its pieces their layout lines and no FAMILY.txt (an old one is deleted);
read's elsewhere, the families it leaves to those tools, reads no FAMILY.txt for them and leaves their pieces out;
layout() gives every piece's place, length and family, theirs too.
"""
import re
from bisect import bisect_right
from pathlib import Path

TYPES = {"u8": (1, False), "s8": (1, True), "u16": (2, False), "s16": (2, True), "u32": (4, False)}  # bytes, signed
# a value: 0x hex or a decimal integer, and *COUNT for a run of it
VALUE = re.compile(r"(0[xX][0-9A-Fa-f]+|-?\d+)(?:\*(\d+))?")
# a line's tokens: a text in quotes (its characters, and \xNN for a byte the characters have none for), or a run of
# anything else but blanks
TOKEN = re.compile(r'"(?:[^"\\]|\\x[0-9A-Fa-f]{2})*"|[^\s"]+')
ESCAPE = re.compile(r"\\x([0-9A-Fa-f]{2})|(.)", re.S)
# the boards' colour words: the bit each of red, green and blue starts at, 5 bits each, the top bit clear
COLOURS = {"xBGR_555": (0, 5, 10)}


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

    def __init__(self, order, ref_bytes, place_digits, program, module, colour=None, characters=None):
        if order not in ("little", "big"):
            raise SystemExit(f"Format: no order {order}")
        if ref_bytes not in (2, 4):
            raise SystemExit(f"Format: no ref_bytes {ref_bytes}")
        self.order = order
        self.ref_bytes = ref_bytes
        self.place_digits = place_digits
        self.program = program
        self.module = module
        if colour is not None and colour not in COLOURS:
            raise SystemExit(f"Format: no colour {colour}")
        self.colour = colour            # the colour words' format (COLOURS), for rgb values
        # {byte: character} of the texts (text "..."), one character a byte; the bytes it has none for are \xNN
        self.characters = dict(characters or {})
        self.bytes_of = {character: byte for byte, character in self.characters.items()}
        if len(self.bytes_of) != len(self.characters) or any(len(character) != 1 or character in '"\\#'
                                                             for character in self.bytes_of):
            raise SystemExit('Format: the characters must be one each, different, and none of " \\ #')

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

    def colour_text(self, word):
        """A colour word as an rgb value, 0xRRGGBB (each part's 5 bits widened to 8), or None if it is no colour"""
        if self.colour is None or word >> 15:
            return None
        value = 0
        for shift in COLOURS[self.colour]:
            five = word >> shift & 0x1F
            value = value << 8 | five << 3 | five >> 2
        return f"0x{value:06X}"

    def colour_word(self, value):
        """An rgb value's colour word: each 8-bit part to the nearest of 5 bits"""
        word = 0
        for number, shift in enumerate(COLOURS[self.colour]):
            eight = value >> 8 * (2 - number) & 0xFF
            word |= (eight * 31 + 127) // 255 << shift
        return word

    def colour_lines(self, data, each=8):
        """rgb lines, 'each' colours a line; a word that is no colour as a u16 in its place"""
        lines = []
        for at in range(0, len(data) - 1, 2 * each):
            items, kind = [], None
            for word_at in range(at, min(at + 2 * each, len(data) - 1), 2):
                word = int.from_bytes(data[word_at:word_at + 2], self.order)
                text = self.colour_text(word)
                wanted = "rgb" if text is not None else "u16"
                if wanted != kind:
                    items.append(wanted)
                    kind = wanted
                items.append(text if text is not None else f"0x{word:04X}")
            lines.append(" ".join(items))
        if len(data) % 2:
            lines.append(f"u8 0x{data[-1]:02X}")
        return lines

    def text_item(self, data):
        """Bytes as a text item, "..." in the characters, \\xNN for a byte they have none for"""
        return '"' + "".join(self.characters.get(byte, f"\\x{byte:02X}") for byte in data) + '"'

    def text_bytes(self, token, where):
        """A text item's bytes"""
        out = bytearray()
        for found in ESCAPE.finditer(token[1:-1]):
            if found.group(1) is not None:
                out.append(int(found.group(1), 16))
            elif found.group(2) in self.bytes_of:
                out.append(self.bytes_of[found.group(2)])
            else:
                raise SystemExit(f"{where}: no byte for {found.group(2)!r} in a text (\\xNN gives any)")
        return bytes(out)

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
            tokens = TOKEN.findall(line.split("#", 1)[0])
            if not tokens:
                continue
            if not (tokens[0] in TYPES or tokens[0] in ("ref", "rgb", "text") or tokens[0].startswith('"')
                    or VALUE.fullmatch(tokens[0])):
                tokens = tokens[1:]
            kind = None
            at = 0
            while at < len(tokens):
                token = tokens[at]
                found = VALUE.fullmatch(token)
                if token in TYPES:
                    kind = token
                elif token == "rgb":
                    if self.colour is None:
                        raise SystemExit(f"{here}: rgb in a format with no colours")
                    kind = token
                elif token == "text":
                    at += 1
                    if at >= len(tokens) or not tokens[at].startswith('"'):
                        raise SystemExit(f'{here}: text with no "..." after it')
                    out += self.text_bytes(tokens[at], here)
                elif token.startswith('"'):
                    raise SystemExit(f"{here}: a text with no 'text' before it")
                elif token == "ref":
                    at += 1
                    if at >= len(tokens):
                        raise SystemExit(f"{here}: ref with no name")
                    out += names.value(tokens[at], here).to_bytes(self.ref_bytes, self.order)
                elif found:
                    if kind is None:
                        raise SystemExit(f"{here}: value {token} with no type")
                    if kind == "rgb":
                        if not found.group(1).lower().startswith("0x") or int(found.group(1), 16) > 0xFFFFFF:
                            raise SystemExit(f"{here}: {token}: an rgb value is 0xRRGGBB")
                        out += self.colour_word(int(found.group(1), 16)).to_bytes(2, self.order) \
                            * int(found.group(2) or 1)
                        at += 1
                        continue
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

    def write(self, out, sections, elsewhere=None):
        """The folder 'out': a FAMILY.txt a family and layout.txt, from [(family, name, kind, place, length, lines)];
        a family of 'elsewhere' ({family: where}) only in layout.txt"""
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        elsewhere = elsewhere or {}
        families = {}
        layout = []
        for family, name, kind, place, length, lines in sections:
            if family in elsewhere:
                layout.append((place, name, length, family))
                continue
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
        for family in elsewhere:
            if (out / f"{family}.txt").exists():
                (out / f"{family}.txt").unlink()
        layout_lines = [f"# NAME PLACE LENGTH FAMILY: each piece, where {self.program} has it "
                        "(PLACE hex, LENGTH decimal)"]
        layout_lines += [f"# the family {family}: {where}" for family, where in elsewhere.items()]
        layout_lines += [f"{name} {place:0{self.place_digits}X} {length} {family}"
                         for place, name, length, family in sorted(layout)]
        (out / "layout.txt").write_text("\n".join(layout_lines) + "\n", encoding="ascii")

    def layout(self, out):
        """{name: (place, length, family)} of every piece of the folder 'out's layout.txt"""
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
        return layout

    def read(self, out, functions, packers=None, elsewhere=()):
        """{name: (place, bytes)} of the pieces the folder 'out' gives ('functions': {address: name}, for the refs;
        'packers': {kind: pack(data, lines, where)}, the bytes of the sections of a kind whose lines are not them;
        the pieces of the families 'elsewhere' left out)"""
        out = Path(out)
        layout = self.layout(out)
        names = Names({place: (place + length, name) for name, (place, length, _) in layout.items()}, functions)
        families = {}
        for name, (_, _, family) in layout.items():
            if family not in elsewhere:
                families.setdefault(family, set()).add(name)
        sections, kinds = {}, {}
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
                    comment = line[end + 1:].split("#", 1)
                    kinds[family, current] = comment[1].strip() if len(comment) == 2 else None
                elif line.startswith("  ") and current is not None:
                    sections[family][current].append(line.strip())
        for name, (_, _, family) in layout.items():
            if family not in elsewhere and name not in sections.get(family, {}):
                raise SystemExit(f"{family}.txt: no section for {name}")
        for family, sections_by_name in sections.items():
            for name in sections_by_name:
                if name not in layout:
                    raise SystemExit(f"{family}.txt: section {name} has no layout line")
        pieces = {}
        for name, (place, length, family) in layout.items():
            if family in elsewhere:
                continue
            where = f"{family}.txt [{name}]"
            data = self.read_lines(sections[family][name], names, where)
            pack = (packers or {}).get(kinds[family, name])
            if pack is not None:
                data = pack(data, sections[family][name], where)
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
