import html
from typing import Iterable

TG_MSG_LIMIT = 3900  # безопасный лимит (у Telegram ~4096 символов)

def esc(s: str | None) -> str:
    return html.escape(s or "")

def format_money(x) -> str:
    if x is None:
        return "—"
    try:
        return f"{int(round(float(x))):,}".replace(",", " ")
    except Exception:
        return "—"

def badge_score(score: int) -> str:
    if score >= 85: return "🟢"
    if score >= 70: return "🟡"
    if score >= 55: return "🟠"
    return "🔴"

def badge_profit(pmin, pmax) -> str:
    # pmin/pmax: int|None
    if pmax is None:
        return "⚪"
    if pmax >= 10000 and (pmin or 0) >= 3000:
        return "💎"
    if pmax >= 7000:
        return "🔥"
    if pmax >= 3000:
        return "✅"
    if pmax > 0:
        return "🟡"
    return "⛔"

def badge_price(price, p25, p50, p75) -> str:
    if price is None or p50 is None:
        return "⚪"
    try:
        price = float(price)
        p25 = float(p25) if p25 is not None else None
        p50 = float(p50)
        p75 = float(p75) if p75 is not None else None
    except Exception:
        return "⚪"

    # Логика: чем ниже рынка — тем «зеленее»
    if p25 is not None and price <= p25:
        return "🟢 ниже p25"
    if price <= p50 * 0.90:
        return "🟢 ниже рынка"
    if price <= p50 * 1.05:
        return "🟡 около рынка"
    if p75 is not None and price <= p75:
        return "🟠 выше рынка"
    return "🔴 сильно выше"

def short_url(url: str) -> str:
    # для красоты в тексте (сама ссылка кликабельна через <a>)
    u = url.replace("https://", "").replace("http://", "")
    if len(u) > 60:
        return u[:57] + "…"
    return u

def split_html_messages(parts: Iterable[str], limit: int = TG_MSG_LIMIT) -> list[str]:
    """
    Склеивает куски в несколько сообщений, чтобы не превышать лимит.
    Куски должны быть самостоятельными HTML-фрагментами (без незакрытых тегов).
    """
    out: list[str] = []
    buf = ""
    for p in parts:
        if not p:
            continue
        if len(buf) + len(p) <= limit:
            buf += p
        else:
            if buf:
                out.append(buf)
            # если один кусок сам по себе огромный — режем грубо
            if len(p) > limit:
                out.append(p[:limit])
                rest = p[limit:]
                while rest:
                    out.append(rest[:limit])
                    rest = rest[limit:]
                buf = ""
            else:
                buf = p
    if buf:
        out.append(buf)
    return out
