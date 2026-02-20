"""Тесты классификатора моделей.

Файл лежит в корне, чтобы не требовать доп. настройки PYTHONPATH в окружении CI.
"""

from __future__ import annotations

import unittest

from src.analysis.classifier import AliasRow, ModelClassifier


class _DummyPool:
    """Простейший заглушечный pool для конструктора ModelClassifier."""


class ModelClassifierTests(unittest.TestCase):
    """Проверяет ключевые сценарии распознавания бренда/семейства/варианта."""

    def _make_classifier(self) -> ModelClassifier:
        cls = ModelClassifier(pool=_DummyPool())
        # Подготавливаем минимальные индексы вручную, чтобы тест был детерминированным
        # и не зависел от БД.
        cls._token_index = {
            "lenovo": [AliasRow(brand_id=1, family_id=None, variant_id=None, match_type="token", pattern="lenovo", weight=2)],
            "thinkpad": [AliasRow(brand_id=1, family_id=10, variant_id=None, match_type="token", pattern="thinkpad", weight=3)],
            "t480": [AliasRow(brand_id=1, family_id=10, variant_id=100, match_type="token", pattern="t480", weight=7)],
        }
        cls._phrase_aliases = [
            AliasRow(brand_id=1, family_id=10, variant_id=None, match_type="phrase", pattern="think pad", weight=2)
        ]
        cls._regex_aliases = []
        cls._variant_to_family = {100: 10}
        cls._variant_to_brand = {100: 1}
        cls._family_to_brand = {10: 1}
        return cls

    def test_cyrillic_code_is_recognized(self) -> None:
        """Похожие кириллические символы должны корректно конвертироваться в латиницу."""
        cls = self._make_classifier()

        result = cls.classify(title="Lenovo ThinkPad т480", description="рабочий")

        self.assertEqual(result["brand_id"], 1)
        self.assertEqual(result["family_id"], 10)
        self.assertEqual(result["variant_id"], 100)
        self.assertGreaterEqual(result["confidence"], 40)

    def test_phrase_alias_is_used(self) -> None:
        """Фразовый алиас должен срабатывать даже без отдельного токена thinkpad."""
        cls = self._make_classifier()

        result = cls.classify(title="Lenovo Think Pad", description=None)

        self.assertEqual(result["brand_id"], 1)
        self.assertEqual(result["family_id"], 10)
        self.assertIsNone(result["variant_id"])
        self.assertEqual(result["debug"]["scope"], "family")


if __name__ == "__main__":
    unittest.main()
