from __future__ import annotations

import json
import os
from dataclasses import asdict

from .binance_market import BinanceMarketClient
from .caption_engine import CaptionEngine
from .config import AppConfig
from .router import AccountRouter
from .news_intelligence import NewsIntelligence
from .scanner import UniverseScanner
from .signal_engine import SignalEngine
from .storage import Storage


async def run_cycle(config: AppConfig) -> dict:
    storage = Storage()
    active_accounts = config.active_accounts()

    # Do not scan the whole market when there is no active destination account.
    if not active_accounts:
        preview = {
            "active_account_count": 0,
            "active_accounts": [],
            "account_statuses": config.account_statuses(),
            "shortlist_count": 0,
            "deep_scan_count": 0,
            "candidate_count": 0,
            "signal_count": 0,
            "shortages": {"global_needed": 0, "global_available_unique": 0},
            "posts": [],
            "message": "No active account. Add a Square key to .env for an enabled account.",
        }
        preview_path = config.root / "output" / "previews" / "latest_cycle.json"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(json.dumps(preview, indent=2), encoding="utf-8")
        return preview

    market = BinanceMarketClient()
    scanner = UniverseScanner(market, config.settings)
    engine = SignalEngine(config.settings)
    caption_engine = CaptionEngine(storage, config.settings)
    try:
        shortlist = await scanner.shortlist()
        news_intelligence = NewsIntelligence(config.settings)
        news_by_asset = {}

        # Account 03 news is opt-in and may only influence an Account 03-only run.
        news_enabled = os.getenv("ACCOUNT03_NEWS_ENABLED", "").strip().lower() in {
            "1", "true", "yes", "on"
        }

        if news_enabled:
            active_account_ids = {account.id for account in active_accounts}
            if active_account_ids != {"account_03"}:
                raise RuntimeError(
                    "Account 03 news cannot be used in a multi-account run"
                )

            cache_path = os.getenv("NEWS_CACHE_PATH", "").strip()
            if not cache_path:
                raise RuntimeError(
                    "ACCOUNT03_NEWS_ENABLED requires NEWS_CACHE_PATH"
                )

            assets = [item.base_asset for item in shortlist]
            news_by_asset = news_intelligence.load_cache(
                cache_path,
                assets,
                max_age_minutes=int(
                    news_intelligence.news_cfg.get("cache_max_age_minutes", 20)
                ),
            )

            # The dedicated news-scan workflow is the sole cache writer.
            # Account 03 publishing consumes this cache read-only.
            for market_symbol in shortlist:
                market_symbol.news_context = news_by_asset.get(
                    market_symbol.base_asset.upper(), []
                )
        deep_items = await scanner.deep_scan(shortlist)
        candidates = []
        for item in deep_items:
            candidates.extend(engine.detect(item))

        minimum = float(config.settings["signals"]["tier_c_minimum_score"])
        signals = []
        cooldown = int(config.settings["signals"]["symbol_cooldown_hours"])
        for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
            if candidate.score < minimum:
                continue
            if storage.symbol_in_cooldown(candidate.symbol.symbol, cooldown):
                continue
            signal = engine.build_signal(candidate)
            if signal is not None:
                signals.append(signal)

        for rank, signal in enumerate(signals, 1):
            signal.facts["scan_rank"] = rank
            signal.facts["scan_pool_size"] = len(signals)

        router = AccountRouter(config.settings)
        routed, shortages = router.route(
            signals,
            active_accounts,
            lambda signal, account_id, style: caption_engine.generate(signal, account_id, style),
        )
        preview_path = config.root / "output" / "previews" / "latest_cycle.json"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview = {
            "active_account_count": len(active_accounts),
            "active_accounts": [account.id for account in active_accounts],
            "account_statuses": config.account_statuses(),
            "universe_filter_stats": scanner.last_filter_stats,
            "news_intelligence": {**news_intelligence.stats, "assets_with_events": len(news_by_asset)},
            "shortlist_count": len(shortlist),
            "deep_scan_count": len(deep_items),
            "candidate_count": len(candidates),
            "signal_count": len(signals),
            "shortages": shortages,
            "posts": [
                {
                    "account_id": post.account_id,
                    "style": post.style,
                    "slot_time": post.slot_time.isoformat(),
                    "caption": post.caption,
                    "signal": asdict(post.signal),
                    "image_path": post.image_path,
                }
                for post in routed
            ],
        }
        preview_path.write_text(json.dumps(preview, indent=2, default=str), encoding="utf-8")
        for post in routed:
            storage.save_signal(post.signal, post.account_id, post.caption, post.image_path, "queued_dry_run")
        return preview
    finally:
        storage.close()
        await market.close()
