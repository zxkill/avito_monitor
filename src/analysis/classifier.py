from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AliasRow:
    brand_id: int | None
    family_id: int | None
    variant_id: int | None
    match_type: str  # token|regex
    pattern: str
    weight: int


def _norm_text(s: str) -> str:
    """Нормализует текст объявления для устойчивого матчинга алиасов."""
    s = (s or "").lower()
    s = s.replace("ё", "е")
    # Нормализуем визуально похожие кириллические символы в латиницу.
    # Это критично для кодов моделей вида: "т480" -> "t480", "х1" -> "x1".
    s = s.translate(str.maketrans({
        "а": "a", "в": "b", "с": "c", "е": "e", "к": "k",
        "м": "m", "н": "h", "о": "o", "р": "p", "т": "t",
        "у": "y", "х": "x",
    }))
    # унификация разделителей
    s = re.sub(r"[\t\r\n]+", " ", s)
    s = s.replace("×", "x")
    s = re.sub(r"[|/\\]+", " ", s)
    s = re.sub(r"[-_]+", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokenize(s: str) -> set[str]:
    """
    Базовые токены + полезные варианты:
    - обычные слова/алфанум (включая 4525s, 5552g, x61sv)
    - токены с дефисом
    - компактная форма без пробелов
    """
    s = _norm_text(s)

    # Основной набор токенов.
    tokens = set(re.findall(r"[a-zа-я0-9]+(?:-[a-zа-я0-9]+)?", s))

    # компактная форма (thinkpad t14 -> thinkpadt14)
    compact = s.replace(" ", "")
    if compact:
        tokens.add(compact)

    # Доп. токены: склейка букв+цифр, если где-то были разделители.
    # Пример: "t 14" -> "t14", "54 20" -> "5420".
    glued = re.findall(r"(?:[a-zа-я]{1,5}\s*\d{2,5}[a-zа-я]{0,3})", s)
    for g in glued:
        tokens.add(g.replace(" ", ""))

    # Для дефисных слов добавляем части отдельно: "e-14" -> "e", "14".
    # Это помогает словарям, где алиасы заведены в разных формах.
    for tok in list(tokens):
        if "-" in tok:
            tokens.update(p for p in tok.split("-") if p)

    return tokens


class ModelClassifier:
    """
    - token aliases: ищем через индекс token -> aliases
    - regex aliases: компилируем один раз и проверяем по тексту
    - Скоринг: сумма weight, tie-break: variant > family > brand
    - Автодостройка связей: variant->family->brand
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

        self._aliases: list[AliasRow] = []
        self._token_index: dict[str, list[AliasRow]] = {}
        self._regex_aliases: list[tuple[AliasRow, re.Pattern]] = []
        self._phrase_aliases: list[AliasRow] = []

        self._family_to_brand: dict[int, int] = {}
        self._variant_to_family: dict[int, int] = {}
        self._variant_to_brand: dict[int, int] = {}

    async def load(self) -> None:
        # 1) aliases
        sql_aliases = """
        SELECT
          brand_id,
          family_id,
          variant_id,
          match_type,
          pattern,
          COALESCE(weight, 1) AS weight
        FROM model_aliases
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql_aliases)

        aliases: list[AliasRow] = []
        token_cnt = 0
        regex_cnt = 0

        token_index: dict[str, list[AliasRow]] = {}
        regex_aliases: list[tuple[AliasRow, re.Pattern]] = []
        phrase_aliases: list[AliasRow] = []

        for r in rows:
            ar = AliasRow(
                brand_id=int(r["brand_id"]) if r["brand_id"] is not None else None,
                family_id=int(r["family_id"]) if r["family_id"] is not None else None,
                variant_id=int(r["variant_id"]) if r["variant_id"] is not None else None,
                match_type=str(r["match_type"]),
                pattern=str(r["pattern"]).strip().lower(),
                weight=int(r["weight"]),
            )
            aliases.append(ar)

            if ar.match_type == "regex":
                regex_cnt += 1
                try:
                    rx = re.compile(ar.pattern)
                    regex_aliases.append((ar, rx))
                except re.error:
                    log.warning("bad regex alias skipped: %r", ar.pattern)
            elif ar.match_type == "phrase":
                phrase_aliases.append(ar)
            else:
                token_cnt += 1
                token_index.setdefault(ar.pattern, []).append(ar)

        self._aliases = aliases
        self._token_index = token_index
        self._regex_aliases = regex_aliases
        self._phrase_aliases = phrase_aliases

        # 2) mappings (variant->family->brand)
        self._family_to_brand = {}
        self._variant_to_family = {}
        self._variant_to_brand = {}

        async with self.pool.acquire() as conn:
            # family -> brand
            try:
                fam_rows = await conn.fetch("SELECT id, brand_id FROM model_families")
                self._family_to_brand = {int(x["id"]): int(x["brand_id"]) for x in fam_rows}
            except Exception as e:
                log.warning("mapping load failed (model_families): %s", e)

            # variant -> family (+ brand через join)
            try:
                var_rows = await conn.fetch(
                    """
                    SELECT mv.id AS variant_id, mv.family_id, mf.brand_id
                    FROM model_variants mv
                    JOIN model_families mf ON mf.id = mv.family_id
                    """
                )
                self._variant_to_family = {int(x["variant_id"]): int(x["family_id"]) for x in var_rows}
                self._variant_to_brand = {int(x["variant_id"]): int(x["brand_id"]) for x in var_rows}
            except Exception as e:
                log.warning("mapping load failed (model_variants): %s", e)

        log.info(
            "classifier loaded: aliases=%s token=%s phrase=%s regex=%s mappings: families=%s variants=%s",
            len(aliases),
            token_cnt,
            len(phrase_aliases),
            regex_cnt,
            len(self._family_to_brand),
            len(self._variant_to_family),
        )

    def classify(self, *, title: str, description: str | None) -> dict[str, Any]:
        text = _norm_text(f"{title or ''} {description or ''}")
        tokens = _tokenize(text)

        # Подробная структура для отладки, чтобы видеть вклад каждого алиаса.
        best = {
            "brand_id": None,
            "family_id": None,
            "variant_id": None,
            "confidence": 0,
            "debug": {"hits": [], "inferred": {}, "scope": "none"},
        }

        brand_scores: dict[int, int] = {}
        family_scores: dict[int, int] = {}
        variant_scores: dict[int, int] = {}
        debug_hits: list[dict[str, Any]] = []

        def add_score(bucket: dict[int, int], key: int | None, weight: int) -> None:
            if key is not None:
                bucket[key] = bucket.get(key, 0) + weight

        def add_hit(a: AliasRow, why: str) -> None:
            add_score(brand_scores, a.brand_id, a.weight)
            add_score(family_scores, a.family_id, a.weight)
            add_score(variant_scores, a.variant_id, a.weight)
            debug_hits.append(
                {
                    "type": a.match_type,
                    "pattern": a.pattern,
                    "w": a.weight,
                    "why": why,
                    "brand_id": a.brand_id,
                    "family_id": a.family_id,
                    "variant_id": a.variant_id,
                }
            )

        # 1) token hits через индекс
        for t in tokens:
            for a in self._token_index.get(t, []):
                add_hit(a, "token")

        # 2) phrase hits (полезно для "think pad", "elite book" и т.п.)
        for a in self._phrase_aliases:
            if a.pattern and a.pattern in text:
                add_hit(a, "phrase")

        # 3) regex hits (обычно их мало)
        for a, rx in self._regex_aliases:
            if rx.search(text):
                add_hit(a, "regex")

        if not any((brand_scores, family_scores, variant_scores)):
            return best

        # Приоритет выбора: variant -> family -> brand, чтобы статистика цены была максимально точной.
        variant_id = max(variant_scores, key=variant_scores.get) if variant_scores else None
        family_id = max(family_scores, key=family_scores.get) if family_scores else None
        brand_id = max(brand_scores, key=brand_scores.get) if brand_scores else None

        inferred: dict[str, Any] = {}

        # Автодостройка variant -> family -> brand и устранение конфликтов.
        if variant_id is not None:
            fam = self._variant_to_family.get(int(variant_id))
            if fam is not None:
                if family_id is None or family_id != fam:
                    family_id = fam
                    inferred["family_id_from_variant"] = fam

        if variant_id is not None:
            b = self._variant_to_brand.get(int(variant_id))
            if b is not None:
                if brand_id is None or brand_id != b:
                    brand_id = b
                    inferred["brand_id_from_variant"] = b

        if family_id is not None:
            b = self._family_to_brand.get(int(family_id))
            if b is not None:
                if brand_id is None or brand_id != b:
                    brand_id = b
                    inferred["brand_id_from_family"] = b

        # Confidence рассчитываем от лучшего найденного уровня.
        scope = "brand"
        best_score = brand_scores.get(brand_id, 0) if brand_id is not None else 0
        if family_id is not None:
            scope = "family"
            best_score = max(best_score, family_scores.get(family_id, 0))
        if variant_id is not None:
            scope = "variant"
            best_score = max(best_score, variant_scores.get(variant_id, 0))

        confidence = min(100, best_score * 5 + (10 if scope == "variant" else (5 if scope == "family" else 0)))

        best.update(
            {
                "brand_id": brand_id,
                "family_id": family_id,
                "variant_id": variant_id,
                "confidence": confidence,
                "debug": {
                    "hits": debug_hits[:40],
                    "scope": scope,
                    "inferred": inferred,
                    "scores": {
                        "brand": dict(sorted(brand_scores.items(), key=lambda x: x[1], reverse=True)[:5]),
                        "family": dict(sorted(family_scores.items(), key=lambda x: x[1], reverse=True)[:5]),
                        "variant": dict(sorted(variant_scores.items(), key=lambda x: x[1], reverse=True)[:5]),
                    },
                },
            }
        )
        log.debug(
            "classify result: conf=%s scope=%s brand=%s family=%s variant=%s title=%r",
            confidence,
            scope,
            brand_id,
            family_id,
            variant_id,
            (title or "")[:120],
        )
        return best
