"""
Name:        select_scenarios.py
Description: which scenarios a change has to run again: the working tree against the state last verified, by
             what each scenario executes and reads
Version:     0.1.0
Created:     11-11-2026
Modified:    11-11-2026
Author:      Sebastian Toyos

Usage: python tools/select_scenarios.py [--big-endian] [--base REV] [--cover] [--run] [--save] [--jobs N]
                                        [--bin DIR]
  (nothing)      list the scenarios the working tree needs, each with why, and what changed
  --big-endian   for the big-endian build (PowerPC under QEMU, tools/big_endian_scenarios.py) instead of the PC's:
                 its own verified state, and the loader's layout counts
  --base REV     compare with a commit instead of the state last saved (the loader's data is then compared
                 only when the saved state is that commit's)
  --cover        of the scenarios needed, the shortest that between them run every function that changed: a
                 first check, not the verification
  --run          run them (the PC: tools/check_scenarios.py NAMES --native-only --video --sound; a scenario
                 whose control script changed runs on the reference again); when all of them are identical
                 the working tree is saved as verified
  --save         save the working tree as verified without running anything (after a full check_scenarios.py)
  --jobs N       scenarios at a time (half the processors by default)
  --bin DIR      the PC's executables (build/bin by default)

What a scenario depends on, and how a change reaches it:

  the C         src/ is compared function by function, comments and layout apart. A function is the same when
                its tokens are, or when they differ only in names: its own locals and parameters renamed one
                for one, and the names symbols.txt renames (a line of it that differs in that one name). The
                declarations are compared too: a macro, a prototype, a table's entry, and each structure's
                members by their offsets and types; a function that names one that changed counts as changed.
                A changed function needs the scenarios whose reference run executed an address of it
                (build/scenarios/NAME/executed.txt, sound_executed.txt for the sound program; a function's
                addresses go from its func line in symbols.txt to the next one).
  the data      the game's loaded data (the headless game's dumps: the bytes of every piece, and where the
                loader lays out the values wider than a byte) against the saved state's: a scenario needs to
                run when the original read an address that changed (build/access_widths/NAME.txt, the
                census of tools/width_census.py run). Where the values lie matters only to a big-endian
                processor. The sound program's data, and the other assets (pictures, samples), are every
                scenario's.
  the rest      the runtime, the headless port, the build's files and the sound's timing model are every
                scenario's; a control script is its own scenario's, whose reference has to run again.

The verified state is a copy of what was compared (build/verified/pc, build/verified/big-endian: src/, the
symbol files, the dumps, the other files' hashes), written by --save or by a --run that ends identical. Without
one the comparison is with the last commit (HEAD), and the loader's data is not compared.
"""
import bisect
import datetime
import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_profile  # noqa: E402  (the game's: its root, its ROM set, its executables' names)

ROOT = game_profile.ROOT
rom = game_profile.rom
CONTROLS = ROOT / "tests" / "controls"
RESULTS = ROOT / "build" / "scenarios"
CENSUS = ROOT / "build" / "access_widths"
VERIFIED = ROOT / "build" / "verified"
SOURCES = "src"
SOUND_SOURCES = getattr(game_profile, "SOUND_SOURCES", "src/sound")
# what every scenario depends on, whatever it executes: the board's runtime, the headless port, the build
EVERY_SCENARIO = getattr(game_profile, "EVERY_SCENARIO", ("runtime", "ports/headless", "CMakeLists.txt", "cmake",
                                                         "build/sound/timing.txt"))
# the assets the loader's dumps hold (the program's data and the sound program's), compared through them;
# the rest of the assets are every scenario's
ASSETS = "assets"
DUMPED_ASSETS = getattr(game_profile, "DUMPED_ASSETS", ("assets/data", "assets/sound/data"))
# the objects the generated C keeps for the runtime that say nothing on their own: how many entries a list has
# (each entry that changed counts by the names in it)
COUNTS = ("game_table_count", "game_code_count", "sound_table_count")

TOKEN = re.compile(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|[A-Za-z_]\w*"
                   r"|\.?\d(?:[eEpP][+-]|[\w.])*|->|\S", re.S)
IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")
SIZES = {"uint8_t": 1, "int8_t": 1, "char": 1, "bool": 1, "uint16_t": 2, "int16_t": 2, "uint32_t": 4, "int32_t": 4,
         "float": 4, "uint64_t": 8, "int64_t": 8, "double": 8}
TYPE_WORDS = set(SIZES) | {"const", "struct", "union", "static", "unsigned", "signed", "int", "short", "long",
                           "size_t", "void", "volatile", "extern", "enum", "uintptr_t"}
KEYWORDS = TYPE_WORDS | {"if", "else", "for", "while", "do", "switch", "case", "default", "break", "continue",
                         "return", "sizeof", "goto", "true", "false", "NULL"}
POINTER_SIZE = 8    # a host pointer in a structure: its size is the host's, the same on both sides of a comparison


# ---------------------------------------------------------------------------------------------------------------
# the C, as units: functions, structures, declarations, macros
# ---------------------------------------------------------------------------------------------------------------

class Unit:
    """A top-level piece of a C file: kind (function, struct, declaration, macro, directive), the name it is
    known by (a function's, a structure's tag, a macro's; None for the rest), its tokens without comments, the
    file it is in"""
    __slots__ = ("kind", "name", "tokens", "path")

    def __init__(self, kind, name, tokens, path):
        self.kind, self.name, self.tokens, self.path = kind, name, tuple(tokens), path


def tokens_of(text):
    return [token for token in TOKEN.findall(text) if not token.startswith(("/*", "//"))]


def is_name(token):
    return IDENTIFIER.match(token) is not None and token not in KEYWORDS


