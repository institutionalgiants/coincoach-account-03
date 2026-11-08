CREATE TABLE slot_authorizations (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL CHECK (account_id = 'account_03'),
    hour_bucket TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms > created_at_ms),
    consumed_at_ms INTEGER,
    UNIQUE (account_id, hour_bucket)
) STRICT;

CREATE INDEX idx_slot_authorizations_expiry
    ON slot_authorizations(expires_at_ms);
