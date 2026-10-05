"""Tournament selection with explicit scores; unevaluated candidates can explore."""
import random


def tournament(items, scores, *, rng=None, size=3):
    if not items or len(items) != len(scores):
        raise ValueError("Selection requires equally sized, nonempty items and scores")
    rng = rng or random.Random()
    indices = rng.sample(range(len(items)), min(size, len(items)))
    return items[max(indices, key=lambda i: scores[i])]
