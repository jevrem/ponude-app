-- BUSINESS_PRO schema (PostgreSQL)
-- Run this once on your Render Postgres (or apply migrations accordingly).

CREATE TABLE IF NOT EXISTS clients (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT,
  address TEXT,
  oib TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS offer_counters (
  year INTEGER PRIMARY KEY,
  last_number INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS offers (
  id SERIAL PRIMARY KEY,
  number TEXT NOT NULL UNIQUE,           -- e.g. 01/2026
  year INTEGER NOT NULL,
  seq INTEGER NOT NULL,                  -- numeric sequence within year
  client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'sent',   -- draft/sent/accepted/rejected/expired
  portal_token TEXT NOT NULL UNIQUE,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS offer_items (
  id SERIAL PRIMARY KEY,
  offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
  description TEXT NOT NULL,
  qty NUMERIC(12,3) NOT NULL DEFAULT 1,
  unit_price NUMERIC(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id INTEGER,
  meta JSONB
);

CREATE INDEX IF NOT EXISTS idx_offers_created_at ON offers(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_offers_updated_at'
  ) THEN
    CREATE TRIGGER trg_offers_updated_at
    BEFORE UPDATE ON offers
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;
