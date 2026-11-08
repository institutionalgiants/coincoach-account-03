from __future__ import annotations

import unittest

from app.caption_engine import _human_analysis_paragraph
from app.models import Signal


class HumanAnalysisTests(unittest.TestCase):
    def signal(self, facts):
        return Signal(
            signal_id="demo-signal", symbol="DOGEUSDT", base_asset="DOGE", timeframe="1h",
            side="LONG", setup="trend_pullback", score=80, current_price=0.2,
            entry_low=0.198, entry_high=0.202, entry_mid=0.2, stop_loss=0.19,
            tp1=0.21, tp2=0.22, tp3=0.23, stop_percent=5, tp1_percent=5,
            tp2_percent=10, tp3_percent=15, facts=facts,
        )

    def test_analysis_uses_measured_trend_momentum_volume_and_levels(self):
        paragraph = _human_analysis_paragraph(self.signal({
            "ema20": 0.205, "ema50": 0.2, "rsi": 62.5, "volume_ratio": 1.8,
            "support_level": 0.18, "resistance_level": 0.24,
        }), bytes(range(32)))
        self.assertRegex(paragraph, r"RSI (?:is|reading of) 62\.5")
        self.assertIn("1.8x", paragraph)
        self.assertIn("0.18", paragraph)
        self.assertIn("0.24", paragraph)

    def test_news_is_attributed_and_not_presented_as_trade_confirmation(self):
        paragraph = _human_analysis_paragraph(self.signal({
            "ema20": 0.205, "ema50": 0.2, "rsi": 60, "volume_ratio": 1.4,
            "support_level": 0.18, "resistance_level": 0.24,
            "news_context": [{
                "title": "Elon Musk mentions Dogecoin payments", "source": "CoinDesk",
                "url": "https://coindesk.com/story", "evidence_level": "reported_media",
                "sentiment": "positive",
            }],
        }), bytes(range(32)))
        self.assertIn("Potential catalyst: CoinDesk reported", paragraph)
        self.assertIn("confirm the project's own announcement", paragraph)
        self.assertIn("https://coindesk.com/story", paragraph)

    def test_caption_wording_changes_with_signal_seed(self):
        signal = self.signal({
            "ema20": 0.205, "ema50": 0.2, "rsi": 62.5, "volume_ratio": 1.8,
            "support_level": 0.18, "resistance_level": 0.24, "change_percent_24h": 4.2,
        })
        first = _human_analysis_paragraph(signal, bytes(range(32)))
        second = _human_analysis_paragraph(signal, bytes(reversed(range(32))))
        self.assertNotEqual(first, second)

    def test_watched_x_profile_is_attributed_without_claiming_verification(self):
        paragraph = _human_analysis_paragraph(self.signal({
            "ema20": 0.205, "ema50": 0.2, "rsi": 60, "volume_ratio": 1.4,
            "support_level": 0.18, "resistance_level": 0.24,
            "news_context": [{
                "title": "Musk discusses Dogecoin payments", "source": "X (@elonmusk)",
                "account_handle": "elonmusk", "evidence_level": "watched_x_profile_post",
                "url": "https://x.com/elonmusk/status/123", "published_at": "2026-11-04T11:30:00+00:00",
            }],
        }), bytes(range(32)))
        self.assertIn("watched X profile @elonmusk", paragraph)
        self.assertNotIn("verified", paragraph.lower())
        self.assertIn("04 Nov 11:30 UTC", paragraph)

    def test_empty_facts_do_not_invent_price_levels(self):
        paragraph = _human_analysis_paragraph(self.signal({}), bytes(range(32)))
        self.assertNotIn("support area is around", paragraph)
        self.assertNotIn("resistance is near", paragraph)


if __name__ == "__main__":
    unittest.main()
