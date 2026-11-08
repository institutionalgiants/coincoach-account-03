import tempfile
import unittest
from pathlib import Path

from app.caption_engine import (
    CaptionEngine,
    _format_signal_detail_lines,
    enforce_caption_maximum_length,
)
from app.models import Signal
from app.storage import Storage


class CaptionVariationTest(unittest.TestCase):
    def test_five_punchy_posts_do_not_repeat_opener(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            settings = {"captions": {"maximum_recent_phrases": 250, "guys_probability": 0.0}}
            engine = CaptionEngine(storage, settings)
            openings = []
            for index in range(5):
                signal = Signal(
                    signal_id=f"sig-{index}", symbol=f"DEMO{index}USDT", base_asset=f"DEMO{index}",
                    timeframe="1h", side="LONG", setup="trend_pullback", score=70,
                    current_price=1.0, entry_low=0.99, entry_high=1.01, entry_mid=1.0,
                    stop_loss=0.96, tp1=1.06, tp2=1.12, tp3=1.2,
                    stop_percent=4.0, tp1_percent=6.0, tp2_percent=12.0, tp3_percent=20.0,
                    facts={},
                )
                caption = engine.generate(signal, "account_01", "punchy")
                openings.append(caption.splitlines()[0].split(",", 1)[0])
            self.assertEqual(len(openings), len(set(openings)))
            storage.close()


class CaptionGrammarTest(unittest.TestCase):
    def _signal(self, signal_id: str, base_asset: str = "XRP", setup: str = "trend_rejection") -> Signal:
        return Signal(
            signal_id=signal_id, symbol=f"{base_asset}USDT", base_asset=base_asset,
            timeframe="1h", side="SHORT", setup=setup, score=70,
            current_price=1.0, entry_low=0.99, entry_high=1.01, entry_mid=1.0,
            stop_loss=1.04, tp1=0.94, tp2=0.88, tp3=0.8,
            stop_percent=4.0, tp1_percent=6.0, tp2_percent=12.0, tp3_percent=20.0,
            facts={},
        )

    def test_guys_replaces_other_opener(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            settings = {"captions": {"maximum_recent_phrases": 250, "guys_probability": 1.0}}
            engine = CaptionEngine(storage, settings)
            opening = engine.generate(self._signal("force-guys"), "account_01", "punchy").splitlines()[0]
            self.assertTrue(opening.startswith("guys, $XRP "))
            self.assertNotIn("guys, quick one", opening)
            self.assertNotIn("guys, worth a look", opening)
            storage.close()

    def test_recent_observation_is_not_reused_when_alternatives_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            settings = {"captions": {"maximum_recent_phrases": 250, "guys_probability": 0.0}}
            engine = CaptionEngine(storage, settings)
            openings = [
                engine.generate(self._signal(f"sig-observation-{index}", f"DEMO{index}"), "account_01", "punchy").splitlines()[0]
                for index in range(4)
            ]
            observations = [
                next(obs for obs in (
                    "bounced into resistance and sellers showed up again",
                    "is struggling to hold the recovery",
                    "keeps rejecting the same area, so the short side is worth watching",
                    "tried to recover but the bounce is losing energy again",
                ) if obs in opening)
                for opening in openings
            ]
            self.assertEqual(4, len(set(observations)))
            storage.close()

    def test_punchy_opener_is_separated_from_cashtag(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            settings = {"captions": {"maximum_recent_phrases": 250, "guys_probability": 0.0}}
            engine = CaptionEngine(storage, settings)
            opening = engine.generate(self._signal("punctuation", "LINK"), "account_01", "punchy").splitlines()[0]
            self.assertNotIn("clean $LINK", opening)
            self.assertRegex(opening, r"^[^$]+, \$LINK ")
            storage.close()


class CaptionStructureTest(unittest.TestCase):
    def _signal(self, side: str = "SHORT", timeframe: str = "4h") -> Signal:
        return Signal(
            signal_id=f"structure-{side}-{timeframe}", symbol="BTCUSDT", base_asset="BTC",
            timeframe=timeframe, side=side, setup="trend_rejection", score=80,
            current_price=64000.0, entry_low=63900.0, entry_high=64100.0, entry_mid=64000.0,
            stop_loss=65000.0, tp1=63000.0, tp2=62000.0, tp3=61000.0,
            stop_percent=1.56, tp1_percent=1.56, tp2_percent=3.12, tp3_percent=4.68,
            facts={},
        )

    def test_default_caption_omits_context_and_risk_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            settings = {
                "market": {"market_type": "spot"},
                "captions": {
                    "maximum_recent_phrases": 250,
                    "guys_probability": 0.0,
                    "include_context_line": False,
                    "include_risk_note": False,
                    "vary_signal_detail_layout": False,
                },
            }
            caption = CaptionEngine(storage, settings).generate(
                self._signal("SHORT", "4h"), "account_01", "punchy"
            )
            self.assertNotIn("Short setup | Spot chart | 4H", caption)
            self.assertNotIn("NFA. DYOR.", caption)
            self.assertIn("Entry:", caption)
            self.assertIn("TP1:", caption)
            self.assertIn("TP2:", caption)
            self.assertIn("TP3:", caption)
            self.assertIn("Stop Loss:", caption)
            self.assertIn("Stop Loss: 65000.00", caption)
            tags = caption.splitlines()[-1].split()
            self.assertEqual(4, len(tags))
            self.assertEqual("#BTC", tags[0])
            self.assertEqual(
                {tag.lower().rstrip("!") for tag in tags[1:]},
                {"#binance", "#write2earn", "#crypto"},
            )
            storage.close()

    def test_signal_details_support_multiple_human_readable_layouts(self):
        signal = self._signal("LONG", "15m")
        layouts = {
            tuple(_format_signal_detail_lines(signal, bytes([value]) * 32, vary_layout=True))
            for value in range(12)
        }
        self.assertGreaterEqual(len(layouts), 4)
        for lines in layouts:
            text = "\n".join(lines)
            for token in ("Entry:", "TP1:", "TP2:", "TP3:", "Stop Loss:"):
                self.assertIn(token, text)

    def test_signal_detail_layout_can_be_fixed(self):
        signal = self._signal("SHORT", "4h")
        lines = _format_signal_detail_lines(signal, bytes([255]) * 32, vary_layout=False)
        self.assertEqual(
            [
                "Entry: 63900.00–64100.00",
                "TP1: 63000.00 | TP2: 62000.00 | TP3: 61000.00",
                "Stop Loss: 65000.00",
            ],
            lines,
        )

    def test_context_and_risk_lines_remain_explicitly_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            settings = {
                "market": {"market_type": "futures"},
                "captions": {
                    "maximum_recent_phrases": 250,
                    "guys_probability": 0.0,
                    "include_context_line": True,
                    "include_risk_note": True,
                    "risk_note": "NFA. DYOR.",
                },
            }
            caption = CaptionEngine(storage, settings).generate(
                self._signal("LONG", "15m"), "account_01", "punchy"
            )
            self.assertIn("Long | Futures | 15M", caption)
            self.assertIn("NFA. DYOR.", caption)
            tags = caption.splitlines()[-1].split()
            self.assertEqual(4, len(tags))
            self.assertEqual("#BTC", tags[0])
            self.assertEqual(
                {tag.lower().rstrip("!") for tag in tags[1:]},
                {"#binance", "#write2earn", "#crypto"},
            )
            storage.close()


class CaptionLengthLimitTest(unittest.TestCase):
    def test_limit_removes_analysis_but_preserves_trade_details_and_hashtags(self):
        analysis = "Extended market explanation " * 15
        expected = "\n".join([
            "A fresh BTC setup is developing",
            "",
            "Entry: 64000.00",
            "TP1: 63000.00",
            "TP2: 62000.00",
            "TP3: 61000.00",
            "Stop Loss: 65000.00",
            "",
            "#BTC #binance #write2earn #crypto",
        ])
        original = (
            "A fresh BTC setup is developing\n\n"
            + analysis + "\n\n"
            + "\n".join(expected.splitlines()[2:])
        )

        result = enforce_caption_maximum_length(
            original, analysis, len(expected)
        )

        self.assertEqual(result, expected)
        self.assertLessEqual(len(result), len(expected))
        for field in ("Entry:", "TP1:", "TP2:", "TP3:", "Stop Loss:"):
            self.assertIn(field, result)
        self.assertIn("#BTC", result)
        self.assertNotIn(analysis, result)

    def test_limit_refuses_to_cut_required_trade_details(self):
        required = "\n".join([
            "BTC setup", "",
            "Entry: 64000.00",
            "TP1: 63000.00",
            "TP2: 62000.00",
            "TP3: 61000.00",
            "Stop Loss: 65000.00",
            "",
            "#BTC #binance #write2earn #crypto",
        ])

        with self.assertRaises(ValueError):
            enforce_caption_maximum_length(
                required, "", len(required) - 1
            )

    def test_disabled_limit_preserves_caption(self):
        original = "A caption with its complete analysis and tags."
        self.assertEqual(
            enforce_caption_maximum_length(original, "", 0),
            original,
        )


class CaptionLeverageTest(unittest.TestCase):
    def _signal(self, side: str = "LONG", *, perpetual: bool = True) -> Signal:
        return Signal(
            signal_id=f"leverage-{side}-{perpetual}", symbol="SUIUSDT", base_asset="SUI",
            timeframe="1h", side=side, setup="trend_pullback" if side == "LONG" else "trend_rejection",
            score=78, current_price=1.0, entry_low=0.99, entry_high=1.01, entry_mid=1.0,
            stop_loss=0.96 if side == "LONG" else 1.04,
            tp1=1.03 if side == "LONG" else 0.97,
            tp2=1.06 if side == "LONG" else 0.94,
            tp3=1.09 if side == "LONG" else 0.91,
            stop_percent=4.0, tp1_percent=3.0, tp2_percent=6.0, tp3_percent=9.0,
            facts={"perpetual_eligible": perpetual, "market_tags": ["perpetual"]},
        )

    @staticmethod
    def _settings() -> dict:
        return {
            "captions": {
                "maximum_recent_phrases": 250,
                "guys_probability": 0.0,
                "vary_signal_detail_layout": False,
                "leverage": {
                    "enabled": True,
                    "require_perpetual_eligible": True,
                    "slots_per_day": 96,
                    "daily_post_options": [30],
                    "values": list(range(15, 26)),
                },
            }
        }

    def test_daily_plan_assigns_exactly_thirty_slots_per_account(self):
        from datetime import date
        from app.caption_engine import _daily_leverage_slots

        day = date(2026, 7, 31)
        account_01 = _daily_leverage_slots(day, [30], account_id="account_01")
        account_02 = _daily_leverage_slots(day, [30], account_id="account_02")
        self.assertEqual(len(account_01), 30)
        self.assertEqual(len(account_02), 30)
        self.assertTrue(all(0 <= slot < 96 for slot in account_01 | account_02))
        self.assertNotEqual(account_01, account_02)

    def test_leverage_is_blended_only_on_selected_perpetual_slots(self):
        from datetime import date, datetime, timezone
        from app.caption_engine import _daily_leverage_slots

        day = date(2026, 7, 31)
        selected_slot = min(_daily_leverage_slots(day, [30], account_id="account_01"))
        hour, quarter = divmod(selected_slot, 4)
        now = datetime(2026, 7, 31, hour, quarter * 15 + 6, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            engine = CaptionEngine(storage, self._settings(), now_provider=lambda: now)
            caption = engine.generate(self._signal("SHORT"), "account_01", "direct")
            first_line = caption.splitlines()[0]
            self.assertRegex(first_line, r"(?:1[5-9]|2[0-5])x")
            leverage_value = int(__import__("re").search(r"(1[5-9]|2[0-5])x", first_line).group(1))
            self.assertGreaterEqual(leverage_value, 15)
            self.assertLessEqual(leverage_value, 25)
            self.assertNotIn("|", first_line)
            self.assertIn("$SUI", first_line)
            self.assertIn("Entry:", caption)
            self.assertIn("TP1:", caption)
            self.assertIn("Stop Loss:", caption)
            storage.close()

    def test_non_perpetual_signal_keeps_the_original_caption(self):
        from datetime import date, datetime, timezone
        from app.caption_engine import _daily_leverage_slots

        day = date(2026, 7, 31)
        selected_slot = min(_daily_leverage_slots(day, [30], account_id="account_01"))
        hour, quarter = divmod(selected_slot, 4)
        now = datetime(2026, 7, 31, hour, quarter * 15 + 6, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            engine = CaptionEngine(storage, self._settings(), now_provider=lambda: now)
            first_line = engine.generate(
                self._signal("LONG", perpetual=False), "account_01", "direct"
            ).splitlines()[0]
            self.assertIn("leverage", first_line.lower())
            self.assertRegex(first_line, r"(?:1[5-9]|[2-9][0-9]|100)x")
            storage.close()

    def test_leverage_is_varied_but_always_inside_the_first_line(self):
        from app.caption_engine import _inject_leverage_phrase

        opening = "watching short on $SUI, sellers are pressing the move"
        rendered = {
            _inject_leverage_phrase(opening, self._signal("SHORT"), 20, bytes([value]) * 32)
            for value in range(8)
        }
        self.assertGreaterEqual(len(rendered), 7)
        self.assertTrue(all("20x" in line for line in rendered))
        self.assertTrue(all(line.startswith("watching short on $SUI") for line in rendered))
        self.assertTrue(all(line.endswith("sellers are pressing the move") for line in rendered))
        self.assertTrue(all(not line.startswith("20x") for line in rendered))
        self.assertTrue(all(not line.endswith("20x") for line in rendered))
        self.assertTrue(all("|" not in line for line in rendered))


if __name__ == "__main__":
    unittest.main()


class CaptionHashtagAndBStockTest(unittest.TestCase):
    def _signal(self, base="BTC", tags=()):
        return Signal(
            signal_id=f"tags-{base}", symbol=f"{base}USDT", base_asset=base, timeframe="1h",
            side="LONG", setup="trend_pullback", score=80, current_price=100.0,
            entry_low=99.0, entry_high=101.0, entry_mid=100.0, stop_loss=96.0,
            tp1=103.0, tp2=106.0, tp3=109.0, stop_percent=4.0, tp1_percent=3.0,
            tp2_percent=6.0, tp3_percent=9.0, facts={"perpetual_eligible": True, "market_tags": list(tags)},
        )

    def test_caption_has_exact_dynamic_four_tag_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.db")
            settings = {"captions": {"maximum_recent_phrases": 250, "guys_probability": 0.0}}
            caption = CaptionEngine(storage, settings).generate(self._signal("BTC"), "account_01", "punchy")
            tags = caption.splitlines()[-1].split()
            self.assertEqual(4, len(tags))
            self.assertEqual("#BTC", tags[0])
            self.assertEqual(
                {tag.lower().rstrip("!") for tag in tags[1:]},
                {"#binance", "#write2earn", "#crypto"},
            )
            storage.close()

    def test_bstock_uses_five_x_override(self):
        from app.caption_engine import _leverage_value
        signal = self._signal("AAPL", ("tradfi",))
        self.assertEqual(5, _leverage_value(signal, bytes(range(32)), {"values": list(range(20, 51)) + [70, 100]}))

    def test_crypto_keeps_configured_high_leverage_values(self):
        from app.caption_engine import _leverage_value
        signal = self._signal("BTC", ("perpetual",))
        values = list(range(20, 51)) + [70, 100]
        value = _leverage_value(signal, bytes(range(32)), {"values": values})
        self.assertIn(value, values)
