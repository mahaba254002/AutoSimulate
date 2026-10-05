"""Exchange scalar subtrees while protecting windows and keyword options."""
import random
from copy import deepcopy

from alpha_platform.generation.gp.trees import DEFAULT_FIELDS, replace_at, signal_slots, validate_tree


def crossover(left, right, *, fields=DEFAULT_FIELDS, rng=None):
    rng = rng or random.Random()
    validate_tree(left, fields)
    validate_tree(right, fields)
    targets, donors = list(signal_slots(left)), list(signal_slots(right))
    for _ in range(40):
        path, _ = rng.choice(targets)
        _, donor = rng.choice(donors)
        child = replace_at(left, path, donor)
        try:
            validate_tree(child, fields)
        except ValueError:
            continue
        if child != left:
            return child
    return deepcopy(left)
