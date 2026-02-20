"""Тесты отчёта по рынку и расчёту персональной статистики лота."""

from __future__ import annotations

import unittest

from src.analysis.report import build_report_v2


class ReportTests(unittest.TestCase):
    """Проверяет, что отчёт учитывает статистику variant/family, а не только общий search."""

    def test_report_uses_lot_market_stats(self) -> None:
        """В p50 и бейдже цены должны использоваться персональные market_stats лота."""
        stats = {"n": 100, "p25": 30000, "p50": 40000, "p75": 50000}
        items = [
            {
                "id": 1,
                "url": "https://example.com/1",
                "title": "Lenovo ThinkPad T480",
                "price": 35000,
                "city": "Москва",
                "description": "рабочий",
                # Персональный рынок конкретной модели существенно выше общего.
                "market_stats": {"scope": "family", "n": 35, "p25": 43000, "p50": 45000, "p75": 47000},
            }
        ]

        messages = build_report_v2("t480", stats, items, top_n=1, score_min=0, profit_min_need=-10**9)
        full = "\n".join(messages)

        self.assertIn("p50 (family): <b>45 000 ₽</b>", full)
        self.assertNotIn("p50 (family): <b>40 000 ₽</b>", full)


if __name__ == "__main__":
    unittest.main()
