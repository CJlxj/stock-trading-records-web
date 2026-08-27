from __future__ import annotations

import unittest

from src.target_review import evaluate_target_plan


class TargetReviewTests(unittest.TestCase):
    def latest(self):
        return {"high_20": 106, "high_60": 116, "atr14": 2}

    def test_consistent_target_has_reward_risk_and_atr_context(self):
        result = evaluate_target_plan(100, 95, 110, self.latest(), min_reward_risk=2)
        self.assertEqual("CONSISTENT", result["status"])
        self.assertEqual(2, result["reward_risk"])
        self.assertEqual(5, result["target_atr"])

    def test_target_below_risk_space_is_unfavorable(self):
        result = evaluate_target_plan(100, 94, 104, self.latest(), min_reward_risk=2)
        self.assertEqual("UNFAVORABLE", result["status"])
        self.assertTrue(any("收益空间" in item for item in result["risks"]))

    def test_distant_target_requires_additional_basis(self):
        result = evaluate_target_plan(100, 95, 130, self.latest(), min_reward_risk=2)
        self.assertEqual("REVIEW", result["status"])
        self.assertTrue(any("60 日高点" in item for item in result["risks"]))


if __name__ == "__main__":
    unittest.main()
