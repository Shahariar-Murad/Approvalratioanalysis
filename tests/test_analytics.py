from __future__ import annotations

import unittest

import pandas as pd

from analytics import (
    DataValidationError,
    apply_filters,
    collapse_checkout_sessions,
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
                    "2026-08-20T21:45:00+00:00",
                    "2026-08-20T22:00:00+00:00",
                    "2026-08-20T23:00:00+00:00",
                    "2026-08-21T02:00:00+00:00",
                ],
                "merchantOrderId": ["checkout-1", "checkout-1", "checkout-2", "checkout-3", "checkout-4"],
                "status": ["declined", "approved", "declined", "declined", "approved"],
                "pspName": ["PSP A", "PSP A", "PSP A", "PSP B", "PSP B"],
                "country": ["US", "US", "US", "US", "US"],
                "midAlias": ["MID A", "MID A", "MID A", "MID B", "MID B"],
                "declineCode": ["59", "", "59", "51", ""],
                "declineReason": ["Suspected fraud", "", "Suspected fraud", "Insufficient funds", ""],
            }
        )

    def test_cascaded_sessions_and_hard_declines_use_the_correct_denominator(self) -> None:
        attempts, diagnostics = prepare_transactions(self.raw)
        self.assertEqual(diagnostics.usable_rows, 5)
        data = collapse_checkout_sessions(attempts)
        self.assertEqual(len(data), 4)
        self.assertTrue(data.loc[data["checkout_session_id"].eq("checkout-1"), "is_approved"].item())
        summary = summarize_metrics(data).iloc[0]
        self.assertEqual(summary["Checkout sessions"], 4)
        self.assertEqual(summary["Hard declines"], 1)
        self.assertEqual(summary["Soft declines"], 1)
        self.assertEqual(summary["Approval ratio (with hard)"], 0.5)
        self.assertEqual(summary["Approval ratio (without hard)"], 2 / 3)

    def test_timezone_conversion_happens_before_date_filtering(self) -> None:
        attempts, _ = prepare_transactions(self.raw)
        local = with_local_time(collapse_checkout_sessions(attempts), "Asia/Dhaka")
        filtered = apply_filters(local, pd.Timestamp("2026-08-21").date(), pd.Timestamp("2026-08-21").date())
        self.assertEqual(len(filtered), 4)

    def test_missing_merchant_order_id_is_rejected(self) -> None:
        with self.assertRaises(DataValidationError):
            prepare_transactions(self.raw.drop(columns=["merchantOrderId"]))

    def test_psp_and_country_analysis_include_requested_performance_fields(self) -> None:
        comparison_input = self.raw.copy()
        comparison_input.loc[3, "status"] = "declined"
        comparison_input.loc[3, "declineCode"] = "51"
        comparison_input.loc[3, "declineReason"] = "Insufficient funds"
        comparison_input.loc[4, "status"] = "declined"
        comparison_input.loc[4, "declineCode"] = "51"
        comparison_input.loc[4, "declineReason"] = "Insufficient funds"
        attempts, _ = prepare_transactions(comparison_input)
        data = collapse_checkout_sessions(attempts)
        psp = performance_by_psp(data)
        self.assertIn("Top decline reason", psp.columns)
        country = performance_by_country(data, minimum_psp_sessions=1, ranking_column="Approval ratio (with hard)")
        self.assertEqual(country.iloc[0]["Best PSP"], "PSP A")
        self.assertEqual(country.iloc[0]["Worst PSP"], "PSP B")


if __name__ == "__main__":
    unittest.main()
