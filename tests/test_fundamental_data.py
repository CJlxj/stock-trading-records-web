from __future__ import annotations

from datetime import date
import unittest

import pandas as pd

from src.fundamental_data import fetch_fundamental_data, review_fundamental_payload


class FundamentalDataTests(unittest.TestCase):
    def test_online_result_normalizes_latest_financial_row(self):
        analysis = pd.DataFrame(
            [
                {
                    "报告期": "2025-09-30",
                    "营业总收入同比增长率": 8.5,
                    "净利润同比增长率": 6.2,
                    "净资产收益率": 11.4,
                    "资产负债率": 42.1,
                },
                {
                    "报告期": "2025-12-31",
                    "营业总收入同比增长率": 10.2,
                    "净利润同比增长率": 9.1,
                    "净资产收益率": 12.3,
                    "资产负债率": 40.8,
                },
            ]
        )
        profile = pd.DataFrame([{"所属行业": "示例制造", "主营业务": "示例产品研发与销售"}])
        result = fetch_fundamental_data(
            "600000.SH",
            analysis_fetcher=lambda **_: analysis,
            profile_fetcher=lambda **_: profile,
        )
        self.assertEqual("2025-12-31", result["as_of_date"])
        self.assertEqual(10.2, result["metrics"]["revenue_growth"])
        self.assertEqual("示例制造", result["industry"])

    def test_manual_source_requires_provenance_and_date(self):
        missing = review_fundamental_payload(
            {"fundamental_source_type": "manual"}, date(2026, 7, 12)
        )
        self.assertEqual("PARTIAL", missing["status"])
        self.assertGreaterEqual(len(missing["missing"]), 3)

        ready = review_fundamental_payload(
            {
                "fundamental_source_type": "manual",
                "fundamental_source": "公司 2025 年报",
                "fundamental_as_of": "2025-12-31",
                "fundamental_source_url": "https://example.com/report",
                "fundamental_summary": "主营收入来自可核对的产品业务",
                "fundamental_revenue_growth": "10.2",
            },
            date(2026, 3, 1),
        )
        self.assertEqual("READY", ready["status"])
        self.assertEqual(10.2, ready["metrics"]["revenue_growth"])


if __name__ == "__main__":
    unittest.main()
