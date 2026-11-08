from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Callable

from .models import Signal
from .storage import Storage


STYLE_OPENERS = {
    "punchy": [
        "quick one here", "this one looks clean", "watching this setup", "keeping it simple",
        "one to watch", "small setup here", "eyes on this one", "worth a look here",
    ],
    "casual": [
        "alright", "okay", "not gonna lie", "this one caught my eye",
        "just noticed this", "looking at this one", "this is getting interesting", "keeping watch here",
    ],
    "calm": [
        "watching", "keeping an eye on", "worth watching", "still tracking",
        "quietly watching", "tracking the structure on", "looking closely at", "following the price action on",
    ],
    "reactive": [
        "looks like", "that move on", "this bounce on", "the reaction on",
        "seeing a shift on", "momentum on", "that rejection on", "price action on",
    ],
    "direct": [
        "long setup on", "short setup on", "taking the long side on", "the short side looks cleaner on",
        "watching long on", "watching short on", "leaning long on", "leaning short on",
    ],
    "chart_reader": [
        "the chart on", "buyers are showing up on", "sellers are leaning on", "structure on",
        "the candles on", "price action on", "the trend on", "the current range on",
    ],
}

SETUP_OBSERVATIONS = {
    "breakout_continuation": [
        "just pushed through the level and buyers are still holding it",
        "is holding above the breakout and momentum is staying firm",
        "reclaimed resistance and the next move could open from here",
        "is trying to build above the breakout instead of giving it back",
    ],
    "breakdown_continuation": [
        "lost support and sellers are still pressing the move",
        "is staying heavy below the broken level",
        "keeps failing to recover support, so the short side still looks cleaner",
        "broke the floor and the bounce is still looking weak",
    ],
    "trend_pullback": [
        "pulled back without breaking structure and buyers are stepping back in",
        "is holding the dip better than expected",
        "gave a controlled pullback and the trend is trying to continue",
        "is finding buyers again around the pullback zone",
    ],
    "trend_rejection": [
        "bounced into resistance and sellers showed up again",
        "is struggling to hold the recovery",
        "keeps rejecting the same area, so the short side is worth watching",
        "tried to recover but the bounce is losing energy again",
    ],
    "rebound_continuation": [
        "is recovering nicely here and buyers are stepping back in",
        "is building again after the rebound",
        "held the low and momentum is slowly returning",
        "is starting to recover after defending the lower zone",
    ],
    "resistance_rejection": [
        "looks tired around this level and sellers are coming back",
        "just rejected again, so the short side makes more sense here",
        "is losing strength near resistance",
        "failed to hold the push and is slipping back under resistance",
    ],
}


def _pick(options: list[str], seed: bytes, offset: int) -> str:
    return options[seed[offset % len(seed)] % len(options)]


def _fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if value >= 0.01:
        return f"{value:.5f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _format_timeframe(value: str) -> str:
    normalized = str(value).strip().lower()
    labels = {"15m": "15M", "1h": "1H", "4h": "4H", "1d": "1D"}
    return labels.get(normalized, str(value).upper())


def _format_signal_detail_lines(
    signal: Signal,
    seed: bytes,
    *,
    vary_layout: bool = True,
) -> list[str]:
    """Format the exact signal levels using one of several readable layouts.

    Layout choice is deterministic for a signal/account/style seed. Numbers never
    change; only line breaks and separators vary so a batch does not look templated.
    """

    entry = f"Entry: {_fmt(signal.entry_low)}–{_fmt(signal.entry_high)}"
    tp1 = f"TP1: {_fmt(signal.tp1)}"
    tp2 = f"TP2: {_fmt(signal.tp2)}"
    tp3 = f"TP3: {_fmt(signal.tp3)}"
    stop = f"Stop Loss: {_fmt(signal.stop_loss)}"

    if not vary_layout:
        return [entry, f"{tp1} | {tp2} | {tp3}", stop]

    layout = seed[19 % len(seed)] % 6
    if layout == 0:
        return [entry, f"{tp1} | {tp2} | {tp3}", stop]
    if layout == 1:
        return [entry, f"{tp1} / {tp2} / {tp3}", stop]
    if layout == 2:
        return [f"{entry} | {stop}", f"{tp1} / {tp2} / {tp3}"]
    if layout == 3:
        return [entry, f"{tp1} → {tp2} → {tp3}", stop]
    if layout == 4:
        return [f"{entry}  •  {stop}", f"{tp1} | {tp2} | {tp3}"]
    return [f"{entry} | {tp1} / {tp2} / {tp3} | {stop}"]


