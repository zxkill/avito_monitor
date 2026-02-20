"""Тесты классификатора моделей."""

from __future__ import annotations

import unittest

from src.analysis.classifier import AliasRow, FamilyRow, ModelClassifier


class _DummyPool:
    """Простейший заглушечный pool для конструктора ModelClassifier."""


class ModelClassifierTests(unittest.TestCase):
    """Проверяет ключевые сценарии распознавания бренда/семейства/варианта."""

    def _make_classifier(self) -> ModelClassifier:
        cls = ModelClassifier(pool=_DummyPool())
        # Подготавливаем индексы вручную: тесты детерминированы и не зависят от БД.
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
        cls._family_to_brand = {10: 1, 20: 2}
        cls._brand_norm_to_id = {"acer": 2, "lenovo": 1}
        cls._family_rows = [
            FamilyRow(family_id=20, brand_id=2, norm="acer aspire 5315", compact="aceraspire5315")
        ]
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

    def test_fallback_dictionary_detects_brand_and_family(self) -> None:
        """Если алиасов нет, классификатор должен взять бренд/семейство из словаря БД."""
        cls = self._make_classifier()

        result = cls.classify(title="Acer Aspire 5315", description="ноутбук")

        self.assertEqual(result["brand_id"], 2)
        self.assertEqual(result["family_id"], 20)
        self.assertEqual(result["debug"]["inferred"]["brand_id_from_dictionary"], 2)
        self.assertEqual(result["debug"]["inferred"]["family_id_from_dictionary"], 20)


if __name__ == "__main__":
    unittest.main()
