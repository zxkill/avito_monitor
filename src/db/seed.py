from __future__ import annotations

import logging
from typing import Iterable

import asyncpg

log = logging.getLogger(__name__)


async def upsert_brand(conn: asyncpg.Connection, *, name: str, name_norm: str) -> int:
    sql = """
    INSERT INTO brands(name, name_norm)
    VALUES ($1, $2)
    ON CONFLICT (name_norm) DO UPDATE SET name = EXCLUDED.name
    RETURNING id
    """
    bid = await conn.fetchval(sql, name, name_norm)
    return int(bid)


async def upsert_family(
    conn: asyncpg.Connection,
    *,
    category: str,
    brand_id: int,
    family_name: str,
    family_name_norm: str,
) -> int:
    sql = """
    INSERT INTO model_families(category, brand_id, family_name, family_name_norm)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (brand_id, family_name_norm) DO UPDATE
      SET family_name = EXCLUDED.family_name
    RETURNING id
    """
    fid = await conn.fetchval(sql, category, brand_id, family_name, family_name_norm)
    return int(fid)


async def upsert_variant(
    conn: asyncpg.Connection,
    *,
    family_id: int,
    variant_name: str,
    variant_name_norm: str,
    gen: int | None,
    year: int | None,
) -> int:
    sql = """
    INSERT INTO model_variants(family_id, variant_name, variant_name_norm, gen, year)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (family_id, variant_name_norm) DO UPDATE
      SET variant_name = EXCLUDED.variant_name,
          gen = EXCLUDED.gen,
          year = EXCLUDED.year
    RETURNING id
    """
    vid = await conn.fetchval(sql, family_id, variant_name, variant_name_norm, gen, year)
    return int(vid)


async def insert_aliases_bulk(
    conn: asyncpg.Connection,
    *,
    rows: Iterable[tuple[int | None, int | None, int | None, str, str, int]],
) -> int:
    """
    rows: (brand_id, family_id, variant_id, match_type, pattern, weight)
    """
    data = list(rows)
    if not data:
        return 0

    sql = """
    INSERT INTO model_aliases(brand_id, family_id, variant_id, match_type, pattern, weight)
    SELECT x.brand_id, x.family_id, x.variant_id, x.match_type, x.pattern, x.weight
    FROM UNNEST($1::bigint[], $2::bigint[], $3::bigint[], $4::text[], $5::text[], $6::smallint[])
      AS x(brand_id, family_id, variant_id, match_type, pattern, weight)
    WHERE NOT EXISTS (
      SELECT 1
      FROM model_aliases ma
      WHERE COALESCE(ma.brand_id, 0) = COALESCE(x.brand_id, 0)
        AND COALESCE(ma.family_id, 0) = COALESCE(x.family_id, 0)
        AND COALESCE(ma.variant_id, 0) = COALESCE(x.variant_id, 0)
        AND ma.match_type = x.match_type
        AND ma.pattern = x.pattern
    )
    """

    b_ids: list[int | None] = []
    f_ids: list[int | None] = []
    v_ids: list[int | None] = []
    mtypes: list[str] = []
    patterns: list[str] = []
    weights: list[int] = []

    for b, f, v, mt, p, w in data:
        b_ids.append(b)
        f_ids.append(f)
        v_ids.append(v)
        mtypes.append(mt)
        patterns.append(p)
        weights.append(int(w))

    await conn.execute(sql, b_ids, f_ids, v_ids, mtypes, patterns, weights)
    return len(data)