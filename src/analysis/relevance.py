from __future__ import annotations

import re
from dataclasses import dataclass

_STOPWORDS = {
    "lenovo", "thinkpad", "ibm", "ноутбук", "ноут", "laptop", "core", "intel", "amd",
    "i3", "i5", "i7", "i9", "ryzen", "ram", "ssd", "hdd", "gb", "tb", "fhd", "ips",
}

# ThinkPad model code examples:
# t480, t480s, t14, t14s, p43s, x1, x1c, x270, x280, e14, l14, w540, etc.
# Детектор "буква + цифры + опциональный суффикс".
# Важно: матчим только латинские буквы, но перед этим нормализуем "похожие" кириллические.
_MODEL_CODE_RE = re.compile(r"\b([a-z])\s*-?\s*(\d{2,4})([a-z]{0,2})\b", re.I)

# Похожие символы кириллицы, которые часто подменяют латиницу (t/x/p/c/a/e/o/k/m/y/b/h).
# Это не транслит, а "visual confusables".
_CONFUSABLES_MAP = str.maketrans(
    {
        # lower
        "а": "a",
        "в": "b",
        "с": "c",
        "е": "e",
        "н": "h",
        "к": "k",
        "м": "m",
        "о": "o",
        "р": "p",
        "т": "t",
        "у": "y",
        "х": "x",
        # upper
        "А": "a",
        "В": "b",
        "С": "c",
        "Е": "e",
        "Н": "h",
        "К": "k",
        "М": "m",
        "О": "o",
        "Р": "p",
        "Т": "t",
        "У": "y",
        "Х": "x",
        # also: ё -> е (частая нормализация)
        "ё": "e",
        "Ё": "e",
    }
)

# Gen parsing:
# "Gen 1", "Gen1", "T14 G1", "1st gen"
_GEN_RE = re.compile(r"\bgen\s*[-:]?\s*(\d{1,2})\b", re.I)
_G_TOK_RE = re.compile(r"\bg\s*(\d{1,2})\b", re.I)
_ORD_GEN_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s*gen\b", re.I)


@dataclass(frozen=True)
class RelevanceResult:
    ok: bool
    reason: str


def normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("ё", "е")
    # подчистим мусор
    s = re.sub(r"[^\w\s\-]+", " ", s, flags=re.U)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_for_models(s: str) -> str:
    """
    Нормализация специально под поиск кодов моделей:
    - lower
    - замена кириллических "похожих" букв на латиницу
    - удаление лишнего мусора (оставляем word/space/hyphen)
    """
    s = (s or "").strip()
    if not s:
        return ""
    s = s.translate(_CONFUSABLES_MAP)
    s = s.lower()
    s = re.sub(r"[^\w\s\-]+", " ", s, flags=re.U)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_primary_model_code(query: str) -> str | None:
    """
    Для ThinkPad запросов обычно содержит код модели: t480, p52, x1 carbon и т.п.
    Возвращаем "t480"/"p43s" и т.д.

    Важно: поддерживаем "t490" набранное русской "т" (и др. confusables).
    """
    q = normalize_for_models(query)

    m = _MODEL_CODE_RE.search(q)
    if not m:
        return None

    letter = m.group(1).lower()
    digits = m.group(2)
    suffix = (m.group(3) or "").lower()
    return f"{letter}{digits}{suffix}"


def extract_generation(text: str) -> int | None:
    """
    Возвращает поколение ThinkPad Gen (1..n), если явно указано.
    Поддержка: 'Gen 1', 'Gen1', '1st gen', 'G1' (как отдельный токен).
    """
    t = normalize_for_models(text)

    m = _GEN_RE.search(t)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None

    m = _ORD_GEN_RE.search(t)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None

    # "g1" встречается в заголовках как отдельный токен: "t14 g1"
    m = _G_TOK_RE.search(t)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None

    return None


def extract_all_model_codes(text: str) -> set[str]:
    """
    Извлекаем все коды моделей из текста.
    Важно: работаем по normalize_for_models(), чтобы ловить кириллицу вида "т490".

    Дополнительно:
    - режем ложные срабатывания вида "1920 x 1200" -> "x1200"
    """
    t = normalize_for_models(text)
    codes: set[str] = set()

    # типовые высоты/ширины для разрешений, которые часто дают ложное "x####"
    _RES_DIM = {
        "720", "768", "800", "900", "1024", "1080", "1100", "1200", "1280",
        "1440", "1600", "1800", "2000", "2160",
    }

    for m in _MODEL_CODE_RE.finditer(t):
        letter = m.group(1).lower()
        digits = m.group(2)
        suffix = (m.group(3) or "").lower()
        code = f"{letter}{digits}{suffix}"

        # отсекаем мусор типа i7 (не модель)
        if code in {"i3", "i5", "i7", "i9"}:
            continue

        # ---- анти-ложные срабатывания для разрешений вида "1920 x 1200" ----
        # Регэксп ловит "x 1200" как модель "x1200". Это не модель, а часть разрешения.
        if letter == "x" and suffix == "" and digits in _RES_DIM:
            start = m.start()
            left_ctx = t[max(0, start - 16):start]  # достаточно, чтобы увидеть "1920 x "
            if re.search(r"\b\d{3,4}\s*$", left_ctx) or re.search(r"\b\d{3,4}\s*x\s*$", left_ctx):
                continue
            # также бывает "1920x1200" без пробелов
            if re.search(r"\b\d{3,4}$", left_ctx):
                continue

        codes.add(code)

    return codes


