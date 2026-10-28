# license:BSD-3-Clause
# copyright-holders:Sebastian Toyos
"""
Name:        floats.py
Description: floating-point values in the IR: the operators a frontend lifts a floating-point unit's code
             to, the C type each value has, and their C
Version:     0.1.0
Created:     28-10-2026
Modified:    28-10-2026
Author:      Sebastian Toyos

The IR's values are integers; a floating-point value is one too, of 64 bits, for the passes that only
move values about (data flow, variables, structure), and every operator that makes or reads one is named
"f.something", which no integer pass knows and so leaves alone. Their C is a float or a double:

    f.from32 x / f.from64 x     the float / double whose bits are x, as the unit loads it (a load:
                                FLOAT_AT(address), MEMD(address))
    f.bits32 v / f.bits64 v     the bits of v as a float / a double (a store: MEMF(address) = v)
    f.const32 c / f.const64 c   a constant, by its bits
    f.int v, width              a signed integer of 'width' bits, as a double
    f.add f.sub f.mul f.div     a + b ..., rounded to a float's precision
    f.sqrt                      its square root, rounded to a float's precision
    f.neg f.abs                 exact, of the operand's type
    f.round32                   a value rounded to a float
    f.helper:NAME args          a double the frontend's own C helper works out (x87_sin...)
    f.toint:NAME v              an integer the frontend's helper NAME makes of v (x87_ftol...), 64 bits
    f.cmp:OUTCOMES a, b         1 when the comparison of a with b comes out as one of OUTCOMES, a word of
                                G (greater), L (less), E (equal) and U (unordered: a NaN)

A value is a double in the C, as the floating-point unit's registers are wider than a float: a float is
made one as the unit loads it (FLOAT_AT, the frontend's own: a signaling NaN made quiet, so a float loaded
and stored again is not a copy of its bits), its operations are done in double and rounded as the unit's
control word asks (x87_round), and a value written to a float's memory is rounded to a float then (MEMF).
"""
import math
import struct

from v60 import ir

FLOAT_RESULT = ("f.from32", "f.from64", "f.const32", "f.const64", "f.int", "f.add", "f.sub", "f.mul",
                "f.div", "f.sqrt", "f.neg", "f.abs", "f.round32")
ARITHMETIC = {"f.add": "+", "f.sub": "-", "f.mul": "*", "f.div": "/"}
OUTCOMES = "GLEU"


def is_float(node):
    """True for an expression whose value is a float or a double."""
    return isinstance(node, ir.Op) and (node.name in FLOAT_RESULT or node.name.startswith("f.helper:"))


def compare(outcomes, a, b):
    """The 1-bit test that 'a' compared with 'b' comes out as one of 'outcomes' ("GE": greater or equal)."""
    word = "".join(letter for letter in OUTCOMES if letter in outcomes)
    return ir.op("f.cmp:" + word, (a, b), 1)


def is_compare(node):
    return isinstance(node, ir.Op) and node.name.startswith("f.cmp:")


def complement(node):
    """The opposite test of an f.cmp."""
    word = node.name[6:]
    return ir.op("f.cmp:" + "".join(letter for letter in OUTCOMES if letter not in word), node.args, 1)


def kind(node, lookup):
    """"f32" or "f64" for a floating-point expression; 'lookup' answers for a register or a temporary
    (None when it holds no floating-point value)."""
    if isinstance(node, (ir.Reg, ir.Temp)):
        return lookup(node)
    if is_float(node):
        return "f64"
    return None


def float_literal(bits):
    """C for the float whose bits are 'bits': the shortest decimal that reads back to it."""
    value = struct.unpack("<f", struct.pack("<I", bits))[0]
    if math.isnan(value) or math.isinf(value):
        return None
    for digits in range(1, 10):
        text = f"{value:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == value:
            break
    if "e" not in text and "." not in text:
        text += ".0"
    return text + "f"


def double_literal(bits):
    value = struct.unpack("<d", struct.pack("<Q", bits))[0]
    if math.isnan(value) or math.isinf(value):
        return None
    text = repr(value)
    return text if "e" in text or "." in text else text + ".0"


