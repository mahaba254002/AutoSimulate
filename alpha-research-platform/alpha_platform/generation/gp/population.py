"""Reproducible candidate populations; persistence is handled by the orchestrator."""
from dataclasses import dataclass
import random

from alpha_platform.generation.gp.crossover import crossover
from alpha_platform.generation.gp.mutation import mutate
from alpha_platform.generation.gp.selection import tournament
from alpha_platform.generation.gp.trees import DEFAULT_FIELDS, validate_tree
from alpha_platform.structure.features import extract_features
from alpha_platform.structure.parser import parse
from alpha_platform.structure.serializer import serialize

DEFAULT_SEEDS = ("rank(-returns)", "rank(ts_delta(close, 5))", "ts_rank(volume, 21)")


@dataclass(frozen=True)
class Candidate:
    expression: str
    ast_hash: str
    mutation_type: str
    parent: str
    partner: str | None = None


def generate_population(seeds=DEFAULT_SEEDS, *, count=20, fields=DEFAULT_FIELDS, seed=42, scores=None):
    if not 1 <= count <= 100 or not 1 <= len(seeds) <= 100:
        raise ValueError("Choose 1–100 candidates and 1–100 seed expressions")
    rng = random.Random(seed)
    parents = [validate_tree(parse(s), fields) for s in seeds]
    scores = scores if scores is not None else [0.0] * len(parents)
    results, seen = [], set()
    for attempt in range(count * 60):
        parent = tournament(parents, scores, rng=rng)
        partner = None
        if attempt < len(parents):
            parent = parents[attempt]
            child, kind = parent, "seed"
        elif len(parents) > 1 and rng.random() < 0.2:
            partner = tournament(parents, scores, rng=rng)
            child, kind = crossover(parent, partner, fields=fields, rng=rng), "crossover"
        else:
            child, kind = mutate(parent, fields=fields, rng=rng)
        text = serialize(child)
        if parse(text) != child:
            raise ValueError("Expression did not round-trip through the serializer")
        ast_hash = extract_features(text).ast_hash
        # Window-only sweeps share a structure; retain one representative.
        if ast_hash in seen:
            continue
        seen.add(ast_hash)
        results.append(Candidate(text, ast_hash, kind, serialize(parent), serialize(partner) if partner else None))
        if len(results) == count:
            break
    return results
