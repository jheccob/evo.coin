from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import math
import xml.etree.ElementTree as ET

import requests

import config


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _symbol_aliases(symbol: str) -> set[str]:
    normalized = _normalize_symbol(symbol)
    aliases = {normalized}
    if normalized.startswith("BTC/"):
        aliases.update({"BTC", "BITCOIN"})
    if normalized.startswith("XLM/"):
        aliases.update({"XLM", "STELLAR", "STELLAR LUMENS"})
    return aliases


@dataclass
class _CachedPayload:
    payload: dict
    fetched_at: datetime


class MarketContextService:
    def __init__(self, ttl_sec: int | None = None, timeout_sec: float | None = None, session=None):
        self.ttl_sec = int(ttl_sec if ttl_sec is not None else getattr(config, "AI_WEB_CONTEXT_CACHE_TTL_SEC", 900))
        self.timeout_sec = float(
            timeout_sec if timeout_sec is not None else getattr(config, "AI_WEB_CONTEXT_TIMEOUT_SEC", 8.0)
        )
        self.session = session or requests.Session()
        self._fear_cache: _CachedPayload | None = None
        self._news_cache: dict[str, _CachedPayload] = {}

    def get_context(self, symbol: str) -> dict:
        resolved_symbol = _normalize_symbol(symbol)
        context = {
            "symbol": resolved_symbol,
            "fetched_at_utc": _utc_now().isoformat(),
            "fear_greed": self._get_fear_and_greed_context(),
            "news": self._get_news_context(resolved_symbol),
        }
        context["bias"] = self._build_bias_summary(context["fear_greed"], context["news"])
        return context

    def _is_fresh(self, cached: _CachedPayload | None) -> bool:
        if cached is None:
            return False
        age_sec = (_utc_now() - cached.fetched_at).total_seconds()
        return age_sec <= max(self.ttl_sec, 0)

    def _get_fear_and_greed_context(self) -> dict:
        if not bool(getattr(config, "AI_FEAR_GREED_ENABLED", True)):
            return {"enabled": False, "available": False, "reason": "fear_greed_disabled"}

        if self._is_fresh(self._fear_cache):
            payload = dict(self._fear_cache.payload)
            payload["cached"] = True
            return payload

        url = str(getattr(config, "AI_FEAR_GREED_API_URL", "https://api.alternative.me/fng/?limit=1&format=json"))
        try:
            response = self.session.get(url, timeout=self.timeout_sec)
            response.raise_for_status()
            raw_payload = response.json() or {}
            data_rows = raw_payload.get("data") or []
            latest = data_rows[0] if data_rows else {}
            value = _safe_int(latest.get("value"), 50)
            classification = str(latest.get("value_classification") or "unknown").strip() or "unknown"
            timestamp_raw = latest.get("timestamp")
            timestamp_value = None
            if timestamp_raw:
                try:
                    timestamp_value = datetime.fromtimestamp(int(timestamp_raw), tz=UTC).isoformat()
                except (TypeError, ValueError, OSError):
                    timestamp_value = None
            payload = {
                "enabled": True,
                "available": True,
                "cached": False,
                "source": "alternative.me",
                "source_url": "https://alternative.me/crypto/fear-and-greed-index/",
                "api_url": url,
                "value": value,
                "classification": classification,
                "timestamp_utc": timestamp_value,
                "time_until_update_sec": _safe_int(latest.get("time_until_update"), 0),
            }
            self._fear_cache = _CachedPayload(payload=payload, fetched_at=_utc_now())
            return payload
        except Exception as exc:
            return {
                "enabled": True,
                "available": False,
                "cached": False,
                "source": "alternative.me",
                "source_url": "https://alternative.me/crypto/fear-and-greed-index/",
                "api_url": url,
                "reason": f"fear_greed_fetch_failed: {exc}",
            }

    def _get_news_context(self, symbol: str) -> dict:
        if not bool(getattr(config, "AI_NEWS_ENABLED", True)):
            return {"enabled": False, "available": False, "reason": "news_disabled"}

        cached = self._news_cache.get(symbol)
        if self._is_fresh(cached):
            payload = dict(cached.payload)
            payload["cached"] = True
            return payload

        feed_urls = list(getattr(config, "AI_NEWS_FEED_URLS", ["https://www.coindesk.com/arc/outboundfeeds/rss/"]))
        headlines: list[dict] = []
        errors: list[str] = []

        for feed_url in feed_urls:
            try:
                headlines.extend(self._fetch_rss_headlines(feed_url, symbol))
            except Exception as exc:
                errors.append(f"{feed_url}: {exc}")

        lookback_hours = int(getattr(config, "AI_NEWS_LOOKBACK_HOURS", 18) or 18)
        cutoff = _utc_now() - timedelta(hours=max(lookback_hours, 1))
        filtered = []
        for item in headlines:
            published_at = item.get("published_at")
            if published_at:
                try:
                    published_dt = datetime.fromisoformat(str(published_at))
                    if published_dt.tzinfo is None:
                        published_dt = published_dt.replace(tzinfo=UTC)
                    else:
                        published_dt = published_dt.astimezone(UTC)
                    if published_dt < cutoff:
                        continue
                except Exception:
                    pass
            filtered.append(item)

        max_items = int(getattr(config, "AI_NEWS_MAX_ITEMS", 12) or 12)
        filtered = filtered[:max_items]
        sentiment = self._score_news_sentiment(filtered, symbol)
        payload = {
            "enabled": True,
            "available": bool(filtered),
            "cached": False,
            "source": "rss",
            "feed_urls": feed_urls,
            "errors": errors,
            "headline_count": len(filtered),
            "sentiment_score": sentiment["score"],
            "positive_hits": sentiment["positive_hits"],
            "negative_hits": sentiment["negative_hits"],
            "symbol_hits": sentiment["symbol_hits"],
            "headlines": filtered,
        }
        self._news_cache[symbol] = _CachedPayload(payload=payload, fetched_at=_utc_now())
        return payload

    def _fetch_rss_headlines(self, feed_url: str, symbol: str) -> list[dict]:
        response = self.session.get(feed_url, timeout=self.timeout_sec)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = root.findall(".//item")
        aliases = _symbol_aliases(symbol)
        results: list[dict] = []

        for item in items:
            title = str(item.findtext("title") or "").strip()
            link = str(item.findtext("link") or "").strip()
            pub_date_raw = str(item.findtext("pubDate") or "").strip()
            source_name = str(item.findtext("source") or "").strip() or "rss"
            title_upper = title.upper()
            matched_symbol = any(alias in title_upper for alias in aliases)
            market_wide = any(token in title_upper for token in {"BITCOIN", "CRYPTO", "ETF", "FED", "BINANCE"})
            if not matched_symbol and not market_wide:
                continue

            published_at = None
            if pub_date_raw:
                try:
                    parsed = parsedate_to_datetime(pub_date_raw)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    else:
                        parsed = parsed.astimezone(UTC)
                    published_at = parsed.isoformat()
                except Exception:
                    published_at = None

            results.append(
                {
                    "title": title,
                    "url": link,
                    "published_at": published_at,
                    "source": source_name,
                    "feed_url": feed_url,
                    "matched_symbol": matched_symbol,
                }
            )
        return results

    def _score_news_sentiment(self, headlines: list[dict], symbol: str) -> dict:
        positive_tokens = {
            "APPROVAL",
            "APPROVED",
            "ADOPTION",
            "ADOPT",
            "INFLOW",
            "PARTNERSHIP",
            "INTEGRATION",
            "LAUNCH",
            "SURGE",
            "RALLY",
            "RECORD",
            "BREAKOUT",
            "COMPLIANT",
            "UPGRADE",
            "GROWTH",
        }
        negative_tokens = {
            "HACK",
            "EXPLOIT",
            "LAWSUIT",
            "FRAUD",
            "REJECT",
            "REJECTED",
            "DELIST",
            "OUTAGE",
            "BAN",
            "SELL-OFF",
            "SLUMP",
            "CRASH",
            "LIQUIDATION",
            "RISK",
            "SHUTDOWN",
            "INVESTIGATION",
            "SCRUTINY",
        }

        aliases = _symbol_aliases(symbol)
        total_score = 0.0
        positive_hits = 0
        negative_hits = 0
        symbol_hits = 0

        for item in headlines:
            title_upper = str(item.get("title") or "").upper()
            weight = 1.0
            if any(alias in title_upper for alias in aliases):
                weight = 1.75
                symbol_hits += 1

            positive_found = sum(1 for token in positive_tokens if token in title_upper)
            negative_found = sum(1 for token in negative_tokens if token in title_upper)
            positive_hits += positive_found
            negative_hits += negative_found
            total_score += (positive_found - negative_found) * weight

        headline_count = max(len(headlines), 1)
        normalized = math.tanh(total_score / headline_count)
        return {
            "score": round(_clamp(normalized, -1.0, 1.0), 4),
            "positive_hits": positive_hits,
            "negative_hits": negative_hits,
            "symbol_hits": symbol_hits,
        }

    def _build_bias_summary(self, fear_greed: dict, news: dict) -> dict:
        long_bias = 0.0
        short_bias = 0.0
        caution = 0.0
        reasons: list[str] = []

        if fear_greed.get("available"):
            value = _safe_float(fear_greed.get("value"), 50.0)
            if value <= 20:
                long_bias += 0.06
                reasons.append("fear_greed_extreme_fear")
            elif value <= 30:
                long_bias += 0.03
                reasons.append("fear_greed_fear")
            elif value >= 80:
                short_bias += 0.06
                reasons.append("fear_greed_extreme_greed")
            elif value >= 70:
                short_bias += 0.03
                reasons.append("fear_greed_greed")

        if news.get("available"):
            sentiment_score = _safe_float(news.get("sentiment_score"), 0.0)
            if sentiment_score >= 0.35:
                long_bias += 0.04
                reasons.append("news_positive")
            elif sentiment_score <= -0.35:
                short_bias += 0.04
                reasons.append("news_negative")

            if abs(sentiment_score) >= 0.65:
                caution += 0.03
                reasons.append("news_high_conviction")

        return {
            "long_bias": round(_clamp(long_bias, 0.0, 0.12), 4),
            "short_bias": round(_clamp(short_bias, 0.0, 0.12), 4),
            "caution_bias": round(_clamp(caution, 0.0, 0.06), 4),
            "reasons": reasons,
        }
