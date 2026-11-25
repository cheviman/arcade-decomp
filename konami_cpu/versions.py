# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        versions.py
Description: carries a game's symbols.txt over to another version of the same Konami CPU program (another ROM set of
             the same board): the base's places matched to the version's, instruction by instruction
Version:     0.1.0
Created:     25-11-2026
Modified:    25-11-2026
Author:      Sebastian Toyos

Usage: python tools/match_version.py VERSION_ROM [--base BASE_ROM] [--out DIR]   (the game's shim)
       Writes DIR/symbols.txt (the base's symbols.txt with its places moved to the version's, and the version's own
       lines, versions/SET/symbols.txt, after them), DIR/review.txt (what could not be carried, and the version's functions
       no base function's name went to) and DIR/places.txt (each base function's and named data's place, and the version's).
       DIR is build/versions/SET by default. versions/SET/symbols.txt holds the version's own lines, and 'drop NAME'
       for a base function the version has not though its code was paired (its lines are not carried).

A version of a program is mostly the same code, moved: a routine put in or taken out moves everything after it in its
bank, so the addresses the code holds change while its instructions do not. Both programs are analysed without traces
(analysis.py: the base with its symbols.txt, the version with its own lines), and their instructions compared, area by
area (each bank, and the fixed program), by their shape: the instruction and its operands without the values that name
places of the program (an immediate a register is loaded with that the CPU can see as the program, an absolute
operand there, a branch's target), which are the ones that move. A run of three instructions or more of the same shape
in both is the same code; between two such runs, as many instructions of the same kind in both are too (the same
instruction with another constant: a count, a coordinate). A base function no run reached is looked for in the whole
version by its bytes, with those operands left out, once: a routine moved to another bank.

The data a pair of instructions names (analysis.named_by: the tables and values the code loads the address of) is the
same data in both: what the base's instruction names at an address, the version's names at its own. A place of
symbols.txt neither code nor named data is looked for by its bytes near where the places around it moved to. What is
left is the review's.
"""
import re
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import game_profile  # noqa: E402  (the game's: its root, its ROM set)
import disassemble  # noqa: E402  (symbols.txt, in the format all the games share)
from konami_cpu import analysis as konami_analysis  # noqa: E402
from konami_cpu import disassembler as dis  # noqa: E402

ROOT = game_profile.ROOT
rom = game_profile.rom
FIXED_START = rom.FIXED_PROGRAM[0]
BANK_SIZE = rom.PROGRAM_BANK_SIZE
PROGRAM_START = 0x6000          # the lowest CPU address the program is seen at (the bank's window)
RUN = 3                         # instructions of the same shape in a row that are the same code
PAIR_SHARE = 3                  # a version function is a base function's when 1 in this many of its instructions went
                                # there in runs, more than to any other
GAP_MOST = 64                   # instructions on either side of a gap between two paired ones that are aligned
LOCAL_SLACK = 64                # how much further from its function's start a local's site can be in the version
LONE_KNOWN = 8                  # bytes a function's pattern has to keep to be looked for alone
SURE_KNOWN = 12                 # and to be sure of where it is when it is found once
NEAR = 0x800                    # how far from where its neighbours moved a place of data is looked for
MOVED_ENTRY_MOST = 6            # instructions of its own the version may put in place of a function's first ones
NAMING = ("ldx", "ldy", "ldu", "lds", "cmpx", "cmpy", "cmpu")


def area_of(place):
    """The bank of a place, the fixed program as one area"""
    return "fixed" if place >= FIXED_START else place // BANK_SIZE


def names_program(instruction, operand):
    """Whether an operand's value is an address of the program the CPU sees (one that moves between versions)"""
    if operand.mode == "address":
        return True
    if operand.mode == "immediate":
        return operand.size == 2 and operand.value >= PROGRAM_START and instruction.mnemonic in NAMING
    if operand.mode == "indexed" and operand.kind == "extended":
        return operand.value >= PROGRAM_START
    return operand.mode == "indexed" and operand.target is not None


def shape(instruction):
    """An instruction without the operands that name the program: what two versions of the same code share"""
    parts = [instruction.mnemonic, instruction.condition]
    for operand in instruction.operands:
        if names_program(instruction, operand):
            parts.append((operand.mode, operand.kind, "place"))
        else:
            parts.append((operand.mode, operand.kind, operand.reg, operand.accumulator, operand.indirect,
                          operand.value, operand.disp, operand.size, operand.registers, operand.source,
                          operand.destination, operand.exchange))
    return tuple(parts)


def kind(instruction):
    """An instruction's kind alone: the same in two versions that give it other numbers"""
    return (instruction.mnemonic, instruction.condition,
            tuple((operand.mode, operand.kind, operand.reg, operand.accumulator, operand.indirect)
                  for operand in instruction.operands))


def masked_pattern(image, analysis, places, whole=False):
    """A function's instructions as a regular expression on the program's bytes, its operands that name the program
    left out, from its first instruction on while they follow one another; and the bytes it keeps. With 'whole',
    every instruction's own: [(place, length, pattern)]"""
    pattern, known, end = [], 0, None
    pieces = []
    for place in places:
        if end is not None and place != end and not whole:
            break
        instruction = analysis.instructions[place]
        raw = image[place:place + instruction.length]
        keep = [True] * instruction.length
        for operand in instruction.operands:
            if names_program(instruction, operand):
                width = 2 if instruction.length >= 3 and operand.mode != "indexed" or operand.kind == "extended" \
                    else 1
                for index in range(instruction.length - width, instruction.length):
                    keep[index] = False
        piece = b"".join(re.escape(bytes([byte])) if kept else b"." for byte, kept in zip(raw, keep))
        pattern.append(piece)
        pieces.append((place, instruction.length, re.compile(piece, re.DOTALL)))
        known += sum(keep)
        end = place + instruction.length
    if whole:
        return pieces, known
    return re.compile(b"".join(pattern), re.DOTALL), known


class Match:
    """The base's places matched to a version's: code (instructions) and data"""

    def __init__(self, base_image, base_symbols, version_image, version_symbols):
        self.base_image, self.version_image = base_image, version_image
        print("analysing the base ...", flush=True)
        self.base = konami_analysis.Analysis(base_image, base_symbols)
        self.base.run()
        print("analysing the version ...", flush=True)
        self.version = konami_analysis.Analysis(version_image, version_symbols)
        self.version.run()
        self.code = {}              # {base instruction place: version instruction place}
        self.data = {}              # {base data place: version data place}
        self.how = Counter()

    def run(self):
        self.sure_functions()
        self.align()
        self.function_pairs()
        self.within_functions()
        self.lone_functions()
        self.between_neighbours()
        for _ in range(3):
            self.by_targets()
            self.gaps()
            self.between_neighbours()
        self.by_targets()
        self.named_data()
        return self

    def sure_functions(self):
        """A base function whose instructions, its operands that name the program aside, are in the version once and
        only once, from its start, its later runs at the same distance: the same function, sure"""
        self.sure = set()
        taken = set()
        for entry, places in self.function_places(self.base).items():
            if not places or entry not in self.base.instructions:
                continue
            pattern, known = masked_pattern(self.base_image, self.base, places)
            if known < SURE_KNOWN:
                continue
            hits = [found.start() for found in pattern.finditer(self.version_image)]
            if len(hits) != 1:
                continue
            delta = hits[0] - entry
            rest, _ = masked_pattern(self.base_image, self.base, places, whole=True)
            if not all(piece.fullmatch(self.version_image[start + delta:start + delta + length])
                       for start, length, piece in rest):
                continue
            if any(place + delta in taken for place in places):
                continue
            for place in places:
                self.code[place] = place + delta
                self.sure.add(place)
                taken.add(place + delta)
        self.how["instructions of functions found whole once, sure"] = len(self.sure)

    def base_key(self, place):
        """A base instruction's kind, and the version place its target went to (None if not known yet)"""
        instruction = self.base.instructions[place]
        target = target_place(place, instruction)
        return kind(instruction), None if target is None else self.code.get(target, -1)

    def version_key(self, place):
        """A version instruction's kind, and its target"""
        instruction = self.version_instruction(place)
        target = target_place(place, instruction)
        return kind(instruction), target

    def gaps(self):
        """The base instructions left out between two paired ones, and the version's left out between their pairs,
        paired by their kinds in order: what changed in a routine is bounded by what did not"""
        places = sorted(self.base.instructions)
        version_places = sorted(self.version.instructions)
        taken = set(self.code.values())
        paired = 0
        index = 0
        while index < len(places):
            if places[index] in self.code:
                index += 1
                continue
            end = index
            while end < len(places) and places[end] not in self.code:
                end += 1
            if index == 0 or end == len(places):
                index = end
                continue
            before, after = places[index - 1], places[end]
            low, high = self.code[before], self.code[after]
            mine = places[index:end]
            index = end
            if area_of(before) != area_of(after) or area_of(low) != area_of(high) or not low < high:
                continue
            theirs = [place for place in version_places[bisect_left(version_places, low + 1):
                                                         bisect_left(version_places, high)] if place not in taken]
            if not theirs or max(len(mine), len(theirs)) > GAP_MOST:
                continue
            matcher = SequenceMatcher(None, [self.base_key(place) for place in mine],
                                      [self.version_key(place) for place in theirs], autojunk=False)
            for block in matcher.get_matching_blocks():
                for offset in range(block.size):
                    self.code[mine[block.a + offset]] = theirs[block.b + offset]
                    taken.add(theirs[block.b + offset])
                    paired += 1
        self.how["instructions paired between two paired ones, by their kinds"] += paired

    def between_neighbours(self):
        """A base instruction left out between two that moved by the same distance, whose bytes are at that distance in
        the version, its operands that name the program aside: code nothing reaches (an unused routine, an empty
        vector), which the version's analysis has no instructions for until its symbols name them"""
        places = sorted(self.base.instructions)
        before = [None] * len(places)
        last = None
        for index, place in enumerate(places):
            if place in self.code:
                last = (area_of(place), self.code[place] - place)
            before[index] = last
        after = None
        moved = 0
        for index in range(len(places) - 1, -1, -1):
            place = places[index]
            if place in self.code:
                after = (area_of(place), self.code[place] - place)
                continue
            if after is None or before[index] != after or after[0] != area_of(place):
                continue
            delta = after[1]
            pattern, _ = masked_pattern(self.base_image, self.base, [place])
            length = self.base.instructions[place].length
            if pattern.fullmatch(self.version_image[place + delta:place + delta + length]):
                self.code[place] = place + delta
                moved += 1
        self.how["instructions between two that moved alike, the same bytes"] += moved

    def version_instruction(self, place):
        """The version's instruction at a place, decoded there if its analysis has none (code it does not reach)"""
        instruction = self.version.instructions.get(place)
        return instruction if instruction is not None else self.version.decode(bank_of(place), cpu_of(place))

    def neighbour_deltas(self, place, mapped):
        """How far the paired base instructions just before and after a place moved, in its area"""
        index = bisect_left(mapped, place)
        deltas = []
        for neighbour in mapped[max(0, index - 1):index + 1]:
            if neighbour != place and area_of(neighbour) == area_of(place) and neighbour in self.code:
                deltas.append(self.code[neighbour] - neighbour)
        return deltas

    def by_targets(self):
        """A branch, jump or call goes where its pair goes. Where the pairs that go to a base place go in the version,
        most of them, is that place's pair; a pair that goes elsewhere is no pair; and an instruction with a target is
        the version's one instruction of its kind that goes where its target went (a table of jumps, LBRA after LBRA,
        is all of one shape: its entries are told apart by where they go)"""
        votes = defaultdict(Counter)
        for place, paired in self.code.items():
            target = target_place(place, self.base.instructions[place])
            theirs = target_place(paired, self.version_instruction(paired))
            if target is not None and theirs is not None and target in self.base.instructions:
                votes[target][theirs] += 1
        moved = 0
        images = set(self.code.values())
        for target, counter in votes.items():
            ranked = counter.most_common(2)
            best = ranked[0][0]
            if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
                continue            # (as many go one way as another: no evidence)
            if target not in self.code and best not in images:
                self.code[target] = best
                images.add(best)
                moved += 1
        self.how["places paired by where the pairs going to them go"] += moved
        by_target = defaultdict(list)
        for place, instruction in self.version.instructions.items():
            target = target_place(place, instruction)
            if target is not None:
                by_target[(instruction.mnemonic, target)].append(place)
        taken = Counter(self.code.values())
        mapped = sorted(self.code)
        undone = fixed = 0
        for place, instruction in self.base.instructions.items():
            target = target_place(place, instruction)
            if target is None or target not in self.code:
                continue
            wanted = self.code[target]
            paired = self.code.get(place)
            if paired is not None and (target_place(paired, self.version_instruction(paired)) == wanted
                                       or place in self.sure):
                continue
            if paired is not None:
                del self.code[place]
                taken[paired] -= 1
                undone += 1
            candidates = [other for other in by_target.get((instruction.mnemonic, wanted), ()) if not taken[other]]
            if len(candidates) != 1:
                # (code the version's analysis does not reach: an unused vector to a used routine) where the paired
                # instructions on either side say it went
                candidates = []
                for delta in self.neighbour_deltas(place, mapped):
                    other = place + delta
                    if not taken[other] and area_of(other) == area_of(place):
                        theirs = self.version_instruction(other)
                        if theirs.mnemonic == instruction.mnemonic and target_place(other, theirs) == wanted:
                            candidates = [other]
                            break
            if len(candidates) == 1:
                self.code[place] = candidates[0]
                taken[candidates[0]] += 1
                fixed += 1
        self.how["pairs undone: their targets were not each other's"] += undone
        self.how["instructions paired by where they go"] += fixed

    def function_pairs(self):
        """Each base function and the version function most of its instructions in runs went to, one each: the same
        function, changed or not (a function whose entry no run reached is a changed one: self.changed)"""
        theirs_of = {}
        for entry, places in self.function_places(self.version).items():
            for place in places:
                theirs_of[place] = entry
        candidates = []
        for entry, places in self.function_places(self.base).items():
            votes = Counter(theirs_of.get(self.code[place]) for place in places if place in self.code)
            votes.pop(None, None)
            if votes:
                ranked = votes.most_common(2)
                target, count = ranked[0]
                if len(ranked) > 1 and ranked[1][1] == count:
                    continue        # (as many went to one function as to another)
                if count * PAIR_SHARE >= len(places):
                    candidates.append((-count, entry, target))
        self.pairs, self.changed, taken = {}, {}, set()
        for _, entry, target in sorted(candidates):
            if target in taken:
                continue
            taken.add(target)
            self.pairs[entry] = target
            if entry not in self.code:
                self.changed[entry] = target
        self.how["functions changed at their start, by most of their instructions"] = len(self.changed)

    def within_functions(self):
        """In each pair of functions, the instructions no run took paired by their kinds: the same instruction with
        another number (a count, a coordinate), in the same order"""
        base_places = self.function_places(self.base)
        version_places = self.function_places(self.version)
        taken = set(self.code.values())
        for entry, target in self.pairs.items():
            mine, theirs = base_places[entry], version_places[target]
            matcher = SequenceMatcher(None, [self.base_key(place) for place in mine],
                                      [self.version_key(place) for place in theirs], autojunk=False)
            for block in matcher.get_matching_blocks():
                for offset in range(block.size):
                    place, other = mine[block.a + offset], theirs[block.b + offset]
                    if place not in self.code and other not in taken:
                        self.code[place] = other
                        taken.add(other)
                        self.how["instructions paired within their functions, other numbers"] += 1

    def align(self):
        """The instructions of each area paired by their shapes"""
        by_area = defaultdict(lambda: ([], []))
        for place in sorted(self.base.instructions):
            by_area[area_of(place)][0].append(place)
        for place in sorted(self.version.instructions):
            by_area[area_of(place)][1].append(place)
        for area, (mine, theirs) in sorted(by_area.items(), key=lambda item: str(item[0])):
            shapes_mine = [shape(self.base.instructions[place]) for place in mine]
            shapes_theirs = [shape(self.version.instructions[place]) for place in theirs]
            matcher = SequenceMatcher(None, shapes_mine, shapes_theirs, autojunk=False)
            blocks = [block for block in matcher.get_matching_blocks() if block.size >= RUN]
            taken = set(self.code.values())
            for block in blocks:
                for offset in range(block.size):
                    place, other = mine[block.a + offset], theirs[block.b + offset]
                    if place not in self.code and other not in taken:
                        self.code[place] = other
                        taken.add(other)
                        self.how["instructions in runs"] += 1

    def function_places(self, analysis):
        """{function entry: its instructions' places}, up to the next function's entry"""
        entries = sorted(analysis.functions)
        places = sorted(analysis.instructions)
        result = {}
        for number, entry in enumerate(entries):
            stop = entries[number + 1] if number + 1 < len(entries) else 1 << 24
            start = bisect_left(places, entry)
            end = bisect_left(places, stop)
            result[entry] = places[start:end]
        return result

    def lone_functions(self):
        """A base function no run reached, looked for by its bytes in the whole version, at an instruction there"""
        taken = set(self.code.values())
        for entry, places in self.function_places(self.base).items():
            if entry in self.code or entry not in self.base.instructions:
                continue
            pattern, known = masked_pattern(self.base_image, self.base, places)
            if known < LONE_KNOWN:
                continue
            hits = [match.start() for match in pattern.finditer(self.version_image)
                    if match.start() in self.version.instructions and match.start() not in taken]
            if len(hits) != 1:
                continue
            delta = hits[0] - entry
            pieces, _ = masked_pattern(self.base_image, self.base, places, whole=True)
            moved = [place for place, length, piece in pieces
                     if place not in self.code and place + delta not in taken
                     and piece.fullmatch(self.version_image[place + delta:place + delta + length])]
            for place in moved:
                self.code[place] = place + delta
                taken.add(place + delta)
            self.how["instructions of functions found alone"] += len(moved)

    def named_data(self):
        """The data each pair of instructions names, paired: the base's names at an address, the version's at its own"""
        votes = defaultdict(Counter)
        for mine, theirs in self.code.items():
            names_mine = self.base.named_by.get(mine)
            names_theirs = self.version.named_by.get(theirs)
            if not names_mine:
                continue
            values_mine = [operand_value(operand) for operand in self.base.instructions[mine].operands
                           if names_program(self.base.instructions[mine], operand)]
            version_instruction = self.version_instruction(theirs)
            values_theirs = [operand_value(operand) for operand in version_instruction.operands
                             if names_program(version_instruction, operand)]
            for value_mine, value_theirs in zip(values_mine, values_theirs):
                for place in names_mine:
                    if cpu_of(place) != value_mine:
                        continue
                    # (the version's analysis may not walk that code: unused, reached by what it does not follow)
                    for other in names_theirs or (place_from(bank_of(place), value_theirs),):
                        if other is not None and cpu_of(other) == value_theirs and bank_of(other) == bank_of(place):
                            votes[place][other] += 1
        for place, counter in votes.items():
            self.data[place] = counter.most_common(1)[0][0]
        self.how["places of data named by paired instructions"] = len(self.data)

    def place_of(self, place, is_code):
        """Where a base place is in the version, or None"""
        if is_code:
            return self.code.get(place, self.changed.get(place))
        if place in self.data:
            return self.data[place]
        return self.code.get(place)

    def near_bytes(self, place, length):
        """A place of data no instruction pair names, looked for by its bytes near where the places around it moved"""
        moved = sorted((mine, theirs) for mine, theirs in list(self.code.items()) + list(self.data.items())
                       if area_of(mine) == area_of(place))
        if not moved:
            return None
        index = bisect_left(moved, (place, -1))
        neighbours = moved[max(0, index - 1):index + 1]
        expected = place + min((theirs - mine for mine, theirs in neighbours), key=abs)
        needle = self.base_image[place:place + length]
        low = max(0, expected - NEAR)
        best = None
        for match in re.finditer(re.escape(needle), self.version_image[low:expected + NEAR + length]):
            found = low + match.start()
            if area_of(found) == area_of(place) and (best is None or abs(found - expected) < abs(best - expected)):
                best = found
        return best


def target_place(place, instruction):
    """The place a branch, jump or call with a fixed target goes to (in its own bank from a bank's code), or None"""
    target = instruction.target
    if target is None or target < PROGRAM_START:
        return None
    if target >= 0x8000:
        return FIXED_START + target - 0x8000
    bank = bank_of(place)
    return None if bank is None else bank * BANK_SIZE + target - PROGRAM_START


def place_from(bank, cpu):
    """The place of a CPU address with a bank in the window (None: the fixed program only)"""
    if cpu >= 0x8000:
        return FIXED_START + cpu - 0x8000
    if bank is None or cpu < PROGRAM_START:
        return None
    return bank * BANK_SIZE + cpu - PROGRAM_START


def operand_value(operand):
    return operand.target if operand.mode == "indexed" and operand.target is not None else operand.value


def cpu_of(place):
    return 0x8000 + place - FIXED_START if place >= FIXED_START else PROGRAM_START + place % BANK_SIZE


def bank_of(place):
    return None if place >= FIXED_START else place // BANK_SIZE


TOKEN = re.compile(r"(?<![0-9A-Za-z_])[0-9A-F]{6}(?![0-9A-Za-z_])")
FOUR = re.compile(r"(?<![0-9A-Za-z_])[0-9A-F]{4}(?![0-9A-Za-z_])")
CODE_KINDS = ("func", "code", "file")
DATA_KINDS = ("data", "table")
BY_FUNCTION = ("comment", "param", "result", "flag", "constant", "window", "task", "unread")


def translate(text, places):
    """Each place of the text the version has moved to its own"""
    return TOKEN.sub(lambda match: format(places[int(match.group(0), 16)], "06X")
                     if int(match.group(0), 16) in places else match.group(0), text)


def rewrite_symbols(lines, places, gone, entries=None):
    """symbols.txt's lines carried to the version: (its lines, the ones that could not be). 'places' maps the base's
    places to the version's, 'gone' names the functions the version does not have; 'entries' gives a function's name
    its base entry, so that a local's sites stay about as far from its function's start as they were. A line that
    cannot be carried is kept as a comment ('#? ' and the line), and so is a code line whose table of targets was not
    carried"""
    out, review = [], []
    entries = entries or {}
    tables_gone = {words[2].split("[")[0] for words in (line.split() for line in lines)
                   if len(words) > 2 and TOKEN.fullmatch(words[0]) and words[1] in DATA_KINDS
                   and int(words[0], 16) not in places}

    def near_its_start(function, where):
        if function not in entries or entries[function] not in places:
            return True
        start = entries[function]
        return all(area_of(int(part, 16)) == area_of(start) and area_of(places[int(part, 16)]) == area_of(places[start])
                   and abs((places[int(part, 16)] - places[start]) - (int(part, 16) - start)) <= LOCAL_SLACK
                   for part in where)

    def drop(line):
        out.append("#? " + line)
        review.append(line)

    for line in lines:
        words = line.split()
        if not words or words[0].startswith("#"):
            out.append(translate(line, places))
        elif TOKEN.fullmatch(words[0]) and len(words) >= 2 and words[1] in CODE_KINDS + DATA_KINDS:
            calls = [word.split("=", 1)[1] for word in words[3:] if word.startswith("calls=")]
            if int(words[0], 16) in places and not any(table in tables_gone for table in calls):
                out.append(translate(line, places))
            else:
                drop(line)
        elif FOUR.fullmatch(words[0]):
            out.append(line)
        elif words[0] == "local" and len(words) >= 3:
            where = words[2].split(",")
            if words[1] in gone or not all(TOKEN.fullmatch(part) and int(part, 16) in places for part in where)                     or not near_its_start(words[1], where):
                drop(line)
                continue
            start = line.index(words[2], line.index(words[1]) + len(words[1]))
            moved = ",".join(format(places[int(part, 16)], "06X") for part in where)
            out.append(line[:start] + moved + line[start + len(words[2]):])
        elif words[0] in BY_FUNCTION and len(words) >= 2 and words[1] in gone:
            drop(line)
        elif words[0] == "comment":
            out.append(translate(line, places))
        else:
            out.append(line)
    return out, review


def carry(match, symbols_lines):
    """The places symbols.txt names, the version's: {base place: version place}, the base functions the version has
    not, and the review's lines about the data looked for by its bytes"""
    places = {}
    named = []
    for line in symbols_lines:
        words = line.split()
        if len(words) >= 2 and TOKEN.fullmatch(words[0]) and words[1] in CODE_KINDS + DATA_KINDS:
            named.append((int(words[0], 16), words[1], words[2] if len(words) > 2 else ""))
    starts = sorted(place for place, _, _ in named)
    # where the base functions' first instructions went
    entry_places = {match.code[place] for place, line_kind, _ in named if line_kind == "func" and place in match.code}
    base_functions = match.function_places(match.base)
    version_places = sorted(match.version.instructions)
    paired = set(match.code.values())

    def moved_entry(entry):
        """Where a base function whose first instructions have no pair starts in the version: the start of the
        stretch of unpaired instructions (MOVED_ENTRY_MOST at most) right before its first paired instruction's
        place, when nothing falls through into that stretch (the version put its own code in place of the base's
        first instructions: a call to a routine the version shares among several)"""
        function = base_functions.get(entry, [])
        first = next((place for place in function if place in match.code), None)
        # (most of the function paired, and only its first few instructions not)
        if first is None or 2 * sum(1 for place in function if place in match.code) < len(function)                 or function.index(first) > MOVED_ENTRY_MOST:
            return None
        index = bisect_left(version_places, match.code[first])
        start = None
        while index > 0 and match.code[first] - version_places[index - 1] <= 3 * MOVED_ENTRY_MOST:
            previous = version_places[index - 1]
            if previous in paired or previous + match.version.instructions[previous].length != version_places[index]                     or area_of(previous) != area_of(match.code[first]):
                break
            start, index = previous, index - 1
        if start is None or len([place for place in version_places[index:] if place < match.code[first]]) \
                > MOVED_ENTRY_MOST:
            return None
        if index > 0:
            before = version_places[index - 1]
            instruction = match.version.instructions[before]
            if before + instruction.length == start and instruction.flow not in (dis.FLOW_JUMP, dis.FLOW_RETURN):
                return None
        return start

    owner = {place: entry for entry, function in base_functions.items() for place in function}
    base_of = {theirs: mine for mine, theirs in match.code.items()}
    moved = {place: moved_entry(place) for place, line_kind, _ in named if line_kind == "func" and place not in match.code}

    def its_own(entry, target):
        """Whether the version function a base function was paired with can take its name: no other base
        function's first instruction went there or starts there (moved_entry), and its first instruction is new or the
        base function's own"""
        return target not in entry_places and all(other == entry or found != target for other, found in moved.items())             and (target not in base_of or owner.get(base_of[target]) == entry)

    gone = set()
    for place, line_kind, name in named:
        # a function whose start changed goes to the function it was paired with, not where its first instruction
        # went (a base function split in two in the version: the name to the part the base's callers reach, the
        # rest the version's own), unless another base function's first instruction went there (the version's
        # function holds code that table jumps reach, its stretches the base's functions); a function whose first
        # instructions have no pair, to where its stretch starts in the version
        found = None
        if line_kind == "func" and place in match.changed and its_own(place, match.changed[place]):
            found = match.changed[place]
        if found is None and line_kind == "func" and place not in match.code:
            found = moved.get(place)
            if found is not None:
                match.how["functions whose first instructions were put in place of"] += 1
        if found is None and not (line_kind == "func" and place not in match.code):
            found = match.place_of(place, line_kind in CODE_KINDS)
        if found is None and line_kind in DATA_KINDS:
            index = bisect_left(starts, place + 1)
            following = starts[index] if index < len(starts) else place + 32
            found = match.near_bytes(place, max(4, min(32, following - place)))
            if found is not None:
                match.how["places of data found by their bytes"] += 1
        if found is None:
            if line_kind == "func":
                gone.add(name)
            continue
        places[place] = found
    # the instructions too, for the locals' sites and the places comments cite
    for mine, theirs in match.code.items():
        places.setdefault(mine, theirs)
    for mine, theirs in match.data.items():
        places.setdefault(mine, theirs)
    return places, gone


def main():
    arguments = sys.argv[1:]
    options = {}
    for option in ("--base", "--out"):
        if option in arguments:
            position = arguments.index(option)
            options[option] = Path(arguments[position + 1])
            del arguments[position:position + 2]
    if len(arguments) != 1:
        raise SystemExit(__doc__)
    found = rom.load_set(Path(arguments[0]))
    set_name = rom.set_of(found) if hasattr(rom, "set_of") else Path(arguments[0]).stem
    version_image = rom.build_program(found)
    base_image = rom.build_program(rom.load_set(options.get("--base", ROOT / "rom")))
    own = ROOT / "versions" / set_name / "symbols.txt"
    out = options.get("--out", ROOT / "build" / "versions" / set_name)
    out.mkdir(parents=True, exist_ok=True)

    # the version's own lines; 'drop NAME' takes a base function the version has not, though its code may be
    # paired (a table's entry past its end that leads elsewhere in the version), with its lines out
    own_lines = own.read_text(encoding="utf-8").splitlines() if own.exists() else []
    dropped = {line.split()[1] for line in own_lines if len(line.split()) == 2 and line.split()[0] == "drop"}
    own_lines = [line for line in own_lines if not (len(line.split()) == 2 and line.split()[0] == "drop")]
    own_path = out / "own_symbols.txt"
    own_path.write_text("\n".join(own_lines) + "\n", encoding="utf-8")

    base_path = ROOT / "symbols.txt"
    match = Match(base_image, disassemble.load_symbols(base_path), version_image,
                  disassemble.load_symbols(own_path)).run()
    lines = base_path.read_text(encoding="utf-8").splitlines()
    places, gone = carry(match, lines)
    entries = {line.split()[2]: int(line.split()[0], 16) for line in lines
               if len(line.split()) > 2 and TOKEN.fullmatch(line.split()[0]) and line.split()[1] == "func"}
    unknown = sorted(dropped - set(entries))
    if unknown:
        raise SystemExit(f"versions/{set_name}/symbols.txt: drop of no base function: {', '.join(unknown)}")
    for name in dropped:
        places.pop(entries[name], None)
    gone |= dropped
    carried, review = rewrite_symbols(lines, places, gone, entries)

    header = [f"# {set_name}: generated by tools/match_version.py from symbols.txt (the base set's) and "
              f"versions/{set_name}/symbols.txt; lines '#? ' are the base's the version could not be given"]
    (out / "symbols.txt").write_text("\n".join(header + carried + own_lines) + "\n", encoding="utf-8")

    # the version's functions no base function's name went to: its own code, to be named
    reached = {places[place] for place in entries.values() if place in places}
    reached |= {int(line.split()[0], 16) for line in own_lines
                if len(line.split()) >= 3 and TOKEN.fullmatch(line.split()[0]) and line.split()[1] == "func"}
    new_functions = sorted(entry for entry in match.version.functions if entry not in reached)
    base_names = {int(line.split()[0], 16): line.split()[2] for line in lines
                  if len(line.split()) > 2 and TOKEN.fullmatch(line.split()[0]) and line.split()[1] == "func"}
    changed = [f"{entry:06X} -> {target:06X} {base_names.get(entry, '')}"
               for entry, target in sorted(match.changed.items())]
    report = [f"{set_name}: {len(match.code)} of {len(match.base.instructions)} base instructions matched "
              f"({len(match.version.instructions)} in the version)"]
    report += [f"  {what}: {count}" for what, count in match.how.items()]
    report.append(f"base functions the version has not (by their entries): {len(gone)}")
    report.append(f"version functions no base function's name went to: {len(new_functions)}")
    report.append(f"symbols.txt lines not carried: {len(review)}")
    (out / "review.txt").write_text("\n".join(
        report + ["", "== functions changed at their start (their names, comments and parameters to check)"]
        + changed + ["", "== lines not carried"] + review + ["", "== version functions no base function's name went to"]
        + [f"{entry:06X}" for entry in new_functions]) + "\n", encoding="utf-8")
    names = {place: name for place, name in ((int(line.split()[0], 16), line.split()[2]) for line in lines
             if len(line.split()) > 2 and TOKEN.fullmatch(line.split()[0])
             and line.split()[1] in CODE_KINDS + DATA_KINDS)}
    (out / "places.txt").write_text("\n".join(
        f"{place:06X} {places[place]:06X} {name}" for place, name in sorted(names.items()) if place in places)
        + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"wrote {out / 'symbols.txt'}, review.txt, places.txt")


if __name__ == "__main__":
    main()
