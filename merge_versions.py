# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        merge_versions.py
Description: merges the decompiled C of two versions of a game's program into one tree that runs either, picked
             when the game runs
Version:     0.1.0
Created:     27-11-2026
Modified:    27-11-2026
Author:      Sebastian Toyos

Usage: python tools/merge_versions.py [--out DIR]   (the game's shim: the versions and their trees from its profile)

Each version is decompiled on its own (tools/decompile.py --set SET, and decompile_sound.py), the C of each checked
against its own scenarios; this writes the C of all of them in one:
  - a function both versions have the same is written once; one whose code differs, or that uses a name whose value
    or type is each version's own, is a variant, written twice as NAME_BASETAG and NAME_VERSIONTAG, next to each other;
    a function one version has alone keeps its name;
  - code of one version (a variant's copy, a function of its own) calls its own version's variants and uses its own
    version's names (NAME_TAG); code both share calls NAME, a dispatcher (versions.c) that runs the one of the
    version the game runs (the profile's test), and reads an address of each version through NAME, which picks it
    (VERSIONED in the runtime's support headers);
  - the headers: a macro both have the same once; an address of each version (a #define NAME_ADDRESS whose value
    differs) as NAME_ADDRESS_BASETAG, NAME_ADDRESS_VERSIONTAG and NAME_ADDRESS = VERSIONED(...); a structure whose
    shape differs as struct NAME_BASETAG and NAME_VERSIONTAG (and the macros over it: functions using them are
    variants), or, named in the profile's unions, one with the views of both in its union (the same bytes); the
    prototypes of the merged functions; the registry of the tables each version loads, picked when the game runs.
The lists come from the two trees: the bound names (addresses whose value differs), the structures whose shape
differs, and the forced names (macros whose text differs and are no address, and the macros over those structures).
The game's program is the tree's top (game.h, ram.h, table.c) and the sound program its sound/ (sound.h, tables.c).
"""
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

SIGNATURE = re.compile(r"^[A-Za-z_][\w \*]*\b(\w+)\s*\((.*)\)\s*$")
DEFINE = re.compile(r"^#define (\w+)")
STRUCT = re.compile(r"^(struct|union) (\w+)$")
ASSERT = re.compile(r"^_Static_assert\(sizeof\(struct (\w+)\)")


def parse_file(path):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    n = len(lines)
    func_entries = []  # (comment_start, sig_idx, end_idx)
    i = 0
    while i < n - 1:
        line = lines[i]
        m = SIGNATURE.match(line)
        if m and not line.startswith(("#", "/*", " ", "\t")) and lines[i + 1] == "{":
            j = i + 2
            while j < n and lines[j] != "}":
                j += 1
            # Find comment block above
            # (the comment right above it: from the line its "/*" is on to the "*/" just above the signature)
            comment_start = i
            if i > 0 and lines[i - 1].rstrip().endswith("*/"):
                k = i - 1
                while k > 0 and "/*" not in lines[k]:
                    k -= 1
                comment_start = k
            func_entries.append((comment_start, i, j))
            i = j + 1
        else:
            i += 1

    if func_entries:
        header_end = func_entries[0][0]
    else:
        header_end = n

    header = lines[:header_end]
    while header and header[-1] == "":
        header.pop()

    funcs = OrderedDict()
    for comment_start, sig_idx, end_idx in func_entries:
        m = SIGNATURE.match(lines[sig_idx])
        name = m.group(1)
        board = re.search(r"BOARD_ORIGINAL\((\w+)\)", lines[sig_idx])
        if board:
            name = board.group(0)    # (the board's own runs in its place: nothing calls the original)
        comment = lines[comment_start:sig_idx]
        body = lines[sig_idx:end_idx + 1]
        funcs[name] = {
            "comment": comment,
            "body": body,
            "sig": lines[sig_idx],
            "params": m.group(2),
        }

    return header, funcs


def get_return_type(sig_line):
    m = SIGNATURE.match(sig_line)
    if not m:
        return "void"
    name = m.group(1)
    line_without_params = sig_line[:sig_line.rfind("(")]
    parts = line_without_params.split()
    if len(parts) < 2:
        return "void"
    return " ".join(parts[:-1])


def get_param_names(params_str):
    if not params_str or params_str.strip() == "void" or params_str.strip() == "":
        return []
    params = [p.strip() for p in params_str.split(",")]
    names = []
    for p in params:
        if not p:
            continue
        identifiers = re.findall(r'\b[A-Za-z_]\w*\b', p)
        if identifiers:
            names.append(identifiers[-1])
    return names


def get_param_types(params_str):
    if not params_str or params_str.strip() == "void" or params_str.strip() == "":
        return []
    params = [p.strip() for p in params_str.split(",")]
    types = []
    for p in params:
        if not p:
            continue
        match = re.search(r'\b[A-Za-z_]\w*\s*$', p)
        if match:
            type_part = p[:match.start()].strip()
            types.append(type_part)
        else:
            types.append(p)
    return types


def renamer(names, tag, calls_only):
    """A function renaming each of 'names' to NAME_TAG in a line: its calls (NAME followed by '('), or every use"""
    if not names:
        return lambda line: line
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    if calls_only:
        pattern = re.compile(r"\b(" + alternatives + r")(\s*\()")
        return lambda line: pattern.sub(lambda m: m.group(1) + "_" + tag + m.group(2), line)
    pattern = re.compile(r"\b(" + alternatives + r")\b")
    return lambda line: pattern.sub(lambda m: m.group(1) + "_" + tag, line)


def struct_renamer(names, tag):
    if not names:
        return lambda line: line
    pattern = re.compile(r"\bstruct (" + "|".join(re.escape(name) for name in names) + r")\b")
    return lambda line: pattern.sub(lambda m: "struct " + m.group(1) + "_" + tag, line)




def merge_functions(base_dir, version_dir, out_dir, base_tag, version_tag, test_expr, bound, forced, structs,
                    skips=(), include="game.h"):
    """The functions of two versions' trees merged into out_dir (see the module's notes): their counts"""
    bound = set(bound) | set(forced)
    skips = tuple(skip.rstrip("/") + "/" for skip in skips)

    def c_files(root):
        found = set()
        for folder, _, files in os.walk(root):
            for f in files:
                rel = os.path.relpath(os.path.join(folder, f), root).replace("\\", "/")
                if f.endswith(".c") and f not in ("table.c", "tables.c", "versions.c") and not rel.startswith(tuple(skips)):
                    found.add(rel)
        return found

    base_files, version_files = c_files(base_dir), c_files(version_dir)
    base_data = {rel: parse_file(os.path.join(base_dir, rel)) for rel in base_files}
    version_data = {rel: parse_file(os.path.join(version_dir, rel)) for rel in version_files}

    def all_functions(data):
        found = {}
        for rel in sorted(data):
            for name, f in data[rel][1].items():
                if name in found:
                    raise SystemExit(f"{name} twice ({found[name]['file']} and {rel})")
                found[name] = dict(f, file=rel)
        return found

    all_base, all_version = all_functions(base_data), all_functions(version_data)
    forced_use = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(forced)) + r")\b") if forced else None
    struct_use = re.compile(r"\bstruct (" + "|".join(re.escape(n) for n in sorted(structs)) + r")\b") if structs else None

    def bound_to_version(body):
        text = "\n".join(body[1:])
        return bool(forced_use and forced_use.search(text)) or bool(struct_use and struct_use.search(text))

    shared, variants, base_only, version_only = set(), set(), set(), set()
    for name in all_base.keys() | all_version.keys():
        if name in all_base and name in all_version:
            same = all_base[name]["body"] == all_version[name]["body"]
            (shared if same and not bound_to_version(all_base[name]["body"]) else variants).add(name)
        elif name in all_base:
            base_only.add(name)
        else:
            version_only.add(name)

    # the variants code both versions share calls (table.c's entries too: the reset's and the interrupts' code)
    callable_variants = [n for n in variants if "(" not in n]
    calls = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(callable_variants, key=len, reverse=True))
                       + r")\s*\(") if callable_variants else None
    called = set()
    shared_bodies = [all_base[name]["body"][1:] for name in shared]
    table = os.path.join(base_dir, "table.c")
    if os.path.exists(table):
        with open(table, encoding="utf-8") as f:
            shared_bodies.append(f.read().splitlines())
    for body in shared_bodies:
        for line in body:
            if calls:
                called.update(m.group(1) for m in calls.finditer(line))

    # (a variant's name with the tag must be no function's name already: test_coin_setting_page_2p)
    taken = all_base.keys() | all_version.keys()
    clashes = sorted(f"{name}_{tag}" for name in variants for tag in (base_tag, version_tag) if f"{name}_{tag}" in taken)
    if clashes:
        raise SystemExit("names a variant's would take: " + ", ".join(clashes))

    def own(tag):
        rename_calls = renamer({name for name in variants if "(" not in name}, tag, calls_only=True)
        rename_bound = renamer(bound, tag, calls_only=False)
        rename_structs = struct_renamer(structs, tag)
        return lambda line: rename_structs(rename_bound(rename_calls(line)))

    own_base, own_version = own(base_tag), own(version_tag)

    def copy_of(name, f, tag, own_line):
        """A function of one version: its calls of variants, and its version's names, to that version's; a variant's
        own name too"""
        # (a variant's own name in its signature is followed by "(": the rename of calls takes it too; a board
        # original's goes in its BOARD_ORIGINAL)
        body = [own_line(line) for line in f["body"]]
        if name in variants and name.startswith("BOARD_ORIGINAL("):
            body[0] = body[0].replace(name, name[:-1] + "_" + tag + ")", 1)
        return f["comment"] + body

    def emit(out, lines):
        while out and out[-1] == "":
            out.pop()
        if out:
            out.append("")
        out.extend(lines)

    output = {}
    for rel in sorted(base_files | version_files):
        if rel in base_data:
            out = list(base_data[rel][0])
            for name, f in base_data[rel][1].items():
                if name in shared:
                    emit(out, f["comment"] + f["body"])
                elif name in base_only:
                    emit(out, copy_of(name, f, base_tag, own_base))
                else:
                    emit(out, copy_of(name, f, base_tag, own_base))
                    emit(out, copy_of(name, all_version[name], version_tag, own_version))
            if rel in version_data:
                for name, f in version_data[rel][1].items():
                    if name in version_only:
                        emit(out, copy_of(name, f, version_tag, own_version))
        else:
            out = list(version_data[rel][0])
            for name, f in version_data[rel][1].items():
                if name in version_only:
                    emit(out, copy_of(name, f, version_tag, own_version))
            # (a variant or a shared function of a file only the version has is written with the base's copy)
        while out and out[-1] == "":
            out.pop()
        output[rel] = out

    incompatible = []
    for name in sorted(called):
        base_f, version_f = all_base[name], all_version[name]
        base_ret, version_ret = get_return_type(base_f["sig"]), get_return_type(version_f["sig"])
        if get_param_types(base_f["params"]) != get_param_types(version_f["params"]) \
                or (base_ret != version_ret and "void" not in (base_ret, version_ret)):
            incompatible.append((name, base_f["sig"], version_f["sig"]))
    if incompatible:
        raise SystemExit("\n".join(f"incompatible: {name} (base: {b_sig}; version: {v_sig})"
                                    for name, b_sig, v_sig in incompatible))

    for rel, lines in output.items():
        out_path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    versions_lines = [
        "/* The functions the versions have with code of their own that code they share calls: each runs the version's",
        "   own (generated by merge_c.py) */",
        f'#include "{include}"',
    ]
    for name in sorted(called):
        base_f, version_f = all_base[name], all_version[name]
        base_ret, version_ret = get_return_type(base_f["sig"]), get_return_type(version_f["sig"])
        ret = version_ret if base_ret == "void" else base_ret
        args_str = ", ".join(get_param_names(base_f["params"]))
        name_b, name_v = name + "_" + base_tag, name + "_" + version_tag
        if base_ret == "void" and version_ret == "void":
            body = [f"\tif ({test_expr})", "\t{", f"\t\t{name_v}({args_str});", "\t\treturn;", "\t}",
                    f"\t{name_b}({args_str});"]
        elif base_ret == version_ret:
            body = [f"\tif ({test_expr})", f"\t\treturn {name_v}({args_str});", f"\treturn {name_b}({args_str});"]
        elif base_ret == "void":
            body = [f"\tif ({test_expr})", f"\t\treturn {name_v}({args_str});", f"\t{name_b}({args_str});",
                    "\treturn 0u;"]
        else:
            body = [f"\tif ({test_expr})", "\t{", f"\t\t{name_v}({args_str});", "\t\treturn 0u;", "\t}",
                    f"\treturn {name_b}({args_str});"]
        versions_lines += ["", f"{ret} {name}({base_f['params']})", "{"] + body + ["}"]
    with open(os.path.join(out_dir, "versions.c"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(versions_lines) + "\n")
    return {"shared": len(shared), "variants": len(variants), "base only": len(base_only),
            "version only": len(version_only), "dispatchers": len(called)}


def items(lines):
    """[(key, [lines])]: a define, a structure's block, a static assert or any other line, keyed for the other
    version's"""
    found = []
    index = 0
    while index < len(lines):
        line = lines[index]
        define, struct, check = DEFINE.match(line), STRUCT.match(line), ASSERT.match(line)
        if struct and index + 1 < len(lines) and lines[index + 1] == "{":
            end = index + 2
            while lines[end] != "};":
                end += 1
            found.append((("struct", struct.group(2)), lines[index:end + 1]))
            index = end + 1
            continue
        if define:
            found.append((("define", define.group(1)), [line]))
        elif check:
            found.append((("assert", check.group(1)), [line]))
        elif line.startswith("extern "):
            found.append((("extern", line), [line]))
        else:
            found.append((("line", line), [line]))
        index += 1
    return found


def union_members(lines):
    """The members of a structure's union, by name: a nested structure ("\t\tstruct" to "\t\t} NAME;") or a line"""
    members, index = [], 0
    while index < len(lines):
        line = lines[index]
        if line == "\t\tstruct":
            end = index
            while not re.match(r"^\t\t\} (\w+);$", lines[end]):
                end += 1
            members.append((re.match(r"^\t\t\} (\w+);$", lines[end]).group(1), lines[index:end + 1]))
            index = end + 1
        else:
            members.append((re.search(r"(\w+)(\[[^]]*\])?;$", line).group(1), [line]))
            index += 1
    return members


def merge_union(name, base, version):
    """A structure whose versions differ only in the members of its union (views of the same bytes): the base's, with
    the views only the version has added (the sound program's channel: each effect's)"""
    def parts(lines):
        start = lines.index("\tunion")
        end = lines.index("\t};", start)
        return lines[:start + 2], lines[start + 2:end], lines[end:]
    base_head, base_members, base_tail = parts(base)
    version_head, version_members, version_tail = parts(version)
    if base_head != version_head or base_tail != version_tail:
        raise SystemExit(f"ram.h: struct {name} differs outside its union")
    have = dict(union_members(base_members))
    out = list(base_members)
    for member, lines in union_members(version_members):
        if member in have and have[member] != lines:
            raise SystemExit(f"ram.h: struct {name}'s {member} differs")
        if member not in have:
            out += lines
    return base_head + out + base_tail


def tagged_structs(text, structs, tag):
    return re.sub(r"\bstruct (" + "|".join(map(re.escape, sorted(structs))) + r")\b",
                  lambda m: "struct " + m.group(1) + "_" + tag, text) if structs else text


def rename_define(line, tag):
    return re.sub(r"^#define (\w+)", lambda m: "#define " + m.group(1) + "_" + tag, line, count=1)


def merge_ram(base, version, bound, forced, structs, tags, unions=()):
    base_tag, version_tag = tags
    base_items, version_items = items(base), items(version)
    version_by_key = {key: lines for key, lines in version_items}
    base_keys = {key for key, _ in base_items}
    out = []
    end = max(i for i, line in enumerate(base) if line.startswith("#endif"))
    for key, lines in base_items:
        kind, name = key
        if lines[0].startswith("#endif") and lines is base_items[-1][1]:
            break
        other = version_by_key.get(key)
        if kind == "define":
            uses_struct = structs and re.search(r"\bstruct (" + "|".join(map(re.escape, structs)) + r")\b", lines[0])
            if name in forced or uses_struct:
                out.append(tagged_structs(rename_define(lines[0], base_tag), structs, base_tag))
                if other is not None:
                    out.append(tagged_structs(rename_define(other[0], version_tag), structs, version_tag))
            elif other is None or other == lines:
                out += lines
            elif name in bound:
                out.append(rename_define(lines[0], base_tag))
                out.append(rename_define(other[0], version_tag))
                out.append(f"#define {name} VERSIONED({name}_{base_tag}, {name}_{version_tag})")
            else:
                raise SystemExit(f"ram.h: {name} differs and is neither bound nor forced:\n  {lines[0]}\n  {other[0]}")
        elif kind == "struct" and name in structs:
            out += [tagged_structs(line, {name}, base_tag) for line in lines]
            if other is not None:
                out.append("")
                out += [tagged_structs(line, {name}, version_tag) for line in other]
        elif kind == "struct" and name in unions and other is not None and other != lines:
            out += merge_union(name, lines, other)
        elif kind == "struct" and other is not None and other != lines:
            raise SystemExit(f"ram.h: struct {name} differs and is not in --structs")
        elif kind == "assert" and name in structs:
            out.append(tagged_structs(lines[0], structs, base_tag))
            if other is not None:
                out.append(tagged_structs(other[0], structs, version_tag))
        else:
            out += lines
    own = [(key, lines) for key, lines in version_items if key not in base_keys and key[0] != "line"]
    if own:
        out += ["", f"/* what the {version_tag} version has alone */"]
        for key, lines in own:
            if key[0] == "struct":
                out.append("")
            out += [tagged_structs(line, structs, version_tag) for line in lines]
            if key[0] == "struct":
                out.append("")
    out += base[end:]
    return out


def signatures(out_dir):
    """{name: its signature line} of every function of the merged tree"""
    found = {}
    for path in sorted(Path(out_dir).rglob("*.c")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines[:-1]):
            m = SIGNATURE.match(line)
            if m and lines[i + 1] == "{" and not line.startswith(("#", "/*", " ", "\t")):
                board = re.search(r"BOARD_ORIGINAL\(\w+\)", line)
                found[board.group(0) if board else m.group(1)] = line
    return found


def merge_game(base, version, out_dir, tags):
    base_tag, version_tag = tags
    merged = signatures(out_dir)
    prototype = re.compile(r"^[A-Za-z_][\w \*]*\b(\w+)\s*\((.*)\);$")
    out, written = [], set()
    end = max(i for i, line in enumerate(base) if line.startswith("#endif"))
    for line in base[:end]:
        m = prototype.match(line)
        if not m:
            out.append(line)
            continue
        board = re.search(r"BOARD_ORIGINAL\((\w+)\)", line)
        if board:
            for key in (board.group(0), f"BOARD_ORIGINAL({board.group(1)}_{base_tag})",
                        f"BOARD_ORIGINAL({board.group(1)}_{version_tag})"):
                if key in merged:
                    out.append(merged[key] + ";")
                    written.add(key)
            continue
        name = m.group(1)
        found = [key for key in (name, f"{name}_{base_tag}", f"{name}_{version_tag}") if key in merged]
        if not found:
            out.append(line)    # (a function of the runtime's: the board's in place of the original)
        for key in found:
            out.append(merged[key] + ";")
            written.add(key)
    own = [line for line in version if line.startswith("extern ") and line not in base]
    rest = [name for name in merged if name not in written]
    if own or rest:
        out += ["", f"/* what the {version_tag} version has alone */"] + own
        # (in the version's game.h order where it has them)
        order = {}
        for i, line in enumerate(version):
            m = prototype.match(line)
            if m:
                order.setdefault(m.group(1), i)
        out += [merged[name] + ";" for name in sorted(rest, key=lambda n: order.get(n.rsplit("_", 1)[0], order.get(n, 10 ** 9)))]
        out.append("")
    out += base[end:]
    return out


def merge_table(base, version, tags, kind="game"):
    base_tag, version_tag = tags
    array = f"const {kind}_table {kind}_tables[]"
    start = next(i for i, line in enumerate(base) if line.startswith(array))
    version_start = next(i for i, line in enumerate(version) if line.startswith(array))
    pointers = [line for line in base[:start] if line.startswith("const void *")]
    out = [line for line in base[:start] if not line.startswith("const void *")]
    while out and out[-1] == "":
        out.pop()
    out += [""] + pointers + [line for line in version[:version_start]
                              if line.startswith("const void *") and line not in pointers] + [""]
    counts = []
    for lines, at, tag in ((base, start, base_tag), (version, version_start, version_tag)):
        table = []
        for line in lines[at:]:
            table.append(line)
            if line.startswith(f"const size_t {kind}_table_count"):
                break
        table[0] = table[0].replace(f"{kind}_tables[]", f"{kind}_tables_{tag}[]")
        counts.append(table[-1].split("=")[1].strip().rstrip(";"))
        table[-1] = table[-1].replace(f"{kind}_table_count", f"{kind}_table_count_{tag}")
        out += table + [""]
    out += [f"/* the tables each version loads: the one of the version the game runs ({kind}/support.h) */",
            f"const {kind}_table *const {kind}_versions_tables[] = {{ {kind}_tables_{base_tag}, {kind}_tables_{version_tag} }};",
            f"const size_t {kind}_versions_table_counts[] = {{ {counts[0]}, {counts[1]} }};"]
    return out


def lists(base_header, version_header, unions=()):
    """(bound, forced, structs) from the two versions' headers: the addresses whose value differs, the macros no code
    both share may use (a text that differs and is no address, or a macro over a structure whose shape differs), and
    those structures (but the profile's unions, merged as one)"""
    base_items = dict(items(base_header))
    version_items = dict(items(version_header))
    both = base_items.keys() & version_items.keys()
    structs = {name for kind, name in both if kind == "struct" and base_items[(kind, name)] != version_items[(kind, name)]
               and name not in unions}
    bound, forced = set(), set()
    for kind, name in both:
        if kind == "define" and base_items[(kind, name)] != version_items[(kind, name)]:
            (bound if name.endswith("_ADDRESS") else forced).add(name)
    over = re.compile(r"\bstruct (" + "|".join(map(re.escape, sorted(structs))) + r")\b") if structs else None
    for found in (base_items, version_items):
        for (kind, name), lines in found.items():
            if kind == "define" and over and over.search(lines[0]):
                forced.add(name)
    return bound, forced, structs


def merge_program(base_dir, version_dir, out_dir, tags, test, kind="game", unions=()):
    """One program of two versions' trees merged into out_dir: the game's (game.h, ram.h, table.c; its sound/ left out)
    or the sound's (sound.h, tables.c). Its counts"""
    base_dir, version_dir, out_dir = Path(base_dir), Path(version_dir), Path(out_dir)
    header = "ram.h" if kind == "game" else "sound.h"

    def read(path):
        return path.read_text(encoding="utf-8").splitlines()

    def write(path, lines):
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    bound, forced, structs = lists(read(base_dir / header), read(version_dir / header), unions)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = merge_functions(str(base_dir), str(version_dir), str(out_dir), tags[0], tags[1], test, bound, forced,
                             structs, skips=("sound",) if kind == "game" else (),
                             include="game.h" if kind == "game" else "sound.h")
    if kind == "game":
        write(out_dir / "ram.h", merge_ram(read(base_dir / "ram.h"), read(version_dir / "ram.h"), bound, forced, structs,
                                           tags, unions))
        write(out_dir / "game.h", merge_game(read(base_dir / "game.h"), read(version_dir / "game.h"), out_dir, tags))
        write(out_dir / "table.c", merge_table(read(base_dir / "table.c"), read(version_dir / "table.c"), tags))
    else:
        version_header = read(version_dir / "sound.h")
        merged = merge_ram(read(base_dir / "sound.h"), version_header, bound, forced, structs, tags, unions)
        write(out_dir / "sound.h", merge_game(merged, version_header, out_dir, tags))
        write(out_dir / "tables.c", merge_table(read(base_dir / "tables.c"), read(version_dir / "tables.c"), tags, "sound"))
    counts.update({"addresses": len(bound), "forced": len(forced), "structures": len(structs)})
    return counts


def merge_trees(base, version, out, tags, test, unions=None):
    """Both programs of two versions' trees (the game's at the top, the sound's in sound/) into out"""
    unions = unions or {}
    for kind, folder in (("game", ""), ("sound", "sound")):
        counts = merge_program(Path(base) / folder, Path(version) / folder, Path(out) / folder, tags, test, kind,
                               set(unions.get(kind, ())))
        print(f"{kind}: " + ", ".join(f"{key} {value}" for key, value in counts.items()))


def main(arguments):
    """BASE_SRC VERSION_SRC OUT --tags BASE_TAG VERSION_TAG --test EXPRESSION [--union KIND:STRUCT]..., or the game's
    profile's: MERGED (the out), MERGE_VERSIONS ((set, tag, its tree), the base first), MERGE_TEST, MERGE_UNIONS"""
    if arguments and not arguments[0].startswith("--"):
        base, version, out = arguments[:3]
        tags = arguments[arguments.index("--tags") + 1:arguments.index("--tags") + 3]
        test = arguments[arguments.index("--test") + 1]
        unions = {}
        for index, argument in enumerate(arguments):
            if argument == "--union":
                kind, name = arguments[index + 1].split(":")
                unions.setdefault(kind, set()).add(name)
        merge_trees(base, version, out, tags, test, unions)
        return 0
    import game_profile
    versions = game_profile.MERGE_VERSIONS
    out = Path(arguments[arguments.index("--out") + 1]) if "--out" in arguments else game_profile.MERGED
    (base_set, base_tag, base_tree), (version_set, version_tag, version_tree) = versions
    merge_trees(base_tree, version_tree, out, (base_tag, version_tag), game_profile.MERGE_TEST,
                getattr(game_profile, "MERGE_UNIONS", {}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
