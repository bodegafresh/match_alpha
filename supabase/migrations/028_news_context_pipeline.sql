-- Migration 028: news context extraction pipeline
-- Stores article bodies and structured match-level context derived from news.

CREATE TABLE IF NOT EXISTS news_item_documents (
  id_hash text PRIMARY KEY REFERENCES news_items(id_hash) ON DELETE CASCADE,
  title text,
  url text NOT NULL,
  source text,
  body_text text,
  fetch_error text,
  fetched_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_news_item_documents_fetched_at
  ON news_item_documents (fetched_at DESC);

CREATE TABLE IF NOT EXISTS match_news_context_snapshots (
  match_id uuid NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
  context_date date NOT NULL,
  context_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_count integer NOT NULL DEFAULT 0,
  high_confidence_signals integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (match_id, context_date)
);

CREATE INDEX IF NOT EXISTS idx_match_news_context_created_at
  ON match_news_context_snapshots (created_at DESC);

COMMENT ON TABLE news_item_documents IS
  'Cached article body per news item for structured extraction.';

COMMENT ON TABLE match_news_context_snapshots IS
  'Daily structured context by match extracted from multiple news bodies.';
