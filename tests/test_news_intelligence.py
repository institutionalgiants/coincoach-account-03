from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.news_intelligence import NewsIntelligence, _x_poll_due


class NewsIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        self.settings = {"market": {"discovery": {"news": {"enabled": True, "max_age_hours": 8, "asset_aliases": {"DOGE": ["Dogecoin"]}}}}}
        self.scanner = NewsIntelligence(self.settings)

    def test_recent_coin_specific_story_is_classified_and_attributed(self):
        rss = f"""<?xml version='1.0'?><rss><channel><item>
          <title>Elon Musk comments on Dogecoin payments launch</title>
          <description>Dogecoin gains new payment integration support.</description>
          <link>https://coindesk.com/example</link>
          <pubDate>{self.now.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
        </item></channel></rss>"""
        events = self.scanner.parse_rss_content(rss, source="CoinDesk", feed_url="https://www.coindesk.com/rss", assets=["DOGE"], now=self.now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["asset"], "DOGE")
        self.assertEqual(events[0]["sentiment"], "positive")
        self.assertEqual(events[0]["evidence_level"], "reported_media")
        self.assertIn("Elon Musk", events[0]["watched_people"])

    def test_stale_and_unrelated_story_is_ignored(self):
        old = "Tue, 03 Nov 2026 01:00:00 +0000"
        rss = f"""<rss><channel><item><title>Bitcoin and macro markets</title><link>https://coindesk.com/old</link><pubDate>{old}</pubDate></item></channel></rss>"""
        events = self.scanner.parse_rss_content(rss, source="CoinDesk", feed_url="https://www.coindesk.com/rss", assets=["DOGE"], now=self.now)
        self.assertEqual(events, [])

    def test_ambiguous_ticker_requires_unambiguous_mention(self):
        text = "The market had one strong move after the update."
        self.assertEqual(self.scanner._find_assets(text, ["ONE"]), [])
        self.assertEqual(self.scanner._find_assets("$ONE announcement: launch confirmed", ["ONE"]), ["ONE"])

    def test_off_domain_story_link_is_rejected(self):
        rss = f"""<rss><channel><item>
          <title>Dogecoin payment integration launch</title>
          <link>https://example.net/redirect</link>
          <pubDate>{self.now.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
        </item></channel></rss>"""
        events = self.scanner.parse_rss_content(
            rss, source="CoinDesk", feed_url="https://www.coindesk.com/rss", assets=["DOGE"], now=self.now
        )
        self.assertEqual(events, [])

    def test_x_poll_interval_is_configurable_and_respects_quarter_hour_boundary(self):
        self.assertTrue(_x_poll_due(datetime(2026, 11, 4, 12, 15, tzinfo=timezone.utc), 15))
        self.assertFalse(_x_poll_due(datetime(2026, 11, 4, 12, 10, tzinfo=timezone.utc), 15))

    def test_cache_roundtrip_and_expiry(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            target = Path(directory) / "news.json"
            grouped = {"DOGE": [{"asset": "DOGE", "url": "https://x.test/1", "published_at": self.now.isoformat()}]}
            self.scanner.save_cache(target, grouped, now=self.now)
            loaded = NewsIntelligence.load_cache(target, ["DOGE"], max_age_minutes=15, now=self.now)
            self.assertEqual(len(loaded["DOGE"]), 1)
            expired = NewsIntelligence.load_cache(target, ["DOGE"], max_age_minutes=15, now=self.now.replace(hour=13))
            self.assertEqual(expired, {})


if __name__ == "__main__":
    unittest.main()
