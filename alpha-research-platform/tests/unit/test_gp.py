import random
import unittest

from alpha_platform.generation.gp.crossover import crossover
from alpha_platform.generation.gp.mutation import mutate
from alpha_platform.generation.gp.population import generate_population
from alpha_platform.generation.gp.trees import validate_tree
from alpha_platform.structure.parser import parse
from alpha_platform.structure.serializer import serialize


class GPTests(unittest.TestCase):
    def test_serializer_preserves_nested_comparisons_and_quotes(self):
        for expression in ('(close < open) == (high > low)', "kth_element(close, 5, 1, ignore='a\"b')"):
            tree = parse(expression)
            self.assertEqual(parse(serialize(tree)), tree)

    def test_reproducible_unique_roundtrips(self):
        first = generate_population(count=30, seed=19)
        self.assertEqual(first, generate_population(count=30, seed=19))
        self.assertEqual(len(first), 30)
        self.assertEqual(len({c.ast_hash for c in first}), 30)
        for candidate in first:
            tree = parse(candidate.expression)
            self.assertEqual(tree, parse(serialize(tree)))
            validate_tree(tree)

    def test_window_mutation_protects_k_and_options(self):
        original = parse('kth_element(close, 5, 2, ignore="NaN")')
        child, kind = mutate(original, kind="window", rng=random.Random(3))
        self.assertEqual(kind, "window")
        self.assertNotEqual(child.args[1], original.args[1])
        self.assertEqual(child.args[0], original.args[0])
        self.assertEqual(child.args[2:], original.args[2:])
        self.assertEqual(original, parse('kth_element(close, 5, 2, ignore="NaN")'))

    def test_operator_swap_preserves_signature(self):
        original = parse("ts_mean(close, 21)")
        child, kind = mutate(original, kind="operator", rng=random.Random(3))
        self.assertEqual(kind, "operator")
        self.assertEqual(child.args, original.args)
        validate_tree(child)

    def test_crossover_does_not_move_options_into_signals(self):
        left, right = parse("hump(close, hump=0.01)"), parse("ts_mean(open, 21)")
        for i in range(100):
            child = crossover(left, right, rng=random.Random(i))
            validate_tree(child)
            self.assertEqual(child, parse(serialize(child)))
        self.assertEqual(left, parse("hump(close, hump=0.01)"))

    def test_rejects_unknown_arity_and_ambiguous_types(self):
        for expression in ("ts_mean(close)", "ts_mean(close, open)", "ts_mean(close, 0)",
                           "rank(close, surprise=2)", "bucket(rank(close), range=3)",
                           "ts_step(1)", "close + secret_field", "hump(close, hump=open)"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                validate_tree(parse(expression))
