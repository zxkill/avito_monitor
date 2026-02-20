from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

# Словарь типовых "человеческих" написаний брендов/линеек (кириллица/опечатки).
# Эти подстановки применяются до основного матчинга и резко улучшают recall
# на реальных заголовках Avito вроде "Самсунг", "Aser", "макбук".
_HUMAN_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bсамсунг\b", re.I), "samsung"),
    (re.compile(r"\bасер\b", re.I), "acer"),
    (re.compile(r"\baser\b", re.I), "acer"),
    (re.compile(r"\bэйсер\b", re.I), "acer"),
    (re.compile(r"\bасус\b", re.I), "asus"),
    (re.compile(r"\bленово\b", re.I), "lenovo"),
    (re.compile(r"\bмакбук\b", re.I), "apple macbook"),
    (re.compile(r"\bmacbook\b", re.I), "apple macbook"),
    (re.compile(r"\bинфиникс\b", re.I), "infinix"),
    (re.compile(r"\bнетбук\b", re.I), "netbook"),
    (re.compile(r"\bнэтбук\b", re.I), "netbook"),
)


def _replace_human_spellings(s: str) -> str:
    """Нормализует частые русские написания брендов/моделей и опечатки."""
    out = s or ""
    for rx, repl in _HUMAN_REPLACEMENTS:
        out = rx.sub(repl, out)
    return out



@dataclass(frozen=True)
class AliasRow:
    brand_id: int | None
    family_id: int | None
    variant_id: int | None
    match_type: str  # token|phrase|regex
    pattern: str
    weight: int


@dataclass(frozen=True)
class FamilyRow:
    """Короткое представление семейства моделей из БД."""

    family_id: int
    brand_id: int
    norm: str
    compact: str