def _market_context(settings: dict, side: str) -> str:
    market_type = str(settings.get("market", {}).get("market_type", "spot")).strip().lower()
    caption_cfg = settings.get("captions", {})
    if market_type in {"future", "futures", "perpetual", "perp"}:
        return str(caption_cfg.get("futures_context_label", "Futures"))
    # A SHORT opinion can still be based on a Spot chart, so call it a setup rather
    # than implying that a native Spot short order is being placed.
    return str(caption_cfg.get("spot_context_label", "Spot chart"))


def _used_openers(recent_phrases: set[str], openers: list[str]) -> set[str]:
    used: set[str] = set()
    for phrase in recent_phrases:
        normalized = phrase.lower().strip()
        for opener in openers:
            opener_lower = opener.lower()
            if normalized.startswith(opener_lower):
                used.add(opener)
    return used


def _used_observations(recent_phrases: set[str], observations: list[str]) -> set[str]:
    used: set[str] = set()
    for phrase in recent_phrases:
        normalized = phrase.lower()
        for observation in observations:
            if observation.lower() in normalized:
                used.add(observation)
    return used


def _compose_line(style: str, opener: str, cashtag: str, observation: str, use_guys: bool) -> str:
    base = f"{cashtag} {observation}"

    # "guys" replaces the opener instead of stacking with it. This prevents awkward
    # constructions such as "guys, worth a look, $XRP...".
    if use_guys:
        return f"guys, {base}"

    if style in {"punchy", "casual"}:
        return f"{opener}, {base}"

    if style == "calm":
        if opener in {"watching", "keeping an eye on", "worth watching", "still tracking", "quietly watching"}:
            return f"{opener} {cashtag}, {observation}"
        return f"{opener} {cashtag}, {observation}"

    if style == "reactive":
        if opener == "looks like":
            return f"looks like {base}"
        return f"{opener} {cashtag}, {observation}"

    if style == "direct":
        return f"{opener} {cashtag}, {observation}"

    if style == "chart_reader":
        return f"{opener} {cashtag}, {observation}"

    return f"{opener}, {base}"


def _slot_index_utc(now: datetime, slots_per_day: int = 96) -> int:
    """Map the current UTC time to one of the 96 fifteen-minute live slots."""

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    utc_now = now.astimezone(timezone.utc)
    index = (utc_now.hour * 60 + utc_now.minute) // 15
    return max(0, min(slots_per_day - 1, index))


def _daily_leverage_slots(
    day: date,
    daily_options: list[int],
    *,
    slots_per_day: int = 96,
    account_id: str = "global",
) -> set[int]:
    """Choose a deterministic per-account daily subset without persistent state.

    Every account receives its own distribution, so leverage captions do not appear
    on all Binance accounts during the same 15-minute slots. The configured quota is
    still exact for each account when all scheduled slots run.
    """

    valid_options = sorted({
        max(0, min(slots_per_day, int(value)))
        for value in daily_options
    })
    if not valid_options:
        return set()
    scope = str(account_id).strip() or "global"
    day_seed = hashlib.sha256(
        f"coincoach-leverage-day:{scope}:{day.isoformat()}".encode()
    ).digest()
    quota = valid_options[int.from_bytes(day_seed[:2], "big") % len(valid_options)]
    ranked = sorted(
        range(slots_per_day),
        key=lambda slot: hashlib.sha256(
            f"coincoach-leverage-slot:{scope}:{day.isoformat()}:{slot}".encode()
        ).digest(),
    )
    return set(ranked[:quota])


def _leverage_value(signal: Signal, seed: bytes, leverage_cfg: dict) -> int:
    # bStock / tokenized TradFi gets a hard 5x override. Normal crypto keeps
    # the configured leverage ladder (including the existing high-leverage values).
    market_tags = {str(tag).lower() for tag in signal.facts.get("market_tags", [])}
    if "tradfi" in market_tags or "bstock" in market_tags:
        return 5
    by_timeframe = leverage_cfg.get("values_by_timeframe", {})
    values = by_timeframe.get(signal.timeframe, leverage_cfg.get("values", list(range(20, 51))))
    normalized = [int(value) for value in values if int(value) > 0]
    if not normalized:
        normalized = [10]
    return normalized[seed[23 % len(seed)] % len(normalized)]


