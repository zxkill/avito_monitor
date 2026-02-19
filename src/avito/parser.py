from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence
from lxml import html
from html import unescape as html_unescape

_PRICE_CLEAN_RE = re.compile(r"[^\d]+")

# --- Встроенные данные (фоллбек), ищем рядом с item_id ---
# В HTML выдачи описание часто встречается как "description":"...."
_DESC_RE = re.compile(r'"description"\s*:\s*"((?:\\.|[^"\\])*)"', re.DOTALL)

# Доставка в выдаче встречается текстом вроде "Доставка от 2 дней"
_DELIVERY_TEXT_RE = re.compile(r'(Доставка[^"<]{0,80})', re.IGNORECASE)
_ONLY_DELIVERY_RE = re.compile(r'Только\s+доставка', re.IGNORECASE)


@dataclass(frozen=True)
class ParsedCard:
    external_id: Optional[str]
    url: str
    title: str
    price: Optional[int]
    city: Optional[str]
    description: Optional[str]
    seller_type: Optional[str]
    photos_count: Optional[int]
    status: str
    raw: dict


def parse_catalog_page(html_text: str) -> list[ParsedCard]:
    doc = html.fromstring(html_text)
    cards = doc.xpath("//div[@data-marker='item' and @data-item-id]")
    out: list[ParsedCard] = []

    for c in cards:
        item_id = (c.get("data-item-id") or "").strip() or None

        title = _norm(_first_text(c.xpath(".//*[@itemprop='name']")) or "")
        if not title:
            title = f"Avito item {item_id or ''}".strip()

        url = _extract_url(c, item_id)
        price = _extract_price(c)

        city = _first_text(c.xpath(".//*[@data-marker='item-location']//text()")) \
               or _first_text(c.xpath(".//*[@data-marker='item-address']//text()"))
        city = _norm(city) if city else None

        # 1) Пытаемся взять описание из DOM (обычно пусто)
        # 1) Описание в выдаче лежит в <meta itemprop="description" content="...">
        desc_attr = c.xpath(".//*[@itemprop='description']/@content")
        description = _norm(desc_attr[0]) if desc_attr else None

        # 1.1) fallback на текст (на случай если когда-то будет не meta)
        if not description:
            description = _norm(_first_text(c.xpath(".//*[@itemprop='description']//text()")) or "") or None

        # 2) Фоллбек: описание/доставка из встроенных данных страницы по item_id
        embedded_desc, delivery_text, delivery_only = _extract_embedded_by_item_id(html_text, item_id)

        if not description:
            description = embedded_desc

        delivery_available = bool(delivery_text) or bool(delivery_only)

        raw = {
            "src": "catalog",
            "delivery_text": delivery_text,
            "delivery_only": bool(delivery_only),
            "delivery_available": bool(delivery_available),
        }

        out.append(
            ParsedCard(
                external_id=item_id,
                url=url,
                title=title,
                price=price,
                city=city,
                description=description,
                seller_type=None,
                photos_count=None,
                status="active",
                raw=raw,
            )
        )
    return out


def _extract_embedded_by_item_id(html_text: str, item_id: Optional[str]) -> tuple[Optional[str], Optional[str], bool]:
    """
    Ищем рядом с item_id небольшой "оконный" кусок текста и пытаемся вытащить:
    - description из JSON-подобного блока
    - delivery_text ("Доставка от ...")
    - delivery_only ("Только доставка")
    """
    if not item_id:
        return None, None, False

    pos = html_text.find(item_id)
    if pos < 0:
        return None, None, False

    # окно: достаточно большое, чтобы захватить payload в выдаче
    start = max(0, pos - 12000)
    end = min(len(html_text), pos + 12000)
    window = html_text[start:end]

    # В выдаче часто: &amp;quot;description&amp;quot;... поэтому делаем двойной unescape:
    # 1) &amp;quot; -> &quot;
    # 2) &quot; -> "
    window_u = html_unescape(html_unescape(window))

    # description
    m = _DESC_RE.search(window_u)
    desc = None
    if m:
        desc = m.group(1)
        # раскодируем \n, \uXXXX и т.п.
        try:
            desc = bytes(desc, "utf-8").decode("unicode_escape")
        except Exception:
            pass
        desc = _norm(desc) or None

    # доставка
    delivery_text = None
    md = _DELIVERY_TEXT_RE.search(window)
    if md:
        delivery_text = _norm(md.group(1)) or None

    delivery_only = bool(_ONLY_DELIVERY_RE.search(window_u))
    return desc, delivery_text, delivery_only


def _extract_price(card) -> Optional[int]:
    meta = card.xpath(".//*[@data-marker='item-price']//*[@itemprop='price' and @content]/@content")
    if meta:
        try:
            return int(meta[0])
        except ValueError:
            pass

    text = _first_text(card.xpath(".//*[@data-marker='item-price']//text()"))
    if not text:
        return None
    digits = _PRICE_CLEAN_RE.sub("", text)
    return int(digits) if digits else None


def _extract_url(card, item_id: Optional[str]) -> str:
    hrefs: Sequence[str] = card.xpath(".//a[@href]/@href")
    if item_id:
        for h in hrefs:
            if h.startswith("/") and item_id in h:
                return "https://www.avito.ru" + h.split("?")[0]
    for h in hrefs:
        if h.startswith("/"):
            return "https://www.avito.ru" + h.split("?")[0]
    return "https://www.avito.ru"


def _first_text(nodes) -> Optional[str]:
    if not nodes:
        return None
    v = nodes[0]
    if isinstance(v, str):
        return v
    try:
        return v.text_content()
    except Exception:
        return None


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").split())
