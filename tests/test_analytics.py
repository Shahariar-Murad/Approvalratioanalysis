from __future__ import annotations

import unittest

import pandas as pd

from analytics import (
    apply_filters,
    performance_by_country,
    performance_by_psp,
    prepare_transactions,
    summarize_metrics,
    with_local_time,
)


class ApprovalRatioAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = pd.DataFrame(
            {
                "processing_date": [
                    "2026-08-20T21:30:00+00:00",
                    "2026-08-20T22:00:00+00:00",
                    "2026-08-21T01:00:00+00:00",
                    "2026-08-21T02:00:00+00:00",
                ],
                "status": ["approved", "declined", "declined", "approved"],
                "pspName": ["PSP A", "PSP A", "PSP B", "PSP B"],
                "country": ["US", "US", "US", "US"],
                "midAlias": ["MID A", "MID A", "MID B", "MID B"],
                "declineCode": ["", "59", "51", ""],
                "declineReason": ["", "Suspected fraud", "Insufficient funds", ""],
            }
        )

    def test_hard_declines_are_excluded_only_from_the_second_ratio(self) -> None:
        data, diagnostics = prepare_transactions(self.raw)
        self.assertEqual(diagnostics.usable_rows, 4)
        summary = summarize_metrics(data).iloc[0]
        self.assertEqual(summary["Hard declines"], 1)
        self.assertEqual(summary["Soft declines"], 1)
        self.assertEqual(summary["Approval ratio (with hard)"], 0.5)
        self.assertEqual(summary["Approval ratio (without hard)"], 2 / 3)

    def test_timezone_conversion_happens_before_date_filtering(self) -> None:
        data, _ = prepare_transactions(self.raw)
        local = with_local_time(data, "Asia/Dhaka")
        filtered = apply_filters(local, pd.Timestamp("2026-08-21").date(), pd.Timestamp("2026-08-21").date())
        self.assertEqual(len(filtered), 4)

    def test_psp_and_country_analysis_include_requested_performance_fields(self) -> None:
        comparison_input = self.raw.copy()
        comparison_input.loc[3, "status"] = "declined"
        comparison_input.loc[3, "declineCode"] = "51"
        comparison_input.loc[3, "declineReason"] = "Insufficient funds"
        data, _ = prepare_transactions(comparison_input)
        psp = performance_by_psp(data)
        self.assertIn("Top decline reason", psp.columns)
        country = performance_by_country(data, minimum_psp_attempts=1, ranking_column="Approval ratio (with hard)")
        self.assertEqual(country.iloc[0]["Best PSP"], "PSP A")
        self.assertEqual(country.iloc[0]["Worst PSP"], "PSP B")


if __name__ == "__main__":
    unittest.main()