def _inject_leverage_phrase(opening: str, signal: Signal, leverage: int, seed: bytes) -> str:
    """Blend leverage naturally inside the opening sentence.

    Leverage is never emitted as a standalone line, sentence prefix, or sentence
    ending. It sits beside the cashtag as a human-sounding aside while the original
    market observation remains the main point of the line.
    """

    cashtag = f"${signal.base_asset}"
    clauses = [
        f"with leverage near {leverage}x",
        f"with the setup mapped around {leverage}x",
        f"keeping leverage around {leverage}x",
        f"with roughly {leverage}x on the setup",
        f"with a {leverage}x leverage plan",
        f"while I keep leverage near {leverage}x",
        f"with leverage sitting around {leverage}x",
        f"with {leverage}x as the working leverage",
    ]
    clause = clauses[seed[24 % len(seed)] % len(clauses)]

    position = opening.find(cashtag)
    if position >= 0:
        end = position + len(cashtag)
        before = opening[:end]
        after = opening[end:]
        if after.startswith(","):
            return f"{before}, {clause}{after}"
        return f"{before}, {clause},{after}"

    if "," in opening:
        first, rest = opening.split(",", 1)
        return f"{first}, {clause}, {rest.lstrip()}"
    return f"{opening} while {clause} remains in view"


def leverage_slot_active(
    caption_cfg: dict,
    now: datetime,
    account_id: str = "global",
) -> bool:
    leverage_cfg = caption_cfg.get("leverage", {})
    if not bool(leverage_cfg.get("enabled", False)):
        return False
    slots_per_day = int(leverage_cfg.get("slots_per_day", 96))
    daily_options = [
        int(value)
        for value in leverage_cfg.get("daily_post_options", [30])
    ]
    utc_now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    slot_index = _slot_index_utc(utc_now, slots_per_day)
    return slot_index in _daily_leverage_slots(
        utc_now.date(),
        daily_options,
        slots_per_day=slots_per_day,
        account_id=account_id,
    )


def _maybe_add_leverage(
    opening: str,
    signal: Signal,
    seed: bytes,
    caption_cfg: dict,
    now: datetime,
    account_id: str,
) -> str:
    leverage_cfg = caption_cfg.get("leverage", {})
    if not leverage_slot_active(caption_cfg, now, account_id):
        return opening

    # bStock / TradFi always uses the fixed 5x rule.
    # Normal crypto can receive leverage regardless of whether the scanned
    # market is currently marked perpetual-eligible. This allows the
    # configured daily leverage slots to work for spot-scanned crypto too.
    leverage = _leverage_value(signal, seed, leverage_cfg)
    return _inject_leverage_phrase(opening, signal, leverage, seed)


def _hashtag_line(signal: Signal, seed: bytes, hashtag_cfg: dict) -> str:
    """Build exactly four hashtags: asset first, then rotate the other three groups."""
    groups = hashtag_cfg or {}
    configured = [
        ("binance", list(groups.get("binance", ["#Binance", "#binance"]))),
        ("write2earn", list(groups.get("write2earn", ["#Write2Earn", "#Write2Earn!"]))),
        ("crypto", list(groups.get("crypto", ["#crypto", "#Crypto"]))),
    ]

    def choose(options: list[str], offset: int) -> str:
        valid = [str(value).strip() for value in options if str(value).strip()]
        return valid[seed[offset % len(seed)] % len(valid)] if valid else ""

    # The coin hashtag is ALWAYS first.
    asset = str(signal.base_asset).strip().upper().replace(" ", "")
    tags = [f"#{asset}"]

    # Rotate the three other hashtag groups.
    order = sorted(
        range(len(configured)),
        key=lambda index: seed[(17 + index * 7) % len(seed)],
    )

    for position, index in enumerate(order):
        _, options = configured[index]
        tag = choose(options, 7 + position * 11)
        if tag:
            tags.append(tag)

    return " ".join(tags)

