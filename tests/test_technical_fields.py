from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.rules.expression_parser import DEFAULT_NAMES
from src.rules.registry import RuleRegistry, _test_frame
from src.rules.simple_editor import save_library_rule, simple_rule_editor_contract
from src.rules.technical_fields import (
    TECHNICAL_FIELD_LOOKBACKS,
    TECHNICAL_FIELD_METADATA,
    TECHNICAL_FIELD_NAMES,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TechnicalFieldMetadataTests(unittest.TestCase):
    def test_one_registry_drives_dsl_lookbacks_and_editor_contract(self):
        contract_fields = {
            item["name"]: item for item in simple_rule_editor_contract()["fields"]
        }

        self.assertEqual(TECHNICAL_FIELD_NAMES, DEFAULT_NAMES)
        self.assertEqual(TECHNICAL_FIELD_NAMES, set(TECHNICAL_FIELD_LOOKBACKS))
        self.assertEqual(TECHNICAL_FIELD_NAMES, set(contract_fields))
        for name, metadata in TECHNICAL_FIELD_METADATA.items():
            self.assertEqual(metadata["lookback"], contract_fields[name]["lookback"])
            self.assertTrue(contract_fields[name]["label"])
            self.assertTrue(contract_fields[name]["evidence_label"])

    def test_registry_covers_every_existing_indicator_and_common_field(self):
        frame = _test_frame()
        self.assertEqual(TECHNICAL_FIELD_NAMES, set(frame.columns))
        self.assertFalse(frame.iloc[-1][sorted(TECHNICAL_FIELD_NAMES)].isna().any())
        self.assertTrue(
            {
                "ma30",
                "ma120",
                "ma250",
                "ema12",
                "ema26",
                "macd",
                "macd_signal",
                "macd_hist_diff",
                "bb_mid",
                "bb_upper",
                "bb_lower",
                "atr14",
                "obv",
                "high_60",
                "low_60",
            }.issubset(TECHNICAL_FIELD_NAMES)
        )

    def test_newly_exposed_existing_fields_run_through_the_normal_rule_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = save_library_rule(
                Path(temp_dir),
                {
                    "request_id": "request_common_fields_001",
                    "name": "常用字段组合验证",
                    "description": "验证已有指标字段可以直接用于安全表达式。",
                    "expression": (
                        "all(ma120 >= ma250, macd >= macd_signal, "
                        "close <= bb_upper, obv >= prev(obv, 1))"
                    ),
                },
            )
            definition = RuleRegistry(temp_dir).get(
                "user.rule_request_common_fields_001",
                1,
            )

        self.assertEqual("ACTIVE", result["rule"]["status"])
        self.assertEqual(250, definition["lookback"])
        self.assertEqual(
            ["bb_upper", "close", "ma120", "ma250", "macd", "macd_signal", "obv"],
            definition["inputs"],
        )

    def test_web_uses_contract_labels_without_a_duplicate_field_map(self):
        source = (
            PROJECT_ROOT / "webapp" / "static" / "modules" / "screening.js"
        ).read_text(encoding="utf-8")
        self.assertNotIn("const evidenceMetricLabels", source)
        self.assertIn("state.rules?.editor_contract?.fields", source)
        self.assertIn("metadata?.evidence_label", source)


if __name__ == "__main__":
    unittest.main()
