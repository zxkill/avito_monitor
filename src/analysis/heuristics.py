from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Decision:
    score: int
    profit_min: int | None
    profit_max: int | None
    reasons: list[str]


# --- Контекст поломок: объект + признаки неисправности рядом ---
_DEFECT_WORDS = re.compile(
    r"\b("
    r"не\s*работа|не\s*включа|не\s*запуска|не\s*груз|"
    r"слом|глюч|тормоз|завис|выключа|перезагруз|"
    r"трещин|разбит|бит(ый|ая|ые)|скол|"
    r"полос(ы|а)|пятн|мерца|"
    r"люфт|болта|"
    r"не\s*держит|быстро\s*сад|"
    r"замен(а|ить)|ремонт|под\s*ремонт"
    r")\b",
    re.I,
)

def _has_defect_near(text: str, obj_rx: re.Pattern, window: int = 48) -> bool:
    """
    Ищем упоминание объекта (экран/клава/акб...) и рядом (±window символов)
    слова дефекта. Это резко снижает ложные срабатывания на “характеристики”.
    """
    for m in obj_rx.finditer(text):
        a = max(0, m.start() - window)
        b = min(len(text), m.end() + window)
        chunk = text[a:b]
        if _DEFECT_WORDS.search(chunk):
            return True
    return False


# Объект -> вилка затрат -> label
_REPAIR_OBJECTS = [
    (re.compile(r"\b(клавиатур|кнопк|тачпад)\b", re.I), (800, 2500), "клавиатура/ввод"),
    (re.compile(r"\b(акб|аккум|батаре)\b", re.I), (1200, 4500), "АКБ"),
    (re.compile(r"\b(петл|креплени|корпус)\b", re.I), (500, 3000), "петли/корпус"),

    # экран: учитываем только при явных признаках дефекта рядом
    (re.compile(r"\b(экран|матриц|диспле)\b", re.I), (2500, 9000), "экран/матрица"),

    (re.compile(r"\b(кулер|вентилятор|шумит|перегрев)\b", re.I), (500, 2500), "охлаждение"),

    # накопитель: “ssd/hdd” сам по себе НЕ дефект; дефект только если рядом "не видит/умер/битый"
    (re.compile(r"\b(ssd|hdd|жестк(ий|ого)\s*диск|диск)\b", re.I), (1500, 6000), "накопитель"),

    (re.compile(r"\b(зарядк|блок\s*питан)\b", re.I), (600, 2500), "зарядка/БП"),
    (re.compile(r"\b(не\s*включа|не\s*запуска|не\s*груз)\b", re.I), (500, 7000), "не включается/не грузится"),
    (re.compile(r"\b(на\s*запчаст|донор|под\s*ремонт)\b", re.I), (0, 8000), "под ремонт/донор"),
]

# Красные флаги — сильный риск
_RED_FLAGS = [
    #(re.compile(r"\b(утоплен|после воды|корроз)\b", re.I), "вода/коррозия"),
    #(re.compile(r"\b(bios пароль|bios password|mdm|locked)\b", re.I), "блокировки (BIOS/MDM)"),
    #(re.compile(r"\b(плата|материнк|сгорел|кз|коротк)\b", re.I), "плата/КЗ/сгорел"),
    #(re.compile(r"\b(только доставка)\b", re.I), "только доставка"),
]


def analyze_lot(
    *,
    title: str,
    description: str | None,
    price: int | None,
    market_p50: int | None,
    market_p25: int | None,
    market_p75: int | None,
) -> Decision:
    text = (title or "") + " " + (description or "")
    text = text.strip()

    reasons: list[str] = []

    if price is None or market_p50 is None or market_p50 <= 0:
        return Decision(score=0, profit_min=None, profit_max=None, reasons=["недостаточно данных (цена/рынок)"])

    # 1) Дисконт относительно рынка
    discount_ratio = (market_p50 - price) / market_p50
    score = 0

    if discount_ratio >= 0.45:
        score += 40
        reasons.append("сильно ниже рынка (≥45%)")
    elif discount_ratio >= 0.30:
        score += 25
        reasons.append("ниже рынка (30–45%)")
    elif discount_ratio >= 0.20:
        score += 15
        reasons.append("ниже рынка (20–30%)")
    elif discount_ratio >= 0.10:
        score += 5
        reasons.append("слегка ниже рынка (10–20%)")

    # 2) Ремонтопригодные подсказки
    cost_min, cost_max = 800, 4000  # базовая вилка “на мелкий ремонт”
    found_repairs = 0
    found_repairs = 0
    for obj_rx, (cmin, cmax), label in _REPAIR_OBJECTS:
        # “донор/под ремонт/не включается” — это уже дефект-сигнал, можно считать без near-check
        is_always_defect = bool(
            re.search(r"(не\s*включа|не\s*запуска|не\s*груз|донор|под\s*ремонт|на\s*запчаст)", obj_rx.pattern, re.I))

        ok = False
        if is_always_defect:
            ok = bool(obj_rx.search(text))
        else:
            ok = _has_defect_near(text, obj_rx, window=52)

        if ok:
            found_repairs += 1
            cost_min = min(cost_min, cmin)
            cost_max = max(cost_max, cmax)
            if found_repairs <= 2:
                reasons.append(f"намёк на ремонт: {label}")

    if found_repairs >= 1:
        score += 12

    # 3) Красные флаги
    red = 0
    for rx, label in _RED_FLAGS:
        if rx.search(text):
            red += 1
            reasons.append(f"риск: {label}")
    score -= red * 18

    # 4) “аномально дешево” — часто мусор/обман → штраф, но не выкидываем
    if market_p25 and price < int(market_p25 * 0.6):
        score -= 10
        reasons.append("аномально дёшево (проверить вручную)")

    # 5) прибыльная вилка: (рынок - цена - ремонт - прочие расходы)
    misc = 700  # логистика/время/расходники
    profit_min = market_p50 - price - cost_max - misc
    profit_max = market_p50 - price - cost_min - misc

    # нормализация score
    if score < 0:
        score = 0
    if score > 100:
        score = 100

    return Decision(score=score, profit_min=profit_min, profit_max=profit_max, reasons=reasons[:5])
