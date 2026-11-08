from datetime import datetime, timezone
from pathlib import Path
import unittest

from scripts.run_square_slot import account03_slot_hour_matches


ROOT = Path(__file__).resolve().parents[1]


class Account03PublishBoundaryTests(unittest.TestCase):
    def test_authorized_hour_matches_utc_hour(self):
        now = datetime(2026, 11, 8, 17, 42, tzinfo=timezone.utc)
        self.assertTrue(account03_slot_hour_matches("2026-11-08-17", now))

    def test_authorized_hour_rejects_next_utc_hour(self):
        now = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
        self.assertFalse(account03_slot_hour_matches("2026-11-08-17", now))

    def test_authorized_hour_normalizes_non_utc_timezone(self):
        from datetime import timedelta
        now = datetime(2026, 11, 8, 23, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        self.assertTrue(account03_slot_hour_matches("2026-11-08-18", now))

    def test_nonce_is_consumed_after_capture_and_before_reserve_and_publish(self):
        source = (ROOT / "scripts/run_square_slot.py").read_text(encoding="utf-8")
        consume = source.index("ledger_client.consume_slot_authorization")
        reserve = source.index("ledger_client.reserve(_account03_signal_payload(signal))")
        publish = source.index('publisher.publish_image(account, post["caption"], image_path)')
        self.assertLess(consume, reserve)
        self.assertLess(reserve, publish)
        self.assertIn("blocked_slot_hour_expired", source)

    def test_workflow_masks_nonce_early_but_does_not_consume_it_early(self):
        source = (ROOT / ".github/workflows/account03-live-square-hourly.yml").read_text(encoding="utf-8")
        self.assertIn("Mask one-time scheduler authorization", source)
        self.assertNotIn("python -m scripts.consume_account03_slot", source)
        self.assertIn("ACCOUNT03_SLOT_NONCE: ${{ inputs.slot_nonce }}", source)


if __name__ == "__main__":
    unittest.main()
