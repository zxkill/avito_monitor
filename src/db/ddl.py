from __future__ import annotations

import asyncpg

DDL = """
-- =========================
-- BASE TABLES (как было)
-- =========================
CREATE TABLE IF NOT EXISTS searches (
  id BIGSERIAL PRIMARY KEY,
  query TEXT NOT NULL,
  city_slug TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_polled_at TIMESTAMPTZ
);

-- один и тот же запрос в одном городе — один раз
CREATE UNIQUE INDEX IF NOT EXISTS ux_searches_city_query ON searches(city_slug, query);

CREATE TABLE IF NOT EXISTS items (
  id BIGSERIAL PRIMARY KEY,
  search_id BIGINT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,

  external_id TEXT,               -- data-item-id
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  price INTEGER,
  city TEXT,
  description TEXT,
  seller_type TEXT,
  photos_count INTEGER,
  status TEXT NOT NULL DEFAULT 'active',

  raw JSONB NOT NULL DEFAULT '{}'::jsonb,

  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- уникальность объявления в рамках всего хранилища по url
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_url ON items(url);

-- ускоряем "есть ли external_id"
CREATE INDEX IF NOT EXISTS ix_items_external_id ON items(external_id);

-- ускоряем выборку по search_id
CREATE INDEX IF NOT EXISTS ix_items_search_last_seen ON items(search_id, last_seen_at DESC);

ALTER TABLE items ADD COLUMN IF NOT EXISTS reported_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_items_unreported
  ON items(search_id, last_seen_at DESC)
  WHERE reported_at IS NULL;


-- =========================
-- TAXONOMY / DICTIONARY
-- =========================

-- Производители (универсально, пригодится и для других категорий)
CREATE TABLE IF NOT EXISTS brands (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,              -- Canonical: "Lenovo"
  name_norm TEXT NOT NULL,         -- "lenovo" (для быстрых сравнений)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_brands_name_norm ON brands(name_norm);

-- Семейства моделей (ThinkPad T14, Latitude 5420, MacBook Pro 13 и т.д.)
-- category пока текстом (для будущего расширения), сейчас используем "laptop"
CREATE TABLE IF NOT EXISTS model_families (
  id BIGSERIAL PRIMARY KEY,
  category TEXT NOT NULL DEFAULT 'laptop',
  brand_id BIGINT NOT NULL REFERENCES brands(id) ON DELETE RESTRICT,

  family_name TEXT NOT NULL,       -- Canonical: "ThinkPad T14"
  family_name_norm TEXT NOT NULL,  -- "thinkpad t14"

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_model_families_brand_family_norm
  ON model_families(brand_id, family_name_norm);

CREATE INDEX IF NOT EXISTS ix_model_families_category_brand
  ON model_families(category, brand_id);

-- Варианты/поколения/годы внутри семейства (Gen 1, 2021, Rev.A...)
CREATE TABLE IF NOT EXISTS model_variants (
  id BIGSERIAL PRIMARY KEY,
  family_id BIGINT NOT NULL REFERENCES model_families(id) ON DELETE CASCADE,

  variant_name TEXT NOT NULL,       -- Canonical: "ThinkPad T14 Gen 1"
  variant_name_norm TEXT NOT NULL,  -- "thinkpad t14 gen 1"

  -- опциональные атрибуты для удобной фильтрации/статистики
  gen SMALLINT,
  year SMALLINT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_model_variants_family_variant_norm
  ON model_variants(family_id, variant_name_norm);

CREATE INDEX IF NOT EXISTS ix_model_variants_family_gen_year
  ON model_variants(family_id, gen, year);

-- Алиасы/паттерны для распознавания (можно хранить и токены, и regex)
-- match_type: 'token' | 'regex' | 'phrase'
CREATE TABLE IF NOT EXISTS model_aliases (
  id BIGSERIAL PRIMARY KEY,

  brand_id BIGINT REFERENCES brands(id) ON DELETE CASCADE,
  family_id BIGINT REFERENCES model_families(id) ON DELETE CASCADE,
  variant_id BIGINT REFERENCES model_variants(id) ON DELETE CASCADE,

  match_type TEXT NOT NULL DEFAULT 'token',
  pattern TEXT NOT NULL,            -- то, чем матчим (токен/regex/фраза)
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
-- ITEMS: classification + specs + condition
-- =========================

-- Гео (город уже есть, добавляем регион)
ALTER TABLE items ADD COLUMN IF NOT EXISTS region TEXT;

-- Классификация
ALTER TABLE items ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'laptop';

ALTER TABLE items ADD COLUMN IF NOT EXISTS brand_id BIGINT REFERENCES brands(id) ON DELETE SET NULL;
ALTER TABLE items ADD COLUMN IF NOT EXISTS model_family_id BIGINT REFERENCES model_families(id) ON DELETE SET NULL;
ALTER TABLE items ADD COLUMN IF NOT EXISTS model_variant_id BIGINT REFERENCES model_variants(id) ON DELETE SET NULL;

ALTER TABLE items ADD COLUMN IF NOT EXISTS model_confidence REAL;      -- 0..1
ALTER TABLE items ADD COLUMN IF NOT EXISTS model_guess TEXT;           -- если не сматчились со справочником

-- Конфигурация и состояние (под ваш текущий анализ + будущее улучшение)
-- specs: cpu, ram_gb, ssd_gb, hdd_gb, gpu, screen_inch, screen_res, etc.
ALTER TABLE items ADD COLUMN IF NOT EXISTS specs JSONB NOT NULL DEFAULT '{}'::jsonb;

-- condition: нормализованные флаги состояния (например: "no_battery", "broken_screen"...)
ALTER TABLE items ADD COLUMN IF NOT EXISTS condition JSONB NOT NULL DEFAULT '{}'::jsonb;

-- defects: детальная диагностика/сигналы (например, результаты вашего heuristics-анализатора)
ALTER TABLE items ADD COLUMN IF NOT EXISTS defects JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Индексы под типовые запросы:
-- 1) статистика по модели (variant -> last_seen)
CREATE INDEX IF NOT EXISTS ix_items_variant_last_seen
  ON items(model_variant_id, last_seen_at DESC)
  WHERE model_variant_id IS NOT NULL AND price IS NOT NULL;

-- 2) fallback статистики по family
CREATE INDEX IF NOT EXISTS ix_items_family_last_seen
  ON items(model_family_id, last_seen_at DESC)
  WHERE model_family_id IS NOT NULL AND price IS NOT NULL;

-- 3) быстрый фильтр "нераспознанных"
CREATE INDEX IF NOT EXISTS ix_items_unclassified_last_seen
  ON items(last_seen_at DESC)
  WHERE model_variant_id IS NULL AND model_family_id IS NULL;

-- 4) гео-статистика (на будущее)
CREATE INDEX IF NOT EXISTS ix_items_region_city_last_seen
  ON items(region, city, last_seen_at DESC);


-- =========================
-- SEED: базовый словарь ноутбуков (минимальный, расширяемый)
-- =========================

INSERT INTO brands(name, name_norm)
VALUES
  ('Acer', 'acer'),
  ('Asus', 'asus'),
  ('Samsung', 'samsung'),
  ('Toshiba', 'toshiba'),
  ('Honor', 'honor'),
  ('Sharp', 'sharp'),
  ('Packard Bell', 'packard bell'),
  ('Lenovo', 'lenovo'),
  ('Dell', 'dell'),
  ('HP', 'hp')
ON CONFLICT (name_norm) DO NOTHING;

INSERT INTO model_families(category, brand_id, family_name, family_name_norm)
SELECT 'laptop', b.id, x.family_name, x.family_name_norm
FROM (
  VALUES
    ('acer', 'Acer Aspire 5552G', 'acer aspire 5552g'),
    ('acer', 'Acer Aspire 1410', 'acer aspire 1410'),
    ('acer', 'Acer Aspire ES1-111', 'acer aspire es1-111'),
    ('acer', 'Acer Aspire 5315', 'acer aspire 5315'),
    ('acer', 'Acer V3-571G', 'acer v3-571g'),
    ('asus', 'Asus X61SV', 'asus x61sv'),
    ('samsung', 'Samsung R528', 'samsung r528'),
    ('honor', 'Honor MagicBook 14', 'honor magicbook 14')
) AS x(brand_norm, family_name, family_name_norm)
JOIN brands b ON b.name_norm = x.brand_norm
ON CONFLICT (brand_id, family_name_norm) DO NOTHING;

-- Алиасы по брендам: даже если семейство не найдено, бренд будет заполнен.
INSERT INTO model_aliases(brand_id, match_type, pattern, weight)
SELECT b.id, 'token', x.pattern, x.weight
FROM (
  VALUES
    ('acer', 'acer', 3),
    ('asus', 'asus', 3),
    ('samsung', 'samsung', 3),
    ('toshiba', 'toshiba', 3),
    ('honor', 'honor', 3),
    ('sharp', 'sharp', 3),
    ('packard bell', 'packardbell', 3),
    ('packard bell', 'packard-bell', 3)
) AS x(brand_norm, pattern, weight)
JOIN brands b ON b.name_norm = x.brand_norm
WHERE NOT EXISTS (
  SELECT 1 FROM model_aliases ma
  WHERE ma.brand_id = b.id AND ma.match_type = 'token' AND ma.pattern = x.pattern
);

"""


async def ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(DDL)