def _norm_text(s: str) -> str:
    """Нормализует текст объявления для устойчивого матчинга."""
    s = _replace_human_spellings((s or "").lower())
    s = s.replace("ё", "е")
    # Нормализуем визуально похожие кириллические символы в латиницу.
    s = s.translate(str.maketrans({
        "а": "a", "в": "b", "с": "c", "е": "e", "к": "k",
        "м": "m", "н": "h", "о": "o", "р": "p", "т": "t",
        "у": "y", "х": "x",
    }))
    s = re.sub(r"[\t\r\n]+", " ", s)
    s = s.replace("×", "x")
    s = re.sub(r"[|/\\]+", " ", s)
    s = re.sub(r"[-_]+", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _compact(s: str) -> str:
    """Удаляет разделители, чтобы сравнивать кодовые написания в стиле x61sv/x61-sv."""
    return re.sub(r"[^a-zа-я0-9]+", "", (s or "").lower())


def _tokenize(s: str) -> set[str]:
    """Возвращает расширенный набор токенов для словарного матчинга."""
    s = _norm_text(s)
    tokens = set(re.findall(r"[a-zа-я0-9]+(?:-[a-zа-я0-9]+)?", s))

    compact = _compact(s)
    if compact:
        tokens.add(compact)

    glued = re.findall(r"(?:[a-zа-я]{1,8}\s*\d{2,5}[a-zа-я]{0,4})", s)
    for g in glued:
        tokens.add(g.replace(" ", ""))

    for tok in list(tokens):
        if "-" in tok:
            tokens.update(p for p in tok.split("-") if p)

    return tokens


class ModelClassifier:
    """Классификатор ноутбуков по иерархии brand -> family -> variant."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

        self._token_index: dict[str, list[AliasRow]] = {}
        self._regex_aliases: list[tuple[AliasRow, re.Pattern]] = []
        self._phrase_aliases: list[AliasRow] = []

        self._family_to_brand: dict[int, int] = {}
        self._variant_to_family: dict[int, int] = {}
        self._variant_to_brand: dict[int, int] = {}

        # Отдельный словарь по каноническим данным БД для fallback-распознавания.
        self._brand_norm_to_id: dict[str, int] = {}
        self._family_rows: list[FamilyRow] = []

    async def load(self) -> None:
        sql_aliases = """
        SELECT brand_id, family_id, variant_id, match_type, pattern, COALESCE(weight, 1) AS weight
        FROM model_aliases
        """
        async with self.pool.acquire() as conn:
            alias_rows = await conn.fetch(sql_aliases)

        token_index: dict[str, list[AliasRow]] = {}
        regex_aliases: list[tuple[AliasRow, re.Pattern]] = []
        phrase_aliases: list[AliasRow] = []

        token_cnt = 0
        regex_cnt = 0
        phrase_cnt = 0

        for r in alias_rows:
            ar = AliasRow(
                brand_id=int(r["brand_id"]) if r["brand_id"] is not None else None,
                family_id=int(r["family_id"]) if r["family_id"] is not None else None,
                variant_id=int(r["variant_id"]) if r["variant_id"] is not None else None,
                match_type=str(r["match_type"]),
                pattern=_norm_text(str(r["pattern"]).strip()),
                weight=int(r["weight"]),
            )
            if ar.match_type == "regex":
                regex_cnt += 1
                try:
                    regex_aliases.append((ar, re.compile(ar.pattern)))
                except re.error:
                    log.warning("bad regex alias skipped: %r", ar.pattern)
            elif ar.match_type == "phrase":
                phrase_cnt += 1
                phrase_aliases.append(ar)
            else:
                token_cnt += 1
                token_index.setdefault(ar.pattern, []).append(ar)

        # Загружаем канонические бренды и семейства для fallback без алиасов.
        brand_norm_to_id: dict[str, int] = {}
        family_rows: list[FamilyRow] = []

        self._family_to_brand = {}
        self._variant_to_family = {}
        self._variant_to_brand = {}

        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch("SELECT id, name_norm FROM brands")
                for x in rows:
                    bid = int(x["id"])
                    bnorm = _norm_text(str(x["name_norm"] or "").strip())
                    if bnorm:
                        brand_norm_to_id[bnorm] = bid
                log.info("brands dictionary loaded: %s", len(brand_norm_to_id))
            except Exception as e:
                log.warning("brands dictionary load failed: %s", e)

            try:
                fam_rows = await conn.fetch("SELECT id, brand_id, family_name_norm FROM model_families")
                for x in fam_rows:
                    fid = int(x["id"])
                    bid = int(x["brand_id"])
                    fnorm = _norm_text(str(x["family_name_norm"] or "").strip())
                    if not fnorm:
                        continue
                    family_rows.append(FamilyRow(family_id=fid, brand_id=bid, norm=fnorm, compact=_compact(fnorm)))
                    self._family_to_brand[fid] = bid
                # Более длинные паттерны проверяем раньше, чтобы "aspire es1-111" победил "aspire".
                family_rows.sort(key=lambda x: len(x.compact), reverse=True)
                log.info("families dictionary loaded: %s", len(family_rows))
            except Exception as e:
                log.warning("families dictionary load failed: %s", e)

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

        self._token_index = token_index
        self._regex_aliases = regex_aliases
        self._phrase_aliases = phrase_aliases
        self._brand_norm_to_id = brand_norm_to_id
        self._family_rows = family_rows

        log.info(
            "classifier loaded: token=%s phrase=%s regex=%s brands=%s families=%s variants=%s",
            token_cnt,
            phrase_cnt,
            regex_cnt,
            len(self._brand_norm_to_id),
            len(self._family_rows),
            len(self._variant_to_family),
        )

    def _fallback_brand(self, text: str, tokens: set[str]) -> int | None:
        """Определяет бренд по справочнику brands, даже если в model_aliases нет соответствующего токена."""
        for bnorm, bid in self._brand_norm_to_id.items():
            # Проверяем и токены, и подпоследовательность в тексте — это закрывает
            # случаи вроде "samsung", "sam sung", "honor" в произвольной форме заголовка.
            if bnorm in tokens or bnorm in text or _compact(bnorm) in _compact(text):
                return bid
        return None

    def _fallback_family(self, text: str, compact_text: str, brand_id: int | None) -> int | None:
        """Определяет family_id по каноническому family_name_norm из таблицы model_families."""
        for fam in self._family_rows:
            if brand_id is not None and fam.brand_id != brand_id:
                continue
            if fam.norm in text or (fam.compact and fam.compact in compact_text):
                return fam.family_id
        return None

    def classify(self, *, title: str, description: str | None) -> dict[str, Any]:
        text = _norm_text(f"{title or ''} {description or ''}")
        compact_text = _compact(text)
        tokens = _tokenize(text)

        best: dict[str, Any] = {
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

        # 1) Алиасы token/phrase/regex.
        for t in tokens:
            for a in self._token_index.get(t, []):
                add_hit(a, "token")

        for a in self._phrase_aliases:
            if a.pattern and a.pattern in text:
                add_hit(a, "phrase")

        for a, rx in self._regex_aliases:
            if rx.search(text):
                add_hit(a, "regex")

        # 2) Базовый выбор из словаря алиасов.
        variant_id = max(variant_scores, key=variant_scores.get) if variant_scores else None
        family_id = max(family_scores, key=family_scores.get) if family_scores else None
        brand_id = max(brand_scores, key=brand_scores.get) if brand_scores else None

        inferred: dict[str, Any] = {}

        # 3) Автодостройка и устранение конфликтов по известным связям.
        if variant_id is not None:
            var_family = self._variant_to_family.get(variant_id)
            if var_family is not None and family_id != var_family:
                family_id = var_family
                inferred["family_id_from_variant"] = var_family

            var_brand = self._variant_to_brand.get(variant_id)
            if var_brand is not None and brand_id != var_brand:
                brand_id = var_brand
                inferred["brand_id_from_variant"] = var_brand

        if family_id is not None:
            fam_brand = self._family_to_brand.get(family_id)
            if fam_brand is not None and brand_id != fam_brand:
                brand_id = fam_brand
                inferred["brand_id_from_family"] = fam_brand

        # 4) NEW fallback: если алиасы не сработали или сработали частично,
        # пытаемся добрать бренд/семейство из канонических справочников.
        if brand_id is None:
            fb_brand = self._fallback_brand(text, tokens)
            if fb_brand is not None:
                brand_id = fb_brand
                inferred["brand_id_from_dictionary"] = fb_brand

        if family_id is None:
            fb_family = self._fallback_family(text, compact_text, brand_id)
            if fb_family is not None:
                family_id = fb_family
                inferred["family_id_from_dictionary"] = fb_family
                if brand_id is None:
                    fb_brand = self._family_to_brand.get(fb_family)
                    if fb_brand is not None:
                        brand_id = fb_brand
                        inferred["brand_id_from_dictionary_family"] = fb_brand

        if brand_id is None and family_id is None and variant_id is None:
            # Чтобы легче отлаживать в проде низкий recall, логируем только короткую выжимку.
            log.debug("classify miss: title=%r", (title or "")[:120])
            return best

        scope = "brand"
        base_score = brand_scores.get(brand_id, 0) if brand_id is not None else 0
        if family_id is not None:
            scope = "family"
            base_score = max(base_score, family_scores.get(family_id, 0), 3 if "family_id_from_dictionary" in inferred else 0)
        if variant_id is not None:
            scope = "variant"
            base_score = max(base_score, variant_scores.get(variant_id, 0))

        # Словарные fallback'и дают умеренный confidence, чтобы не замещать точные alias-совпадения.
        if "brand_id_from_dictionary" in inferred:
            base_score = max(base_score, 2)
        if "family_id_from_dictionary" in inferred:
            base_score = max(base_score, 3)

        confidence = min(100, base_score * 5 + (10 if scope == "variant" else (5 if scope == "family" else 0)))

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
            "classify result: conf=%s scope=%s brand=%s family=%s variant=%s title=%r inferred=%s",
            confidence,
            scope,
            brand_id,
            family_id,
            variant_id,
            (title or "")[:120],
            inferred,
        )
        return best