def file_units(text, path):
    """The units of a C file"""
    # comments out first (a # in one is no directive), strings kept; then the directives, a line each
    text = re.sub(r"/\*.*?\*/|//[^\n]*|(\"(?:\\.|[^\"\\\n])*\")", lambda found: found.group(1) or " ", text, flags=re.S)
    text = text.replace("\\\n", " ")
    units, code = [], []
    for line in text.split("\n"):
        if line.lstrip().startswith("#"):
            tokens = tokens_of(line)
            if len(tokens) > 2 and tokens[1] == "define":
                units.append(Unit("macro", tokens[2], tokens, path))
            else:
                units.append(Unit("directive", None, tokens, path))
        else:
            code.append(line)
    tokens = tokens_of("\n".join(code))
    start, depth, body = 0, 0, None
    for index, token in enumerate(tokens):
        if token in "{(":
            # (a function's body: the first brace outside everything, right after its parameters)
            if token == "{" and depth == 0 and body is None and index > start and tokens[index - 1] == ")":
                body = index
            depth += 1
        elif token in "})":
            depth -= 1
            if token == "}" and depth == 0 and body is not None:
                units.append(function_unit(tokens[start:index + 1], path))
                start, body = index + 1, None
        elif token == ";" and depth == 0:
            units.append(declaration_unit(tokens[start:index + 1], path))
            start = index + 1
    return units


def function_unit(tokens, path):
    name = tokens[tokens.index("(") - 1]
    if tokens[tokens.index("(") + 2] == ")" and tokens[tokens.index("(") + 3] == "(":
        # a definition through a macro of the runtime's that takes its name: BOARD_ORIGINAL(name)(...)
        name = tokens[tokens.index("(") + 1]
    return Unit("function", name, tokens, path)


def declaration_unit(tokens, path):
    if tokens[0] in ("struct", "union") and len(tokens) > 2 and tokens[2] == "{":
        return Unit("struct", tokens[1], tokens, path)
    return Unit("declaration", None, tokens, path)


def declared_names(tokens):
    """The names a declaration declares: of each declarator, the last name outside brackets before its
    parameters, its dimensions or its value"""
    names, last, depth, open_for_names = [], None, 0, True
    for token in tokens:
        if token in "([{":
            if depth == 0 and open_for_names and last is not None:
                names.append(last)
                last, open_for_names = None, False
            depth += 1
        elif token in ")]}":
            depth -= 1
        elif depth == 0:
            if token == "=":
                if open_for_names and last is not None:
                    names.append(last)
                last, open_for_names = None, False
            elif token in ",;":
                if open_for_names and last is not None:
                    names.append(last)
                last, open_for_names = None, True
            elif is_name(token) and open_for_names:
                last = token
    return names


def is_prototype(tokens):
    """A declaration that defines nothing: an extern, or a function's prototype"""
    if tokens[0] == "extern" or "=" in tokens:
        return tokens[0] == "extern"
    return "(" in tokens


def name_uses(tokens):
    """(the names a unit uses outside member accesses, the member names it reaches)"""
    plain, members = set(), set()
    for index, token in enumerate(tokens):
        if IDENTIFIER.match(token) and token not in KEYWORDS:
            (members if index and tokens[index - 1] in (".", "->") else plain).add(token)
    return plain, members


# ---------------------------------------------------------------------------------------------------------------
# names that changed and nothing else
# ---------------------------------------------------------------------------------------------------------------

class Renames:
    """The names the symbol files rename: (old, new) of every line of the new file that differs from a line
    of the old one in that one name"""

    def __init__(self):
        self.pairs = set()
        self.by_old = defaultdict(set)

    def add_files(self, old_text, new_text):
        def lines(text):
            found = set()
            for line in text.splitlines():
                tokens = tuple(line.split("#", 1)[0].split())
                if tokens and tokens[0] != "comment":
                    found.add(tokens)
            return found

        old, new = lines(old_text or ""), lines(new_text or "")
        added = defaultdict(list)
        for tokens in new - old:
            for index, token in enumerate(tokens):
                if IDENTIFIER.match(token):
                    added[tokens[:index] + (None,) + tokens[index + 1:]].append(token)
        for tokens in old - new:
            for index, token in enumerate(tokens):
                if IDENTIFIER.match(token):
                    for other in added.get(tokens[:index] + (None,) + tokens[index + 1:], ()):
                        self.pairs.add((token, other))
                        self.by_old[token].add(other)

    def allows(self, old, new):
        """True when old -> new is a renaming: the two names, or the names the C makes out of them (NAME_ADDRESS,
        NAME_data, call through its slot...)"""
        if (old, new) in self.pairs:
            return True
        for first, second in self.pairs:
            if first in old and old.replace(first, second, 1) == new and len(old) - len(first) == len(new) - len(second):
                return True
        return False


def same_but_names(old, new, renames):
    """True when two token lists differ only in names the symbol files rename"""
    if len(old) != len(new):
        return False
    for before, after in zip(old, new):
        if before != after and not (is_name(before) and is_name(after) and renames.allows(before, after)):
            return False
    return True