def operator(printer, node):
    """The cprint.Value of an f. operator."""
    from .cprint import Value, PRIMARY, UNARY, MULTIPLY, ADD

    name, args = node.name, node.args
    if name in ("f.from32", "f.from64"):
        source = args[0]
        macro = "FLOAT_AT" if name == "f.from32" else "MEMD"
        if isinstance(source, ir.Load):
            return Value(f"{macro}({printer.any(source.address).text})", PRIMARY, True, "f64")
        helper = "x87_float" if name == "f.from32" else "double_from_bits"
        return Value(f"{helper}({printer.exact(source).text})", PRIMARY, True, "f64")
    if name in ("f.const32", "f.const64"):
        bits = args[0].value
        if name == "f.const32":
            bits = struct.unpack("<Q", struct.pack("<d", struct.unpack("<f", struct.pack("<I", bits))[0]))[0]
        literal = double_literal(bits)
        if literal is None:
            helper = "float_from_bits" if name == "f.const32" else "double_from_bits"
            return Value(f"{helper}({printer.exact(args[0]).text})", PRIMARY, True,
                         "f32" if name == "f.const32" else "f64")
        prec = UNARY if literal.startswith("-") else PRIMARY
        return Value(literal, prec, True, "f64")
    if name in ("f.bits32", "f.bits64"):
        helper = "float_to_bits" if name == "f.bits32" else "double_to_bits"
        value = printer.emit(args[0])
        if name == "f.bits32" and value.ctype == "f64":
            return Value(f"{helper}((float){printer.wrap(value, UNARY)})", PRIMARY, True, "u32")
        return Value(f"{helper}({value.text})", PRIMARY, True, "u32" if name == "f.bits32" else "u64")
    if name == "f.int":
        width = args[1].value
        signed = {16: "int16_t", 32: "int32_t", 64: "int64_t"}[width]
        return Value(f"(double)({signed}){printer.wrap(printer.any(args[0]), UNARY)}", UNARY, True, "f64")
    if name in ARITHMETIC:
        symbol = ARITHMETIC[name]
        prec = ADD if symbol in "+-" else MULTIPLY
        left, right = printer.emit(args[0]), printer.emit(args[1])
        left_text = printer.wrap(left, prec)
        if left.ctype == "f32" and right.ctype == "f32":
            left_text = f"(double){printer.wrap(left, UNARY)}"   # two floats: done in double all the same
        return Value(f"x87_round({left_text} {symbol} {printer.wrap(right, prec + 1)})", PRIMARY, True, "f64")
    if name == "f.sqrt":
        return Value(f"x87_round(sqrt({printer.emit(args[0]).text}))", PRIMARY, True, "f64")
    if name == "f.round32":
        value = printer.emit(args[0])
        if value.ctype == "f32":
            return value
        return Value(f"(float){printer.wrap(value, UNARY)}", UNARY, True, "f32")
    if name == "f.neg":
        value = printer.emit(args[0])
        return Value(f"-{printer.wrap(value, UNARY)}", UNARY, True, value.ctype)
    if name == "f.abs":
        value = printer.emit(args[0])
        return Value(f"fabs({value.text})", PRIMARY, True, "f64")
    if name.startswith("f.helper:"):
        values = ", ".join(printer.emit(arg).text for arg in args)
        return Value(f"{name[9:]}({values})", PRIMARY, True, "f64")
    if name.startswith("f.toint:"):
        values = ", ".join(printer.emit(arg).text for arg in args)
        return Value(f"{name[8:]}({values})", PRIMARY, True, "u64")
    if is_compare(node):
        return comparison(printer, node.name[6:], args)
    raise ValueError(f"unknown floating-point operator {name}")


def comparison(printer, word, args):
    """C for the test that args[0] compared with args[1] comes out as one of the outcomes in 'word'. C's
    < > <= >= == are false and != is true when an operand is a NaN, as the comparison's unordered outcome
    is for none or all of them."""
    from .cprint import Value, PRIMARY, UNARY, RELATION, EQUALITY, LOGICAL_OR

    a = printer.wrap(printer.emit(args[0]), RELATION + 1)
    b = printer.wrap(printer.emit(args[1]), RELATION + 1)
    ordered = word.replace("U", "")
    tests = {"G": (f"{a} > {b}", RELATION), "L": (f"{a} < {b}", RELATION), "E": (f"{a} == {b}", EQUALITY),
             "GE": (f"{a} >= {b}", RELATION), "LE": (f"{a} <= {b}", RELATION),
             "GL": (f"{a} < {b} || {a} > {b}", LOGICAL_OR),
             "GLE": (f"!x87_unordered({printer.emit(args[0]).text}, {printer.emit(args[1]).text})", UNARY)}
    if "U" not in word:
        if not ordered:
            return Value("0", PRIMARY, True, "int")
        return Value(tests[ordered][0], tests[ordered][1], True, "int")
    # with the unordered outcome: the opposite of the ordered test of the outcomes left out
    rest = "".join(letter for letter in "GLE" if letter not in ordered)
    if not rest:
        return Value("1", PRIMARY, True, "int")
    if rest == "E":
        return Value(f"{a} != {b}", EQUALITY, True, "int")
    if rest == "GLE":
        return Value(f"x87_unordered({printer.emit(args[0]).text}, {printer.emit(args[1]).text})", PRIMARY,
                     True, "int")
    test, prec = tests[rest]
    return Value(f"!({test})", UNARY, True, "int")
