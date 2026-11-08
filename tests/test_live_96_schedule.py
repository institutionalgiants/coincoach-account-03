from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from scripts.run_square_slot import (
    apply_account03_runtime_overrides,
    validate_account03_live_mode,
)


class Account03ScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.workflow = (cls.root / ".github/workflows/account03-live-square-hourly.yml").read_text(encoding="utf-8")
        cls.worker = (cls.root / "cloudflare_scheduler/src/index.js").read_text(encoding="utf-8")
        cls.wrangler = (cls.root / "cloudflare_scheduler/wrangler.toml").read_text(encoding="utf-8")
        cls.settings = yaml.safe_load((cls.root / "config/settings.yaml").read_text(encoding="utf-8"))

    def test_live_workflow_is_locked_to_account03(self):
        self.assertIn("SELECTED_ACCOUNT: account_03", self.workflow)
        self.assertIn('default: account_03', self.workflow)
        self.assertNotIn('default: account_01', self.workflow)
        self.assertIn('max-parallel: 1', self.workflow)
        self.assertIn("group: coincoach-account03-square-hourly", self.workflow)
        legacy = (self.root / ".github/workflows/live-square-96-per-day.yml").read_text(encoding="utf-8")
        self.assertIn("group: coincoach-square-live-slot", legacy)

    def test_publish_cadence_and_limits(self):
        self.assertIn('LIVE_EXPECTED_POSTS_PER_HOUR: "1"', self.workflow)
        self.assertIn('PER_ACCOUNT_DAILY_LIMIT: "24"', self.workflow)

        # Shared YAML retains the original defaults for all other accounts.
        self.assertEqual(self.settings["schedule"]["signals_per_account_per_hour"], 4)
        self.assertEqual(self.settings["schedule"]["slot_spacing_minutes"], 15)
        self.assertEqual(self.settings["market"]["shortlist_size"], 220)
        self.assertEqual(self.settings["market"]["deep_scan_limit"], 140)
        self.assertEqual(self.settings["publishing"]["per_account_daily_limit"], 96)

        # Account 03 receives its special values only through its runtime override.
        account03_settings = copy.deepcopy(self.settings)
        apply_account03_runtime_overrides(account03_settings)

        self.assertEqual(account03_settings["schedule"]["signals_per_account_per_hour"], 1)
        self.assertEqual(account03_settings["schedule"]["slot_spacing_minutes"], 60)
        self.assertEqual(account03_settings["market"]["shortlist_size"], 300)
        self.assertEqual(account03_settings["market"]["deep_scan_limit"], 180)
        self.assertEqual(account03_settings["publishing"]["per_account_daily_limit"], 24)
        self.assertEqual(account03_settings["captions"]["maximum_length"], 1000)
        self.assertTrue(account03_settings["captions"]["enforce_maximum_length"])
        self.assertTrue(account03_settings["captions"]["include_context_line"])
        self.assertTrue(account03_settings["captions"]["include_market_analysis"])

        # Applying Account 03 settings must not mutate the shared defaults.
        self.assertEqual(self.settings["schedule"]["signals_per_account_per_hour"], 4)

    def test_account03_live_mode_guard_is_strict(self):
        with self.assertRaises(RuntimeError):
            validate_account03_live_mode(
                None, True, False, {"account_03"}
            )

        with self.assertRaises(RuntimeError):
            validate_account03_live_mode(
                "account_01", True, True, {"account_01"}
            )

        with self.assertRaises(RuntimeError):
            validate_account03_live_mode(
                "account_03", True, False, {"account_03"}
            )

        # Other accounts retain their independent live publishing path.
        validate_account03_live_mode(
            "account_01", True, False, {"account_01"}
        )

        # The dedicated Account 03 workflow is accepted.
        validate_account03_live_mode(
            "account_03", True, True, {"account_03"}
        )

    def test_social_syndication_is_disabled(self):
        self.assertIn('SOCIAL_SYNDICATION_ENABLED: "false"', self.workflow)
        for platform in ("DISCORD", "FACEBOOK", "INSTAGRAM", "THREADS"):
            self.assertIn(f'{platform}_PUBLISH_ENABLED: "false"', self.workflow)

    def test_legacy_shared_workflow_excludes_account03_only(self):
        legacy = (self.root / ".github/workflows/live-square-96-per-day.yml").read_text(encoding="utf-8")
        self.assertNotIn("account_03", legacy)
        self.assertIn("          - all", legacy)
        for suffix in ("01", "02", "04", "05", "06"):
            self.assertIn(f'"account_id": "account_{suffix}"', legacy)

    def test_worker_selects_one_seeded_random_minute_per_utc_hour(self):
        self.assertIn('crons = ["* * * * *"]', self.wrangler)
        self.assertIn("minuteForHour", self.worker)
        self.assertIn("value % 60", self.worker)
        self.assertIn('account_id: "account_03"', self.worker)
        self.assertIn("crypto.getRandomValues(new Uint8Array(32))", self.worker)
        self.assertIn("SHA-256", self.worker)
        self.assertIn("slot_nonce: slotNonce", self.worker)
        self.assertIn("/v1/slot-authorizations/consume", self.worker)
        self.assertIn('GITHUB_WORKFLOW = "account03-live-square-hourly.yml"', self.wrangler)
        self.assertIn('env.GITHUB_WORKFLOW || "account03-live-square-hourly.yml"', self.worker)

    def test_news_cache_has_one_writer_and_read_only_publisher(self):
        publisher = (self.root / ".github/workflows/account03-live-square-hourly.yml").read_text(encoding="utf-8")
        scanner = (self.root / ".github/workflows/account03-news-scan.yml").read_text(encoding="utf-8")
        cycle = (self.root / "app/cycle.py").read_text(encoding="utf-8")
        scanner_code = (self.root / "scripts/scan_market_intelligence.py").read_text(encoding="utf-8")
        expected_path = "data/news/account_03/news_intelligence.json"

        self.assertIn(f"NEWS_CACHE_PATH: {expected_path}", publisher)
        self.assertIn(f"NEWS_CACHE_PATH: {expected_path}", scanner)
        self.assertIn(f"path: {expected_path}", publisher)
        self.assertIn(f"path: {expected_path}", scanner)
        self.assertIn('ACCOUNT03_NEWS_ENABLED: "true"', publisher)
        self.assertIn("uses: actions/cache/restore@v4", publisher)
        self.assertIn("uses: actions/cache@v4", scanner)

        # Publisher has explicit opt-in and rejects multi-account consumption.
        self.assertIn('news_enabled = os.getenv("ACCOUNT03_NEWS_ENABLED"', cycle)
        self.assertIn('active_account_ids != {"account_03"}', cycle)
        self.assertIn('cache_path = os.getenv("NEWS_CACHE_PATH", "").strip()', cycle)
        self.assertIn("news_by_asset = {}", cycle)
        self.assertNotIn(
            'os.getenv("NEWS_CACHE_PATH", "data/news/account_03/news_intelligence.json")',
            cycle,
        )

        # Publisher never scans or writes news. The separate scanner is sole writer.
        self.assertNotIn("fresh_news = await news_intelligence.scan", cycle)
        self.assertNotIn("news_intelligence.save_cache(cache_path", cycle)
        self.assertIn("intelligence.save_cache(target, grouped)", scanner_code)

    def test_news_scan_is_separate_and_non_publishing(self):
        text = (self.root / ".github/workflows/account03-news-scan.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "*/5 * * * *"', text)
        self.assertIn("scripts.scan_market_intelligence", text)
        self.assertNotIn("PUBLISH_ENABLED: \"true\"", text)


if __name__ == "__main__":
    unittest.main()
