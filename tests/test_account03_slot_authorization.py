from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Account03SlotAuthorizationTests(unittest.TestCase):
    def test_workflow_requires_nonce_and_has_no_public_confirmation_bypass(self):
        workflow = (ROOT / ".github/workflows/account03-live-square-hourly.yml").read_text(encoding="utf-8")
        self.assertIn("slot_nonce:", workflow)
        self.assertIn("Mask one-time scheduler authorization", workflow)
        self.assertNotIn("python -m scripts.consume_account03_slot", workflow)
        self.assertNotIn("RUN_LIVE_SLOT", workflow)
        self.assertNotIn("ACCOUNT03_RANDOM_SLOT", workflow)
        self.assertNotIn("trigger_source:", workflow)
        self.assertIn('SOCIAL_SYNDICATION_ENABLED: "false"', workflow)
        self.assertIn("vars.COINCOACH_LIVE_96_ENABLED == 'true'", workflow)

    def test_worker_creates_hashed_nonce_and_dispatches_it(self):
        worker = (ROOT / "cloudflare_scheduler/src/index.js").read_text(encoding="utf-8")
        self.assertIn("crypto.getRandomValues(new Uint8Array(32))", worker)
        self.assertIn("SHA-256", worker)
        self.assertIn("INSERT OR IGNORE INTO slot_authorizations", worker)
        self.assertIn('slot_nonce: slotNonce', worker)
        self.assertIn("/v1/slot-authorizations/consume", worker)

    def test_nonce_consume_is_atomic_expiring_and_same_hour_only(self):
        worker = (ROOT / "cloudflare_scheduler/src/index.js").read_text(encoding="utf-8")
        self.assertIn("UPDATE slot_authorizations", worker)
        self.assertIn("consumed_at_ms IS NULL", worker)
        self.assertIn("expires_at_ms >= ?", worker)
        self.assertIn("hour_bucket = ?", worker)

    def test_migration_restricts_authorizations_to_account03_and_one_per_hour(self):
        migration = (ROOT / "cloudflare_scheduler/migrations/0002_create_slot_authorizations.sql").read_text(encoding="utf-8")
        self.assertIn("CHECK (account_id = 'account_03')", migration)
        self.assertIn("UNIQUE (account_id, hour_bucket)", migration)
        self.assertIn("token_hash TEXT PRIMARY KEY", migration)


if __name__ == "__main__":
    unittest.main()
