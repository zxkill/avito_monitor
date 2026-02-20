from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..db.pool import create_pool
from ..db.ddl import ensure_schema
from ..db.seed import upsert_brand, upsert_family, upsert_variant, insert_aliases_bulk
from ..data import laptop_taxonomy, laptop_aliases


log = logging.getLogger(__name__)


async def seed() -> None:
    s = Settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO))
    log.info("seed taxonomy: start")

    pool = await create_pool(s.pg_dsn)
    try:
        async with pool.acquire() as conn:
            await ensure_schema(conn)

            # 1) brands
            brand_id_by_norm: dict[str, int] = {}
            for b in laptop_taxonomy.brands():
                bid = await upsert_brand(conn, name=b.name, name_norm=b.name_norm)
                brand_id_by_norm[b.name_norm] = bid
            log.info("seed taxonomy: brands=%s", len(brand_id_by_norm))

            # 2) families (генератор 1000+)
            families_list = laptop_taxonomy.families()
            family_id_by_norm: dict[str, int] = {}

            for i, f in enumerate(families_list, start=1):
                bid = brand_id_by_norm.get(f.brand_norm)
                if not bid:
                    continue
                fid = await upsert_family(
                    conn,
                    category="laptop",
                    brand_id=bid,
                    family_name=f.family_name,
                    family_name_norm=f.family_name_norm,
                )
                family_id_by_norm[f.family_name_norm] = fid

                if i % 500 == 0:
                    log.info("seed taxonomy: families upsert progress=%s", i)

            log.info("seed taxonomy: families_total=%s", len(family_id_by_norm))

            # 3) variants
            variant_id_by_norm: dict[str, int] = {}
            for v in laptop_taxonomy.variants():
                fid = family_id_by_norm.get(v.family_name_norm)
                if not fid:
                    continue
                vid = await upsert_variant(
                    conn,
                    family_id=fid,
                    variant_name=v.variant_name,
                    variant_name_norm=v.variant_name_norm,
                    gen=v.gen,
                    year=v.year,
                )
                variant_id_by_norm[v.variant_name_norm] = vid
            log.info("seed taxonomy: variants_total=%s", len(variant_id_by_norm))

            # 4) aliases (bulk)
            alias_rows = []

            # 4.1) brand token aliases
            for a in laptop_aliases.brand_aliases():
                bid = brand_id_by_norm.get(a.key)
                if bid:
                    alias_rows.append((bid, None, None, a.match_type, a.pattern, a.weight))

            # 4.2) brand regex aliases (низкий вес)
            for a in laptop_aliases.brand_regex_aliases():
                bid = brand_id_by_norm.get(a.key)
                if bid:
                    alias_rows.append((bid, None, None, a.match_type, a.pattern, a.weight))

            # 4.3) family aliases auto-generated for ALL families
            auto_family_aliases = laptop_aliases.build_family_aliases(families_list)
            fam_added = 0
            for a in auto_family_aliases:
                fid = family_id_by_norm.get(a.key)
                if fid:
                    alias_rows.append((None, fid, None, a.match_type, a.pattern, a.weight))
                    fam_added += 1
            log.info("seed taxonomy: auto_family_aliases=%s (resolved=%s)", len(auto_family_aliases), fam_added)

            # 4.4) variant aliases
            var_added = 0
            for a in laptop_aliases.variant_aliases():
                vid = variant_id_by_norm.get(a.key)
                if vid:
                    alias_rows.append((None, None, vid, a.match_type, a.pattern, a.weight))
                    var_added += 1
            log.info("seed taxonomy: variant_aliases_resolved=%s", var_added)

            inserted = await insert_aliases_bulk(conn, rows=alias_rows)
            log.info("seed taxonomy: aliases_candidate=%s inserted_attempt=%s", len(alias_rows), inserted)

            log.info("seed taxonomy: done")
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()