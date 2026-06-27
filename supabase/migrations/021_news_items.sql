-- Migration 021: news_items
-- Caché central de noticias. GAS pushea vía POST /api/v1/web/news/ingest.
-- AI adjuster y frontend leen de aquí en lugar de fetch RSS en vivo.

CREATE TABLE IF NOT EXISTS news_items (
  id_hash    text        PRIMARY KEY,                 -- SHA1(title+url) — viene de GAS
  match_id   uuid        REFERENCES matches(match_id) ON DELETE SET NULL,
  home_team  text        NOT NULL,
  away_team  text        NOT NULL,
  title      text        NOT NULL,
  url        text        NOT NULL,
  source     text        NOT NULL DEFAULT 'Google News RSS',
  pub_date   timestamptz,
  fetched_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_news_items_match_id  ON news_items (match_id);
CREATE INDEX IF NOT EXISTS idx_news_items_pub_date  ON news_items (pub_date DESC);
CREATE INDEX IF NOT EXISTS idx_news_items_fetched_at ON news_items (fetched_at DESC);

COMMENT ON TABLE news_items IS
  'Noticias sincronizadas desde GAS world_cup_2026. '
  'Push diario via POST /api/v1/web/news/ingest con internal key.';
