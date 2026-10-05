"""
Walks a parsed FASTEXPR AST (from structure/parser.py) to produce the
fields needed for an alpha_structure row: operators, data_fields,
counts, expression_depth, complexity_score, ast_json, and ast_hash.

Operator vs. field disambiguation: every FunctionCall.operator is
unambiguously an operator. Every bare Identifier is a data field
(close, volume), a group/category name (industry, sector), or a
boolean word (true/false) -- the AST alone can't tell these apart.
If a set of known field names is provided (typically sourced from
field_catalog), identifiers are classified against it; otherwise
identifiers default to being treated as fields, minus a small built-in
list of group names and boolean words.

Keyword argument NAMES (std, rettype, filter, ...) are parameter names,
not data fields, and are never collected as identifiers. The keyword's
VALUE is still walked, so a real field inside it still counts.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from alpha_platform.structure.parser import (
    ASTNode,
    BinaryOp,
    FunctionCall,
    Identifier,
    KeywordArg,
    NumberLiteral,
    StringLiteral,
    UnaryOp,
    parse,
)

# Fallback heuristic for when no field_catalog is supplied; not a
# substitute for real catalog lookups.
_COMMON_GROUP_NAMES = {
    "industry", "sector", "subindustry", "market", "country",
    "exchange", "sic", "gics",
}
_BOOLEAN_WORDS = {"true", "false"}


@dataclass
class ExpressionFeatures:
    operators: list[str]
    operator_count: int
    unique_operator_count: int

    data_fields: list[str]
    field_count: int

    expression_depth: int
    expression_node_count: int
    complexity_score: float

    ast_json: dict
    ast_hash: str


def _node_to_dict(node: ASTNode) -> dict:
    """Canonical JSON-serializable dict form of a node, for ast_json storage."""
    if isinstance(node, NumberLiteral):
        return {"type": "number", "value": node.value}
    if isinstance(node, StringLiteral):
        return {"type": "string", "value": node.value}
    if isinstance(node, Identifier):
        return {"type": "identifier", "name": node.name}
    if isinstance(node, KeywordArg):
        return {"type": "kwarg", "name": node.name, "value": _node_to_dict(node.value)}
    if isinstance(node, FunctionCall):
        return {"type": "call", "operator": node.operator, "args": [_node_to_dict(a) for a in node.args]}
    if isinstance(node, BinaryOp):
        return {"type": "binary", "op": node.op, "left": _node_to_dict(node.left), "right": _node_to_dict(node.right)}
    if isinstance(node, UnaryOp):
        return {"type": "unary", "op": node.op, "operand": _node_to_dict(node.operand)}
    raise TypeError(f"Unknown AST node type: {type(node)}")


def _depth(node: ASTNode) -> int:
    """Leaf nodes (literals, identifiers) are depth 0. KeywordArg adds no depth of its own."""
    if isinstance(node, (NumberLiteral, StringLiteral, Identifier)):
        return 0
    if isinstance(node, KeywordArg):
        return _depth(node.value)
    if isinstance(node, FunctionCall):
        if not node.args:
            return 1
        return 1 + max(_depth(a) for a in node.args)
    if isinstance(node, BinaryOp):
        return 1 + max(_depth(node.left), _depth(node.right))
    if isinstance(node, UnaryOp):
        return 1 + _depth(node.operand)
    raise TypeError(f"Unknown AST node type: {type(node)}")


def _node_count(node: ASTNode) -> int:
    if isinstance(node, (NumberLiteral, StringLiteral, Identifier)):
        return 1
    if isinstance(node, KeywordArg):
        return 1 + _node_count(node.value)
    if isinstance(node, FunctionCall):
        return 1 + sum(_node_count(a) for a in node.args)
    if isinstance(node, BinaryOp):
        return 1 + _node_count(node.left) + _node_count(node.right)
    if isinstance(node, UnaryOp):
        return 1 + _node_count(node.operand)
    raise TypeError(f"Unknown AST node type: {type(node)}")


def _collect_operators(node: ASTNode, out: list[str]) -> None:
    """
    Pre-order operator collection. FunctionCall names are operators;
    BinaryOp/UnaryOp symbols also count (arithmetic/comparison/boolean
    are still structural operations).
    """
    if isinstance(node, (NumberLiteral, StringLiteral, Identifier)):
        return
    if isinstance(node, KeywordArg):
        _collect_operators(node.value, out)
        return
    if isinstance(node, FunctionCall):
        out.append(node.operator)
        for a in node.args:
            _collect_operators(a, out)
        return
    if isinstance(node, BinaryOp):
        out.append(node.op)
        _collect_operators(node.left, out)
        _collect_operators(node.right, out)
        return
    if isinstance(node, UnaryOp):
        out.append(f"unary{node.op}")
        _collect_operators(node.operand, out)
        return
    raise TypeError(f"Unknown AST node type: {type(node)}")


def _collect_identifiers(node: ASTNode, out: list[str]) -> None:
    if isinstance(node, Identifier):
        out.append(node.name)
        return
    if isinstance(node, (NumberLiteral, StringLiteral)):
        return
    if isinstance(node, KeywordArg):
        # The keyword NAME is a parameter name, not a data field; only
        # walk the value.
        _collect_identifiers(node.value, out)
        return
    if isinstance(node, FunctionCall):
        for a in node.args:
            _collect_identifiers(a, out)
        return
    if isinstance(node, BinaryOp):
        _collect_identifiers(node.left, out)
        _collect_identifiers(node.right, out)
        return
    if isinstance(node, UnaryOp):
        _collect_identifiers(node.operand, out)
        return
    raise TypeError(f"Unknown AST node type: {type(node)}")


def _canonical_hash_string(node: ASTNode) -> str:
    """
    Canonical string used for ast_hash: tree shape, operators and
    identifiers are kept, but every literal constant (number or string)
    becomes a placeholder, so ts_mean(close,20) and ts_mean(close,60)
    hash identically while ts_mean vs ts_median, or close vs open, do
    not. Keyword argument NAMES are kept (std= vs rettype= is a real
    structural difference); their literal values are placeholdered.
    """
    if isinstance(node, NumberLiteral):
        return "#"
    if isinstance(node, StringLiteral):
        return "$"
    if isinstance(node, Identifier):
        return f"id:{node.name}"
    if isinstance(node, KeywordArg):
        return f"kw:{node.name}={_canonical_hash_string(node.value)}"
    if isinstance(node, FunctionCall):
        args = ",".join(_canonical_hash_string(a) for a in node.args)
        return f"{node.operator}({args})"
    if isinstance(node, BinaryOp):
        return f"({_canonical_hash_string(node.left)}{node.op}{_canonical_hash_string(node.right)})"
    if isinstance(node, UnaryOp):
        return f"({node.op}{_canonical_hash_string(node.operand)})"
    raise TypeError(f"Unknown AST node type: {type(node)}")


def compute_complexity_score(depth: int, operator_count: int, field_count: int) -> float:
    """
    Default complexity formula per the schema's own comment:
    depth * operator_count * field_count. Kept as its own function so
    it's trivially swappable later.
    """
    return float(depth * operator_count * field_count)


def extract_features(
    expression: str,
    *,
    known_fields: set[str] | None = None,
) -> ExpressionFeatures:
    """
    Parse `expression` and extract everything needed for an
    alpha_structure row. `known_fields`, if given (typically the
    field_name values from field_catalog), classifies identifiers as
    data_fields vs. group names; without it, all identifiers count as
    data_fields except common group names and boolean words.

    Pure function: does not touch the database.
    """
    ast = parse(expression)

    operators: list[str] = []
    _collect_operators(ast, operators)

    identifiers: list[str] = []
    _collect_identifiers(ast, identifiers)

    if known_fields is not None:
        data_fields = [name for name in identifiers if name in known_fields]
    else:
        data_fields = [
            name for name in identifiers
            if name.lower() not in _COMMON_GROUP_NAMES and name.lower() not in _BOOLEAN_WORDS
        ]

    depth = _depth(ast)
    node_count = _node_count(ast)
    complexity = compute_complexity_score(depth, len(operators), len(data_fields))

    ast_json = _node_to_dict(ast)
    ast_hash = hashlib.sha256(_canonical_hash_string(ast).encode("utf-8")).hexdigest()

    return ExpressionFeatures(
        operators=operators,
        operator_count=len(operators),
        unique_operator_count=len(set(operators)),
        data_fields=data_fields,
        field_count=len(data_fields),
        expression_depth=depth,
        expression_node_count=node_count,
        complexity_score=complexity,
        ast_json=ast_json,
        ast_hash=ast_hash,
    )