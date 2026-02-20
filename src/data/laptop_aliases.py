from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from .laptop_taxonomy import FamilyDef


@dataclass(frozen=True)
class AliasDef:
    target: str            # "brand" | "family" | "variant"
    key: str               # brand_norm OR family_name_norm OR variant_name_norm
    match_type: str        # token|phrase|regex
    pattern: str
    weight: int = 1


# ----------------------------
# Brand aliases (ручные + базовые)
# ----------------------------

def brand_aliases() -> list[AliasDef]:
    return [
        AliasDef("brand", "lenovo", "token", "lenovo", 3),
        AliasDef("brand", "lenovo", "token", "леново", 3),

        AliasDef("brand", "hp", "token", "hp", 3),
        AliasDef("brand", "hp", "token", "hewlett", 2),

        AliasDef("brand", "dell", "token", "dell", 3),

        AliasDef("brand", "asus", "token", "asus", 3),
        AliasDef("brand", "asus", "token", "асус", 3),

        AliasDef("brand", "acer", "token", "acer", 3),
        AliasDef("brand", "acer", "token", "асер", 3),
        AliasDef("brand", "acer", "token", "aser", 2),

        AliasDef("brand", "apple", "token", "apple", 3),
        AliasDef("brand", "apple", "token", "macbook", 4),
        AliasDef("brand", "apple", "token", "макбук", 4),

        AliasDef("brand", "msi", "token", "msi", 3),
        AliasDef("brand", "huawei", "token", "huawei", 3),
        AliasDef("brand", "honor", "token", "honor", 3),
        AliasDef("brand", "xiaomi", "token", "xiaomi", 3),

        AliasDef("brand", "samsung", "token", "samsung", 3),
        AliasDef("brand", "samsung", "token", "самсунг", 4),
    ]


def brand_regex_aliases() -> list[AliasDef]:
    """
    Низкий вес. Задача — подсказать бренд, когда модельный код есть, а бренд не написан явно.
    Осторожно: regex'ы должны быть НЕ агрессивными.
    """
    return [
        # HP: "250 g8", "840 g6", "450 g7", "255 g8"
        AliasDef("brand", "hp", "regex", r"\b(2[45]0|25[05]|24[05])\s*g\d{1,2}\b", 2),
        AliasDef("brand", "hp", "regex", r"\b(4[45]0|4[47]0)\s*g\d{1,2}\b", 2),
        AliasDef("brand", "hp", "regex", r"\b(84\d|85\d)\s*g\d{1,2}\b", 2),

        # Dell: "latitude 54xx/55xx/74xx/75xx", "inspiron 35xx/55xx/75xx"
        AliasDef("brand", "dell", "regex", r"\blatitude\s*(3\d{3}|5\d{3}|7\d{3})\b", 2),
        AliasDef("brand", "dell", "regex", r"\b(inspiron|vostro)\s*\d{4}\b", 2),

        # Lenovo ThinkPad: Txxx / Xxxx (брендовый сигнал)
        AliasDef("brand", "lenovo", "regex", r"\bthinkpad\s*[txelp]\d{2,4}\b", 2),
    ]


# ----------------------------
# Auto family aliases
# ----------------------------

_RX_HP_G = re.compile(r"^hp\s+(?P<series>\d{3})\s+g(?P<gen>\d{1,2})$", re.I)
_RX_HP_ELITEBOOK = re.compile(r"^hp\s+elitebook\s+(?P<series>\d{3})\s+g(?P<gen>\d{1,2})$", re.I)
_RX_HP_PROBOOK = re.compile(r"^hp\s+probook\s+(?P<series>\d{3})\s+g(?P<gen>\d{1,2})$", re.I)

_RX_DELL_LAT = re.compile(r"^dell\s+latitude\s+(?P<num>\d{4})$", re.I)
_RX_DELL_INSP = re.compile(r"^dell\s+inspiron\s+(?P<num>\d{4})$", re.I)
_RX_DELL_VOS = re.compile(r"^dell\s+vostro\s+(?P<num>\d{4})$", re.I)

_RX_TP_T = re.compile(r"^lenovo\s+thinkpad\s+t(?P<num>\d{3,4})$", re.I)
_RX_TP_X = re.compile(r"^lenovo\s+thinkpad\s+x(?P<num>\d{3,4})$", re.I)
_RX_TP_E = re.compile(r"^lenovo\s+thinkpad\s+e(?P<num>\d{3,4})$", re.I)
_RX_TP_L = re.compile(r"^lenovo\s+thinkpad\s+l(?P<num>\d{3,4})$", re.I)
_RX_TP_P = re.compile(r"^lenovo\s+thinkpad\s+p(?P<num>\d{1,3})$", re.I)

_RX_ASUS_VIVO_X = re.compile(r"^asus\s+vivobook\s+x(?P<num>\d{3})$", re.I)
_RX_ASUS_ZEN_UX = re.compile(r"^asus\s+zenbook\s+ux(?P<num>\d{3})$", re.I)
_RX_ASUS_TUF_FX = re.compile(r"^asus\s+tuf\s+gaming\s+fx(?P<num>\d{3})$", re.I)

_RX_ACER_A315 = re.compile(r"^acer\s+aspire\s+a315-(?P<num>\d{2})$", re.I)
_RX_ACER_AN = re.compile(r"^acer\s+nitro\s+5\s+an(?P<num>\d{3})$", re.I)
_RX_ACER_SWIFT = re.compile(r"^acer\s+swift\s+(?P<num>\d{3})$", re.I)


