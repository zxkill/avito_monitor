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
-- SEED: расширенный словарь ноутбуков (массовое покрытие)
-- =========================

-- 1) Бренды: широкий набор производителей, встречающихся на вторичном рынке.
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
  ('HP', 'hp'),
  ('Apple', 'apple'),
  ('MSI', 'msi'),
  ('Huawei', 'huawei'),
  ('Xiaomi', 'xiaomi'),
  ('LG', 'lg'),
  ('Sony', 'sony'),
  ('Fujitsu', 'fujitsu'),
  ('Gigabyte', 'gigabyte'),
  ('Chuwi', 'chuwi'),
  ('Haier', 'haier'),
  ('DNS', 'dns'),
  ('Dexp', 'dexp'),
  ('Irbis', 'irbis'),
  ('Razer', 'razer'),
  ('Microsoft', 'microsoft')
ON CONFLICT (name_norm) DO NOTHING;

-- 2) Семейства: наиболее частые линейки и серии, чтобы резко поднять recall по family.
INSERT INTO model_families(category, brand_id, family_name, family_name_norm)
SELECT 'laptop', b.id, x.family_name, x.family_name_norm
FROM (
  VALUES
    -- Acer
    ('acer', 'Acer Aspire 5552G', 'acer aspire 5552g'),
    ('acer', 'Acer Aspire 1410', 'acer aspire 1410'),
    ('acer', 'Acer Aspire ES1-111', 'acer aspire es1-111'),
    ('acer', 'Acer Aspire 5315', 'acer aspire 5315'),
    ('acer', 'Acer V3-571G', 'acer v3-571g'),
    ('acer', 'Acer Aspire E1-571', 'acer aspire e1-571'),
    ('acer', 'Acer Aspire A315', 'acer aspire a315'),
    ('acer', 'Acer Nitro 5', 'acer nitro 5'),
    ('acer', 'Acer Swift 3', 'acer swift 3'),
    ('acer', 'Acer Predator Helios 300', 'acer predator helios 300'),

    -- Asus
    ('asus', 'Asus X61SV', 'asus x61sv'),
    ('asus', 'Asus K53', 'asus k53'),
    ('asus', 'Asus X550', 'asus x550'),
    ('asus', 'Asus VivoBook 15', 'asus vivobook 15'),
    ('asus', 'Asus TUF Gaming A15', 'asus tuf gaming a15'),
    ('asus', 'Asus ROG Strix G15', 'asus rog strix g15'),
    ('asus', 'Asus ZenBook 14', 'asus zenbook 14'),

    -- Samsung
    ('samsung', 'Samsung R528', 'samsung r528'),
    ('samsung', 'Samsung R540', 'samsung r540'),
    ('samsung', 'Samsung NP300', 'samsung np300'),

    -- Honor / Huawei / Xiaomi
    ('honor', 'Honor MagicBook 14', 'honor magicbook 14'),
    ('honor', 'Honor MagicBook 15', 'honor magicbook 15'),
    ('huawei', 'Huawei MateBook D 14', 'huawei matebook d 14'),
    ('huawei', 'Huawei MateBook D 15', 'huawei matebook d 15'),
    ('xiaomi', 'Xiaomi RedmiBook 15', 'xiaomi redmibook 15'),

    -- Lenovo
    ('lenovo', 'Lenovo IdeaPad 320', 'lenovo ideapad 320'),
    ('lenovo', 'Lenovo IdeaPad 3', 'lenovo ideapad 3'),
    ('lenovo', 'Lenovo IdeaPad 5', 'lenovo ideapad 5'),
    ('lenovo', 'Lenovo Legion 5', 'lenovo legion 5'),
    ('lenovo', 'Lenovo ThinkPad T480', 'lenovo thinkpad t480'),
    ('lenovo', 'Lenovo ThinkPad T14', 'lenovo thinkpad t14'),
    ('lenovo', 'Lenovo ThinkPad X1 Carbon', 'lenovo thinkpad x1 carbon'),
    ('lenovo', 'Lenovo ThinkPad X280', 'lenovo thinkpad x280'),

    -- Dell
    ('dell', 'Dell Latitude 5420', 'dell latitude 5420'),
    ('dell', 'Dell Latitude 5490', 'dell latitude 5490'),
    ('dell', 'Dell Inspiron 15 3000', 'dell inspiron 15 3000'),
    ('dell', 'Dell Inspiron 15 5000', 'dell inspiron 15 5000'),
    ('dell', 'Dell Vostro 15 3500', 'dell vostro 15 3500'),
    ('dell', 'Dell G15', 'dell g15'),
    ('dell', 'Dell XPS 13', 'dell xps 13'),

    -- HP
    ('hp', 'HP 250 G8', 'hp 250 g8'),
    ('hp', 'HP 255 G8', 'hp 255 g8'),
    ('hp', 'HP ProBook 450 G8', 'hp probook 450 g8'),
    ('hp', 'HP EliteBook 840 G5', 'hp elitebook 840 g5'),
    ('hp', 'HP Pavilion 15', 'hp pavilion 15'),
    ('hp', 'HP Omen 15', 'hp omen 15'),

    -- Apple
    ('apple', 'Apple MacBook Air 13', 'apple macbook air 13'),
    ('apple', 'Apple MacBook Pro 13', 'apple macbook pro 13'),
    ('apple', 'Apple MacBook Pro 14', 'apple macbook pro 14'),

    -- MSI
    ('msi', 'MSI GF63 Thin', 'msi gf63 thin'),
    ('msi', 'MSI Katana GF66', 'msi katana gf66'),
    ('msi', 'MSI Modern 14', 'msi modern 14'),

    -- Toshiba / Sony / Fujitsu / LG
    ('toshiba', 'Toshiba Satellite C650', 'toshiba satellite c650'),
    ('toshiba', 'Toshiba Satellite L300', 'toshiba satellite l300'),
    ('sony', 'Sony VAIO VPC', 'sony vaio vpc'),
    ('fujitsu', 'Fujitsu Lifebook A544', 'fujitsu lifebook a544'),
    ('lg', 'LG Gram 14', 'lg gram 14'),

    -- Packard Bell / DNS / Dexp / Irbis / Chuwi / Microsoft
    ('packard bell', 'Packard Bell EasyNote TE', 'packard bell easynote te'),
    ('dns', 'DNS Home 015', 'dns home 015'),
    ('dexp', 'DEXP Atlas H115', 'dexp atlas h115'),
    ('irbis', 'Irbis NB', 'irbis nb'),
    ('chuwi', 'Chuwi HeroBook', 'chuwi herobook'),
    ('microsoft', 'Microsoft Surface Laptop 4', 'microsoft surface laptop 4')
) AS x(brand_norm, family_name, family_name_norm)
JOIN brands b ON b.name_norm = x.brand_norm
ON CONFLICT (brand_id, family_name_norm) DO NOTHING;

