"""
Inverse of structure/parser.py: turns an AST back into a valid FASTEXPR
string. Needed by the GP engine -- mutation and crossover operate on
ASTs (so mutations are always structurally valid), then the result
must be serialized back to a string before it can be submitted via
ace.generate_alpha(regular=...).

Parenthesization: serialize() adds parentheses around a BinaryOp/UnaryOp
child wherever needed to preserve the tree's operator precedence,
since without them re-parsing the output could group operators
differently than the tree specifies.

Keyword arguments serialize as name=value with no spaces around '='.
"""
from __future__ import annotations

from decimal import Decimal

from alpha_platform.structure.parser import (
    ASTNode,
    BinaryOp,
    FunctionCall,
    Identifier,
    KeywordArg,
    NumberLiteral,
    StringLiteral,
    UnaryOp,
)

# Precedence levels, matching parser.py's grammar (higher binds tighter).
_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    ">": 3, "<": 3, ">=": 3, "<=": 3, "==": 3, "!=": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5,
}
_UNARY_PRECEDENCE = 6
_ATOM_PRECEDENCE = 7


def _format_number(value: float) -> str:
    """
    Integers serialize without a trailing '.0' (20 not 20.0). Non-integers
    serialize as plain decimals, NEVER scientific notation: Python's repr()
    turns 0.00001 into '1e-05', and GP mutating a small numeric argument
    would otherwise emit expressions the platform may reject.
    """
    if value == int(value):
        return str(int(value))
    # repr() is the shortest string that round-trips exactly; Decimal then
    # expands any exponent form to plain digits without losing precision.
    # (format(value, "f") is NOT safe here: it defaults to 6 decimals, so
    # 1e-10 would silently become '0'.)
    text = format(Decimal(repr(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def serialize(node: ASTNode, *, _parent_precedence: int = 0) -> str:
    """
    Serializes an AST node back to a FASTEXPR string. _parent_precedence
    is used internally during recursion to decide whether a node needs
    wrapping parens; callers should not pass it.
    """
    if isinstance(node, NumberLiteral):
        return _format_number(node.value)

    if isinstance(node, StringLiteral):
        # Parser has no escape syntax; select a delimiter not in the value.
        if '"' not in node.value:
            return f'"{node.value}"'
        if "'" not in node.value:
            return f"'{node.value}'"
        raise ValueError("String contains both quote delimiters and cannot be serialized")

    if isinstance(node, Identifier):
        return node.name

    if isinstance(node, KeywordArg):
        return f"{node.name}={serialize(node.value)}"

    if isinstance(node, FunctionCall):
        args = ", ".join(serialize(a) for a in node.args)
        return f"{node.operator}({args})"

    if isinstance(node, UnaryOp):
        inner = serialize(node.operand, _parent_precedence=_UNARY_PRECEDENCE)
        text = f"{node.op}{inner}"
        if _parent_precedence > _UNARY_PRECEDENCE:
            return f"({text})"
        return text

    if isinstance(node, BinaryOp):
        my_prec = _PRECEDENCE[node.op]
        # Comparisons are non-associative in the parser, on both sides.
        left = serialize(node.left, _parent_precedence=my_prec + (my_prec == 3))
        # my_prec + 1 on the right so equal-precedence right operands
        # get parenthesized: a - (b - c) is not the same as a - b - c.
        right = serialize(node.right, _parent_precedence=my_prec + 1)
        text = f"{left} {node.op} {right}"
        if _parent_precedence > my_prec:
            return f"({text})"
        return text

    raise TypeError(f"Unknown AST node type: {type(node)}")