def split_function(tokens):
    """(the tokens of a function without its locals' declarations, each token a (member?, text) pair; its
    declarations as (type tokens, declarator tokens); the names of its parameters and locals)"""
    body = tokens.index("{", tokens.index("("))
    locals_ = set()
    depth = 0
    # the parameters: the last name of each
    last = None
    for token in tokens[tokens.index("(") + 1:body]:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        if token in (",", ")") and depth <= 0 and last is not None:
            locals_.add(last)
            last = None
        elif is_name(token):
            last = token
    rest, declarations = list(tokens[:body + 1]), []
    index, round_depth = body + 1, 0
    while index < len(tokens):
        token = tokens[index]
        starts = tokens[index - 1] in ("{", "}", ";") and round_depth == 0
        if starts and token in TYPE_WORDS and token != "sizeof":
            end = index
            nested = 0
            while tokens[end] != ";" or nested:
                nested += tokens[end] in "({[" and 1 or tokens[end] in ")}]" and -1 or 0
                end += 1
            statement = tokens[index:end]
            # the type: its words, a structure's tag with them
            cut = 0
            while cut < len(statement) and (statement[cut] in TYPE_WORDS or (cut and statement[cut - 1] in ("struct", "union"))):
                cut += 1
            kind, declarators, current, nested = tuple(statement[:cut]), [], [], 0
            for part in statement[cut:] + (",",):
                nested += part in "({[" and 1 or part in ")}]" and -1 or 0
                if part == "," and nested == 0:
                    declarators.append(tuple(current))
                    current = []
                else:
                    current.append(part)
            for declarator in declarators:
                declarations.append((kind, declarator))
                for part in declarator:
                    if is_name(part):
                        locals_.add(part)
                        break
            index = end + 1
            continue
        round_depth += token == "(" and 1 or token == ")" and -1 or 0
        rest.append(token)
        index += 1
    tagged = [(position > 0 and rest[position - 1] in (".", "->"), token) for position, token in enumerate(rest)]
    return tagged, declarations, locals_


def same_function(old, new, renames):
    """True when two functions do the same: their tokens the same, or the same but for names renamed one for
    one, each of them a local of the function or a renaming of the symbol files"""
    if old.tokens == new.tokens:
        return True
    old_rest, old_declarations, old_locals = split_function(old.tokens)
    new_rest, new_declarations, new_locals = split_function(new.tokens)
    if len(old_rest) != len(new_rest) or len(old_declarations) != len(new_declarations):
        return False
    forward, backward = {}, {}
    for (member, before), (other_member, after) in zip(old_rest, new_rest):
        if member != other_member:
            return False
        if not (IDENTIFIER.match(before) and before not in KEYWORDS):
            if before != after:
                return False
            continue
        if not (IDENTIFIER.match(after) and after not in KEYWORDS):
            return False
        if forward.setdefault((member, before), after) != after or backward.setdefault((member, after), before) != before:
            return False
        if before != after:
            both_local = not member and before in old_locals and after in new_locals
            if not both_local and not renames.allows(before, after):
                return False
        elif not member and (before in old_locals) != (after in new_locals):
            return False

    def renamed(declaration):
        kind, declarator = declaration
        return (tuple(forward.get((False, token), token) for token in kind),
                tuple(forward.get((False, token), token) for token in declarator))

    return Counter(renamed(each) for each in old_declarations) == Counter(new_declarations)


# ---------------------------------------------------------------------------------------------------------------
# the structures' members, by offset
# ---------------------------------------------------------------------------------------------------------------

class NoLayout(Exception):
    pass


def struct_members(tokens, sizes):
    """(size, [(offset, type, dimensions, name)]) of a structure's definition, its members laid out one after
    another (the generated structures are packed), those of the structures and unions without a name inside it
    as its own"""
    def number(text):
        try:
            return int(text.rstrip("uUlL"), 0)
        except ValueError:
            raise NoLayout(text)

    def aggregate(index, union):
        """The members from the token after a '{' to its '}': (the index after the '}', size, members)"""
        offset, size, members = 0, 0, []
        while tokens[index] != "}":
            if tokens[index] in ("struct", "union") and "{" in tokens[index + 1:index + 3]:
                inner_union = tokens[index] == "union"
                index, inner_size, inner = aggregate(tokens.index("{", index) + 1, inner_union)
                count, name = 1, None
                if is_name(tokens[index]):
                    name = tokens[index]
                    index += 1
                while tokens[index] == "[":
                    count *= number(tokens[index + 1])
                    index += 3
                if tokens[index] != ";":
                    raise NoLayout(tokens[index])
                index += 1
                if name is None:
                    members += [(offset + at, kind, dimensions, member) for at, kind, dimensions, member in inner]
                else:
                    members.append((offset, f"{{{inner_size}}}", (count,), name))
                width = inner_size * count
            else:
                end = tokens.index(";", index)
                statement = tokens[index:end]
                index = end + 1
                cut = 0
                while cut < len(statement) and (statement[cut] in TYPE_WORDS or statement[cut - 1] in ("struct", "union")
                                                and cut):
                    cut += 1
                kind = " ".join(word for word in statement[:cut] if word not in ("const", "volatile"))
                if kind in SIZES:
                    unit = SIZES[kind]
                elif kind.startswith("struct ") and kind.split()[1] in sizes:
                    unit = sizes[kind.split()[1]]
                else:
                    unit = None
                width, position = 0, cut
                while position < len(statement):
                    pointer = False
                    while statement[position] == "*" or statement[position] == "const":
                        pointer = pointer or statement[position] == "*"
                        position += 1
                    name = statement[position]
                    if not is_name(name):
                        raise NoLayout(name)
                    position += 1
                    dimensions = []
                    while position < len(statement) and statement[position] == "[":
                        # (name[]: a record's values that go on past it, no bytes of its own)
                        open_ended = statement[position + 1] == "]"
                        dimensions.append(0 if open_ended else number(statement[position + 1]))
                        position += 2 if open_ended else 3
                    if position < len(statement) and statement[position] != ",":
                        raise NoLayout(statement[position])
                    position += 1
                    each = POINTER_SIZE if pointer else unit
                    if each is None:
                        raise NoLayout(kind)
                    count = 1
                    for dimension in dimensions:
                        count *= dimension
                    place = offset if union else offset + width
                    members.append((place, kind + (" *" if pointer else ""), tuple(dimensions), name))
                    width = max(width, each * count) if union else width + each * count
            if union:
                size = max(size, width)
            else:
                offset += width
                size = offset
        return index + 1, size, members

    _, size, members = aggregate(tokens.index("{") + 1, tokens[0] == "union")
    return size, members


