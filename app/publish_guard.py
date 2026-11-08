from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _as_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _close(left: Any, right: Any, tolerance: float) -> bool:
    try:
        a, b = float(left), float(right)
    except (TypeError, ValueError):
        return False
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1e-12)
    return abs(a - b) <= scale * tolerance


def _same_trade_idea(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if str(previous.get("signal_id", "")) and previous.get("signal_id") == current.get("signal_id"):
        return True
    previous_coin = str(previous.get("base_asset") or str(previous.get("symbol", "")).removesuffix("USDT")).upper()
    current_coin = str(current.get("base_asset") or str(current.get("symbol", "")).removesuffix("USDT")).upper()
    if not previous_coin or previous_coin != current_coin:
        return False
    if str(previous.get("side", "")).upper() != str(current.get("side", "")).upper():
        return False
    if str(previous.get("timeframe", "")).lower() != str(current.get("timeframe", "")).lower():
        return False
    if not _close(previous.get("entry_mid"), current.get("entry_mid"), 0.01):
        return False
    return all(
        _close(previous.get(key), current.get(key), 0.20)
        for key in ("stop_percent", "tp1_percent", "tp2_percent", "tp3_percent")
    )


def check_publish_guard(
    ledger: dict[str, Any],
    account_id: str,
    signal: dict[str, Any],
    *,
    now: datetime | None = None,
    duplicate_hours: int = 24,
    max_coin_posts_per_utc_day: int = 2,
    min_interval_minutes: int = 0,
) -> str | None:
    """Return a block reason, or None when the signal is eligible to publish.

    Enforces a UTC-hour bucket, a rolling duplicate-signal window, and a per-coin
    UTC-calendar-day cap. An optional rolling interval can add a stricter throttle.
    """
    current = _as_utc(now or datetime.now(timezone.utc))
    assert current is not None
    rows = ledger.get("published_signals", [])
    if not isinstance(rows, list):
        rows = []

    hour_bucket = current.strftime("%Y-%m-%dT%H")
    min_interval_cutoff = current - timedelta(minutes=max(1, int(min_interval_minutes)))
    duplicate_cutoff = current - timedelta(hours=max(1, int(duplicate_hours)))
    coin = str(signal.get("base_asset") or str(signal.get("symbol", "")).removesuffix("USDT")).upper()
    day_key = current.date().isoformat()
    day_count = 0

    for row in rows:
        if not isinstance(row, dict) or str(row.get("account_id", "")) != account_id:
            continue
        published_at = _as_utc(row.get("published_at"))
        if published_at is None:
            continue
        if min_interval_minutes > 0 and published_at >= min_interval_cutoff:
            return "hourly_limit"
        if str(row.get("hour_bucket", "")) == hour_bucket:
            return "hourly_limit"
        row_coin = str(row.get("base_asset") or str(row.get("symbol", "")).removesuffix("USDT")).upper()
        if row_coin == coin and published_at.date().isoformat() == day_key:
            day_count += 1
        if published_at > duplicate_cutoff and _same_trade_idea(row, signal):
            return "duplicate_signal_24h"

    if day_count >= max(0, int(max_coin_posts_per_utc_day)):
        return "coin_daily_limit"
    return None


def record_publish_attempt(
    ledger: dict[str, Any],
    account_id: str,
    signal: dict[str, Any],
    *,
    now: datetime | None = None,
    status: str = "published",
) -> None:
    """Reserve a publish attempt; uncertain remote results are blocked conservatively."""
    current = _as_utc(now or datetime.now(timezone.utc))
    assert current is not None
    hour_bucket = current.strftime("%Y-%m-%dT%H")
    signal_id = str(signal.get("signal_id", ""))
    rows = ledger.setdefault("published_signals", [])
    if not isinstance(rows, list):
        rows = []
        ledger["published_signals"] = rows

    # A pending reservation is finalized in place after Binance responds. This
    # preserves the quota guard even if the runner is interrupted mid-request.
    for existing in reversed(rows):
        if (
            isinstance(existing, dict)
            and str(existing.get("account_id", "")) == account_id
            and signal_id
            and str(existing.get("signal_id", "")) == signal_id
            and str(existing.get("status", "")) == "publish_pending"
        ):
            # Attribute a confirmed post to the UTC hour/day when the platform
            # accepted it, even when image upload crossed a clock boundary.
            if status == "published":
                previous_time = _as_utc(existing.get("published_at"))
                previous_day = previous_time.date().isoformat() if previous_time else current.date().isoformat()
                current_day = current.date().isoformat()
                if previous_day != current_day:
                    days = ledger.setdefault("days", {})
                    previous_counts = days.setdefault(previous_day, {})
                    previous_counts[account_id] = max(0, int(previous_counts.get(account_id, 0)) - 1)
                    current_counts = days.setdefault(current_day, {})
                    current_counts[account_id] = int(current_counts.get(account_id, 0)) + 1
                existing["hour_bucket"] = hour_bucket
            existing["status"] = status
            existing["published_at"] = current.isoformat()
            return

    days = ledger.setdefault("days", {})
    day = days.setdefault(current.date().isoformat(), {})
    day[account_id] = int(day.get(account_id, 0)) + 1
    row = {
        "account_id": account_id,
        "signal_id": str(signal.get("signal_id", "")),
        "symbol": str(signal.get("symbol", "")),
        "base_asset": str(signal.get("base_asset", "")).upper(),
        "side": str(signal.get("side", "")).upper(),
        "timeframe": str(signal.get("timeframe", "")),
        "entry_mid": signal.get("entry_mid"),
        "stop_percent": signal.get("stop_percent"),
        "tp1_percent": signal.get("tp1_percent"),
        "tp2_percent": signal.get("tp2_percent"),
        "tp3_percent": signal.get("tp3_percent"),
        "published_at": current.isoformat(),
        "hour_bucket": hour_bucket,
        "status": status,
    }
    rows.append(row)
    retention_cutoff = current - timedelta(days=7)
    ledger["published_signals"] = [
        item for item in rows
        if isinstance(item, dict) and (_as_utc(item.get("published_at")) or current) >= retention_cutoff
    ]
    for old_day in sorted(days)[:-7]:
        days.pop(old_day, None)


def record_successful_publish(
    ledger: dict[str, Any],
    account_id: str,
    signal: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    record_publish_attempt(ledger, account_id, signal, now=now, status="published")
