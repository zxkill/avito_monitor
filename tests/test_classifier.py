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
        cls._family_to_brand = {10: 1, 20: 2, 30: 3, 40: 4, 50: 5, 60: 6, 70: 7, 80: 8, 90: 9}
        cls._brand_norm_to_id = {"acer": 2, "lenovo": 1, "samsung": 3, "apple": 4, "maibenben": 5, "digma": 6, "ardor": 7, "roverbook": 8, "jumper": 9}
        cls._family_rows = [
            FamilyRow(family_id=20, brand_id=2, norm="acer aspire 5315", compact="aceraspire5315"),
            FamilyRow(family_id=30, brand_id=3, norm="samsung r528", compact="samsungr528"),
            FamilyRow(family_id=40, brand_id=4, norm="apple macbook pro 13 2013", compact="applemacbookpro132013"),
            FamilyRow(family_id=50, brand_id=5, norm="maibenben x558", compact="maibenbenx558"),
            FamilyRow(family_id=60, brand_id=6, norm="digma eve c5802", compact="digmaevec5802"),
            FamilyRow(family_id=70, brand_id=7, norm="ardor gaming", compact="ardorgaming"),
            FamilyRow(family_id=80, brand_id=8, norm="roverbook pro", compact="roverbookpro"),
            FamilyRow(family_id=90, brand_id=9, norm="jumper ezbook 3 pro", compact="jumperezbook3pro")
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


    def test_variant_phrase_alias_builds_full_hierarchy(self) -> None:
        """Фразовый variant-алиас должен давать variant и корректно достраивать family/brand."""
        cls = self._make_classifier()
        cls._phrase_aliases.append(
            AliasRow(brand_id=None, family_id=None, variant_id=100, match_type="phrase", pattern="t480 type-c", weight=10)
        )

        result = cls.classify(title="ThinkPad T480 type-c", description="lenovo")

        self.assertEqual(result["variant_id"], 100)
        self.assertEqual(result["family_id"], 10)
        self.assertEqual(result["brand_id"], 1)
        self.assertEqual(result["debug"]["scope"], "variant")


    def test_russian_brand_word_is_normalized(self) -> None:
        """Русское название бренда должно нормализоваться в каноническую форму."""
        cls = self._make_classifier()

        result = cls.classify(title="Самсунг R528", description="ноутбук")

        self.assertEqual(result["brand_id"], 3)
        self.assertEqual(result["family_id"], 30)

    def test_typo_aser_is_normalized_to_acer(self) -> None:
        """Опечатка Aser должна приводиться к Acer для срабатывания семейства."""
        cls = self._make_classifier()
        cls._family_rows.append(FamilyRow(family_id=21, brand_id=2, norm="acer aspire 5635zg", compact="aceraspire5635zg"))
        cls._family_to_brand[21] = 2
        cls._phrase_aliases.append(AliasRow(brand_id=2, family_id=21, variant_id=None, match_type="phrase", pattern="5635 zg", weight=9))

        result = cls.classify(title="Ноутбук Aser 5635 zg", description=None)

        self.assertEqual(result["brand_id"], 2)
        self.assertEqual(result["family_id"], 21)

    def test_macbook_with_year_detects_family(self) -> None:
        """MacBook Pro 13 (2013) должен определяться как отдельное семейство."""
        cls = self._make_classifier()

        result = cls.classify(title="MacBook Pro 13 (2013)", description="512GB")

        self.assertEqual(result["brand_id"], 4)
        self.assertEqual(result["family_id"], 40)


    def test_maibenben_series_detected(self) -> None:
        """Maibenben X558 из проблемного списка должен определяться как family."""
        cls = self._make_classifier()
        result = cls.classify(title="Игровой ноутбук Maibenben x558 RTX 3060 6Gb", description=None)
        self.assertEqual(result["brand_id"], 5)
        self.assertEqual(result["family_id"], 50)

    def test_digma_eve_series_detected(self) -> None:
        """Digma EVE C5802 должен распознаваться по словарю/токенам."""
        cls = self._make_classifier()
        result = cls.classify(title="Ноутбук digma eve C5802 донор на запчасти", description=None)
        self.assertEqual(result["brand_id"], 6)
        self.assertEqual(result["family_id"], 60)

    def test_ardor_gaming_detected(self) -> None:
        """Ardor gaming должен давать корректное семейство."""
        cls = self._make_classifier()
        result = cls.classify(title="Ardor gaming", description=None)
        self.assertEqual(result["brand_id"], 7)
        self.assertEqual(result["family_id"], 70)

    def test_ezbook_detected(self) -> None:
        """EZbook 3 PRO должен маппиться в семейство Jumper EZbook 3 Pro."""
        cls = self._make_classifier()
        result = cls.classify(title="Ультрабук Ezbook 3 PRO", description=None)
        self.assertEqual(result["brand_id"], 9)
        self.assertEqual(result["family_id"], 90)

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
