CREATE TABLE publish_attempts (
    reservation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL CHECK (account_id = 'account_03'),
    signal_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL,
    base_asset TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    timeframe TEXT NOT NULL,
    entry_mid REAL NOT NULL CHECK (entry_mid > 0),
    stop_percent REAL NOT NULL CHECK (stop_percent > 0),
    tp1_percent REAL NOT NULL CHECK (tp1_percent > 0),
    tp2_percent REAL NOT NULL CHECK (tp2_percent > 0),
    tp3_percent REAL NOT NULL CHECK (tp3_percent > 0),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    utc_day TEXT NOT NULL,
    hour_bucket TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('publish_pending', 'published', 'publish_uncertain')
    ),
    finalized_at_ms INTEGER
) STRICT;

CREATE UNIQUE INDEX idx_publish_attempts_account_hour
    ON publish_attempts(account_id, hour_bucket);

CREATE INDEX idx_publish_attempts_day_coin
    ON publish_attempts(account_id, utc_day, base_asset);

CREATE INDEX idx_publish_attempts_duplicate
    ON publish_attempts(account_id, base_asset, created_at_ms);

CREATE INDEX idx_publish_attempts_signal
    ON publish_attempts(account_id, signal_id, created_at_ms);
