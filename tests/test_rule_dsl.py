from __future__ import annotations

import unittest

import pandas as pd

from src.rules.expression_parser import ExpressionSyntaxError, parse_expression
from src.rules.expression_runtime import (
    ExpressionDataGap,
    ExpressionRuntimeError,
    evaluate_expression,
)


class RuleDslTests(unittest.TestCase):
    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "close": list(range(1, 31)),
                "open": [value - 0.1 for value in range(1, 31)],
                "high": [value + 0.2 for value in range(1, 31)],
                "low": [value - 0.2 for value in range(1, 31)],
                "volume": [100.0] * 29 + [150.0],
            }
        )

    def test_normalizes_and_runs_registered_expression(self):
        parsed = parse_expression(
            "all(ma(close, 5) > ma(close, 10), volume / ma(volume, 20) >= 1.3)"
        )
        result = evaluate_expression(parsed, self.frame())
        self.assertTrue(result["boolean_result"])
        self.assertEqual(2, len(result["comparisons"]))
        self.assertIn("ma(close, 5)", result["normalized_expression"])

    def test_rejects_attribute_subscript_lambda_and_unknown_function(self):
        expressions = [
            "close.__class__",
            "close[0]",
            "(lambda: 1)()",
            "__import__(1)",
            "open_file(close)",
        ]
        for expression in expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(ExpressionSyntaxError):
                    parse_expression(expression)

    def test_rejects_unknown_names_and_excessive_nesting(self):
        with self.assertRaises(ExpressionSyntaxError):
            parse_expression("secret_value > 1")
        nested = "close > 1"
        for _ in range(20):
            nested = f"all({nested})"
        with self.assertRaises(ExpressionSyntaxError):
            parse_expression(nested)

    def test_division_by_zero_is_an_error(self):
        frame = self.frame()
        frame["volume"] = 0.0
        with self.assertRaises(ExpressionRuntimeError):
            evaluate_expression("close / volume > 1", frame)

    def test_latest_nan_is_data_gap_not_an_earlier_value(self):
        frame = self.frame()
        frame.loc[frame.index[-1], "close"] = float("nan")
        with self.assertRaises(ExpressionDataGap):
            evaluate_expression("close > ma(close, 5)", frame)


if __name__ == "__main__":
    unittest.main()
