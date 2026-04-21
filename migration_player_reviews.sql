-- Migration: player_reviews table with comment support
-- Run on Railway Postgres

CREATE TABLE IF NOT EXISTS player_reviews (
    reviewer_faceit_id VARCHAR(255) NOT NULL REFERENCES users(faceit_id) ON DELETE CASCADE,
    target_account_id  BIGINT NOT NULL,
    rating             SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
    comment            TEXT,
    updated_at         TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (reviewer_faceit_id, target_account_id)
);

CREATE INDEX IF NOT EXISTS idx_player_reviews_target ON player_reviews(target_account_id);

-- Safe upgrade if table already exists without comment column
ALTER TABLE player_reviews ADD COLUMN IF NOT EXISTS comment TEXT;
