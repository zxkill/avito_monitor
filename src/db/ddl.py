from __future__ import annotations

import asyncpg

DDL = """
-- =========================
-- BASE TABLES
-- =========================
CREATE TABLE IF NOT EXISTS searches (
  id BIGSERIAL PRIMARY KEY,
  query TEXT NOT NULL,
  city_slug TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_polled_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_searches_city_query ON searches(city_slug, query);

CREATE TABLE IF NOT EXISTS items (
  id BIGSERIAL PRIMARY KEY,
  search_id BIGINT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,

  external_id TEXT,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  price INTEGER,
  city TEXT,
  region TEXT,
  description TEXT,
  seller_type TEXT,
  photos_count INTEGER,
  status TEXT NOT NULL DEFAULT 'active',

  category TEXT NOT NULL DEFAULT 'laptop',

  brand_id BIGINT REFERENCES brands(id) ON DELETE SET NULL,
  model_family_id BIGINT REFERENCES model_families(id) ON DELETE SET NULL,
  model_variant_id BIGINT REFERENCES model_variants(id) ON DELETE SET NULL,

  model_confidence REAL,         -- 0..1
  model_guess TEXT,
  model_debug JSONB NOT NULL DEFAULT '{}'::jsonb,

  specs JSONB NOT NULL DEFAULT '{}'::jsonb,
  condition JSONB NOT NULL DEFAULT '{}'::jsonb,
  defects JSONB NOT NULL DEFAULT '{}'::jsonb,

  raw JSONB NOT NULL DEFAULT '{}'::jsonb,

  reported_at TIMESTAMPTZ,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_items_url ON items(url);
CREATE INDEX IF NOT EXISTS ix_items_external_id ON items(external_id);
CREATE INDEX IF NOT EXISTS ix_items_search_last_seen ON items(search_id, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS ix_items_unreported
  ON items(search_id, last_seen_at DESC)
  WHERE reported_at IS NULL;

-- =========================
-- TAXONOMY / DICTIONARY
-- =========================

CREATE TABLE IF NOT EXISTS brands (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_brands_name_norm ON brands(name_norm);

CREATE TABLE IF NOT EXISTS model_families (
  id BIGSERIAL PRIMARY KEY,
  category TEXT NOT NULL DEFAULT 'laptop',
  brand_id BIGINT NOT NULL REFERENCES brands(id) ON DELETE RESTRICT,

  family_name TEXT NOT NULL,
  family_name_norm TEXT NOT NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_model_families_brand_family_norm
  ON model_families(brand_id, family_name_norm);

CREATE INDEX IF NOT EXISTS ix_model_families_category_brand
  ON model_families(category, brand_id);

CREATE TABLE IF NOT EXISTS model_variants (
  id BIGSERIAL PRIMARY KEY,
  family_id BIGINT NOT NULL REFERENCES model_families(id) ON DELETE CASCADE,

  variant_name TEXT NOT NULL,
  variant_name_norm TEXT NOT NULL,

  gen SMALLINT,
  year SMALLINT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_model_variants_family_variant_norm
  ON model_variants(family_id, variant_name_norm);

CREATE INDEX IF NOT EXISTS ix_model_variants_family_gen_year
  ON model_variants(family_id, gen, year);

CREATE TABLE IF NOT EXISTS model_aliases (
  id BIGSERIAL PRIMARY KEY,

  brand_id BIGINT REFERENCES brands(id) ON DELETE CASCADE,
  family_id BIGINT REFERENCES model_families(id) ON DELETE CASCADE,
  variant_id BIGINT REFERENCES model_variants(id) ON DELETE CASCADE,

  match_type TEXT NOT NULL DEFAULT 'token',  -- token|phrase|regex
  pattern TEXT NOT NULL,
  weight SMALLINT NOT NULL DEFAULT 1,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT ck_model_alias_target CHECK (
    (variant_id IS NOT NULL) OR (family_id IS NOT NULL) OR (brand_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS ix_model_aliases_variant ON model_aliases(variant_id);
CREATE INDEX IF NOT EXISTS ix_model_aliases_family ON model_aliases(family_id);
CREATE INDEX IF NOT EXISTS ix_model_aliases_brand ON model_aliases(brand_id);

-- =========================
-- ITEMS: indexes for analytics
-- =========================

CREATE INDEX IF NOT EXISTS ix_items_variant_last_seen
  ON items(model_variant_id, last_seen_at DESC)
  WHERE model_variant_id IS NOT NULL AND price IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_items_family_last_seen
  ON items(model_family_id, last_seen_at DESC)
  WHERE model_family_id IS NOT NULL AND price IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_items_unclassified_last_seen
  ON items(last_seen_at DESC)
  WHERE model_variant_id IS NULL AND model_family_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_items_region_city_last_seen
  ON items(region, city, last_seen_at DESC);

"""

async def ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(DDL)