def _human_analysis_paragraph(signal: Signal, seed: bytes) -> str:
    """Explain the chart thesis using measured signal facts and attributed news."""
    facts = signal.facts or {}
    ema_fast = float(facts.get("ema20", 0) or 0)
    ema_slow = float(facts.get("ema50", 0) or 0)
    rsi_value = float(facts.get("rsi", 50) or 50)
    volume_ratio = float(facts.get("volume_ratio", 1) or 1)
    support = float(facts.get("support_level", 0) or 0)
    resistance = float(facts.get("resistance_level", 0) or 0)

    choose = lambda items, offset: items[seed[offset % len(seed)] % len(items)]
    if ema_fast > ema_slow * 1.001:
        trend = choose([
            "The short-term trend is still above its slower average, which keeps the broader structure constructive.",
            "Price structure is leaning upward: the faster average remains above the slower trend line.",
            "The faster average is holding over the slower one, so buyers still have the trend advantage for now.",
        ], 2)
    elif ema_fast and ema_slow and ema_fast < ema_slow * 0.999:
        trend = choose([
            "The faster average is below the slower trend line, so sellers still have the broader structure on their side.",
            "The trend remains under pressure, with the short-term average sitting beneath the slower one.",
            "The moving-average structure is still bearish; any bounce needs to prove it can hold before the bias changes.",
        ], 3)
    else:
        trend = choose([
            "The moving averages are close together, so this is a level-led setup rather than a strong trend call.",
            "Trend direction is not especially clear yet; the nearby levels and the next price reaction matter more here.",
        ], 4)

    if rsi_value >= 70:
        momentum = choose([
            f"RSI is {rsi_value:.1f}, showing strong momentum but also warning against chasing an extended move.",
            f"Momentum is strong with RSI at {rsi_value:.1f}; the risk now is paying up after the move has already stretched.",
        ], 5)
    elif rsi_value <= 30:
        momentum = choose([
            f"RSI is {rsi_value:.1f}; momentum is weak, so a reversal needs confirmation rather than an assumption.",
            f"RSI has slipped to {rsi_value:.1f}. That shows pressure, but it does not make a bounce automatic.",
        ], 6)
    elif rsi_value >= 55:
        momentum = choose([
            f"RSI is {rsi_value:.1f}, which points to positive momentum without being deeply overbought.",
            f"The RSI reading of {rsi_value:.1f} suggests buyers have the initiative, while leaving some room before an overheated reading.",
        ], 7)
    elif rsi_value <= 45:
        momentum = choose([
            f"RSI is {rsi_value:.1f}, showing that buyers have not yet regained clear momentum.",
            f"With RSI at {rsi_value:.1f}, upside momentum still needs to improve before the move looks convincing.",
        ], 8)
    else:
        momentum = f"RSI is {rsi_value:.1f}, a fairly balanced reading that leaves the next price reaction to decide direction."

    if volume_ratio >= 1.5:
        participation = choose([
            f"Volume is running about {volume_ratio:.1f}x its recent average, giving this move better participation.",
            f"Activity is up to roughly {volume_ratio:.1f}x normal volume, so the move is attracting more participation than usual.",
        ], 9)
    elif volume_ratio <= 0.75:
        participation = choose([
            f"Volume is only about {volume_ratio:.1f}x its recent average, so stronger participation would help confirm the move.",
            f"Participation is still light at {volume_ratio:.1f}x average volume; I would not treat this as confirmed momentum yet.",
        ], 10)
    else:
        participation = f"Volume is close to its recent average ({volume_ratio:.1f}x), so follow-through will matter more than one candle."

    change_24h = facts.get("change_percent_24h")
    if isinstance(change_24h, (int, float)):
        movement = choose([
            f"{signal.base_asset} is {abs(float(change_24h)):.2f}% {'higher' if float(change_24h) >= 0 else 'lower'} over 24 hours and is trading around {_fmt(signal.current_price)}.",
            f"The 24-hour move stands at {float(change_24h):+.2f}%, with the latest price near {_fmt(signal.current_price)}.",
        ], 11)
    else:
        movement = f"The latest price is around {_fmt(signal.current_price)}."

    if support > 0 and resistance > 0:
        levels = choose([
            f"The recent support area is around {_fmt(support)}, while resistance is near {_fmt(resistance)}.",
            f"The nearby map is support around {_fmt(support)} and overhead resistance close to {_fmt(resistance)}.",
        ], 12)
    elif support > 0:
        levels = f"The nearest measured support area is around {_fmt(support)}."
    elif resistance > 0:
        levels = f"The nearest measured resistance area is around {_fmt(resistance)}."
    else:
        levels = "The entry band and stop define the key levels for this plan."

    if signal.side == "LONG":
        entry = choose([
            "For the long, the entry band gives a defined pullback area to assess instead of buying a stretched candle.",
            "What makes the long interesting is the planned entry zone: it lets price come to the setup rather than chasing it.",
            "Before taking the long, I would want price to respect the entry area and hold its structure rather than spike and fade.",
        ], 13)
    else:
        entry = choose([
            "The planned entry is an area to watch for a failed bounce; it avoids chasing an already extended drop.",
            "The short idea depends on sellers defending the entry zone, not simply on price falling quickly.",
            "A weak reaction around the entry band would support the plan; a clean reclaim would weaken it.",
        ], 14)

    rank = int(facts.get("scan_rank", 0) or 0)
    pool_size = int(facts.get("scan_pool_size", 0) or 0)
    ranking = ""
    if pool_size > 1 and 1 <= rank <= 3:
        if rank == 1:
            ranking = f"Among {pool_size} setups that passed the scan filters, this currently ranks first; the entry still needs to hold before it becomes actionable."
        else:
            ranking = f"This sits in the top three of {pool_size} setups that passed the scan filters, with the mapped entry and stop defining the risk."

    blocks = [trend, movement, momentum, participation, levels, entry, ranking]
    orders = [
        (1, 0, 2, 3, 4, 5, 6),
        (0, 4, 1, 2, 5, 3, 6),
        (2, 1, 0, 4, 3, 5, 6),
        (5, 0, 1, 3, 2, 4, 6),
    ]
    order = choose(orders, 15)
    paragraph = " ".join(blocks[index] for index in order if blocks[index])

    news_rows = facts.get("news_context", [])
    if isinstance(news_rows, list) and news_rows:
        event = news_rows[0]
        title = " ".join(str(event.get("title", "")).split())[:150].rstrip(" .")
        source = str(event.get("source", "market news")).strip()
        url = str(event.get("url", "")).strip()
        evidence = str(event.get("evidence_level", "reported_media"))
        published = str(event.get("published_at", "")).strip()
        time_note = ""
        try:
            event_time = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            time_note = f" Published {event_time.astimezone(timezone.utc):%d %b %H:%M UTC}."
        except (TypeError, ValueError):
            pass
        sentiment = str(event.get("sentiment", "neutral")).lower()
        if title and evidence == "watched_x_profile_post":
            handle = str(event.get("account_handle", "")).strip()
            profile = f"@{handle}" if handle else source
            news_line = f"A recent post from the watched X profile {profile} says: ‘{title}’. Treat the post as a catalyst to verify, not as trade confirmation.{time_note}"
        elif title and sentiment == "negative":
            news_line = f"Risk context: {source} reported: ‘{title}’. This is reported information, so check the original notice and the market's reaction before acting on it.{time_note}"
        elif title and sentiment == "positive":
            news_line = f"Potential catalyst: {source} reported: ‘{title}’. This is reported context; confirm the project's own announcement and market reaction before treating it as actionable.{time_note}"
        elif title:
            news_line = f"Latest reported context from {source}: ‘{title}’. Check the primary announcement and price reaction before treating it as actionable.{time_note}"
        else:
            news_line = ""
        if news_line:
            if url:
                news_line += f" Source: {url}"
            paragraph += " " + news_line
    return paragraph
