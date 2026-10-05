"""Offline operator metadata from the checked-in BRAIN catalog.

This catalog describes signatures, not a complete BRAIN type system. Keyword
syntax is preserved independently of whether a value is required. Placeholder
defaults are never treated as usable values. No network requests are made.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CATALOG_PATH = Path(__file__).with_name("operators_catalog.json")
Role = Literal["signal", "window", "group", "parameter", "unknown"]


@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    role: Role
    keyword_only: bool = False
    required: bool = True
    default: str | None = None
    source: str = ""


@dataclass(frozen=True)
class OperatorSpec:
    name: str
    category: str
    signature: str
    arguments: tuple[ArgumentSpec, ...]
    variadic: bool = False
    infix: str | None = None
    description: str = ""
    issues: tuple[str, ...] = ()

    @property
    def positional(self) -> tuple[ArgumentSpec, ...]:
        return tuple(a for a in self.arguments if not a.keyword_only)

    @property
    def keywords(self) -> tuple[ArgumentSpec, ...]:
        return tuple(a for a in self.arguments if a.keyword_only)

    @property
    def min_arity(self) -> int:
        """Minimum positional arguments; required keywords are separate."""
        return sum(a.required for a in self.positional)

    @property
    def max_arity(self) -> int | None:
        return None if self.variadic else len(self.positional)

    @property
    def signal_arity(self) -> int:
        return sum(a.role == "signal" for a in self.positional)


@dataclass
class ValidationReport:
    parsed: int
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def _split_top_level(text: str) -> list[str]:
    """Split commas outside nested expressions and quoted strings."""
    parts, start, depth = [], 0, 0
    quote = None
    escaped = False
    for i, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
        elif char == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    if quote or depth:
        raise ValueError("unclosed quote or parentheses")
    parts.append(text[start:].strip())
    return parts


def _role(name: str, keyword: bool) -> Role:
    if name in {"d", "lookback"}:
        return "window"
    if name in {"group", "g1", "g2"}:
        return "group"
    if keyword or name in {"k", "weight"}:
        return "parameter"
    if name in {"x", "y", "z", "input", "input1", "input2", "input3"}:
        return "signal"
    return "unknown"


_BINARY = re.compile(r"\w+\s*(<=|>=|==|!=|<|>|\+|\*|/|-)\s*\w+")


def parse_signature(signature: str, category: str, description: str = "") -> OperatorSpec:
    # Normalize typographic quotes before tokenizing, without altering the source.
    normalized = signature.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    pieces = _split_top_level(normalized)
    call = re.fullmatch(r"(\w+)\((.*)\)", pieces[0], re.DOTALL)
    if not call:
        binary = _BINARY.fullmatch(normalized.strip())
        if not binary:
            raise ValueError(f"unrecognized signature: {signature}")
        symbol = binary.group(1)
        return OperatorSpec(symbol, category, signature,
                            (ArgumentSpec("x", "signal"), ArgumentSpec("y", "signal")), infix=symbol)
    name, body = call.groups()
    infix = None
    if len(pieces) > 1:
        alias = _BINARY.fullmatch(pieces[1]) if len(pieces) == 2 else None
        if not alias:
            raise ValueError(f"unrecognized alias: {pieces[1:]}")
        infix = alias.group(1)
    args, issues = [], []
    variadic = False
    keyword_seen = False
    for source in _split_top_level(body) if body.strip() else []:
        if not source:
            raise ValueError("empty argument")
        # Assignment must precede ellipsis handling: range=... is an option.
        if "=" in source:
            arg_name, default = (v.strip() for v in source.split("=", 1))
            if not re.fullmatch(r"[A-Za-z_]\w*", arg_name) or not default:
                raise ValueError(f"invalid keyword: {source}")
            placeholder = default in {"..", "...", "d"}
            args.append(ArgumentSpec(arg_name, _role(arg_name, True), True,
                                     placeholder, None if placeholder else default, source))
            keyword_seen = True
            continue
        if re.search(r"\.{2,3}$", source):
            variadic = True
            source = re.sub(r"\s*\.{2,3}$", "", source).strip()
            if not source:
                continue
        if keyword_seen:
            raise ValueError("positional argument follows keyword option")
        arg_name = re.sub(r"\s+", "", source)
        if re.fullmatch(r"\w+\(.*\)", source):
            # bucket(rank(x), ...) documents an expression in the signal slot.
            arg_name = "x"
            issues.append(f"{source} is an example expression, treated as one signal slot")
        role = _role(arg_name, False)
        if role == "unknown":
            issues.append(f"unclassified positional argument {source!r}")
        args.append(ArgumentSpec(arg_name, role, source=source))
    if len({a.name for a in args}) != len(args):
        raise ValueError("duplicate argument names")
    # These descriptions explicitly permit two or more inputs even without dots.
    if name in {"add", "subtract"}:
        variadic = True
    if variadic and (not args or any(a.role != "signal" for a in args if not a.keyword_only)):
        raise ValueError("unsupported non-signal variadic signature")
    return OperatorSpec(name, category, signature, tuple(args), variadic, infix, description, tuple(issues))


def _read_catalog(path: str | Path) -> tuple[dict[str, OperatorSpec], list[str]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    catalog, errors = {}, []
    for category, entries in raw["operators"].items():
        for signature, metadata in entries.items():
            try:
                spec = parse_signature(signature, category, metadata.get("description", ""))
                if spec.name in catalog:
                    raise ValueError(f"duplicate operator {spec.name}")
                catalog[spec.name] = spec
            except ValueError as exc:
                errors.append(f"{signature}: {exc}")
    return catalog, errors


def load_catalog(path: str | Path = CATALOG_PATH) -> dict[str, OperatorSpec]:
    catalog, errors = _read_catalog(path)
    if errors:
        raise ValueError("\n".join(errors))
    return catalog


# Hand-reviewed expectations independent of parsing logic. Tuple contents are
# positional roles, keyword (name, required, default), variadic, and infix.
_EXPECTED = {
    "add": (("signal", "signal"), (("filter", False, "false"),), True, "+"),
    "subtract": (("signal", "signal"), (("filter", False, "false"),), True, "-"),
    "multiply": (("signal", "signal"), (("filter", False, "false"),), True, "*"),
    "divide": (("signal", "signal"), (), False, "/"),
    "min": (("signal", "signal"), (), True, None),
    "max": (("signal", "signal"), (), True, None),
    "bucket": (("signal",), (("range", True, None),), False, None),
    "kth_element": (("signal", "window", "parameter"), (("ignore", False, '"NaN"'),), False, None),
    "to_nan": (("signal",), (("value", False, "0"), ("reverse", False, "false")), False, None),
    "hump": (("signal",), (("hump", False, "0.01"),), False, None),
    "ts_backfill": (("signal",), (("lookback", True, None), ("k", False, "1")), False, None),
    "ts_corr": (("signal", "signal", "window"), (), False, None),
    "group_backfill": (("signal", "group", "window"), (("std", False, "4.0"),), False, None),
    "if_else": (("signal", "signal", "signal"), (), False, None),
}


def validate_catalog(path: str | Path = CATALOG_PATH, *,
                     catalog: dict[str, OperatorSpec] | None = None) -> ValidationReport:
    """Check parsing AND hand-reviewed classifications; retain ambiguity warnings."""
    if catalog is None:
        catalog, errors = _read_catalog(path)
    else:
        errors = []
    warnings = [f"{name}: {issue}" for name, op in catalog.items() for issue in op.issues]
    for name, expected in _EXPECTED.items():
        op = catalog.get(name)
        if op is None:
            errors.append(f"missing expected operator {name}")
            continue
        actual = (tuple(a.role for a in op.positional),
                  tuple((a.name, a.required, a.default) for a in op.keywords), op.variadic, op.infix)
        if actual != expected or op.min_arity != len(expected[0]):
            errors.append(f"{name}: classification mismatch: expected {expected!r}, got {actual!r}")
    return ValidationReport(len(catalog), errors, warnings)


if __name__ == "__main__":
    report = validate_catalog()
    print(f"{report.parsed} parsed; {len(report.errors)} errors; {len(report.warnings)} warnings")
    for message in report.errors + report.warnings:
        print(message)
    raise SystemExit(0 if report.ok else 1)
