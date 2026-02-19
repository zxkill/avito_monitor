from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from ..analysis.report import build_report_v2
from ..analysis.classifier import ModelClassifier  # <-- ДОБАВИЛИ
from ..avito.client import AvitoClient
from ..db.repo import Repo
from ..jobs.poller import initial_collect_for_search
from .keyboards import main_kb

log = logging.getLogger(__name__)
router = Router()


def looks_like_url(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith("http://") or t.startswith("https://")


def is_supported_avito_url(url: str) -> bool:
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    if "avito.ru" not in (p.netloc or ""):
        return False
    parts = [x for x in (p.path or "").split("/") if x]
    return len(parts) >= 2


def parse_avito_city_slug(url: str) -> str | None:
    """
    https://www.avito.ru/magnitogorsk/noutbuki?... -> magnitogorsk
    https://www.avito.ru/rossiya/noutbuki?...      -> rossiya
    """
    try:
        p = urlparse(url.strip())
    except Exception:
        return None
    if "avito.ru" not in (p.netloc or ""):
        return None
    parts = [x for x in (p.path or "").split("/") if x]
    if len(parts) < 2:
        return None
    return parts[0]


def fmt_dt(v) -> str:
    if not v:
        return "—"
    try:
        return v.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(v)


async def _run_initial_collect(
    m: Message,
    repo: Repo,
    client: AvitoClient,
    classifier: ModelClassifier,  # <-- ДОБАВИЛИ
    search_id: int,
    source: str,
) -> None:
    try:
        # <-- ВАЖНО: передаём classifier третьим аргументом
        saved = await initial_collect_for_search(repo, client, classifier, search_id, source)
        total = await repo.count_items_for_search(search_id)

        unrep = await repo.list_unreported_items(search_id, limit=50)
        items = [
            {
                "id": int(r["id"]),
                "url": r["url"],
                "title": r["title"],
                "price": r["price"],
                "city": r["city"],
                "description": r["description"],
            }
            for r in unrep
        ]
        stats = await repo.get_price_stats(search_id, window=500)

        messages = build_report_v2(source, stats, items, top_n=10, score_min=65, profit_min_need=1500)
        for msg in messages:
            await m.answer(msg, parse_mode="HTML", disable_web_page_preview=True)

        await repo.mark_items_reported([it["id"] for it in items])
        await m.answer(f"Готово. Собрано: {saved}. Всего по источнику: {total}.", reply_markup=main_kb())
    except Exception as e:
        log.exception("initial collect failed: search_id=%s source=%r", search_id, source)
        await m.answer(f"Ошибка при сборе: {type(e).__name__}: {e}", reply_markup=main_kb())


@router.message(Command("start"))
async def start(m: Message) -> None:
    await m.answer(
        "Команды:\n"
        "/addcat <URL категории> — новый режим (парсинг категории)\n"
        "/addq <текстовый запрос> — старый режим (q=...)\n"
        "/add <URL|текст> — авто-режим\n"
        "/list — список источников (id/source/last_polled_at)\n"
        "/stats — статистика классификации\n"
        "/unknown — последние нераспознанные объявления\n"
        "\nПримеры:\n"
        "/addcat https://www.avito.ru/magnitogorsk/noutbuki?localPriority=0&s=104\n"
        "/addq lenovo thinkpad t480",
        reply_markup=main_kb(),
    )


@router.message(Command("add"))
async def add_auto(m: Message, repo: Repo, client: AvitoClient, classifier: ModelClassifier) -> None:
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование:\n/add <URL категории | текстовый запрос>", reply_markup=main_kb())
        return

    value = parts[1].strip()
    if looks_like_url(value):
        await _add_category(m, repo, client, classifier, value)
    else:
        await _add_query(m, repo, client, classifier, value)


@router.message(Command("addcat"))
async def add_category(m: Message, repo: Repo, client: AvitoClient, classifier: ModelClassifier) -> None:
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer(
            "Использование:\n"
            "/addcat <URL категории>\n"
            "Пример:\n"
            "/addcat https://www.avito.ru/magnitogorsk/noutbuki?localPriority=0&s=104",
            reply_markup=main_kb(),
        )
        return
    url = parts[1].strip()
    await _add_category(m, repo, client, classifier, url)


async def _add_category(m: Message, repo: Repo, client: AvitoClient, classifier: ModelClassifier, url: str) -> None:
    if not looks_like_url(url) or not is_supported_avito_url(url):
        await m.answer("Нужна корректная ссылка avito.ru на категорию/раздел.", reply_markup=main_kb())
        return

    city_slug = parse_avito_city_slug(url) or "url"
    search_id = await repo.create_search(query=url, city_slug=city_slug)

    await m.answer(
        f"Источник добавлен (категория).\n"
        f"id={search_id}\n"
        f"city_slug={city_slug}\n"
        f"{url}\n\nЗапускаю первичный сбор...",
        reply_markup=main_kb(),
    )
    asyncio.create_task(_run_initial_collect(m, repo, client, classifier, search_id, url))


@router.message(Command("addq"))
async def add_query(m: Message, repo: Repo, client: AvitoClient, classifier: ModelClassifier) -> None:
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer(
            "Использование:\n/addq <текстовый запрос>\nПример:\n/addq lenovo thinkpad t480",
            reply_markup=main_kb(),
        )
        return
    query = parts[1].strip()
    await _add_query(m, repo, client, classifier, query)


async def _add_query(m: Message, repo: Repo, client: AvitoClient, classifier: ModelClassifier, query: str) -> None:
    if len(query) < 2:
        await m.answer("Запрос слишком короткий.", reply_markup=main_kb())
        return

    search_id = await repo.create_search(query=query, city_slug=client.cfg.city_slug)
    await m.answer(
        f"Источник добавлен (query).\n"
        f"id={search_id}\n"
        f"city_slug={client.cfg.city_slug}\n"
        f"{query}\n\nЗапускаю первичный сбор...",
        reply_markup=main_kb(),
    )
    asyncio.create_task(_run_initial_collect(m, repo, client, classifier, search_id, query))


@router.message(Command("list"))
async def cmd_list(m: Message, repo: Repo) -> None:
    rows = await repo.list_searches()
    if not rows:
        await m.answer("Источников пока нет. Добавьте: /addcat или /addq", reply_markup=main_kb())
        return

    lines = ["Источники:"]
    for r in rows[:80]:
        sid = int(r["id"])
        source = str(r["query"])
        city_slug = str(r["city_slug"])
        last_polled_at = fmt_dt(r.get("last_polled_at"))
        kind = "URL" if looks_like_url(source) else "QUERY"
        lines.append(f"- #{sid} [{kind}] city={city_slug} polled={last_polled_at}\n  {source}")

    await m.answer("\n".join(lines), reply_markup=main_kb())


@router.message(Command("stats"))
async def cmd_stats(m: Message, repo: Repo) -> None:
    s = await repo.get_classification_stats(category="laptop")
    await m.answer(
        "Статистика (category=laptop):\n"
        f"- всего объявлений: {s['total']}\n"
        f"- с brand_id: {s['with_brand']}\n"
        f"- с model_family_id: {s['with_family']}\n"
        f"- с model_variant_id: {s['with_variant']}\n"
        f"- нераспознано (family NULL): {s['unknown']}",
        reply_markup=main_kb(),
    )


@router.message(Command("unknown"))
async def cmd_unknown(m: Message, repo: Repo) -> None:
    rows = await repo.list_unknown_items(category="laptop", limit=20)
    if not rows:
        await m.answer("Нераспознанных объявлений не найдено.", reply_markup=main_kb())
        return

    lines = ["Последние нераспознанные (model_family_id IS NULL):"]
    for r in rows:
        iid = int(r["id"])
        price = r["price"]
        city = r["city"] or "—"
        title = (r["title"] or "").strip()
        url = r["url"]
        seen = fmt_dt(r["last_seen_at"])
        lines.append(f"- #{iid} price={price} city={city} seen={seen}\n  {title}\n  {url}")

    await m.answer("\n".join(lines), disable_web_page_preview=True, reply_markup=main_kb())


# ---------- callbacks ----------
@router.callback_query(F.data == "cmd:list")
async def cb_list(cq: CallbackQuery, repo: Repo) -> None:
    await cmd_list(cq.message, repo)
    await cq.answer()


@router.callback_query(F.data == "cmd:stats")
async def cb_stats(cq: CallbackQuery, repo: Repo) -> None:
    await cmd_stats(cq.message, repo)
    await cq.answer()


@router.callback_query(F.data == "cmd:unknown")
async def cb_unknown(cq: CallbackQuery, repo: Repo) -> None:
    await cmd_unknown(cq.message, repo)
    await cq.answer()