def enforce_caption_maximum_length(
    caption: str,
    analysis_paragraph: str,
    maximum_length: int,
) -> str:
    """Remove optional analysis before enforcing the caption length limit."""
    if maximum_length < 1 or len(caption) <= maximum_length:
        return caption

    content = caption
    if analysis_paragraph:
        content = content.replace(analysis_paragraph, "", 1)

    cleaned_lines = []
    previous_blank = True

    for line in content.splitlines():
        line = line.rstrip()
        if line:
            cleaned_lines.append(line)
            previous_blank = False
        elif cleaned_lines and not previous_blank:
            cleaned_lines.append("")
            previous_blank = True

    while cleaned_lines and not cleaned_lines[-1]:
        cleaned_lines.pop()

    result = "\n".join(cleaned_lines)

    if len(result) > maximum_length:
        raise ValueError(
            "Required trade details exceed the configured caption maximum length."
        )

    return result


class CaptionEngine:
    def __init__(
        self,
        storage: Storage,
        settings: dict,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.settings = settings
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def generate(self, signal: Signal, account_id: str, style: str) -> str:
        seed = hashlib.sha256(f"{signal.signal_id}:{account_id}:{style}".encode()).digest()
        max_recent = int(self.settings["captions"]["maximum_recent_phrases"])
        recent = self.storage.recent_phrases(account_id, max_recent)
        openers = STYLE_OPENERS.get(style, STYLE_OPENERS["casual"])
        observations = SETUP_OBSERVATIONS.get(signal.setup, ["is setting up around this level"])
        cashtag = f"${signal.base_asset}"
        used_openers = _used_openers(recent, openers)
        used_observations = _used_observations(recent, observations)

        # Use "guys" only occasionally and never as the default voice.
        guys_probability = float(self.settings["captions"].get("guys_probability", 0.05))
        guys_unit = int.from_bytes(seed[:2], "big") / 65535
        use_guys = guys_unit < guys_probability and not any(p.lower().startswith("guys") for p in recent)

        candidates: list[tuple[str, str, str]] = []
        attempts = max(len(openers), len(observations)) * 8
        for index in range(attempts):
            opener = _pick(openers, seed, index + 2)
            observation = _pick(observations, seed, index + 11)

            if style == "direct":
                if signal.side == "SHORT" and opener in {
                    "long setup on", "taking the long side on", "watching long on", "leaning long on"
                }:
                    opener = "watching short on"
                elif signal.side == "LONG" and opener in {
                    "short setup on", "the short side looks cleaner on", "watching short on", "leaning short on"
                }:
                    opener = "watching long on"

            line = _compose_line(style, opener, cashtag, observation, use_guys and index == 0)
            line = line.replace("  ", " ").strip()
            candidates.append((opener, observation, line))

        # Prefer a fresh full line, opener, and observation. This stops a five-post
        # batch from sounding like the same template with a different coin ticker.
        opening = None
        for opener, observation, line in candidates:
            if line not in recent and opener not in used_openers and observation not in used_observations:
                opening = line
                break

        if opening is None:
            for opener, observation, line in candidates:
                if line not in recent and observation not in used_observations:
                    opening = line
                    break

        if opening is None:
            opening = next((line for _, _, line in candidates if line not in recent), candidates[0][2])

        caption_cfg = self.settings.get("captions", {})
        opening = _maybe_add_leverage(
            opening, signal, seed, caption_cfg, self.now_provider(), account_id
        )
        self.storage.remember_phrase(account_id, opening)
        analysis_paragraph = ""
        if bool(caption_cfg.get("include_market_analysis", True)):
            for variant_index in range(12):
                variant_seed = hashlib.sha256(seed + f":analysis:{variant_index}".encode()).digest()
                candidate_analysis = _human_analysis_paragraph(signal, variant_seed)
                if candidate_analysis not in recent:
                    analysis_paragraph = candidate_analysis
                    break
            if not analysis_paragraph:
                analysis_paragraph = _human_analysis_paragraph(signal, seed)
        if analysis_paragraph:
            self.storage.remember_phrase(account_id, analysis_paragraph)
        signal_detail_lines = _format_signal_detail_lines(
            signal,
            seed,
            vary_layout=bool(caption_cfg.get("vary_signal_detail_layout", True)),
        )

        parts = [opening, ""]
        if analysis_paragraph:
            parts.extend([analysis_paragraph, ""])

        # Direction / market / timeframe context is optional. CoinCoach's default public
        # format keeps the caption cleaner and lets the official chart show the timeframe.
        if bool(caption_cfg.get("include_context_line", False)):
            direction = "Long" if signal.side == "LONG" else "Short"
            timeframe = _format_timeframe(signal.timeframe)
            market_context = _market_context(self.settings, signal.side)
            direction_label = (
                f"{direction} setup"
                if market_context.lower().startswith("spot")
                else direction
            )
            parts.append(f"{direction_label} | {market_context} | {timeframe}")

        parts.extend(signal_detail_lines)

        # Risk-note text is also opt-in. The default no longer appends "NFA. DYOR.".
        risk_note = str(caption_cfg.get("risk_note", "")).strip()
        if bool(caption_cfg.get("include_risk_note", False)) and risk_note:
            parts.extend(["", risk_note])

        parts.extend(["", _hashtag_line(signal, seed, caption_cfg.get("hashtags", {}))])
        caption = "\n".join(parts)

        if bool(caption_cfg.get("enforce_maximum_length", False)):
            caption = enforce_caption_maximum_length(
                caption,
                analysis_paragraph,
                int(caption_cfg.get("maximum_length", 0) or 0),
            )

        return caption
