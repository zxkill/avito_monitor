from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import asyncpg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ItemUpsert:
    search_id: int
    external_id: Optional[str]
    url: str
    title: str
    price: Optional[int]
    city: Optional[str]
    description: Optional[str]
    seller_type: Optional[str]
    photos_count: Optional[int]
    status: str
    raw: dict[str, Any]


class Repo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    def _jsonb(self, d: dict | None) -> str:
        return json.dumps(d or {}, ensure_ascii=False)

    async def create_search(self, *, query: str, city_slug: str) -> int:
        sql = """
        INSERT INTO searches (query, city_slug)
        VALUES ($1, $2)
        ON CONFLICT (city_slug, query) DO UPDATE SET query=EXCLUDED.query
        RETURNING id
        """
        async with self.pool.acquire() as conn:
            sid = await conn.fetchval(sql, query, city_slug)
            return int(sid)

    async def list_searches(self) -> Sequence[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM searches ORDER BY id")

    async def touch_search_polled(self, search_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE searches SET last_polled_at=now() WHERE id=$1", search_id)

    async def item_exists_by_external_id(self, external_id: str) -> bool:
        if not external_id:
            return False
        async with self.pool.acquire() as conn:
            v = await conn.fetchval("SELECT 1 FROM items WHERE external_id=$1 LIMIT 1", external_id)
            return v is not None

    async def item_exists_by_url(self, url: str) -> bool:
        async with self.pool.acquire() as conn:
            v = await conn.fetchval("SELECT 1 FROM items WHERE url=$1 LIMIT 1", url)
            return v is not None

    async def upsert_item(self, item: ItemUpsert) -> int:
        sql = """
              INSERT INTO items (search_id, external_id, url, title, price, city, description, seller_type, \
                                 photos_count, status, raw, \
                                 category, brand_id, model_family_id, model_variant_id, model_confidence, model_debug)
              VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, \
                      $12, $13, $14, $15, $16, $17::jsonb) ON CONFLICT (url) DO \
              UPDATE SET
                  search_id=EXCLUDED.search_id, \
                  external_id= COALESCE (EXCLUDED.external_id, items.external_id), \
                  title=EXCLUDED.title, \
                  price=EXCLUDED.price, \
                  city=EXCLUDED.city, \
                  description=EXCLUDED.description, \
                  seller_type=EXCLUDED.seller_type, \
                  photos_count=EXCLUDED.photos_count, \
                  status=EXCLUDED.status, \
                  raw=EXCLUDED.raw, \
                  category=EXCLUDED.category, \
                  brand_id=EXCLUDED.brand_id, \
                  model_family_id=EXCLUDED.model_family_id, \
                  model_variant_id=EXCLUDED.model_variant_id, \
                  model_confidence=EXCLUDED.model_confidence, \
                  model_debug=EXCLUDED.model_debug, \
                  last_seen_at=now() \
                  RETURNING id \
              """
        async with self.pool.acquire() as conn:
            item_id = await conn.fetchval(
                sql,
                item.search_id,
                item.external_id,
                item.url,
                item.title,
                item.price,
                item.city,
                item.description,
                item.seller_type,
                item.photos_count,
                item.status,
                self._jsonb(item.raw),
                item.raw.get("category", "laptop") if isinstance(item.raw, dict) else "laptop",
                item.raw.get("brand_id") if isinstance(item.raw, dict) else None,
                item.raw.get("model_family_id") if isinstance(item.raw, dict) else None,
                item.raw.get("model_variant_id") if isinstance(item.raw, dict) else None,
                item.raw.get("model_confidence") if isinstance(item.raw, dict) else None,
                self._jsonb(item.raw.get("model_debug") if isinstance(item.raw, dict) else None),
            )
            return int(item_id)

    async def count_items_for_search(self, search_id: int) -> int:
        async with self.pool.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM items WHERE search_id=$1", search_id))

    async def get_price_stats(self, search_id: int, *, window: int = 500) -> dict:
        """
        Возвращает общую статистику по поиску.
        Используется как fallback, когда классификация лота не определена.
        """
        sql = """
        WITH t AS (
          SELECT price
          FROM items
          WHERE search_id=$1 AND price IS NOT NULL
          ORDER BY last_seen_at DESC
          LIMIT $2
        )
        SELECT
          COUNT(*)::int AS n,
          percentile_cont(0.25) WITHIN GROUP (ORDER BY price) AS p25,
          percentile_cont(0.50) WITHIN GROUP (ORDER BY price) AS p50,
          percentile_cont(0.75) WITHIN GROUP (ORDER BY price) AS p75
        FROM t
        """
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow(sql, search_id, window)
            return {
                "n": int(r["n"] or 0),
                "p25": int(r["p25"]) if r["p25"] is not None else None,
                "p50": int(r["p50"]) if r["p50"] is not None else None,
                "p75": int(r["p75"]) if r["p75"] is not None else None,
            }

    async def get_price_stats_for_item(self, *, item_id: int, search_id: int, window: int = 500) -> dict:
        """
        Возвращает рыночные перцентили для конкретного лота по приоритету:
        1) model_variant_id
        2) model_family_id
        3) fallback на весь search_id
        """
        sql = """
        WITH base AS (
          SELECT model_variant_id, model_family_id
          FROM items
          WHERE id = $1
          LIMIT 1
        ), sel AS (
          SELECT
            CASE
              WHEN b.model_variant_id IS NOT NULL THEN 'variant'
              WHEN b.model_family_id IS NOT NULL THEN 'family'
              ELSE 'search'
            END AS scope,
            b.model_variant_id,
            b.model_family_id
          FROM base b
        ), target AS (
          SELECT i.price
          FROM items i
          CROSS JOIN sel s
          WHERE i.search_id = $2
            AND i.price IS NOT NULL
            AND (
              (s.model_variant_id IS NOT NULL AND i.model_variant_id = s.model_variant_id)
              OR (
                s.model_variant_id IS NULL
                AND s.model_family_id IS NOT NULL
                AND i.model_family_id = s.model_family_id
              )
              OR (
                s.model_variant_id IS NULL
                AND s.model_family_id IS NULL
              )
            )
          ORDER BY i.last_seen_at DESC
          LIMIT $3
        )
        SELECT
          (SELECT scope FROM sel LIMIT 1) AS scope,
          COUNT(*)::int AS n,
          percentile_cont(0.25) WITHIN GROUP (ORDER BY price) AS p25,
          percentile_cont(0.50) WITHIN GROUP (ORDER BY price) AS p50,
          percentile_cont(0.75) WITHIN GROUP (ORDER BY price) AS p75
        FROM target
        """
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow(sql, item_id, search_id, window)

        stats = {
            "scope": str(r["scope"] or "search"),
            "n": int(r["n"] or 0),
            "p25": int(r["p25"]) if r["p25"] is not None else None,
            "p50": int(r["p50"]) if r["p50"] is not None else None,
            "p75": int(r["p75"]) if r["p75"] is not None else None,
        }
        log.debug("price stats for item: item_id=%s search_id=%s stats=%s", item_id, search_id, stats)
        return stats

    async def list_unreported_items(self, search_id: int, *, limit: int = 50) -> Sequence[asyncpg.Record]:
        sql = """
        SELECT id, url, title, price, city, description, external_id, raw, first_seen_at
        FROM items
        WHERE search_id=$1 AND reported_at IS NULL
        ORDER BY first_seen_at DESC
        LIMIT $2
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, search_id, limit)

    async def mark_items_reported(self, item_ids: list[int]) -> None:
        if not item_ids:
            return
        sql = "UPDATE items SET reported_at=now() WHERE id = ANY($1::bigint[])"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, item_ids)

    # --------- NEW: classification stats ----------
    async def get_classification_stats(self, *, category: str = "laptop") -> dict:
        """
        Требует, чтобы в items были добавлены поля:
        category, brand_id, model_family_id, model_variant_id
        """
        sql = """
        SELECT
          COUNT(*)::int AS total,
          COUNT(*) FILTER (WHERE brand_id IS NOT NULL)::int AS with_brand,
          COUNT(*) FILTER (WHERE model_family_id IS NOT NULL)::int AS with_family,
          COUNT(*) FILTER (WHERE model_variant_id IS NOT NULL)::int AS with_variant,
          COUNT(*) FILTER (WHERE model_family_id IS NULL)::int AS unknown
        FROM items
        WHERE category = $1
        """
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow(sql, category)
            return {
                "total": int(r["total"] or 0),
                "with_brand": int(r["with_brand"] or 0),
                "with_family": int(r["with_family"] or 0),
                "with_variant": int(r["with_variant"] or 0),
                "unknown": int(r["unknown"] or 0),
            }

    # --------- NEW: unknown items ----------
    async def list_unknown_items(self, *, category: str = "laptop", limit: int = 20) -> Sequence[asyncpg.Record]:
        sql = """
        SELECT id, url, title, price, city, last_seen_at
        FROM items
        WHERE category = $1
          AND brand_id IS NULL
        ORDER BY last_seen_at DESC
        LIMIT $2
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, category, limit)

    async def list_unclassified_items(
        self,
        *,
        category: str = "laptop",
        limit: int = 200,
        with_description: bool = False,
    ) -> Sequence[asyncpg.Record]:
        """
        Нераспознанные лоты: когда не определили ни family, ни variant.
        Полезно для системного пополнения словаря.
        """
        fields = "id, url, title, price, city, last_seen_at"
        if with_description:
            fields += ", description"

        sql = f"""
        SELECT {fields}
        FROM items
        WHERE category = $1
          AND model_family_id IS NULL
          AND model_variant_id IS NULL
        ORDER BY last_seen_at DESC
        LIMIT $2
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, category, limit)
