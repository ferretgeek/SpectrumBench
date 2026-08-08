from __future__ import annotations

import unittest

from stress_tool.measurement import (
    bootstrap_median_ratio_ci,
    calculate_speed_measurement,
    percentile,
    summarize,
)


class MeasurementTests(unittest.TestCase):
    def test_reasoning_tokens_do_not_inflate_visible_speed(self) -> None:
        result = calculate_speed_measurement(
            output_tokens=100,
            reasoning_tokens=40,
            request_started_at=10.0,
            first_visible_at=12.0,
            last_visible_at=14.0,
            request_completed_at=14.5,
            stream_text_chunks=10,
            first_visible_chunk_tokens=5,
        )
        self.assertEqual(result.visible_output_tokens, 60)
        self.assertEqual(result.first_visible_chunk_tokens, 5)
        self.assertEqual(result.timed_visible_tokens, 55)
        self.assertAlmostEqual(result.visible_tokens_per_second, 27.5)
        self.assertAlmostEqual(result.end_to_end_visible_tokens_per_second, 15.0)
        self.assertAlmostEqual(result.billed_output_tokens_per_second, 100 / 4.5, places=3)
        self.assertTrue(result.speed_valid)

    def test_single_buffered_chunk_is_not_a_speed_sample(self) -> None:
        result = calculate_speed_measurement(
            output_tokens=200,
            reasoning_tokens=0,
            request_started_at=0.0,
            first_visible_at=1.0,
            last_visible_at=1.0,
            request_completed_at=1.1,
            stream_text_chunks=1,
        )
        self.assertFalse(result.speed_valid)
        self.assertEqual(result.speed_exclusion_reason, "stream_buffered_single_chunk")
        self.assertEqual(result.visible_tokens_per_second, 0.0)

    def test_summary_and_interpolated_percentile_case(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(percentile(values, 0.25), 17.5)
        result = summarize(values)
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["median"], 25.0)
        self.assertEqual(result["p75"], 32.5)

    def test_bootstrap_ratio_is_deterministic(self) -> None:
        first = bootstrap_median_ratio_ci([20, 40, 60], [10, 20, 30])
        second = bootstrap_median_ratio_ci([20, 40, 60], [10, 20, 30])
        self.assertEqual(first, second)
        self.assertEqual(first["median_ratio"], 2.0)
        self.assertEqual(first["ci_low"], 2.0)
        self.assertEqual(first["ci_high"], 2.0)


if __name__ == "__main__":
    unittest.main()