def _soft_token_match(query: str, text: str) -> bool:
    """
    Если в query нет кода модели — используем мягкое совпадение по токенам.
    """
    q_tokens = [t for t in normalize_text(query).split() if t not in _STOPWORDS and len(t) >= 3]
    t = normalize_text(text)
    hit = sum(1 for tok in set(q_tokens) if tok in t)
    return hit >= 2


def _contains_code(text: str, code: str) -> bool:
    """
    Проверяем наличие кода модели в тексте, учитывая пробелы/дефисы:
    t480 / t 480 / t-480 / т480 (за счет normalize_for_models)
    """
    t = normalize_for_models(text)
    if not t:
        return False

    # прямое
    if code in t.replace(" ", ""):
        return True

    # компактный вариант без пробелов/дефисов
    tcompact = re.sub(r"[\s\-]+", "", t)
    return code in tcompact


def is_relevant_for_query(*, query: str, title: str, description: str | None) -> RelevanceResult:
    """
    Улучшенная фильтрация:
    - primary код модели извлекается из query (с поддержкой кириллицы t/т и др. похожих букв)
    - поиск кода делаем и в title, и в description
    - конфликтующие коды оцениваем отдельно для title и description:
        * если title содержит primary и НЕ содержит конфликты -> OK (даже если description "шумит")
        * если title содержит конфликты -> reject
        * если title не содержит primary, но description содержит -> OK (мягкий матч)

    Дополнительно:
    - если в query указан Gen (например "T14 Gen1") — исключаем лоты с другим Gen (например "Gen 5")
      (если у лота Gen не указан — пропускаем, но это поведение можно сделать строгим в 1 строку)
    """
    primary = extract_primary_model_code(query)
    text_all = f"{title or ''} {description or ''}".strip()

    # --- GEN gating (если в запросе есть поколение) ---
    q_gen = extract_generation(query)
    if q_gen is not None:
        title_gen = extract_generation(title or "")
        desc_gen = extract_generation(description or "")
        item_gen = title_gen or desc_gen  # приоритет заголовка

        if item_gen is not None and item_gen != q_gen:
            return RelevanceResult(
                False,
                f"skip: generation mismatch (query gen={q_gen}, item gen={item_gen})",
            )

        # Строгий режим (если хотите выкидывать лоты без указанного gen):
        # if item_gen is None:
        #     return RelevanceResult(False, f"skip: generation unknown (query gen={q_gen})")

    if not primary:
        if _soft_token_match(query, text_all):
            return RelevanceResult(True, "ok: token-match (no model code in query)")
        return RelevanceResult(False, "skip: no model code and weak token match")

    title = title or ""
    description = description or ""

    title_has_primary = _contains_code(title, primary)
    desc_has_primary = _contains_code(description, primary)

    if not title_has_primary and not desc_has_primary:
        return RelevanceResult(False, f"skip: missing primary model code '{primary}' in title/description")

    # Извлекаем коды отдельно (это критично, чтобы не убиваться о мусорный description)
    title_codes = extract_all_model_codes(title)
    desc_codes = extract_all_model_codes(description)

    title_other = {c for c in title_codes if c != primary}
    desc_other = {c for c in desc_codes if c != primary}

    # 1) Если primary есть в TITLE — считаем это сильным сигналом.
    #    Конфликты в TITLE — реальная проблема (часто "T480/T490", "T480 + L480" и т.п.)
    if title_has_primary:
        if title_other:
            return RelevanceResult(False, f"skip: other model codes in title: {sorted(title_other)[:6]}")
        if desc_other:
            return RelevanceResult(True, f"ok: primary in title, ignore noisy description codes: {sorted(desc_other)[:6]}")
        return RelevanceResult(True, "ok: strict title match")

    # 2) Если primary нет в TITLE, но есть в DESCRIPTION — это слабее,
    #    но полезно пропускать, чтобы не терять хорошие лоты.
    #    Тут конфликты в DESCRIPTION уже существенны → если есть другие коды, лучше reject.
    if desc_has_primary:
        if desc_other:
            return RelevanceResult(False, f"skip: other model codes in description: {sorted(desc_other)[:6]}")
        return RelevanceResult(True, "ok: primary found in description (title missing)")

    return RelevanceResult(False, "skip: unreachable")
