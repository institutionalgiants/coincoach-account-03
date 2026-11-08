from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from app.config import load_config
from app.news_intelligence import NewsIntelligence
from app.binance_market import BinanceMarketClient


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root)
    market = BinanceMarketClient()
    try:
        market_cfg = config.settings.get("market", {})
        quote_assets = [str(value).upper() for value in market_cfg.get("quote_assets", ["USDT"])]
        tasks = [market.universe(quote) for quote in quote_assets]
        if bool(market_cfg.get("include_futures_universe", False)):
            futures_quotes = [str(value).upper() for value in market_cfg.get("futures_quote_assets", ["USDT"])]
            tasks.extend(market.futures_universe(quote) for quote in futures_quotes)
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        assets: set[str] = set()
        for response in responses:
            if isinstance(response, dict):
                assets.update(str(item.base_asset).upper() for item in response.values() if getattr(item, "base_asset", None))
        intelligence = NewsIntelligence(config.settings)
        target = Path(os.getenv("NEWS_CACHE_PATH", "data/news/account_03/news_intelligence.json"))
        if not target.is_absolute():
            target = root / target
        cached = intelligence.load_cache(
            target, assets,
            max_age_minutes=int(intelligence.news_cfg.get("cache_max_age_minutes", 20)),
        )
        fresh = await intelligence.scan(assets)
        grouped = intelligence.merge(cached, fresh)
        saved = intelligence.save_cache(target, grouped)
        print(json.dumps({"assets_scanned": len(assets), "events_cached": saved, "stats": intelligence.stats, "cache_path": str(target)}, indent=2))
    finally:
        await market.close()


if __name__ == "__main__":
    asyncio.run(main())
