from __future__ import annotations

from .report_fmt import (
    esc, format_money, badge_score, badge_profit, badge_price, split_html_messages
)
from .heuristics import analyze_lot


def format_money(v: int | None) -> str:
    if v is None:
        return "—"
    return f"{v:,}".replace(",", " ")

def build_report(query: str, stats: dict, items: list[dict], *, top_n: int = 10, score_min: int = 65, profit_min_need: int = 1500) -> str:
    p25 = stats.get("p25")
    p50 = stats.get("p50")
    p75 = stats.get("p75")
    n = stats.get("n", 0)

    header = (
        f"📊 <b>Отчёт по запросу:</b> {esc(query)}\n\n"
        f"<pre>"
        f"Рынок (n={n})\n"
        f"p25: {format_money(p25)} ₽\n"
        f"p50: {format_money(p50)} ₽\n"
        f"p75: {format_money(p75)} ₽"
        f"</pre>\n"
        f"🆕 Новых лотов: <b>{len(items)}</b>\n"
    )

    if not items:
        return header + "\nНовых объявлений нет."

    scored = []
    for it in items:
        dec = analyze_lot(
            title=it["title"],
            description=it.get("description"),
            price=it.get("price"),
            market_p50=p50,
            market_p25=p25,
            market_p75=p75,
        )
        scored.append((dec, it))

    candidates = [
        (dec, it) for dec, it in scored
        if dec.profit_max is not None and dec.score >= score_min and dec.profit_max >= profit_min_need
    ]

    candidates.sort(key=lambda x: (x[0].score, x[0].profit_max or -10**9), reverse=True)

    if not candidates:
        scored.sort(key=lambda x: (x[0].score, x[0].profit_max or -10**9), reverse=True)
        show = scored[:top_n]
        title_block = "\n⚠ <b>Подходящих по порогам нет</b>\nТоп новых по score:\n"
    else:
        show = candidates[:top_n]
        title_block = f"\n✅ <b>Кандидаты (top {len(show)})</b>\n"

    lines = [header, title_block]

    for dec, it in show:
        reasons = "; ".join(dec.reasons) if dec.reasons else "—"

        block = (
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"⭐ <b>Score:</b> {dec.score}\n"
            f"💰 <b>Цена:</b> {format_money(it.get('price'))} ₽\n"
            f"📊 <b>Рынок (p50):</b> {format_money(p50)} ₽\n"
            f"📈 <b>Профит:</b> {format_money(dec.profit_min)} .. {format_money(dec.profit_max)} ₽\n"
            f"🧩 <b>Причины:</b> {esc(reasons)}\n"
            f"🔗 <a href=\"{esc(it['url'])}\">Открыть объявление</a>\n"
        )
        lines.append(block)

    return "".join(lines)

def build_report_v2(
    query: str,
    stats: dict,
    items: list[dict],
    *,
    top_n: int = 10,
    score_min: int = 65,
    profit_min_need: int = 1500,
) -> list[str]:
    """
    v2: возвращает список HTML-сообщений.
    Сигнатура совместима с build_report (v1), чтобы можно было легко переключать.
    """
    p25 = stats.get("p25")
    p50 = stats.get("p50")
    p75 = stats.get("p75")
    n = stats.get("n", 0)

    header = (
        f"📊 <b>Отчёт по запросу:</b> {esc(query)}\n"
        f"<pre>"
        f"Рынок (окно): n={n}\n"
        f"p25: {format_money(p25)} ₽\n"
        f"p50: {format_money(p50)} ₽\n"
        f"p75: {format_money(p75)} ₽"
        f"</pre>\n"
        f"🆕 Новых лотов: <b>{len(items)}</b>\n"
    )

    if not items:
        return [header + "\nНовых объявлений нет."]

    # 1) считаем решения по каждому лоту
    scored: list[tuple] = []
    for it in items:
        dec = analyze_lot(
            title=it.get("title") or "",
            description=it.get("description"),
            price=it.get("price"),
            market_p50=p50,
            market_p25=p25,
            market_p75=p75,
        )
        scored.append((dec, it))

    # 2) кандидаты по порогам
    candidates = [
        (dec, it) for dec, it in scored
        if dec.profit_max is not None and dec.score >= score_min and dec.profit_max >= profit_min_need
    ]
    candidates.sort(key=lambda x: (x[0].score, (x[0].profit_max or -10**9)), reverse=True)

    # 3) если кандидатов нет — показываем топ новых по score
    if candidates:
        show = candidates[:top_n]
        title_block = f"\n✅ <b>Кандидаты (top {len(show)})</b>\n"
    else:
        scored.sort(key=lambda x: (x[0].score, (x[0].profit_max or -10**9)), reverse=True)
        show = scored[:top_n]
        title_block = "\n⚠ <b>Кандидатов по порогам нет</b>\nТоп новых по score:\n"

    parts: list[str] = [header, title_block]
    parts.append("────────────────────\n")
    parts.append("Легенда: 💎/🔥/✅ — профит, 🟢🟡🟠🔴 — score, 📌 — цена относительно рынка.\n\n")

    for idx, (dec, it) in enumerate(show, start=1):
        price = it.get("price")
        url = it.get("url") or ""
        title = it.get("title") or ""
        city = it.get("city") or it.get("location") or ""
        reasons = "; ".join(dec.reasons) if getattr(dec, "reasons", None) else "—"

        s_badge = badge_score(int(dec.score or 0))
        p_badge = badge_profit(dec.profit_min, dec.profit_max)
        pr_badge = badge_price(price, p25, p50, p75)

        parts.append(f"{idx}) {p_badge} {s_badge} <b>{esc(title)}</b>\n")
        parts.append(f"💰 <b>{format_money(price)} ₽</b> · 📌 {esc(pr_badge)} · 📍 {esc(city)}\n" if city else f"💰 <b>{format_money(price)} ₽</b> · 📌 {esc(pr_badge)}\n")
        parts.append(f"📊 p50: <b>{format_money(p50)} ₽</b>\n")
        parts.append(f"📈 Профит: <b>{format_money(dec.profit_min)} .. {format_money(dec.profit_max)} ₽</b>\n")
        parts.append(f"🧩 {esc(reasons)}\n")
        parts.append(f"🔗 <a href=\"{esc(url)}\">Открыть объявление</a>\n\n")

    return split_html_messages(parts)