# ---------------------------------------------------------------------------------------------------------------
# one program's C, old against new
# ---------------------------------------------------------------------------------------------------------------

class Comparison:
    """What changed between two copies of a program's C: .functions {name: why} of the functions that have to
    be checked again, .every [why] of what reaches every scenario, .notes [text] of what changed and reaches
    none"""

    def __init__(self, old_files, new_files, renames):
        self.functions, self.every, self.notes = {}, [], []
        self.renames = renames
        self.runtime_objects = set()    # the objects the C defines that changed: the runtime's, when no code names them
        old_units = [unit for path, text in sorted(old_files.items()) for unit in file_units(text, path)]
        new_units = [unit for path, text in sorted(new_files.items()) for unit in file_units(text, path)]
        self.new_functions = {unit.name: unit for unit in new_units if unit.kind == "function"}
        old_functions = {unit.name: unit for unit in old_units if unit.kind == "function"}
        plain, members = self.changed_names([unit for unit in old_units if unit.kind != "function"],
                                            [unit for unit in new_units if unit.kind != "function"])
        self.spread(plain, members, new_units)
        # the functions themselves, a renamed one against the one it was
        was = {new: old for old, new in renames.pairs if old in old_functions and new in self.new_functions
               and old not in self.new_functions}
        for name, unit in self.new_functions.items():
            old = old_functions.get(name) or old_functions.get(was.get(name))
            if old is None:
                self.functions[name] = "new"
            elif not same_function(old, unit, renames):
                self.functions[name] = "changed"
        gone = set(old_functions) - set(self.new_functions) - set(was.values())
        if gone:
            self.notes.append(f"{len(gone)} functions gone: " + ", ".join(sorted(gone)[:6]) + ("..." if len(gone) > 6 else ""))
        # the functions that name something that changed
        for name, unit in self.new_functions.items():
            if name in self.functions:
                continue
            uses, reaches = name_uses(unit.tokens)
            found = sorted(uses & set(plain)) or sorted(reaches & set(members))
            if found:
                source = plain if found[0] in plain else members
                self.functions[name] = f"names {found[0]} ({source[found[0]]})"

    def changed_names(self, old_units, new_units):
        """({name: why}, {member name: why}) of the declarations that changed: the names code uses, and the
        structures' members"""
        plain, members = {}, {}
        old_count, new_count = Counter(unit.tokens for unit in old_units), Counter(unit.tokens for unit in new_units)
        removed = [unit for unit in old_units if unit.tokens not in new_count]
        added = [unit for unit in new_units if unit.tokens not in old_count]
        # (a declaration renamed and nothing else is the same one)
        by_shape = defaultdict(list)
        for unit in added:
            by_shape[(unit.kind, len(unit.tokens))].append(unit)
        left = []
        for unit in removed:
            same = next((other for other in by_shape[(unit.kind, len(unit.tokens))]
                         if same_but_names(unit.tokens, other.tokens, self.renames)), None)
            if same is None:
                left.append(unit)
            else:
                by_shape[(unit.kind, len(unit.tokens))].remove(same)
        removed, added = left, [unit for found in by_shape.values() for unit in found]

        old_structs = {unit.name: unit for unit in old_units if unit.kind == "struct"}
        new_structs = {unit.name: unit for unit in new_units if unit.kind == "struct"}
        old_sizes, new_sizes = {}, {}
        old_layouts = self.layouts(old_units, old_sizes)
        new_layouts = self.layouts(new_units, new_sizes)
        resized = set()
        for unit in removed + added:
            where = unit.path
            if unit.kind == "directive":
                self.every.append(f"{where}: {' '.join(unit.tokens)[:60]}")
            elif unit.kind == "macro":
                plain.setdefault(unit.name, f"its definition, {where}")
            elif unit.kind == "struct":
                tag = unit.name
                if tag not in old_structs or tag not in new_structs:
                    # a structure that is new or gone: what is declared as one names it
                    plain.setdefault(tag, f"struct {tag}, {where}")
                    continue
                if tag not in old_layouts or tag not in new_layouts:
                    # one whose members cannot be laid out: all its names
                    plain.setdefault(tag, f"struct {tag}, {where}")
                    for token in unit.tokens:
                        if is_name(token):
                            members.setdefault(token, f"struct {tag}, {where}")
                    continue
                if old_sizes[tag] != new_sizes[tag]:
                    resized.add(tag)
                    plain.setdefault(tag, f"struct {tag}'s size, {where}")
                before, after = defaultdict(set), defaultdict(set)
                for target, layout in ((before, old_layouts[tag]), (after, new_layouts[tag])):
                    for offset, kind, dimensions, name in layout:
                        target[(offset, kind, dimensions)].add(name)
                for key in set(before) | set(after):
                    old_names, new_names = before[key] - after[key], after[key] - before[key]
                    for name in sorted(old_names):
                        match = next((other for other in sorted(new_names) if self.renames.allows(name, other)), None)
                        if match is not None:
                            new_names.discard(match)
                        else:
                            members.setdefault(name, f"struct {tag}'s member at {key[0]:X}")
                    for name in new_names:
                        members.setdefault(name, f"struct {tag}'s member at {key[0]:X}")
            else:
                self.declaration_names(unit, old_units if unit in added else new_units, plain, where)
        # whatever holds a structure whose size changed moves what comes after it by its own offsets (compared
        # above); the arrays of it are reached by its size
        for tag in resized:
            for layouts in (old_layouts, new_layouts):
                for layout in layouts.values():
                    for _, kind, _, name in layout:
                        if kind.split()[:2] == ["struct", tag]:
                            members.setdefault(name, f"struct {tag}'s size")
        return plain, members

    @staticmethod
    def layouts(units, sizes):
        found = {}
        for unit in units:
            if unit.kind == "struct":
                try:
                    sizes[unit.name], found[unit.name] = struct_members(unit.tokens, sizes)
                except (NoLayout, ValueError, IndexError):
                    pass
        return found

    def declaration_names(self, unit, other_side, plain, where):
        """A declaration that changed: the names it declares; of a list with values, the names in the entries
        that changed"""
        names = declared_names(unit.tokens)
        if "{" in unit.tokens and names:
            other = next((each for each in other_side if each.kind == "declaration" and "{" in each.tokens
                          and declared_names(each.tokens) == names), None)
            if other is not None:
                mine, theirs = Counter(self.entries(unit.tokens)), Counter(self.entries(other.tokens))
                for entry in (mine - theirs) + (theirs - mine):
                    for token in entry:
                        if is_name(token):
                            plain.setdefault(token, f"{names[0]}'s entry, {where}")
                        elif token.startswith('"') and IDENTIFIER.match(token[1:-1]):
                            plain.setdefault(token[1:-1], f"{names[0]}'s entry, {where}")
                return
        for name in names:
            if name not in COUNTS:
                plain.setdefault(name, f"its declaration, {where}")
                if not is_prototype(unit.tokens) and where.endswith(".c"):
                    self.runtime_objects.add(name)

    @staticmethod
    def entries(tokens):
        """The entries of a list's values: what the commas of its outer braces part"""
        found, current, depth = [], [], 0
        for token in tokens[tokens.index("{") + 1:]:
            if token in "{(":
                depth += 1
            elif token in "})":
                depth -= 1
                if depth < 0:
                    break
            if token == "," and depth == 0:
                found.append(tuple(current))
                current = []
            else:
                current.append(token)
        if current:
            found.append(tuple(current))
        return found

    def spread(self, plain, members, new_units):
        """A macro or a declaration that names something that changed has changed too; an object of the C that
        changed and no function names is the runtime's to read: every scenario's"""
        declarations = []
        for unit in new_units:
            if unit.kind in ("macro", "declaration"):
                names = [unit.name] if unit.kind == "macro" else declared_names(unit.tokens)
                uses, reaches = name_uses(unit.tokens)
                declarations.append((names, uses - set(names), reaches, unit))
        grew = True
        while grew:
            grew = False
            for names, uses, reaches, unit in declarations:
                if all(name in plain for name in names):
                    continue
                found = sorted(uses & set(plain)) or sorted(reaches & set(members))
                if found:
                    for name in names:
                        if name not in plain and name not in COUNTS:
                            plain[name] = f"through {found[0]}"
                            grew = True
        used = set()
        for unit in new_units:
            if unit.kind == "function":
                used |= name_uses(unit.tokens)[0]
        for name in sorted(self.runtime_objects):
            if name not in used and name not in self.new_functions:
                self.every.append(f"{name} ({plain[name]}), which the runtime reads")