-- 3) Варианты: добавляем поколения/ревизии для популярных семейств.
INSERT INTO model_variants(family_id, variant_name, variant_name_norm, gen, year)
SELECT mf.id, x.variant_name, x.variant_name_norm, x.gen, x.year
FROM (
  VALUES
    ('lenovo thinkpad t14', 'Lenovo ThinkPad T14 Gen 1', 'lenovo thinkpad t14 gen 1', 1, 2020),
    ('lenovo thinkpad t14', 'Lenovo ThinkPad T14 Gen 2', 'lenovo thinkpad t14 gen 2', 2, 2021),
    ('lenovo thinkpad x1 carbon', 'Lenovo ThinkPad X1 Carbon Gen 6', 'lenovo thinkpad x1 carbon gen 6', 6, 2018),
    ('lenovo thinkpad x1 carbon', 'Lenovo ThinkPad X1 Carbon Gen 7', 'lenovo thinkpad x1 carbon gen 7', 7, 2019),
    ('dell latitude 5420', 'Dell Latitude 5420 2021', 'dell latitude 5420 2021', NULL, 2021),
    ('hp elitebook 840 g5', 'HP EliteBook 840 G5 2018', 'hp elitebook 840 g5 2018', NULL, 2018),
    ('apple macbook air 13', 'Apple MacBook Air 13 M1', 'apple macbook air 13 m1', NULL, 2020),
    ('apple macbook pro 13', 'Apple MacBook Pro 13 M1', 'apple macbook pro 13 m1', NULL, 2020),
    ('honor magicbook 14', 'Honor MagicBook 14 2021', 'honor magicbook 14 2021', NULL, 2021),
    ('acer nitro 5', 'Acer Nitro 5 AN515', 'acer nitro 5 an515', NULL, NULL)
) AS x(family_name_norm, variant_name, variant_name_norm, gen, year)
JOIN model_families mf ON mf.family_name_norm = x.family_name_norm
ON CONFLICT (family_id, variant_name_norm) DO NOTHING;

