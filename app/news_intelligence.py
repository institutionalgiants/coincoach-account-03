from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import httpx


DEFAULT_FEEDS = [
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "domain": "coindesk.com"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss", "domain": "cointelegraph.com"},
    {"name": "The Block", "url": "https://www.theblock.co/rss.xml", "domain": "theblock.co"},
    {"name": "Decrypt", "url": "https://decrypt.co/feed", "domain": "decrypt.co"},
]

DEFAULT_WATCHED_X_ACCOUNTS = {
    "elonmusk": "Elon Musk",
    "cz_binance": "Changpeng Zhao",
    "VitalikButerin": "Vitalik Buterin",
    "saylor": "Michael Saylor",
}

POSITIVE_TERMS = {
    "partnership", "partners", "integrates", "integration", "launch", "launches",
    "launched", "listing", "listed", "approval", "approved", "adoption", "mainnet",
    "upgrade", "funding", "investment", "inflows", "accumulation", "expands",
    "expansion", "support added", "adds support", "bullish", "surges", "rallies",
    "soars", "breakout", "wins", "etf", "acquisition", "collaboration", "launches on",
}
NEGATIVE_TERMS = {
    "hack", "hacked", "exploit", "exploited", "breach", "delist", "delisting",
    "lawsuit", "investigation", "probe", "charges", "charged", "scam", "rug pull",
    "suspension", "shutdown", "insolvency", "bankruptcy", "outflows", "ban", "banned",
    "fine", "attack", "theft", "stolen", "vulnerability", "withdrawals suspended",
    "liquidation", "liquidations", "crash", "warning", "warns", "depeg", "depegged",
}
AMBIGUOUS_TICKERS = {"ONE", "GAS", "HOT", "LINK", "OP", "INJ", "NEAR", "ATOM", "DOT", "TRX"}


def _local_name(tag: str) -> str:
    return str(tag).split("}")[-1].lower()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _x_poll_due(now: datetime, interval_minutes: int) -> bool:
    interval = max(1, int(interval_minutes))
    current = _utc(now)
    return (current.hour * 60 + current.minute) % interval == 0