def _uniq(seq: Iterable[AliasDef]) -> list[AliasDef]:
    seen = set()
    out = []
    for a in seq:
        key = (a.target, a.key, a.match_type, a.pattern, a.weight)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def build_family_aliases(families: Sequence[FamilyDef]) -> list[AliasDef]:
    """
    Генерация алиасов (token/phrase) для каждого семейства.
    Важно: алиасы привязываются к family_id (через family_name_norm),
    поэтому коллизии типа "5420" не страшны — это будет именно Dell Latitude 5420.
    """
    out: list[AliasDef] = []

    for f in families:
        norm = f.family_name_norm

        # --- HP: "hp 250 g8" => token: 250g8 / phrase: 250 g8
        m = _RX_HP_G.match(norm)
        if m:
            series = m.group("series")
            gen = m.group("gen")
            out.append(AliasDef("family", norm, "token", f"{series}g{gen}", 9))
            out.append(AliasDef("family", norm, "phrase", f"{series} g{gen}", 10))
            continue

        m = _RX_HP_ELITEBOOK.match(norm)
        if m:
            series = m.group("series")
            gen = m.group("gen")
            out.append(AliasDef("family", norm, "token", f"{series}g{gen}", 9))
            out.append(AliasDef("family", norm, "phrase", f"elitebook {series} g{gen}", 10))
            out.append(AliasDef("family", norm, "phrase", f"{series} g{gen}", 8))
            continue

        m = _RX_HP_PROBOOK.match(norm)
        if m:
            series = m.group("series")
            gen = m.group("gen")
            out.append(AliasDef("family", norm, "token", f"{series}g{gen}", 9))
            out.append(AliasDef("family", norm, "phrase", f"probook {series} g{gen}", 10))
            out.append(AliasDef("family", norm, "phrase", f"{series} g{gen}", 8))
            continue

        # --- Dell
        m = _RX_DELL_LAT.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", num, 8))
            out.append(AliasDef("family", norm, "phrase", f"latitude {num}", 9))
            continue

        m = _RX_DELL_INSP.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", num, 7))
            out.append(AliasDef("family", norm, "phrase", f"inspiron {num}", 9))
            continue

        m = _RX_DELL_VOS.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", num, 7))
            out.append(AliasDef("family", norm, "phrase", f"vostro {num}", 9))
            continue

        # --- ThinkPad
        m = _RX_TP_T.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"t{num}", 9))
            out.append(AliasDef("family", norm, "phrase", f"thinkpad t{num}", 10))
            continue

        m = _RX_TP_X.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"x{num}", 9))
            out.append(AliasDef("family", norm, "phrase", f"thinkpad x{num}", 10))
            continue

        m = _RX_TP_E.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"e{num}", 8))
            out.append(AliasDef("family", norm, "phrase", f"thinkpad e{num}", 9))
            continue

        m = _RX_TP_L.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"l{num}", 8))
            out.append(AliasDef("family", norm, "phrase", f"thinkpad l{num}", 9))
            continue

        m = _RX_TP_P.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"p{num}", 7))
            out.append(AliasDef("family", norm, "phrase", f"thinkpad p{num}", 8))
            continue

        # --- Asus
        m = _RX_ASUS_VIVO_X.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"x{num}", 8))
            out.append(AliasDef("family", norm, "phrase", f"vivobook x{num}", 10))
            continue

        m = _RX_ASUS_ZEN_UX.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"ux{num}", 8))
            out.append(AliasDef("family", norm, "phrase", f"zenbook ux{num}", 10))
            continue

        m = _RX_ASUS_TUF_FX.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"fx{num}", 8))
            out.append(AliasDef("family", norm, "phrase", f"tuf fx{num}", 9))
            continue

        # --- Acer
        m = _RX_ACER_A315.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"a315-{num}", 9))
            out.append(AliasDef("family", norm, "phrase", f"a315-{num}", 10))
            out.append(AliasDef("family", norm, "phrase", f"aspire a315-{num}", 9))
            continue

        m = _RX_ACER_AN.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"an{num}", 10))
            out.append(AliasDef("family", norm, "phrase", f"nitro 5 an{num}", 9))
            out.append(AliasDef("family", norm, "phrase", "nitro 5", 6))
            continue

        m = _RX_ACER_SWIFT.match(norm)
        if m:
            num = m.group("num")
            out.append(AliasDef("family", norm, "token", f"swift{num}", 7))
            out.append(AliasDef("family", norm, "phrase", f"swift {num}", 9))
            continue

        # --- Apple (семейства без кодовых чисел — только фразы)
        if norm.startswith("apple macbook"):
            # чтобы "macbook air 13" вытягивался даже без apple
            out.append(AliasDef("family", norm, "phrase", norm.replace("apple ", ""), 8))

    return _uniq(out)


# ----------------------------
# Variants (ручные, точечные)
# ----------------------------

def variant_aliases() -> list[AliasDef]:
    return [
        AliasDef("variant", "lenovo thinkpad t14 gen 1", "phrase", "t14 gen 1", 10),
        AliasDef("variant", "lenovo thinkpad t14 gen 2", "phrase", "t14 gen 2", 10),
        AliasDef("variant", "lenovo thinkpad t14 gen 3", "phrase", "t14 gen 3", 10),
        AliasDef("variant", "lenovo thinkpad t14 gen 4", "phrase", "t14 gen 4", 10),

        AliasDef("variant", "apple macbook air 13 m1", "phrase", "air m1", 10),
        AliasDef("variant", "apple macbook air 13 m2", "phrase", "air m2", 10),
        AliasDef("variant", "apple macbook air 13 m3", "phrase", "air m3", 10),
    ]