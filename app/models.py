from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Side = Literal["LONG", "SHORT"]


@dataclass(slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    close_time: int


@dataclass(slots=True)
class MarketSymbol:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    tick_size: float
    step_size: float
    quote_precision: int
    price: float = 0.0
    change_percent_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    quote_volume_24h: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    market_tags: tuple[str, ...] = ()
    perpetual_eligible: bool = False
    onboard_date_ms: int | None = None
    market_type: str = "spot"
    news_context: list[dict[str, Any]] = field(default_factory=list)

    @property
    def spread_percent(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 999.0
        midpoint = (self.bid + self.ask) / 2
        return ((self.ask - self.bid) / midpoint) * 100


@dataclass(slots=True)
class Candidate:
    symbol: MarketSymbol
    timeframe: str
    candles: list[Candle]
    setup: str
    side: Side
    score: float
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Signal:
    signal_id: str
    symbol: str
    base_asset: str
    timeframe: str
    side: Side
    setup: str
    score: float
    current_price: float
    entry_low: float
    entry_high: float
    entry_mid: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    stop_percent: float
    tp1_percent: float
    tp2_percent: float
    tp3_percent: float
    facts: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class RoutedPost:
    account_id: str
    style: str
    slot_time: datetime
    signal: Signal
    caption: str
    image_path: str | None = None