-- 4) Базовые алиасы брендов и массовых семейств.
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
    ('packard bell', 'packard-bell', 3),
    ('lenovo', 'lenovo', 3),
    ('dell', 'dell', 3),
    ('hp', 'hp', 3),
    ('apple', 'apple', 3),
    ('huawei', 'huawei', 3),
    ('xiaomi', 'xiaomi', 3),
    ('msi', 'msi', 3),
    ('sony', 'vaio', 3),
    ('microsoft', 'surface', 3)
) AS x(brand_norm, pattern, weight)
JOIN brands b ON b.name_norm = x.brand_norm
WHERE NOT EXISTS (
  SELECT 1 FROM model_aliases ma
  WHERE ma.brand_id = b.id AND ma.match_type = 'token' AND ma.pattern = x.pattern
);

INSERT INTO model_aliases(family_id, match_type, pattern, weight)
SELECT mf.id, 'token', x.pattern, x.weight
FROM (
  VALUES
    ('acer aspire 5552g', '5552g', 8),
    ('acer aspire 1410', '1410', 7),
    ('acer aspire es1-111', 'es1-111', 9),
    ('acer aspire 5315', '5315', 8),
    ('acer v3-571g', 'v3-571g', 9),
    ('asus x61sv', 'x61sv', 9),
    ('samsung r528', 'r528', 9),
    ('honor magicbook 14', 'magicbook14', 8),
    ('lenovo thinkpad t480', 't480', 9),
    ('lenovo thinkpad t14', 't14', 8),
    ('lenovo thinkpad x1 carbon', 'x1carbon', 8),
    ('dell latitude 5420', '5420', 8),
    ('hp elitebook 840 g5', '840g5', 8),
    ('apple macbook air 13', 'air13', 7),
    ('apple macbook pro 13', 'pro13', 7),
    ('acer nitro 5', 'nitro5', 8)
) AS x(family_name_norm, pattern, weight)
JOIN model_families mf ON mf.family_name_norm = x.family_name_norm
WHERE NOT EXISTS (
  SELECT 1 FROM model_aliases ma
  WHERE ma.family_id = mf.id AND ma.match_type = 'token' AND ma.pattern = x.pattern
);

INSERT INTO model_aliases(variant_id, match_type, pattern, weight)
SELECT mv.id, 'phrase', x.pattern, x.weight
FROM (
  VALUES
    ('lenovo thinkpad t14 gen 1', 't14 gen 1', 10),
    ('lenovo thinkpad t14 gen 2', 't14 gen 2', 10),
    ('lenovo thinkpad x1 carbon gen 6', 'x1 carbon gen 6', 10),
    ('lenovo thinkpad x1 carbon gen 7', 'x1 carbon gen 7', 10),
    ('apple macbook air 13 m1', 'air m1', 10),
    ('apple macbook pro 13 m1', 'pro m1', 10),
    ('acer nitro 5 an515', 'an515', 10)
) AS x(variant_name_norm, pattern, weight)
JOIN model_variants mv ON mv.variant_name_norm = x.variant_name_norm
WHERE NOT EXISTS (
  SELECT 1 FROM model_aliases ma
  WHERE ma.variant_id = mv.id AND ma.match_type = 'phrase' AND ma.pattern = x.pattern
);

"""


async def ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(DDL)
