-- Migration: add is_toxicity_checked flag to track which comments have been analyzed
ALTER TABLE player_reviews
  ADD COLUMN IF NOT EXISTS is_toxicity_checked BOOLEAN NOT NULL DEFAULT FALSE;
