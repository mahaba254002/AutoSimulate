"""AST mutation with catalog arities and role-preserving replacements."""
import random
from copy import deepcopy

from alpha_platform.generation.gp.trees import (
    DEFAULT_FIELDS, SCALAR_OPERATORS, WINDOWS, catalog, replace_at, signal_slots, validate_tree,
)
from alpha_platform.structure.parser import FunctionCall, Identifier, KeywordArg, NumberLiteral

MUTATIONS = ("operator", "field", "window", "wrap", "unwrap")


def mutate(tree, *, fields=DEFAULT_FIELDS, rng=None, kind=None):
    rng = rng or random.Random()
    validate_tree(tree, fields)
    kind = kind or rng.choice(MUTATIONS)
    if kind not in MUTATIONS:
        raise ValueError(f"Unknown mutation: {kind}")
    choices = []
    specs = catalog()
    for path, node in signal_slots(tree):
        if kind == "field" and isinstance(node, Identifier):
            choices.extend((path, Identifier(f)) for f in fields if f != node.name)
        if kind == "wrap":
            choices.extend((path, FunctionCall(op, [deepcopy(node)])) for op in ("rank", "zscore", "reverse", "abs"))
        if not isinstance(node, FunctionCall):
            continue
        spec = specs[node.operator]
        if kind == "operator":
            for name in sorted(SCALAR_OPERATORS):
                other = specs[name]
                # Equal roles/defaults/option names avoids moving k, d or flags into signal slots.
                shape = lambda s: (tuple((a.name if a.role != "signal" else "signal", a.role,
                                         a.keyword_only, a.required, a.default) for a in s.arguments), s.variadic)
                if name != node.operator and other.category == spec.category and shape(other) == shape(spec):
                    choices.append((path, FunctionCall(name, deepcopy(node.args))))
        if kind == "unwrap":
            if spec.signal_arity == 1:
                choices.append((path, node.args[0]))
        if kind == "window":
            for i, arg in enumerate(node.args):
                if isinstance(arg, KeywordArg):
                    continue
                if i < len(spec.positional) and spec.positional[i].role == "window":
                    choices.extend((path + ('args', i), NumberLiteral(float(w)))
                                   for w in WINDOWS if NumberLiteral(float(w)) != arg)
    rng.shuffle(choices)
    for path, replacement in choices:
        candidate = replace_at(tree, path, replacement)
        try:
            validate_tree(candidate, fields)
        except ValueError:
            continue
        if candidate != tree:
            return candidate, kind
    return deepcopy(tree), "unchanged"
