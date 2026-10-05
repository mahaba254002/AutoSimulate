"""
Recursive-descent parser for FASTEXPR (WorldQuant BRAIN's alpha
expression language). Produces a real AST -- not a regex/token
heuristic -- because expression_depth, ast_hash, and operator/field
extraction all depend on the tree actually being correct, especially
once GP starts breeding and mutating trees based on this structure.

Grammar (informal, precedence low-to-high):
    expr        := or_expr
    or_expr     := and_expr ('||' and_expr)*
    and_expr    := cmp_expr ('&&' cmp_expr)*
    cmp_expr    := add_expr (('>' | '<' | '>=' | '<=' | '==' | '!=') add_expr)?
    add_expr    := term (('+' | '-') term)*
    term        := unary (('*' | '/') unary)*
    unary       := '-' unary | atom
    atom        := NUMBER
                 | STRING
                 | IDENTIFIER '(' arglist ')'      # function call
                 | IDENTIFIER                       # field/group reference
                 | '(' expr ')'
    arglist     := arg (',' arg)*  | (empty)
    arg         := IDENTIFIER '=' expr             # keyword argument
                 | expr                            # positional argument

Precedence, loosest to tightest: || , && , comparisons, + -, * /, unary -.
Comparisons are non-associative.

Keyword arguments (std=4.0, rettype=2, filter=true, driver="gaussian")
appear throughout the real BRAIN operator catalog. A lone '=' is only
ever valid immediately after an IDENTIFIER inside an argument list;
'==' is tokenized separately as a comparison operator, so the two
can't be confused.

Numeric literals accept an optional exponent (1e-05, 2.5E3).

Notes on FASTEXPR specifics handled here:
  - Function calls are the "operators" (ts_mean, rank, group_neutralize, ...)
  - Bare identifiers not followed by '(' are data fields, group names,
    or boolean words like true/false; features.py classifies them.
  - String literals are a literal type distinct from identifiers.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Union


class FastExprSyntaxError(Exception):
    """Raised on malformed FASTEXPR input, with position info when available."""


# ---------------------------------------------------------------------
# AST node types
# ---------------------------------------------------------------------

@dataclass
class NumberLiteral:
    value: float


@dataclass
class StringLiteral:
    value: str


@dataclass
class Identifier:
    name: str


@dataclass
class KeywordArg:
    """A named argument inside a call, e.g. std=4.0 in winsorize(x, std=4.0)."""
    name: str
    value: object = None


@dataclass
class FunctionCall:
    operator: str
    args: list = dc_field(default_factory=list)  # positional ASTNodes and KeywordArg nodes


@dataclass
class BinaryOp:
    op: str  # '+', '-', '*', '/', '>', '<', '>=', '<=', '==', '!=', '&&', '||'
    left: object = None
    right: object = None


@dataclass
class UnaryOp:
    op: str  # '-'
    operand: object = None


ASTNode = Union[NumberLiteral, StringLiteral, Identifier, KeywordArg, FunctionCall, BinaryOp, UnaryOp]


# ---------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------

@dataclass
class Token:
    kind: str   # 'NUMBER', 'STRING', 'IDENT', 'OP', 'EQUALS', 'LPAREN', 'RPAREN', 'COMMA', 'EOF'
    value: str
    pos: int


# Multi-character operators are checked before single-character ones so
# '>=' isn't tokenized as '>' then '=', and '==' isn't two EQUALS tokens.
_MULTI_CHAR_OPS = ["&&", "||", ">=", "<=", "==", "!="]
_SINGLE_CHAR_OPS = "+-*/><"


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(source)

    while i < n:
        c = source[i]

        if c.isspace():
            i += 1
            continue

        if c == "(":
            tokens.append(Token("LPAREN", c, i))
            i += 1
            continue

        if c == ")":
            tokens.append(Token("RPAREN", c, i))
            i += 1
            continue

        if c == ",":
            tokens.append(Token("COMMA", c, i))
            i += 1
            continue

        matched_multi = False
        for op in _MULTI_CHAR_OPS:
            if source[i:i + len(op)] == op:
                tokens.append(Token("OP", op, i))
                i += len(op)
                matched_multi = True
                break
        if matched_multi:
            continue

        if c == "=":
            # A lone '=' (the multi-char check above already consumed '==').
            tokens.append(Token("EQUALS", c, i))
            i += 1
            continue

        if c in _SINGLE_CHAR_OPS:
            tokens.append(Token("OP", c, i))
            i += 1
            continue

        if c == '"' or c == "'":
            quote = c
            start = i
            i += 1
            buf = []
            while i < n and source[i] != quote:
                buf.append(source[i])
                i += 1
            if i >= n:
                raise FastExprSyntaxError(f"Unterminated string literal starting at position {start}")
            i += 1  # consume closing quote
            tokens.append(Token("STRING", "".join(buf), start))
            continue

        if c.isdigit() or (c == "." and i + 1 < n and source[i + 1].isdigit()):
            start = i
            has_dot = False
            while i < n and (source[i].isdigit() or (source[i] == "." and not has_dot)):
                if source[i] == ".":
                    has_dot = True
                i += 1
            # Optional exponent: e/E, optional sign, at least one digit.
            # Only consumed if a digit really follows, so an identifier
            # that merely starts with 'e' after a number isn't swallowed.
            if i < n and source[i] in "eE":
                j = i + 1
                if j < n and source[j] in "+-":
                    j += 1
                if j < n and source[j].isdigit():
                    while j < n and source[j].isdigit():
                        j += 1
                    i = j
            tokens.append(Token("NUMBER", source[start:i], start))
            continue

        if c.isalpha() or c == "_":
            start = i
            while i < n and (source[i].isalnum() or source[i] == "_"):
                i += 1
            tokens.append(Token("IDENT", source[start:i], start))
            continue

        raise FastExprSyntaxError(f"Unexpected character {c!r} at position {i}")

    tokens.append(Token("EOF", "", n))
    return tokens


# ---------------------------------------------------------------------
# Recursive-descent parser
# ---------------------------------------------------------------------

_COMPARISON_OPS = (">", "<", ">=", "<=", "==", "!=")


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect(self, kind: str) -> Token:
        tok = self._peek()
        if tok.kind != kind:
            raise FastExprSyntaxError(
                f"Expected {kind} but got {tok.kind} ({tok.value!r}) at position {tok.pos}"
            )
        return self._advance()

    def parse_expression_full(self) -> ASTNode:
        node = self._parse_or()
        if self._peek().kind != "EOF":
            tok = self._peek()
            raise FastExprSyntaxError(f"Unexpected trailing input at position {tok.pos}: {tok.value!r}")
        return node

    def _parse_or(self) -> ASTNode:
        node = self._parse_and()
        while self._peek().kind == "OP" and self._peek().value == "||":
            self._advance()
            right = self._parse_and()
            node = BinaryOp(op="||", left=node, right=right)
        return node

    def _parse_and(self) -> ASTNode:
        node = self._parse_cmp()
        while self._peek().kind == "OP" and self._peek().value == "&&":
            self._advance()
            right = self._parse_cmp()
            node = BinaryOp(op="&&", left=node, right=right)
        return node

    def _parse_cmp(self) -> ASTNode:
        node = self._parse_add()
        if self._peek().kind == "OP" and self._peek().value in _COMPARISON_OPS:
            op = self._advance().value
            right = self._parse_add()
            node = BinaryOp(op=op, left=node, right=right)
        return node

    def _parse_add(self) -> ASTNode:
        node = self._parse_term()
        while self._peek().kind == "OP" and self._peek().value in ("+", "-"):
            op = self._advance().value
            right = self._parse_term()
            node = BinaryOp(op=op, left=node, right=right)
        return node

    def _parse_term(self) -> ASTNode:
        node = self._parse_unary()
        while self._peek().kind == "OP" and self._peek().value in ("*", "/"):
            op = self._advance().value
            right = self._parse_unary()
            node = BinaryOp(op=op, left=node, right=right)
        return node

    def _parse_unary(self) -> ASTNode:
        if self._peek().kind == "OP" and self._peek().value == "-":
            self._advance()
            operand = self._parse_unary()
            return UnaryOp(op="-", operand=operand)
        return self._parse_atom()

    def _parse_atom(self) -> ASTNode:
        tok = self._peek()

        if tok.kind == "NUMBER":
            self._advance()
            return NumberLiteral(value=float(tok.value))

        if tok.kind == "STRING":
            self._advance()
            return StringLiteral(value=tok.value)

        if tok.kind == "LPAREN":
            self._advance()
            node = self._parse_or()
            self._expect("RPAREN")
            return node

        if tok.kind == "IDENT":
            name = self._advance().value
            if self._peek().kind == "LPAREN":
                self._advance()
                args = self._parse_arglist()
                self._expect("RPAREN")
                return FunctionCall(operator=name, args=args)
            return Identifier(name=name)

        raise FastExprSyntaxError(f"Unexpected token {tok.kind} ({tok.value!r}) at position {tok.pos}")

    def _parse_arg(self) -> ASTNode:
        # Keyword argument: IDENT '=' expr. Two-token lookahead so a plain
        # identifier argument (e.g. close) isn't mistaken for a keyword.
        if self._peek().kind == "IDENT" and self._peek(1).kind == "EQUALS":
            name = self._advance().value
            self._advance()  # consume '='
            value = self._parse_or()
            return KeywordArg(name=name, value=value)
        return self._parse_or()

    def _parse_arglist(self) -> list:
        args: list = []
        if self._peek().kind == "RPAREN":
            return args  # zero-arg call
        args.append(self._parse_arg())
        while self._peek().kind == "COMMA":
            self._advance()
            args.append(self._parse_arg())
        return args


def parse(source: str) -> ASTNode:
    """
    Parse a single FASTEXPR expression string into an AST. Raises
    FastExprSyntaxError on malformed input. Pure parsing: no evaluation,
    no network or DB access.
    """
    tokens = tokenize(source)
    parser = _Parser(tokens)
    return parser.parse_expression_full()