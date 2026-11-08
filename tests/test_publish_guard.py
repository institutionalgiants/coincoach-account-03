from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.publish_guard import check_publish_guard, record_publish_attempt, record_successful_publish


class PublishGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)
        self.signal = {
            "signal_id": "signal-001", "symbol": "DOGEUSDT", "base_asset": "DOGE",
            "side": "LONG", "timeframe": "1h", "entry_mid": 0.2,
            "stop_percent": 2.0, "tp1_percent": 3.0, "tp2_percent": 5.0, "tp3_percent": 8.0,
        }
        self.ledger = {"days": {}, "published_signals": []}

    def publish(self, signal=None, account="account_03", now=None):
        record_successful_publish(self.ledger, account, signal or self.signal, now=now or self.now)

    def test_same_signal_is_blocked_for_24_hours_even_with_new_id(self):
        self.publish(now=self.now - timedelta(hours=2))
        changed_id = {**self.signal, "signal_id": "signal-002"}
        self.assertEqual(check_publish_guard(self.ledger, "account_03", changed_id, now=self.now), "duplicate_signal_24h")

    def test_duplicate_trade_idea_is_blocked_after_hour_window_but_within_24h(self):
        self.publish()
        changed_id = {**self.signal, "signal_id": "signal-002"}
        later = self.now + timedelta(hours=2)
        self.assertEqual(check_publish_guard(self.ledger, "account_03", changed_id, now=later), "duplicate_signal_24h")

    def test_same_signal_can_return_after_24h_when_still_freshly_eligible(self):
        self.publish()
        later = self.now + timedelta(hours=25)
        new_signal = {**self.signal, "signal_id": "signal-002"}
        self.assertIsNone(check_publish_guard(self.ledger, "account_03", new_signal, now=later))

    def test_only_two_posts_per_coin_per_utc_day(self):
        self.publish(now=self.now - timedelta(hours=4))
        second = {**self.signal, "signal_id": "different-idea", "entry_mid": 0.21}
        self.publish(signal=second, now=self.now - timedelta(hours=2))
        third = {**self.signal, "signal_id": "third-idea", "entry_mid": 0.22}
        self.assertEqual(check_publish_guard(self.ledger, "account_03", third, now=self.now), "coin_daily_limit")

    def test_hourly_limit_is_account_scoped_to_utc_clock_hour(self):
        self.publish()
        other_account = check_publish_guard(self.ledger, "account_04", self.signal, now=self.now + timedelta(minutes=10))
        self.assertIsNone(other_account)
        eth_signal = {**self.signal, "signal_id": "eth-idea", "base_asset": "ETH", "symbol": "ETHUSDT"}
        same_hour = check_publish_guard(self.ledger, "account_03", eth_signal, now=self.now + timedelta(minutes=20))
        self.assertEqual(same_hour, "hourly_limit")
        next_utc_hour = check_publish_guard(self.ledger, "account_03", eth_signal, now=self.now + timedelta(minutes=31))
        self.assertIsNone(next_utc_hour)

    def test_pending_reservation_is_finalized_without_double_counting(self):
        record_publish_attempt(self.ledger, "account_03", self.signal, now=self.now, status="publish_pending")
        record_publish_attempt(self.ledger, "account_03", self.signal, now=self.now, status="published")
        self.assertEqual(len(self.ledger["published_signals"]), 1)
        self.assertEqual(self.ledger["published_signals"][0]["status"], "published")
        self.assertEqual(self.ledger["days"][self.now.date().isoformat()]["account_03"], 1)

    def test_successful_publish_crossing_utc_midnight_moves_daily_counter(self):
        pending_at = datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc)
        accepted_at = datetime(2026, 11, 5, 0, 1, tzinfo=timezone.utc)
        record_publish_attempt(self.ledger, "account_03", self.signal, now=pending_at, status="publish_pending")
        record_successful_publish(self.ledger, "account_03", self.signal, now=accepted_at)
        self.assertEqual(self.ledger["days"]["2026-11-04"]["account_03"], 0)
        self.assertEqual(self.ledger["days"]["2026-11-05"]["account_03"], 1)
        self.assertEqual(self.ledger["published_signals"][0]["hour_bucket"], "2026-11-05T00")

    def test_ambiguous_remote_publish_attempt_is_reserved_conservatively(self):
        record_publish_attempt(self.ledger, "account_03", self.signal, now=self.now, status="publish_uncertain")
        later = self.now + timedelta(hours=2)
        self.assertEqual(
            check_publish_guard(self.ledger, "account_03", {**self.signal, "signal_id": "new-id"}, now=later),
            "duplicate_signal_24h",
        )
        self.assertEqual(self.ledger["days"][self.now.date().isoformat()]["account_03"], 1)

    def test_daily_coin_limit_uses_utc_calendar_day(self):
        self.publish(now=datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc))
        self.publish(signal={**self.signal, "signal_id": "idea-2", "entry_mid": 0.21}, now=datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc))
        next_day = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)
        self.assertIsNone(check_publish_guard(self.ledger, "account_03", self.signal, now=next_day))


if __name__ == "__main__":
    unittest.main()