# ---------------------------------------------------------------------------------------------------------------
# the state last verified, and the working tree
# ---------------------------------------------------------------------------------------------------------------

def git(*arguments):
    result = subprocess.run(["git", "-C", str(ROOT)] + list(arguments), capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout


def tree_files(folder):
    """{path from the root: Path} of the files under a folder of the working tree (or the file itself)"""
    base = ROOT / folder
    if base.is_file():
        return {folder: base}
    return {path.relative_to(ROOT).as_posix(): path for path in sorted(base.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts}


def sha1(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


def read_text(path):
    return path.read_text(encoding="utf-8") if path.exists() else None


def watched_files():
    """{path: Path} of the files whose change is told by their hash: what every scenario depends on, the
    control scripts, the assets"""
    found = {}
    for folder in EVERY_SCENARIO + ("tests/controls", ASSETS):
        if (ROOT / folder).exists():
            found.update(tree_files(folder))
    return found


class Saved:
    """The verified state --save wrote"""

    def __init__(self, folder):
        self.folder = folder
        self.name = f"the state verified {read_text(folder / 'state.txt').splitlines()[0]}"
        self.hashes = dict(reversed(line.split(" ", 1)) for line in read_text(folder / "files.txt").splitlines())

    def sources(self):
        base = self.folder / SOURCES
        return {path.relative_to(self.folder).as_posix(): path.read_text(encoding="utf-8")
                for path in sorted(base.rglob("*")) if path.is_file()}

    def text(self, path):
        return read_text(self.folder / path)

    def changed(self):
        """The watched files that are other than they were (changed, new or gone)"""
        now = watched_files()
        return sorted(path for path in set(now) | set(self.hashes)
                      if path not in now or self.hashes.get(path) != sha1(now[path]))

    def dump(self, name):
        path = self.folder / name
        return path if path.exists() else None


class Commit:
    """A commit as the state to compare with: what it holds of the files git follows"""

    def __init__(self, revision, saved):
        self.revision = git("rev-parse", "--short", revision).strip()
        self.name = f"commit {self.revision}"
        # (the loader's dumps are not in a commit: the saved state's, when it was saved at this one)
        self.saved = saved if saved is not None and self.revision in read_text(saved.folder / "state.txt") else None

    def sources(self):
        paths = git("ls-tree", "-r", "--name-only", self.revision, "--", SOURCES).splitlines()
        return {path: self.text(path) for path in paths}

    def text(self, path):
        result = subprocess.run(["git", "-C", str(ROOT), "show", f"{self.revision}:{path}"], capture_output=True)
        return result.stdout.decode("utf-8").replace("\r\n", "\n") if result.returncode == 0 else None

    def changed(self):
        folders = [folder for folder in EVERY_SCENARIO + ("tests/controls", ASSETS)]
        found = set(git("diff", "--name-only", self.revision, "--", *folders).splitlines())
        found |= set(git("ls-files", "--others", "--exclude-standard", "--", *folders).splitlines())
        if self.saved is not None:
            # (what git does not follow, the assets and the build's files, by the saved state's hashes)
            followed = set(git("ls-files", "--", *folders).splitlines())
            found |= {path for path in self.saved.changed() if path not in followed}
        return sorted(found)

    def dump(self, name):
        return self.saved.dump(name) if self.saved is not None else None


def game_executable(bin_folder):
    for path in (bin_folder / f"{game_profile.BINARY}_game_headless.exe", bin_folder / f"{game_profile.BINARY}_game_headless"):
        if path.exists() and (path.suffix == ".exe") == (os.name == "nt"):
            return path
    return None


def write_dumps(bin_folder, folder):
    """The loader's dumps of the game as it is built: units.txt (where the values wider than a byte lie) and
    data.bin (every piece's bytes) in folder; what stopped them, or None"""
    game = game_executable(bin_folder)
    scripts = sorted(CONTROLS.glob("*.txt"))
    if game is None or not scripts:
        return f"{bin_folder / (game_profile.BINARY + '_game_headless')}: no such executable"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("units.txt", "data.bin"):
        (folder / name).unlink(missing_ok=True)
    environment = dict(os.environ)
    environment[f"{game_profile.GUARD}_DUMP_UNITS"] = str(folder / "units.txt")
    environment[f"{game_profile.GUARD}_DUMP_ASSETS"] = str(folder / "data.bin")
    result = subprocess.run([str(game), "--script", str(scripts[0]), "--frames", "1", "--hashes", os.devnull],
                            capture_output=True, text=True, env=environment, cwd=ROOT)
    if not (folder / "units.txt").exists() or not (folder / "data.bin").exists():
        return (result.stderr.strip().splitlines() or ["the game wrote no dumps"])[0]
    return None


def stale_build(bin_folder, game=None):
    """The newest file the game is built from that is newer than the game, or None"""
    game = game or game_executable(bin_folder)
    if game is None:
        return None
    built = game.stat().st_mtime
    newest = None
    for folder in (SOURCES,) + tuple(each for each in EVERY_SCENARIO if not each.startswith("build/")):
        if (ROOT / folder).exists():
            for name, path in tree_files(folder).items():
                if path.suffix in (".c", ".h") and path.stat().st_mtime > built and (newest is None or path.stat().st_mtime > newest[1]):
                    newest = (name, path.stat().st_mtime)
    return newest[0] if newest else None


def save(folder, bin_folder):
    """The working tree as the verified state"""
    fresh = folder.with_name(folder.name + ".new")
    shutil.rmtree(fresh, ignore_errors=True)
    problem = write_dumps(bin_folder, fresh)
    if problem:
        shutil.rmtree(fresh, ignore_errors=True)
        raise SystemExit(f"not saved: {problem}")
    for name, path in tree_files(SOURCES).items():
        (fresh / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, fresh / name)
    for name in ("symbols.txt", "sound_symbols.txt"):
        if (ROOT / name).exists():
            shutil.copyfile(ROOT / name, fresh / name)
    (fresh / "files.txt").write_text("".join(f"{sha1(path)} {name}\n" for name, path in sorted(watched_files().items())),
                                     encoding="utf-8")
    commit = git("rev-parse", "--short", "HEAD").strip()
    dirty = " with changes" if git("status", "--porcelain", "--untracked-files=no").strip() else ""
    (fresh / "state.txt").write_text(f"{datetime.datetime.now():%d-%m-%Y %H:%M}, at commit {commit}{dirty}\n", encoding="utf-8")
    shutil.rmtree(folder, ignore_errors=True)
    fresh.rename(folder)
    print(f"saved as verified: {folder.relative_to(ROOT).as_posix()} ({read_text(folder / 'state.txt').strip()})")


# ---------------------------------------------------------------------------------------------------------------
# what each scenario executes and reads
# ---------------------------------------------------------------------------------------------------------------

def function_ranges(symbols_text, width):
    """{name: (first address, past its last)} of a symbol file's functions: each up to the next one"""
    found = sorted((int(address, 16), name) for address, name in
                   re.findall(rf"^([0-9A-Fa-f]{{{width}}})\s+func\s+(\w+)", symbols_text or "", flags=re.M))
    return {name: (address, found[index + 1][0] if index + 1 < len(found) else address + 0x10000)
            for index, (address, name) in enumerate(found)}


def executed(path, fold):
    """The sorted addresses of a reference run's trace, or None"""
    if not path.exists():
        return None
    found = set()
    for line in path.read_text(encoding="ascii").splitlines():
        if line and not line.startswith("#"):
            address = fold(int(line, 16))
            if address is not None:
                found.add(address)
    return sorted(found)


def runs(addresses, first, end):
    at = bisect.bisect_left(addresses, first)
    return at < len(addresses) and addresses[at] < end


def data_changes(old_folder_dump, new_folder):
    """What changed in the loader's dumps: (ranges of program addresses whose bytes changed, ranges whose values
    lie otherwise, whether the sound program's data changed)"""
    def pieces(path):
        data = path.read_bytes()
        found, at = {}, 0
        while at + 8 <= len(data):
            address, size = int.from_bytes(data[at:at + 4], "little"), int.from_bytes(data[at + 4:at + 8], "little")
            found[address] = data[at + 8:at + 8 + size]
            at += 8 + size
        return found

    def units(path):
        found = {}
        for line in path.read_text(encoding="ascii").splitlines():
            if line and not line.startswith("#"):
                address, width = line.split()
                found[int(address, 16)] = int(width)
        return found

    old_units, new_units = units(old_folder_dump("units.txt")), units(new_folder / "units.txt")
    laid = [(address, address + max(old_units.get(address, 1), new_units.get(address, 1)))
            for address in set(old_units) | set(new_units) if old_units.get(address) != new_units.get(address)]
    old, new = pieces(old_folder_dump("data.bin")), pieces(new_folder / "data.bin")
    bytes_, sound = [], False
    for address in set(old) | set(new):
        before, after = old.get(address), new.get(address)
        if before == after:
            continue
        if address & 0x80000000:
            sound = True
        elif before is None or after is None or len(before) != len(after):
            bytes_.append((address, address + max(len(before or b""), len(after or b""))))
        else:
            start = None
            for index in range(len(before)):
                if before[index] != after[index]:
                    if start is None:
                        start = index
                elif start is not None:
                    bytes_.append((address + start, address + index))
                    start = None
            if start is not None:
                bytes_.append((address + start, address + len(before)))
    return merged(bytes_), merged(laid), sound


def merged(ranges):
    found = []
    for first, end in sorted(ranges):
        if found and first <= found[-1][1]:
            found[-1][1] = max(found[-1][1], end)
        else:
            found.append([first, end])
    return found


def first_read(census, ranges):
    """The first address of the ranges a scenario's census reads, or None"""
    starts = [first for first, _ in ranges]
    with census.open(encoding="ascii") as lines:
        for line in lines:
            if line[0] in "#!":
                continue
            fields = line.split()
            if fields[2] != "R":
                continue
            address = rom.canonical_address(int(fields[0], 16))
            if address is None:
                continue
            at = bisect.bisect_right(starts, address + int(fields[1]) - 1) - 1
            if at >= 0 and ranges[at][1] > address:
                return max(address, ranges[at][0])
    return None


# ---------------------------------------------------------------------------------------------------------------

def select(base, big_endian, bin_folder):
    """({scenario: [why]}, {scenario: {changed function it runs}}, notes, the changed functions) of the working
    tree against a state"""
    names = sorted(path.stem for path in CONTROLS.glob("*.txt"))
    needed, covers, notes = defaultdict(list), defaultdict(set), []

    def everyone(why):
        for name in names:
            needed[name].append(why)

    # the other files
    changed = base.changed()
    dumped = [path for path in changed if path.startswith(tuple(folder + "/" for folder in DUMPED_ASSETS))]
    for path in changed:
        if path.startswith("tests/controls/"):
            if Path(path).stem in names:
                needed[Path(path).stem].append(f"{path} changed: its reference has to run again")
        elif path not in dumped:
            everyone(f"{path} changed")
    # the C: the game's program and the sound program
    old_sources = base.sources()
    new_sources = {name: path.read_text(encoding="utf-8") for name, path in tree_files(SOURCES).items()}
    all_functions = {}
    for sound in (False, True):
        symbols = "sound_symbols.txt" if sound else "symbols.txt"
        mine = lambda path: path.startswith(SOUND_SOURCES + "/") == sound    # noqa: E731
        renames = Renames()
        renames.add_files(base.text(symbols), read_text(ROOT / symbols))
        comparison = Comparison({path: text for path, text in old_sources.items() if mine(path)},
                                {path: text for path, text in new_sources.items() if mine(path)}, renames)
        notes += comparison.notes
        for why in comparison.every:
            everyone(why)
        if not comparison.functions:
            continue
        ranges = function_ranges(read_text(ROOT / symbols), 4 if sound else 6)
        fold = (lambda address: address) if sound else rom.canonical_address
        traces = {name: executed(RESULTS / name / ("sound_executed.txt" if sound else "executed.txt"), fold) for name in names}
        for function, why in sorted(comparison.functions.items()):
            label = f"{function} ({why})"
            slot = re.match(r"call_([0-9A-F]+)\Z", function)
            if function in ranges:
                first, end = ranges[function]
            elif slot:
                # how call_code() runs the function at an address: with that function
                first = int(slot.group(1), 16)
                end = first + 1
            else:
                everyone(f"{label}: symbols.txt has no func line of that name")
                continue
            all_functions[function] = why
            for name in names:
                if traces[name] is None:
                    needed[name].append(f"no {'sound_' if sound else ''}executed.txt: the reference has not run it")
                elif runs(traces[name], first, end):
                    needed[name].append(label)
                    covers[name].add(function)
    # the loaded data
    if base.dump("units.txt") is None or base.dump("data.bin") is None:
        notes.append(f"the loader's data is not compared: {base.name} has no dumps of it")
        if dumped:
            everyone(f"{dumped[0]} changed")
    else:
        current = VERIFIED / "now"
        problem = write_dumps(bin_folder, current)
        stale = stale_build(bin_folder)
        if problem:
            notes.append(f"the loader's data is not compared: {problem}")
            if dumped:
                everyone(f"{dumped[0]} changed")
        else:
            if stale:
                notes.append(f"the loader's data is the one of a game older than {stale}")
            bytes_, laid, sound = data_changes(base.dump, current)
            if sound:
                everyone("the sound program's data changed")
            kinds = [("bytes that changed", bytes_)]
            if big_endian:
                kinds.append(("values that lie otherwise", laid))
            elif laid:
                notes.append(f"the loader lays out {len(laid)} ranges of the program's data otherwise "
                             f"(first {laid[0][0]:06X}): a big-endian processor's only")
            for title, ranges in kinds:
                if not ranges:
                    continue
                for name in names:
                    census = CENSUS / f"{name}.txt"
                    if not census.exists():
                        needed[name].append(f"the program's data changed and {census.relative_to(ROOT).as_posix()} is not there")
                        continue
                    address = first_read(census, ranges)
                    if address is not None:
                        needed[name].append(f"reads the program's data at {address:06X}, one of {len(ranges)} ranges of {title}")
                if not any(title in why for reasons in needed.values() for why in reasons):
                    notes.append(f"{len(ranges)} ranges of the program's data with {title} (first "
                                 f"{ranges[0][0]:06X}), which no scenario reads")
    return needed, covers, notes, all_functions


def frames(name):
    path = RESULTS / name / "hashes.txt"
    return sum(1 for _ in path.open(encoding="ascii")) if path.exists() else 0


def cover(needed, covers):
    """The shortest scenarios that between them run every changed function, and those needed for anything else"""
    chosen = {name for name, reasons in needed.items() if len(reasons) > len(covers.get(name, ()))}
    left = set().union(*covers.values()) if covers else set()
    for name in chosen:
        left -= covers.get(name, set())
    while left:
        best = min((name for name in covers if covers[name] & left),
                   key=lambda name: frames(name) / len(covers[name] & left))
        chosen.add(best)
        left -= covers[best]
    return chosen


def main():
    arguments = sys.argv[1:]
    known = {"--big-endian", "--cover", "--run", "--save", "--base", "--jobs", "--bin"}
    values = {}
    for option in ("--base", "--jobs", "--bin"):
        if option in arguments:
            position = arguments.index(option)
            if position + 1 >= len(arguments):
                raise SystemExit(f"{option}: no value")
            values[option] = arguments[position + 1]
            del arguments[position:position + 2]
    unknown = [argument for argument in arguments if argument not in known]
    if unknown:
        print(__doc__.split("\n\n")[1])
        raise SystemExit(f"{unknown[0]}: no such option")
    big_endian = "--big-endian" in arguments
    bin_folder = Path(values.get("--bin", ROOT / "build" / "bin")).resolve()
    jobs = max(1, int(values.get("--jobs", (os.cpu_count() or 2) // 2)))
    folder = VERIFIED / ("big-endian" if big_endian else "pc")
    if "--save" in arguments:
        save(folder, bin_folder)
        return 0
    saved = Saved(folder) if (folder / "state.txt").exists() else None
    base = Commit(values.get("--base", "HEAD"), saved) if "--base" in values or saved is None else saved
    target = "a big-endian processor" if big_endian else "the PC"
    print(f"the working tree against {base.name}, for {target}")
    needed, covers, notes, functions = select(base, big_endian, bin_folder)
    names = sorted(path.stem for path in CONTROLS.glob("*.txt"))
    if functions:
        print(f"\n{len(functions)} functions to check again:")
        for function, why in sorted(functions.items())[:40]:
            ran = [name for name in names if function in covers.get(name, ())]
            print(f"  {function} ({why}): " + (f"{len(ran)} scenarios" if ran else "no scenario runs it"))
        if len(functions) > 40:
            print(f"  ... and {len(functions) - 40} more")
    for note in notes:
        print(f"note: {note}")
    chosen = sorted(needed)
    if "--cover" in arguments and chosen:
        chosen = sorted(cover(needed, covers))
        print(f"\nthe shortest {len(chosen)} of the {len(needed)} scenarios needed that run every changed function "
              f"(a first check):")
    else:
        print(f"\n{len(chosen)} of {len(names)} scenarios to run" + (":" if chosen else ": nothing that changed reaches a scenario"))
    total = 0
    for name in chosen:
        reasons = needed[name]
        total += frames(name)
        print(f"  {name:<16} {frames(name):6} frames  {reasons[0]}" + (f" (+{len(reasons) - 1})" if len(reasons) > 1 else ""))
    if chosen:
        everything = sum(frames(name) for name in names)
        print(f"  {total} of {everything} frames ({100 * total // max(everything, 1)}%)")
    if "--run" not in arguments:
        if chosen:
            print("\n" + " ".join(chosen))
        return 1 if chosen else 0
    if not chosen:
        return 0
    game = None if big_endian else game_executable(bin_folder)
    stale = None if big_endian else stale_build(bin_folder, game)
    if stale:
        raise SystemExit(f"{game.relative_to(ROOT).as_posix()} is older than {stale}: the game has to be built first")
    tools = Path(game_profile.__file__).resolve().parent    # the game's scripts, which run the core's with its profile
    again = [name for name in chosen if any("its reference has to run again" in why for why in needed[name])]
    sys.stdout.flush()
    failed = False
    if big_endian:
        failed = subprocess.run([sys.executable, str(tools / "big_endian_scenarios.py")] + chosen + ["--jobs", str(jobs)]).returncode != 0
    else:
        options = ["--video", "--sound", "--jobs", str(jobs), "--bin", str(bin_folder)]
        rest = [name for name in chosen if name not in again]
        if again:
            print(f"\non the reference again: {' '.join(again)}", flush=True)
            failed = subprocess.run([sys.executable, str(tools / "check_scenarios.py")] + again + options).returncode != 0
        if rest:
            print(flush=True)
            failed = subprocess.run([sys.executable, str(tools / "check_scenarios.py")] + rest + ["--native-only"] + options).returncode != 0 or failed
    if failed:
        return 1
    if "--cover" in arguments and set(chosen) != set(needed):
        print(f"\nnot saved as verified: {len(needed) - len(chosen)} of the scenarios needed did not run")
        return 0
    save(folder, bin_folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
