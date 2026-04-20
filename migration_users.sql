-- Migration: Add users table for Faceit OAuth
-- Run this on your Railway Postgres database

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    faceit_id VARCHAR(255) UNIQUE NOT NULL,
    nickname VARCHAR(255) NOT NULL,
    avatar TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_faceit_id ON users(faceit_id);

-- Add reviews table for future feature
CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    reviewer_faceit_id VARCHAR(255) NOT NULL REFERENCES users(faceit_id) ON DELETE CASCADE,
    target_account_id BIGINT NOT NULL,
    rating SMALLINT CHECK (rating >= 1 AND rating <= 5),
    comment TEXT,
    match_room_id VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(reviewer_faceit_id, target_account_id, match_room_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_target ON reviews(target_account_id);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer_faceit_id);
