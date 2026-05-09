-- Migration: toxicity detection + player ban
-- Run on Railway/Supabase Postgres

ALTER TABLE player_reviews
  ADD COLUMN IF NOT EXISTS is_toxic BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS is_toxic_override BOOLEAN DEFAULT NULL;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS banned_at TIMESTAMP DEFAULT NULL;
