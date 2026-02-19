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
    s = (s or "").lower()
    s = s.replace("ё", "е")
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

    # основной набор токенов
    tokens = set(re.findall(r"[a-zа-я0-9]+(?:-[a-zа-я0-9]+)?", s))

    # компактная форма (thinkpad t14 -> thinkpadt14)
    compact = s.replace(" ", "")
    if compact:
        tokens.add(compact)

    # доп. токены: склейка букв+цифр, если где-то были разделители
    # пример: "t 14" -> "t14", "54 20" -> "5420"
    glued = re.findall(r"(?:[a-zа-я]{1,5}\s*\d{2,5}[a-zа-я]{0,3})", s)
    for g in glued:
        tokens.add(g.replace(" ", ""))

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
            else:
                token_cnt += 1
                token_index.setdefault(ar.pattern, []).append(ar)

        self._aliases = aliases
        self._token_index = token_index
        self._regex_aliases = regex_aliases

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
            "classifier loaded: aliases=%s token=%s regex=%s mappings: families=%s variants=%s",
            len(aliases),
            token_cnt,
            regex_cnt,
            len(self._family_to_brand),
            len(self._variant_to_family),
        )

    def classify(self, *, title: str, description: str | None) -> dict[str, Any]:
        text = _norm_text(f"{title or ''} {description or ''}")
        tokens = _tokenize(text)

        best = {
            "brand_id": None,
            "family_id": None,
            "variant_id": None,
            "confidence": 0,
            "debug": {"hits": [], "inferred": {}},
        }

        scores: dict[tuple[int | None, int | None, int | None], int] = {}
        hits: dict[tuple[int | None, int | None, int | None], list[dict]] = {}

        def add_hit(a: AliasRow, why: str) -> None:
            k = (a.brand_id, a.family_id, a.variant_id)
            scores[k] = scores.get(k, 0) + a.weight
            hits.setdefault(k, []).append({"type": a.match_type, "pattern": a.pattern, "w": a.weight, "why": why})

        # 1) token hits через индекс
        for t in tokens:
            for a in self._token_index.get(t, []):
                add_hit(a, "token")

        # 2) regex hits (обычно их мало)
        for a, rx in self._regex_aliases:
            if rx.search(text):
                add_hit(a, "regex")

        if not scores:
            return best

        def rank_key(k: tuple[int | None, int | None, int | None]) -> tuple[int, int]:
            score = scores[k]
            _, fam, var = k
            specificity = 3 if var is not None else (2 if fam is not None else 1)
            return (score, specificity)

        winner = max(scores.keys(), key=rank_key)
        w_score, w_spec = rank_key(winner)

        brand_id, family_id, variant_id = winner

        inferred: dict[str, Any] = {}

        # --- автодостройка: variant -> family
        if variant_id is not None and family_id is None:
            fam = self._variant_to_family.get(int(variant_id))
            if fam is not None:
                family_id = fam
                inferred["family_id_from_variant"] = fam

        # --- автодостройка: variant/family -> brand
        if brand_id is None and variant_id is not None:
            b = self._variant_to_brand.get(int(variant_id))
            if b is not None:
                brand_id = b
                inferred["brand_id_from_variant"] = b

        if brand_id is None and family_id is not None:
            b = self._family_to_brand.get(int(family_id))
            if b is not None:
                brand_id = b
                inferred["brand_id_from_family"] = b

        # confidence
        confidence = min(100, w_score * 5 + (10 if w_spec == 3 else (5 if w_spec == 2 else 0)))

        best.update(
            {
                "brand_id": brand_id,
                "family_id": family_id,
                "variant_id": variant_id,
                "confidence": confidence,
                "debug": {"hits": hits[winner], "score": w_score, "spec": w_spec, "inferred": inferred},
            }
        )
        return best
