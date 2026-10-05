"""Conservative scalar GP grammar. Catalog arity is necessary, not a type system."""
from copy import deepcopy
from functools import lru_cache
import math

from alpha_platform.config.operators import load_catalog, validate_catalog
from alpha_platform.structure.parser import (
    BinaryOp, FunctionCall, Identifier, KeywordArg, NumberLiteral, UnaryOp,
)

# Only operators with reviewed scalar output and simple argument roles enter GP.
# Vector fields, group producers, and ambiguous signatures need richer typing.
SCALAR_OPERATORS = frozenset("""
abs add divide inverse log max min multiply power reverse sign signed_power sqrt subtract to_nan
and if_else is_nan not or days_from_last_change hump jump_decay kth_element last_diff_value
ts_arg_max ts_arg_min ts_av_diff ts_corr ts_count_nans ts_covariance ts_decay_linear ts_delay
ts_delta ts_max ts_mean ts_min ts_product ts_quantile ts_rank ts_regression ts_scale ts_std_dev
ts_sum ts_zscore normalize quantile rank scale scale_down vector_neut winsorize zscore trade_when
""".split())
WINDOWS = (5, 10, 21, 63, 121, 252)
DEFAULT_FIELDS = ("close", "open", "high", "low", "vwap", "volume", "returns")


@lru_cache(maxsize=1)
def catalog():
    result = load_catalog()
    report = validate_catalog(catalog=result)
    if not report.ok:
        raise ValueError("Operator catalog failed validation: " + "; ".join(report.errors))
    return result


def children(node):
    if isinstance(node, FunctionCall):
        return [(('args', i), a) for i, a in enumerate(node.args)]
    if isinstance(node, BinaryOp):
        return [(('left',), node.left), (('right',), node.right)]
    if isinstance(node, UnaryOp):
        return [(('operand',), node.operand)]
    if isinstance(node, KeywordArg):
        return [(('value',), node.value)]
    return []


def walk(node, path=()):
    yield path, node
    for step, child in children(node):
        yield from walk(child, path + step)


def replace_at(node, path, replacement):
    result = deepcopy(node)
    if not path:
        return deepcopy(replacement)
    parent = result
    for key in path[:-1]:
        parent = parent[key] if isinstance(key, int) else getattr(parent, key)
    if isinstance(path[-1], int):
        parent[path[-1]] = deepcopy(replacement)
    else:
        setattr(parent, path[-1], deepcopy(replacement))
    return result


def signal_slots(node, path=()):
    """Yield only scalar expression locations, never windows/options/groups."""
    yield path, node
    if isinstance(node, FunctionCall):
        spec = catalog()[node.operator]
        positional = [(i, a) for i, a in enumerate(node.args) if not isinstance(a, KeywordArg)]
        for j, (i, arg) in enumerate(positional):
            role = spec.positional[j].role if j < len(spec.positional) else "signal"
            if role == "signal":
                yield from signal_slots(arg, path + ('args', i))
    elif isinstance(node, (BinaryOp, UnaryOp)):
        for step, child in children(node):
            yield from signal_slots(child, path + step)


def validate_tree(node, fields=DEFAULT_FIELDS, max_depth=8, max_nodes=100,
                  operators=SCALAR_OPERATORS, groups=()):
    allowed = set(fields)
    allowed_groups = set(groups)
    count = 0

    def check(n, depth=0):
        nonlocal count
        count += 1
        if count > max_nodes or depth > max_depth:
            raise ValueError("Expression exceeds the generation size limit")
        if isinstance(n, Identifier):
            if n.name not in allowed:
                raise ValueError(f"Unknown scalar field: {n.name}")
        elif isinstance(n, NumberLiteral):
            if not math.isfinite(n.value):
                raise ValueError("Non-finite number")
        elif isinstance(n, BinaryOp):
            if n.op not in {"+", "-", "*", "/", "<", "<=", ">", ">=", "==", "!=", "&&", "||"}:
                raise ValueError("Unsupported binary operator")
            check(n.left, depth + 1)
            check(n.right, depth + 1)
        elif isinstance(n, UnaryOp):
            if n.op != "-":
                raise ValueError("Unsupported unary operator")
            check(n.operand, depth + 1)
        elif isinstance(n, FunctionCall):
            if n.operator not in operators:
                raise ValueError(f"Operator is not in the reviewed scalar grammar: {n.operator}")
            if n.operator == "ts_step":
                if len(n.args) != 1 or not isinstance(n.args[0], NumberLiteral) or n.args[0].value != 1:
                    raise ValueError("Reviewed ts_step syntax is ts_step(1)")
                return
            if n.operator == "ts_regression":
                for option in n.args:
                    if isinstance(option, KeywordArg) and option.name in {"rettype", "lag"}:
                        v = option.value
                        if not isinstance(v, NumberLiteral) or not math.isfinite(v.value) or v.value != int(v.value) or v.value < 0 or (option.name == "rettype" and v.value > 9):
                            raise ValueError("Regression rettype must be 0..9 and lag a nonnegative integer")
            spec = catalog()[n.operator]
            pos = [a for a in n.args if not isinstance(a, KeywordArg)]
            kw = [a for a in n.args if isinstance(a, KeywordArg)]
            if n.args != pos + kw or len({a.name for a in kw}) != len(kw):
                raise ValueError("Invalid keyword ordering or duplicate keyword")
            if len(pos) < spec.min_arity or (spec.max_arity is not None and len(pos) > spec.max_arity):
                raise ValueError(f"Wrong positional arity for {n.operator}")
            options = {a.name: a for a in spec.keywords}
            if any(a.name not in options for a in kw):
                raise ValueError(f"Unknown keyword for {n.operator}")
            if any(a.required and a.name not in {k.name for k in kw} for a in spec.keywords):
                raise ValueError(f"Missing required keyword for {n.operator}")
            for i, arg in enumerate(pos):
                role = spec.positional[i].role if i < len(spec.positional) else "signal"
                if role == "signal":
                    check(arg, depth + 1)
                elif role in {"window", "parameter"}:
                    if not isinstance(arg, NumberLiteral) or not math.isfinite(arg.value):
                        raise ValueError(f"{n.operator} requires a numeric {role}")
                    if (role == "window" or spec.positional[i].name == "k") and (
                        arg.value < 1 or arg.value != int(arg.value)
                    ):
                        raise ValueError("Window/k must be a positive integer")
                elif role == "group":
                    if not isinstance(arg, Identifier) or arg.name not in allowed_groups:
                        raise ValueError(f"{n.operator} requires an available group identifier")
                else:
                    raise ValueError("Unsupported argument role")
            # Options are literals only; do not breed or traverse them as signals.
            from alpha_platform.structure.parser import StringLiteral
            for arg in kw:
                if not isinstance(arg.value, (NumberLiteral, StringLiteral)) and not (
                    isinstance(arg.value, Identifier) and arg.value.name in {"true", "false", "gaussian", "uniform", "cauchy"}
                ):
                    raise ValueError(f"Option {arg.name} must be a literal")
                if isinstance(arg.value, NumberLiteral) and not math.isfinite(arg.value.value):
                    raise ValueError("Non-finite option")
        else:
            raise ValueError("Unsupported scalar expression")

    check(node)
    if sum(1 for _ in walk(node)) > max_nodes:
        raise ValueError("Expression exceeds the generation size limit")
    return node