class NewsIntelligence:
    """Lightweight fail-open scanner. News raises/lower ranks; it never creates trades."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.discovery = settings.get("market", {}).get("discovery", {})
        self.news_cfg = self.discovery.get("news", {})
        self.stats: dict[str, int] = {"feeds_checked": 0, "feed_errors": 0, "events_found": 0, "x_accounts_checked": 0}

    def _asset_aliases(self, asset: str) -> list[str]:
        custom = self.news_cfg.get("asset_aliases", {}).get(asset, [])
        aliases = [asset]
        if isinstance(custom, str):
            aliases.append(custom)
        elif isinstance(custom, Iterable):
            aliases.extend(str(item) for item in custom)
        return [item.strip() for item in aliases if item and item.strip()]

    def _find_assets(self, text: str, assets: list[str]) -> list[str]:
        found: list[str] = []
        for asset in assets:
            matched = False
            for alias in self._asset_aliases(asset):
                if alias.upper() == asset.upper():
                    if len(asset) <= 4 or asset.upper() in AMBIGUOUS_TICKERS:
                        pattern = rf"(?<![A-Za-z0-9])\${re.escape(asset)}(?![A-Za-z0-9])|(?<![A-Za-z0-9]){re.escape(asset)}(?![A-Za-z0-9])"
                        matched = re.search(pattern, text) is not None
                    else:
                        pattern = rf"(?<![A-Za-z0-9])\$?{re.escape(asset)}(?![A-Za-z0-9])"
                        matched = re.search(pattern, text, re.IGNORECASE) is not None
                else:
                    pattern = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])"
                    matched = re.search(pattern, text, re.IGNORECASE) is not None
                if matched:
                    break
            if matched:
                found.append(asset.upper())
        return found

    def _sentiment(self, text: str) -> tuple[str, float, list[str]]:
        lowered = text.lower()
        positive = [term for term in POSITIVE_TERMS if term in lowered]
        negative = [term for term in NEGATIVE_TERMS if term in lowered]
        if negative and (len(negative) >= len(positive) or any(term in lowered for term in ("hack", "exploit", "delist", "rug pull", "withdrawals suspended", "depeg"))):
            return "negative", -min(8.0, 3.0 + len(negative)), negative[:4]
        if positive:
            return "positive", min(6.0, 2.0 + len(positive)), positive[:4]
        return "neutral", 0.0, []

    def _people(self, text: str) -> list[str]:
        people = self.news_cfg.get("watched_people", ["Elon Musk", "Changpeng Zhao", "Vitalik Buterin", "Michael Saylor"])
        return [str(person) for person in people if str(person).lower() in text.lower()]

    def parse_rss_content(self, content: str, *, source: str, feed_url: str, assets: list[str], now: datetime | None = None) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return []
        current = _utc(now or datetime.now(timezone.utc))
        cutoff = current - timedelta(hours=max(1, int(self.news_cfg.get("max_age_hours", 8))))
        events: list[dict[str, Any]] = []
        for item in [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}][:50]:
            values: dict[str, str] = {}
            for child in list(item):
                name = _local_name(child.tag)
                if name == "link":
                    values[name] = str(child.attrib.get("href") or child.text or "").strip()
                elif name in {"title", "description", "summary", "content", "pubdate", "published", "updated", "date"}:
                    values[name] = " ".join("".join(child.itertext()).split())
            title = values.get("title", "").strip()
            if not title:
                continue
            published = _parse_datetime(values.get("pubdate") or values.get("published") or values.get("updated") or values.get("date"))
            if published is None or published < cutoff or published > current + timedelta(minutes=5):
                continue
            body = values.get("description", "") or values.get("summary", "") or values.get("content", "")
            story = f"{title} {body}"
            matches = self._find_assets(story, assets)
            if not matches:
                continue
            sentiment, impact, catalysts = self._sentiment(story)
            people = self._people(story)
            link = values.get("link", "")
            host = (urlparse(feed_url).hostname or "").lower().removeprefix("www.")
            link_parts = urlparse(link)
            link_host = (link_parts.hostname or "").lower().removeprefix("www.")
            # Do not surface an RSS entry whose source link points off-domain or
            # uses a non-HTTPS destination; attribution must remain auditable.
            if (
                not host or link_parts.scheme.lower() != "https" or not link_host
                or not (link_host == host or link_host.endswith("." + host))
            ):
                continue
            for asset in matches:
                events.append({
                    "asset": asset, "title": title[:240], "summary": body[:420],
                    "source": source, "source_domain": host, "url": link,
                    "published_at": published.isoformat(), "sentiment": sentiment,
                    "impact_score": impact, "catalysts": catalysts,
                    "watched_people": people, "evidence_level": "reported_media",
                    "confidence": 0.58,
                })
        return events

    async def _fetch_feed(self, client: httpx.AsyncClient, feed: dict[str, Any], assets: list[str]) -> list[dict[str, Any]]:
        url = str(feed.get("url", "")).strip()
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        expected_domain = str(feed.get("domain", host)).lower().removeprefix("www.")
        if not url.startswith("https://") or not host or not (host == expected_domain or host.endswith("." + expected_domain)):
            return []
        try:
            response = await client.get(url)
            response.raise_for_status()
            final_host = (urlparse(str(response.url)).hostname or "").lower().removeprefix("www.")
            if not final_host or not (final_host == expected_domain or final_host.endswith("." + expected_domain)):
                return []
            return self.parse_rss_content(response.text, source=str(feed.get("name", expected_domain)), feed_url=str(response.url), assets=assets)
        except (httpx.HTTPError, ValueError):
            self.stats["feed_errors"] += 1
            return []

    async def _fetch_x(self, client: httpx.AsyncClient, assets: list[str]) -> list[dict[str, Any]]:
        token = os.getenv("X_API_BEARER_TOKEN", "").strip()
        if not token:
            return []
        handles = self.news_cfg.get("watched_x_accounts", DEFAULT_WATCHED_X_ACCOUNTS)
        if not isinstance(handles, dict):
            return []
        headers = {"Authorization": f"Bearer {token}"}
        events: list[dict[str, Any]] = []
        for handle, display_name in handles.items():
            self.stats["x_accounts_checked"] += 1
            try:
                user_response = await client.get(f"https://api.x.com/2/users/by/username/{handle}", headers=headers, params={"user.fields": "username,name"})
                user_response.raise_for_status()
                user = user_response.json().get("data", {})
                # Handles are an explicit, human-maintained watchlist. Do not infer
                # identity from a platform verification badge or badge subscription.
                if str(user.get("username", "")).lower() != str(handle).lower():
                    continue
                user_id = str(user.get("id", ""))
                if not user_id:
                    continue
                tweet_response = await client.get(f"https://api.x.com/2/users/{user_id}/tweets", headers=headers, params={"max_results": 10, "tweet.fields": "created_at,entities", "exclude": "retweets,replies"})
                tweet_response.raise_for_status()
                for tweet in tweet_response.json().get("data", []):
                    story = str(tweet.get("text", ""))
                    published = _parse_datetime(tweet.get("created_at"))
                    if not published or published < datetime.now(timezone.utc) - timedelta(hours=max(1, int(self.news_cfg.get("max_age_hours", 8)))):
                        continue
                    for asset in self._find_assets(story, assets):
                        sentiment, impact, catalysts = self._sentiment(story)
                        events.append({
                            "asset": asset, "title": story[:240], "summary": story[:420],
                            "source": f"X (@{handle})", "source_domain": "x.com",
                            "url": f"https://x.com/{handle}/status/{tweet.get('id', '')}",
                            "published_at": published.isoformat(), "sentiment": sentiment,
                            "impact_score": impact * 1.15, "catalysts": catalysts,
                            "watched_people": [str(display_name)], "account_handle": str(handle),
                            "evidence_level": "watched_x_profile_post", "confidence": 0.78,
                        })
            except (httpx.HTTPError, ValueError, TypeError):
                # API entitlements and rate limits vary; technical scanning must continue.
                continue
        return events

    @staticmethod
    def load_cache(path: str | Path, assets: Iterable[str], *, max_age_minutes: int = 20, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
        cache_path = Path(path)
        if not cache_path.exists():
            return {}
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_at = _parse_datetime(payload.get("cached_at"))
            current = _utc(now or datetime.now(timezone.utc))
            if cached_at is None or current - cached_at > timedelta(minutes=max_age_minutes):
                return {}
            allowed = {str(asset).upper() for asset in assets}
            events = payload.get("events", [])
            grouped: dict[str, list[dict[str, Any]]] = {}
            for event in events if isinstance(events, list) else []:
                asset = str(event.get("asset", "")).upper()
                published = _parse_datetime(event.get("published_at"))
                if asset in allowed and published and published >= current - timedelta(hours=8):
                    grouped.setdefault(asset, []).append(event)
            return grouped
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    @staticmethod
    def save_cache(path: str | Path, grouped_events: dict[str, list[dict[str, Any]]], *, now: datetime | None = None) -> int:
        current = _utc(now or datetime.now(timezone.utc))
        events = [event for rows in grouped_events.values() for event in rows]
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps({"cached_at": current.isoformat(), "events": events}, indent=2), encoding="utf-8")
        temporary.replace(destination)
        return len(events)

    @staticmethod
    def merge(*groups: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
        merged: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, str]] = set()
        for group in groups:
            for asset, rows in group.items():
                for event in rows:
                    key = (asset, str(event.get("url", "")))
                    if key not in seen:
                        seen.add(key)
                        merged.setdefault(asset, []).append(event)
        for asset, rows in merged.items():
            def priority(event: dict[str, Any]) -> tuple[float, str]:
                try:
                    impact = abs(float(event.get("impact_score", 0.0)))
                except (TypeError, ValueError):
                    impact = 0.0
                return impact, str(event.get("published_at", ""))
            rows.sort(key=priority, reverse=True)
            merged[asset] = rows[:3]
        return merged

    async def scan(self, assets: Iterable[str], *, include_x: bool = True) -> dict[str, list[dict[str, Any]]]:
        normalized = sorted({str(asset).strip().upper() for asset in assets if str(asset).strip()})
        if not normalized or not bool(self.news_cfg.get("enabled", True)):
            return {}
        feeds = self.news_cfg.get("feeds", DEFAULT_FEEDS)
        if not isinstance(feeds, list):
            feeds = DEFAULT_FEEDS
        timeout = max(3.0, float(self.news_cfg.get("timeout_seconds", 8)))
        headers = {"User-Agent": "CoinCoach-Account03-Market-Research/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
            self.stats["feeds_checked"] = len(feeds)
            feed_results = await asyncio.gather(*(self._fetch_feed(client, feed, normalized) for feed in feeds if isinstance(feed, dict)))
            interval = max(1, int(self.news_cfg.get("x_poll_interval_minutes", 15)))
            x_due = include_x and _x_poll_due(datetime.now(timezone.utc), interval)
            x_events = await self._fetch_x(client, normalized) if x_due else []
            if include_x and not x_due and os.getenv("X_API_BEARER_TOKEN", "").strip():
                self.stats["x_poll_deferred"] = 1
        combined = [event for batch in feed_results for event in batch] + x_events
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in combined:
            grouped.setdefault(str(event["asset"]), []).append(event)
        grouped = self.merge(grouped)
        self.stats["events_found"] = sum(len(events) for events in grouped.values())
        return grouped
