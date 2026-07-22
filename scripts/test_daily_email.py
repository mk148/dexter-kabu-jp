#!/usr/bin/env python3

import unittest

from daily_email import DISPLAY_WIDTH, build_body, display_width


class DailyEmailTest(unittest.TestCase):
    def test_mobile_body_has_complete_identity_and_short_lines(self) -> None:
        distortion = [{
            "date": "2026-07-21",
            "code": "1234",
            "company": "とても長い日本語の株式会社サンプルホールディングス",
            "signal": "D",
            "price_at_signal": 1234,
        }]
        yutai = [{
            "code": "367A",
            "company": "プリモグローバルHD",
            "source_url": "https://example.com/benefit",
            "total_yield_pct": 5.8,
            "benefit_content": "全国の店舗で利用できる長い説明の株主優待券です",
        }]

        body = build_body("success", [], distortion, [], yutai, [])

        self.assertIn("1234", body)
        self.assertIn("367A", body)
        self.assertNotIn("欠損", body)
        self.assertTrue(all(display_width(line) <= DISPLAY_WIDTH for line in body.splitlines()))

    def test_missing_identity_is_reported(self) -> None:
        invalid = [{"code": "", "company": ""}, {"code": "254A", "company": "通常"}]
        body = build_body("failure", [], invalid, [], [], ["ERROR sample"])
        self.assertIn("欠損 2件", body)
        self.assertIn("ERROR sample", body)


if __name__ == "__main__":
    unittest.main()
