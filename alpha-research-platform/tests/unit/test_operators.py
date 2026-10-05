"""Offline regression tests; no BRAIN session or simulation is used."""
from dataclasses import replace
import unittest

from alpha_platform.config.operators import load_catalog, parse_signature, validate_catalog


class OperatorCatalogTests(unittest.TestCase):
    def test_checked_in_catalog(self):
        report = validate_catalog()
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.parsed, 82)
        self.assertTrue(any("ts_step" in message for message in report.warnings))

    def test_arithmetic_aliases_and_variadic_bounds(self):
        catalog = load_catalog()
        for name, symbol in (("add", "+"), ("subtract", "-"), ("multiply", "*"), ("divide", "/")):
            self.assertEqual(catalog[name].infix, symbol)
            self.assertEqual(catalog[name].min_arity, 2)
        for name in ("min", "max", "add", "multiply", "subtract"):
            self.assertEqual(catalog[name].signal_arity, 2)
            self.assertIsNone(catalog[name].max_arity)
        self.assertEqual(catalog["divide"].max_arity, 2)

    def test_nested_signal_and_keyword_placeholder(self):
        op = parse_signature('bucket(rank(x), range="0, 1, 0.1")', "Transformational")
        self.assertEqual(op.signal_arity, 1)
        self.assertEqual(op.keywords[0].default, '"0, 1, 0.1"')
        op = load_catalog()["bucket"]
        self.assertFalse(op.variadic)
        self.assertEqual(op.keywords[0].name, "range")
        self.assertTrue(op.keywords[0].required)
        self.assertIsNone(op.keywords[0].default)

    def test_positional_parameter_is_not_a_required_keyword(self):
        op = load_catalog()["kth_element"]
        self.assertEqual([a.name for a in op.positional], ["x", "d", "k"])
        self.assertEqual(op.min_arity, 3)
        self.assertEqual(op.signal_arity, 1)
        self.assertEqual(op.keywords[0].default, '"NaN"')

    def test_keyword_options_do_not_inflate_signal_arity(self):
        catalog = load_catalog()
        for name in ("to_nan", "hump", "normalize", "rank", "scale", "winsorize", "ts_backfill"):
            self.assertEqual(catalog[name].signal_arity, 1)
            self.assertEqual(catalog[name].min_arity, 1)
            self.assertTrue(all(a.keyword_only for a in catalog[name].keywords))

    def test_validator_detects_successfully_parsed_wrong_metadata(self):
        catalog = load_catalog()
        corruptions = {
            "add": replace(catalog["add"], infix="y"),
            "min": replace(catalog["min"], arguments=catalog["min"].arguments[:1]),
            "bucket": replace(catalog["bucket"], arguments=catalog["bucket"].positional),
            "kth_element": replace(catalog["kth_element"], arguments=tuple(
                replace(a, keyword_only=True) if a.name == "k" else a
                for a in catalog["kth_element"].arguments)),
        }
        for name, broken in corruptions.items():
            with self.subTest(name=name):
                report = validate_catalog(catalog={**catalog, name: broken})
                self.assertFalse(report.ok)
                self.assertTrue(any(message.startswith(name + ":") for message in report.errors))

    def test_rejects_malformed_signatures(self):
        for signature in ("foo(x", "foo(x,,y)", "foo(x), x nonsense y", "foo(x, x)", 'foo(x, a="oops)'):
            with self.subTest(signature=signature), self.assertRaises(ValueError):
                parse_signature(signature, "Test")

    def test_unknown_classification_is_visible(self):
        op = parse_signature("foo(mystery)", "Test")
        self.assertEqual(op.positional[0].role, "unknown")
        self.assertTrue(op.issues)


if __name__ == "__main__":
    unittest.main()
