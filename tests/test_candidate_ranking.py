from __future__ import annotations

import unittest

from src.screening.ranking import apply_candidate_ranking


def _definition(rule_id: str, name: str | None = None) -> dict:
    return {
        "id": rule_id,
        "version": 1,
        "name": name or rule_id,
    }


def _evidence(
    rule_id: str,
    *,
    status: str = "PASS",
    left: float | None = None,
    operator: str = ">=",
    right: float | None = None,
) -> dict:
    comparisons = []
    if left is not None and right is not None:
        comparisons.append(
            {
                "left": left,
                "operator": operator,
                "right": right,
                "result": status == "PASS",
            }
        )
    return {
        "rule_id": rule_id,
        "rule_version": 1,
        "status": status,
        "comparisons": comparisons,
    }


def _candidate(symbol: str, evidence: list[dict]) -> dict:
    return {
        "symbol": symbol,
        "selection_status": "PASS",
        "technical_status": "BUY_CANDIDATE",
        "technical_score_ratio": 1.0,
        "rule_evidence": evidence,
    }


def _rule_set(*, secondary_minimum: int = 0, top_n: int = 5) -> dict:
    return {
        "secondary": {"minimum_match": secondary_minimum},
        "ranking": {"top_n": top_n},
    }


class CandidateRankingTests(unittest.TestCase):
    def test_primary_fail_or_gap_skips_secondary(self):
        secondary = [_definition("secondary_rule")]
        rows = [
            {
                "symbol": "000001.SZ",
                "selection_status": "PASS",
                "technical_status": "WATCH",
                "rule_evidence": [_evidence("secondary_rule")],
            },
            {
                "symbol": "000002.SZ",
                "selection_status": "PASS",
                "technical_status": "DATA_GAP",
                "rule_evidence": [_evidence("secondary_rule")],
            },
        ]

        summary = apply_candidate_ranking(
            rows,
            {
                "editor_mode": "simple_all",
                "secondary": {"minimum_match": 1},
                "ranking": {"top_n": 5},
            },
            {"scored": [], "secondary": secondary},
        )

        self.assertTrue(all(row["secondary_status"] == "SKIPPED" for row in rows))
        self.assertTrue(all(not row["final_candidate"] for row in rows))
        self.assertEqual(0, summary["secondary_pass_count"])

    def test_no_secondary_keeps_primary_candidate_as_final(self):
        rows = [_candidate("000001.SZ", [])]
        summary = apply_candidate_ranking(
            rows,
            {
                "editor_mode": "simple_all",
                "secondary": {"minimum_match": 0},
                "ranking": {"top_n": 5},
            },
            {"scored": [], "secondary": []},
        )
        self.assertEqual("INACTIVE", rows[0]["secondary_status"])
        self.assertTrue(rows[0]["final_candidate"])
        self.assertEqual(1, summary["final_candidate_count"])

    def test_all_secondary_passes_form_final_candidate(self):
        secondary = [_definition("secondary_one"), _definition("secondary_two")]
        rows = [
            _candidate(
                "000001.SZ",
                [
                    _evidence("secondary_one", left=2.0, right=1.0),
                    _evidence("secondary_two", left=3.0, right=1.0),
                ],
            )
        ]
        summary = apply_candidate_ranking(
            rows,
            {
                "editor_mode": "simple_all",
                "secondary": {"minimum_match": 2},
                "ranking": {"top_n": 5},
            },
            {"scored": [], "secondary": secondary},
        )
        self.assertEqual("PASS", rows[0]["secondary_status"])
        self.assertTrue(rows[0]["final_candidate"])
        self.assertEqual(1, summary["final_candidate_count"])

    def test_secondary_strength_never_reorders_primary_ranking(self):
        primary = _definition("primary_rule")
        secondary = _definition("secondary_rule")
        rows = [
            _candidate(
                "000001.SZ",
                [
                    _evidence("primary_rule", left=12.0, right=10.0),
                    _evidence("secondary_rule", left=1.1, right=1.0),
                ],
            ),
            _candidate(
                "000002.SZ",
                [
                    _evidence("primary_rule", left=11.0, right=10.0),
                    _evidence("secondary_rule", left=100.0, right=1.0),
                ],
            ),
        ]
        apply_candidate_ranking(
            rows,
            {
                "editor_mode": "simple_all",
                "secondary": {"minimum_match": 1},
                "ranking": {"top_n": 5},
            },
            {"scored": [primary], "secondary": [secondary]},
        )
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(1, by_symbol["000001.SZ"]["rank_position"])
        self.assertEqual(2, by_symbol["000002.SZ"]["rank_position"])

    def test_simple_two_stage_always_requires_every_secondary_rule(self):
        secondary = [_definition("secondary_one"), _definition("secondary_two")]
        rows = [
            _candidate(
                "000001.SZ",
                [
                    _evidence("secondary_one", left=2.0, right=1.0),
                    _evidence(
                        "secondary_two",
                        status="FAIL",
                        left=0.5,
                        right=1.0,
                    ),
                ],
            )
        ]

        summary = apply_candidate_ranking(
            rows,
            {
                "editor_mode": "simple_all",
                # 即使调用方带入宽松旧门槛，简单组合也必须按 ALL 执行。
                "secondary": {"minimum_match": 1},
                "ranking": {"top_n": 5},
            },
            {"scored": [], "secondary": secondary},
        )

        self.assertEqual("FAIL", rows[0]["secondary_status"])
        self.assertFalse(rows[0]["final_candidate"])
        self.assertEqual(2, summary["secondary_minimum_match"])
        self.assertEqual("ALL", summary["secondary_match_mode"])

    def test_first_rule_priority_dominates_all_lower_priority_rules(self):
        definitions = [
            _definition("priority_one"),
            _definition("priority_two"),
            _definition("priority_three"),
        ]
        rows = [
            _candidate(
                "000001.SZ",
                [
                    _evidence("priority_one", left=12.0, right=10.0),
                    _evidence("priority_two", left=1.1, right=1.0),
                    _evidence("priority_three", left=1.1, right=1.0),
                ],
            ),
            _candidate(
                "000002.SZ",
                [
                    _evidence("priority_one", left=11.0, right=10.0),
                    _evidence("priority_two", left=10.0, right=1.0),
                    _evidence("priority_three", left=10.0, right=1.0),
                ],
            ),
        ]

        apply_candidate_ranking(
            rows,
            _rule_set(),
            {"scored": definitions, "secondary": []},
        )

        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(1, by_symbol["000001.SZ"]["rank_position"])
        self.assertEqual(2, by_symbol["000002.SZ"]["rank_position"])
        self.assertGreater(
            by_symbol["000001.SZ"]["ranking_score"],
            by_symbol["000002.SZ"]["ranking_score"],
        )

    def test_same_rule_ranks_larger_positive_threshold_margin_first(self):
        definition = _definition("close_above_ma60", "收盘站上 MA60")
        rows = [
            _candidate(
                "000001.SZ",
                [_evidence("close_above_ma60", left=11.0, operator=">", right=10.0)],
            ),
            _candidate(
                "000002.SZ",
                [_evidence("close_above_ma60", left=13.0, operator=">", right=10.0)],
            ),
        ]

        apply_candidate_ranking(
            rows,
            _rule_set(),
            {"scored": [definition], "secondary": []},
        )

        by_symbol = {row["symbol"]: row for row in rows}
        stronger = by_symbol["000002.SZ"]
        weaker = by_symbol["000001.SZ"]
        self.assertEqual(1, stronger["rank_position"])
        self.assertEqual(2, weaker["rank_position"])
        self.assertGreater(stronger["ranking_score"], weaker["ranking_score"])
        self.assertGreater(
            stronger["ranking_evidence"]["primary"][0]["threshold_margin"],
            weaker["ranking_evidence"]["primary"][0]["threshold_margin"],
        )

    def test_less_than_operator_treats_lower_observation_as_stronger(self):
        definition = _definition("atr_not_high", "ATR 波动未过高")
        rows = [
            _candidate(
                "000001.SZ",
                [_evidence("atr_not_high", left=0.02, operator="<=", right=0.05)],
            ),
            _candidate(
                "000002.SZ",
                [_evidence("atr_not_high", left=0.04, operator="<=", right=0.05)],
            ),
        ]

        apply_candidate_ranking(
            rows,
            _rule_set(),
            {"scored": [definition], "secondary": []},
        )

        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(1, by_symbol["000001.SZ"]["rank_position"])
        self.assertGreater(
            by_symbol["000001.SZ"]["ranking_score"],
            by_symbol["000002.SZ"]["ranking_score"],
        )

    def test_non_comparable_rule_ties_and_uses_symbol_as_stable_tiebreaker(self):
        definition = _definition("bounded_rule", "区间规则")
        rows = [
            _candidate("000002.SZ", [_evidence("bounded_rule")]),
            _candidate("000001.SZ", [_evidence("bounded_rule")]),
        ]

        apply_candidate_ranking(
            rows,
            _rule_set(),
            {"scored": [definition], "secondary": []},
        )

        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(
            by_symbol["000001.SZ"]["ranking_score"],
            by_symbol["000002.SZ"]["ranking_score"],
        )
        self.assertEqual(1, by_symbol["000001.SZ"]["rank_position"])
        self.assertEqual(2, by_symbol["000002.SZ"]["rank_position"])
        self.assertFalse(
            by_symbol["000001.SZ"]["ranking_evidence"]["primary"][0][
                "comparable_strength"
            ]
        )

    def test_only_first_five_ranked_candidates_are_marked_top(self):
        definition = _definition("close_above_ma60")
        rows = [
            _candidate(
                f"00000{index}.SZ",
                [
                    _evidence(
                        "close_above_ma60",
                        left=10.0 + index,
                        operator=">",
                        right=10.0,
                    )
                ],
            )
            for index in range(1, 8)
        ]

        summary = apply_candidate_ranking(
            rows,
            _rule_set(top_n=5),
            {"scored": [definition], "secondary": []},
        )

        ranked = sorted(rows, key=lambda row: row["rank_position"])
        self.assertEqual(list(range(1, 8)), [row["rank_position"] for row in ranked])
        self.assertEqual(5, sum(row["is_top_candidate"] for row in rows))
        self.assertTrue(all(row["is_top_candidate"] for row in ranked[:5]))
        self.assertTrue(all(not row["is_top_candidate"] for row in ranked[5:]))
        self.assertEqual(5, summary["top_candidate_count"])
        self.assertEqual(2, summary["overflow_candidate_count"])

    def test_secondary_pass_fail_and_data_gap_are_distinct(self):
        primary = _definition("primary_rule")
        secondary = _definition("secondary_rule")
        rows = [
            _candidate(
                "000001.SZ",
                [
                    _evidence("primary_rule", left=12.0, right=10.0),
                    _evidence("secondary_rule", left=2.0, right=1.0),
                ],
            ),
            _candidate(
                "000002.SZ",
                [
                    _evidence("primary_rule", left=12.0, right=10.0),
                    _evidence(
                        "secondary_rule",
                        status="FAIL",
                        left=0.5,
                        right=1.0,
                    ),
                ],
            ),
            _candidate(
                "000003.SZ",
                [
                    _evidence("primary_rule", left=12.0, right=10.0),
                    _evidence("secondary_rule", status="DATA_GAP"),
                ],
            ),
        ]

        summary = apply_candidate_ranking(
            rows,
            _rule_set(secondary_minimum=1),
            {"scored": [primary], "secondary": [secondary]},
        )

        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual("PASS", by_symbol["000001.SZ"]["secondary_status"])
        self.assertTrue(by_symbol["000001.SZ"]["final_candidate"])
        self.assertEqual("FAIL", by_symbol["000002.SZ"]["secondary_status"])
        self.assertFalse(by_symbol["000002.SZ"]["final_candidate"])
        self.assertEqual("DATA_GAP", by_symbol["000003.SZ"]["secondary_status"])
        self.assertFalse(by_symbol["000003.SZ"]["final_candidate"])
        self.assertEqual(1, summary["secondary_pass_count"])
        self.assertEqual(1, summary["secondary_filtered_count"])
        self.assertEqual(1, summary["secondary_data_gap_count"])
        self.assertEqual(1, summary["final_candidate_count"])

    def test_secondary_can_produce_zero_final_candidates_without_forcing_top_five(self):
        primary = _definition("primary_rule")
        secondary = _definition("secondary_rule")
        rows = [
            _candidate(
                "000001.SZ",
                [
                    _evidence("primary_rule", left=12.0, right=10.0),
                    _evidence(
                        "secondary_rule",
                        status="FAIL",
                        left=0.5,
                        right=1.0,
                    ),
                ],
            ),
            _candidate(
                "000002.SZ",
                [
                    _evidence("primary_rule", left=12.0, right=10.0),
                    _evidence("secondary_rule", status="DATA_GAP"),
                ],
            ),
        ]

        summary = apply_candidate_ranking(
            rows,
            _rule_set(secondary_minimum=1),
            {"scored": [primary], "secondary": [secondary]},
        )

        self.assertEqual(0, summary["final_candidate_count"])
        self.assertEqual(0, summary["top_candidate_count"])
        self.assertEqual(0, summary["overflow_candidate_count"])
        self.assertTrue(all(row["rank_position"] is None for row in rows))
        self.assertTrue(all(not row["is_top_candidate"] for row in rows))


if __name__ == "__main__":
    unittest.main()
