from __future__ import annotations

import asyncio
import logging
import random

from aiogram import Bot

from ..avito.client import AvitoClient
from ..db.repo import ItemUpsert, Repo
from ..analysis.classifier import ModelClassifier
from ..analysis.report import build_report_v2

log = logging.getLogger(__name__)


def _build_raw_with_classification(raw: dict | None, cls: dict) -> dict:
    r = dict(raw or {})
    r["category"] = r.get("category") or "laptop"
    r["brand_id"] = cls.get("brand_id")
    r["model_family_id"] = cls.get("family_id")
    r["model_variant_id"] = cls.get("variant_id")
    r["model_confidence"] = cls.get("confidence")
    r["model_debug"] = cls.get("debug")
    return r


async def initial_collect_for_search(
    repo: Repo,
    client: AvitoClient,
    classifier: ModelClassifier,
    search_id: int,
    source: str,
) -> int:
    pages = await client.fetch_pages(source)
    log.info("initial collect: id=%s source=%r pages=%s", search_id, source, len(pages))
    saved = 0

    for page_cards in pages:
        for c in page_cards:
            cls = classifier.classify(title=c.title, description=c.description)
            raw2 = _build_raw_with_classification(c.raw, cls)

            await repo.upsert_item(
                ItemUpsert(
                    search_id=search_id,
                    external_id=c.external_id,
                    url=c.url,
                    title=c.title,
                    price=c.price,
                    city=c.city,
                    description=c.description,
                    seller_type=c.seller_type,
                    photos_count=c.photos_count,
                    status=c.status,
                    raw=raw2,
                )
            )
            saved += 1

    await repo.touch_search_polled(search_id)
    return saved


async def incremental_poll_all(
    repo: Repo,
    client: AvitoClient,
    classifier: ModelClassifier,
    bot: Bot | None = None,
    notify_chat_id: int | None = None,
    between_queries_delay_s: int = 60,
    jitter_s: float = 10.0,
) -> None:
    searches = await repo.list_searches()
    log.info("incremental_poll_all: searches=%s notify_chat_id=%s", len(searches), notify_chat_id)

    for idx, s in enumerate(searches):
        search_id = int(s["id"])
        source = str(s["query"])

        new_items: list[dict] = []
        new_item_ids: list[int] = []

        async with client._make_session() as session:
            for page in range(1, client.cfg.max_pages + 1):
                page_cards = await client.fetch_page_cards_in_session(session, source, page)
                if not page_cards:
                    log.info("stop pagination: empty page_cards: search_id=%s page=%s", search_id, page)
                    break

                exists_flags: list[bool] = []
                for c in page_cards:
                    exists = False
                    if c.external_id:
                        exists = await repo.item_exists_by_external_id(c.external_id)
                    if not exists and c.url:
                        exists = await repo.item_exists_by_url(c.url)
                    exists_flags.append(exists)

                all_new = all(not x for x in exists_flags)
                log.info(
                    "page check: search_id=%s source=%r page=%s page_cards=%s all_new=%s old_found=%s",
                    search_id, source, page, len(page_cards), all_new, any(exists_flags)
                )

                for c, exists in zip(page_cards, exists_flags):
                    if exists:
                        continue

                    cls = classifier.classify(title=c.title, description=c.description)
                    raw2 = _build_raw_with_classification(c.raw, cls)

                    item_id = await repo.upsert_item(
                        ItemUpsert(
                            search_id=search_id,
                            external_id=c.external_id,
                            url=c.url,
                            title=c.title,
                            price=c.price,
                            city=c.city,
                            description=c.description,
                            seller_type=c.seller_type,
                            photos_count=c.photos_count,
                            status=c.status,
                            raw=raw2,
                        )
                    )
                    new_item_ids.append(item_id)
                    new_items.append(
                        {
                            "id": item_id,
                            "url": c.url,
                            "title": c.title,
                            "price": c.price,
                            "city": c.city,
                            "description": c.description,
                        }
                    )

                    log.info(
                        "classified: item_id=%s conf=%s brand=%s family=%s variant=%s title=%r",
                        item_id,
                        cls.get("confidence"),
                        cls.get("brand_id"),
                        cls.get("family_id"),
                        cls.get("variant_id"),
                        (c.title or "")[:80],
                    )

                if not all_new:
                    log.info("stop pagination: found old items on page: search_id=%s page=%s", search_id, page)
                    break

                if page != client.cfg.max_pages:
                    delay = client.cfg.page_delay_s + random.uniform(0.5, 2.0)
                    await asyncio.sleep(delay)

        await repo.touch_search_polled(search_id)

        if new_items and bot and notify_chat_id:
            stats = await repo.get_price_stats(search_id, window=500)
            messages = build_report_v2(source, stats, new_items, top_n=10, score_min=65, profit_min_need=1500)
            for msg in messages:
                await bot.send_message(notify_chat_id, msg, parse_mode="HTML", disable_web_page_preview=True)

            await repo.mark_items_reported(new_item_ids)

        log.info("poll result: search_id=%s source=%r new_total=%s", search_id, source, len(new_items))

        if idx != len(searches) - 1:
            extra = random.uniform(0.0, float(jitter_s)) if jitter_s and jitter_s > 0 else 0.0
            delay = float(between_queries_delay_s) + extra
            log.info("sleep between sources: %.1fs", delay)
            await asyncio.sleep(delay)
