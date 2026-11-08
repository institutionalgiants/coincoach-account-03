from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .binance_market import round_to_tick
from .indicators import atr, ema, rsi, sma
from .models import Candidate, Signal
from .scanner import DeepScanItem


@dataclass(slots=True)
class Detection:
    setup: str
    side: str
    score: float
    facts: dict


class SignalEngine:
    def __init__(self, settings: dict) -> None:
        self.settings = settings

    def detect(self, item: DeepScanItem) -> list[Candidate]:
        candles = item.candles
        closes = [c.close for c in candles]
        volumes = [c.volume for c in candles]
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        rsi14 = rsi(closes, 14)
        volume_avg = sma(volumes, 20)
        atr14 = atr(candles, 14)
        last = candles[-1]
        previous = candles[-2]
        highest20 = max(c.high for c in candles[-21:-1])
        lowest20 = min(c.low for c in candles[-21:-1])
        vol_ratio = last.volume / max(volume_avg[-1], 1e-12)
        atr_pct = atr14[-1] / max(last.close, 1e-12) * 100
        trend_gap = abs(ema20[-1] - ema50[-1]) / max(last.close, 1e-12) * 100

        detections: list[Detection] = []
        if last.close > highest20 and ema20[-1] > ema50[-1] and 50 <= rsi14[-1] <= 79:
            score = 52 + min(vol_ratio, 3) * 8 + min(trend_gap, 4) * 4 + min(atr_pct, 8)
            detections.append(Detection("breakout_continuation", "LONG", score, {
                "momentum": "building", "structure": "resistance_reclaimed", "volume_ratio": vol_ratio,
                "rsi": rsi14[-1], "atr_percent": atr_pct,
            }))
        if last.close < lowest20 and ema20[-1] < ema50[-1] and 21 <= rsi14[-1] <= 50:
            score = 52 + min(vol_ratio, 3) * 8 + min(trend_gap, 4) * 4 + min(atr_pct, 8)
            detections.append(Detection("breakdown_continuation", "SHORT", score, {
                "momentum": "weakening", "structure": "support_lost", "volume_ratio": vol_ratio,
                "rsi": rsi14[-1], "atr_percent": atr_pct,
            }))

        near_ema20 = abs(last.low - ema20[-1]) <= atr14[-1] * 0.55
        bullish_close = last.close > last.open and last.close > previous.close
        bearish_close = last.close < last.open and last.close < previous.close
        if ema20[-1] > ema50[-1] and near_ema20 and bullish_close and 43 <= rsi14[-1] <= 70:
            score = 47 + min(vol_ratio, 2.5) * 7 + min(trend_gap, 4) * 5
            detections.append(Detection("trend_pullback", "LONG", score, {
                "momentum": "returning", "structure": "pullback_holding", "volume_ratio": vol_ratio,
                "rsi": rsi14[-1], "atr_percent": atr_pct,
            }))
        near_ema20_short = abs(last.high - ema20[-1]) <= atr14[-1] * 0.55
        if ema20[-1] < ema50[-1] and near_ema20_short and bearish_close and 30 <= rsi14[-1] <= 58:
            score = 47 + min(vol_ratio, 2.5) * 7 + min(trend_gap, 4) * 5
            detections.append(Detection("trend_rejection", "SHORT", score, {
                "momentum": "fading", "structure": "bounce_rejected", "volume_ratio": vol_ratio,
                "rsi": rsi14[-1], "atr_percent": atr_pct,
            }))

        # Recovery / rejection patterns make the pool broad enough for multi-account routing.
        recent_low = min(c.low for c in candles[-12:])
        recent_high = max(c.high for c in candles[-12:])
        recovery = (last.close - recent_low) / max(recent_low, 1e-12) * 100
        rejection = (recent_high - last.close) / max(recent_high, 1e-12) * 100
        if recovery >= max(atr_pct * 1.5, 2.0) and last.close > ema20[-1] and bullish_close:
            score = 42 + min(recovery, 18) * 1.2 + min(vol_ratio, 3) * 5
            detections.append(Detection("rebound_continuation", "LONG", score, {
                "momentum": "recovering", "structure": "higher_low", "volume_ratio": vol_ratio,
                "rsi": rsi14[-1], "atr_percent": atr_pct,
            }))
        if rejection >= max(atr_pct * 1.5, 2.0) and last.close < ema20[-1] and bearish_close:
            score = 42 + min(rejection, 18) * 1.2 + min(vol_ratio, 3) * 5
            detections.append(Detection("resistance_rejection", "SHORT", score, {
                "momentum": "losing_strength", "structure": "lower_high", "volume_ratio": vol_ratio,
                "rsi": rsi14[-1], "atr_percent": atr_pct,
            }))

        candidates: list[Candidate] = []
        for detection in detections:
            facts = dict(detection.facts)
            news_context = list(getattr(item.symbol, "news_context", []))[:3]
            news_adjustment = 0.0
            for event in news_context:
                sentiment = str(event.get("sentiment", "neutral")).lower()
                confidence = max(0.0, min(float(event.get("confidence", 0.5)), 1.0))
                impact = abs(float(event.get("impact_score", 0.0)))
                aligned = (detection.side == "LONG" and sentiment == "positive") or (detection.side == "SHORT" and sentiment == "negative")
                opposed = (detection.side == "LONG" and sentiment == "negative") or (detection.side == "SHORT" and sentiment == "positive")
                if aligned:
                    news_adjustment += min(3.0, impact * confidence * 0.5)
                elif opposed:
                    news_adjustment -= min(5.0, impact * confidence * 0.7)
            news_adjustment = max(-8.0, min(6.0, news_adjustment))
            news_adjusted_score = max(0.0, min(99.0, detection.score + news_adjustment))
            facts.update({
                "market_tags": list(item.symbol.market_tags),
                "perpetual_eligible": bool(item.symbol.perpetual_eligible),
                "quote_asset": item.symbol.quote_asset,
                "onboard_date_ms": item.symbol.onboard_date_ms,
                "market_type": item.symbol.market_type,
                "support_level": lowest20,
                "resistance_level": highest20,
                "ema20": ema20[-1],
                "ema50": ema50[-1],
                "rsi": rsi14[-1],
                "volume_ratio": vol_ratio,
                "change_percent_24h": item.symbol.change_percent_24h,
                "quote_volume_24h": item.symbol.quote_volume_24h,
                "news_context": news_context,
                "news_score_adjustment": round(news_adjustment, 2),
            })
            candidates.append(Candidate(
                symbol=item.symbol,
                timeframe=item.timeframe,
                candles=item.candles,
                setup=detection.setup,
                side=detection.side,  # type: ignore[arg-type]
                score=news_adjusted_score,
                facts=facts,
            ))
        return candidates

    def build_signal(self, candidate: Candidate) -> Signal | None:
        cfg = self.settings["signals"]
        candles = candidate.candles
        atr_value = atr(candles, 14)[-1]
        current = candles[-1].close
        tick = candidate.symbol.tick_size
        entry_half_width = atr_value * float(cfg["entry_zone_atr_fraction"])
        recent_lows = [c.low for c in candles[-12:]]
        recent_highs = [c.high for c in candles[-12:]]

        if candidate.side == "LONG":
            entry_mid = current - atr_value * 0.10
            entry_low = entry_mid - entry_half_width
            entry_high = entry_mid + entry_half_width
            structural_stop = min(recent_lows) - atr_value * float(cfg["stop_buffer_atr_fraction"])
            stop_loss = structural_stop
            raw_risk = (entry_mid - stop_loss) / max(entry_mid, 1e-12) * 100
        else:
            entry_mid = current + atr_value * 0.10
            entry_low = entry_mid - entry_half_width
            entry_high = entry_mid + entry_half_width
            structural_stop = max(recent_highs) + atr_value * float(cfg["stop_buffer_atr_fraction"])
            stop_loss = structural_stop
            raw_risk = (stop_loss - entry_mid) / max(entry_mid, 1e-12) * 100

        min_risk = float(cfg["minimum_stop_percent"])
        timeframe_max = cfg.get("maximum_stop_percent_by_timeframe", {})
        max_risk = float(timeframe_max.get(candidate.timeframe, cfg["maximum_stop_percent"]))
        if not math.isfinite(raw_risk) or raw_risk <= 0:
            return None
        # Do not pull a structural stop inward just to fit a configured cap. If the true
        # invalidation is too far away, reject the setup instead of publishing a misleading SL.
        if raw_risk > max_risk:
            return None
        risk_percent = max(raw_risk, min_risk)

        # Deterministic setup-aware variation: values feel natural without random fabricated claims.
        seed_text = f"{candidate.symbol.symbol}:{candidate.timeframe}:{candidate.setup}:{candles[-1].close_time}"
        digest = hashlib.sha256(seed_text.encode()).digest()
        unit1 = int.from_bytes(digest[:2], "big") / 65535
        unit2 = int.from_bytes(digest[2:4], "big") / 65535
        unit3 = int.from_bytes(digest[4:6], "big") / 65535
        score_factor = max(0.0, min((candidate.score - 35) / 60, 1.0))
        r1 = 1.28 + 0.30 * score_factor + 0.18 * unit1
        r2 = 2.35 + 0.50 * score_factor + 0.32 * unit2
        r3 = 3.75 + 0.85 * score_factor + 0.55 * unit3
        tp_percents = [risk_percent * r for r in (r1, r2, r3)]
        timeframe_caps = cfg.get("maximum_tp_percent_by_timeframe", {})
        caps = timeframe_caps.get(candidate.timeframe)
        if caps:
            tp_percents = [min(value, float(cap)) for value, cap in zip(tp_percents, caps)]
        # A target sequence must remain strictly increasing after caps are applied.
        if not (tp_percents[0] < tp_percents[1] < tp_percents[2]):
            return None

        if candidate.side == "LONG":
            tp_prices = [entry_mid * (1 + pct / 100) for pct in tp_percents]
            stop_loss = entry_mid * (1 - risk_percent / 100)
        else:
            tp_prices = [entry_mid * (1 - pct / 100) for pct in tp_percents]
            stop_loss = entry_mid * (1 + risk_percent / 100)

        entry_low = round_to_tick(entry_low, tick)
        entry_high = round_to_tick(entry_high, tick)
        entry_mid = round_to_tick((entry_low + entry_high) / 2, tick)
        stop_loss = round_to_tick(stop_loss, tick)
        tp1, tp2, tp3 = (round_to_tick(value, tick) for value in tp_prices)
        signal_id = hashlib.sha1(seed_text.encode()).hexdigest()[:16]
        return Signal(
            signal_id=signal_id,
            symbol=candidate.symbol.symbol,
            base_asset=candidate.symbol.base_asset,
            timeframe=candidate.timeframe,
            side=candidate.side,
            setup=candidate.setup,
            score=round(candidate.score, 2),
            current_price=current,
            entry_low=entry_low,
            entry_high=entry_high,
            entry_mid=entry_mid,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            stop_percent=round(risk_percent, 2),
            tp1_percent=round(tp_percents[0], 2),
            tp2_percent=round(tp_percents[1], 2),
            tp3_percent=round(tp_percents[2], 2),
            facts=candidate.facts,
        )
