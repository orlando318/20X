"""Market discovery via direct slug construction and Gamma API."""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import aiohttp

from config.settings import Settings
from models.market import Market, MarketOutcome
from models.crypto_market import Coin, CryptoMarket, Duration, Instrument

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Slug construction
# ---------------------------------------------------------------------------

# Coin ticker -> slug name used by Polymarket
_COIN_SLUG: dict[Coin, str] = {
    Coin.BTC: "bitcoin",
    Coin.ETH: "ethereum",
    Coin.SOL: "solana",
    Coin.DOGE: "dogecoin",
    Coin.XRP: "xrp",
    Coin.MATIC: "polygon",
    Coin.AVAX: "avalanche",
    Coin.LINK: "chainlink",
    Coin.ADA: "cardano",
    Coin.DOT: "polkadot",
}

# Short ticker used in 5m/15m/1h/4h slugs
_COIN_SHORT: dict[Coin, str] = {
    Coin.BTC: "btc",
    Coin.ETH: "eth",
    Coin.SOL: "sol",
    Coin.DOGE: "doge",
    Coin.XRP: "xrp",
    Coin.MATIC: "matic",
    Coin.AVAX: "avax",
    Coin.LINK: "link",
    Coin.ADA: "ada",
    Coin.DOT: "dot",
}

# Reverse lookups
_SLUG_TO_COIN = {v: k for k, v in _COIN_SLUG.items()}
_SHORT_TO_COIN = {v: k for k, v in _COIN_SHORT.items()}

_MONTH_NAMES = [
    "", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def build_daily_slugs(coin: Coin, date: datetime) -> list[str]:
    """Generate candidate slugs for daily markets on a given date.

    Examples:
        bitcoin-up-or-down-on-april-1-2026
        what-price-will-bitcoin-hit-on-april-1
    """
    name = _COIN_SLUG[coin]
    month = _MONTH_NAMES[date.month]
    day = date.day
    year = date.year
    return [
        f"{name}-up-or-down-on-{month}-{day}-{year}",
        f"what-price-will-{name}-hit-on-{month}-{day}",
        f"{name}-up-or-down-on-{month}-{day}",
        f"what-price-will-{name}-hit-on-{month}-{day}-{year}",
    ]


def build_weekly_slugs(coin: Coin, week_start: datetime) -> list[str]:
    """Generate candidate slugs for weekly markets.

    Example: what-price-will-bitcoin-hit-march-30-april-5
    """
    name = _COIN_SLUG[coin]
    week_end = week_start + timedelta(days=6)
    sm = _MONTH_NAMES[week_start.month]
    em = _MONTH_NAMES[week_end.month]
    sd = week_start.day
    ed = week_end.day
    return [
        f"what-price-will-{name}-hit-{sm}-{sd}-{em}-{ed}",
        f"what-price-will-{name}-hit-{sm}-{sd}-{em}-{ed}-{week_start.year}",
    ]


def build_short_duration_slugs(coin: Coin, duration: Duration, end_timestamp: int) -> list[str]:
    """Generate slugs for 5m/15m/4h markets (timestamp-based).

    Examples:
        btc-updown-5m-1775055900
        eth-updown-15m-1775057400
        btc-updown-4h-1775059200
    """
    short = _COIN_SHORT[coin]
    dur_tag = {
        Duration.MIN_5: "5m",
        Duration.MIN_15: "15m",
        Duration.HOUR_4: "4h",
    }.get(duration)
    if dur_tag is None:
        return []
    return [f"{short}-updown-{dur_tag}-{end_timestamp}"]


def _format_hour(hour_24: int) -> str:
    """Convert 24h hour to Polymarket format: 9am, 10am, 12pm, 1pm, etc."""
    if hour_24 == 0:
        return "12am"
    elif hour_24 < 12:
        return f"{hour_24}am"
    elif hour_24 == 12:
        return "12pm"
    else:
        return f"{hour_24 - 12}pm"


def build_hourly_slugs(coin: Coin, date: datetime, hour_et: int) -> list[str]:
    """Generate slugs for 1-hour markets.

    Pattern: {name}-up-or-down-{month}-{day}-{year}-{hour}-et
    Example: bitcoin-up-or-down-april-1-2026-11am-et

    Args:
        hour_et: Hour in ET (Eastern Time), 0-23.
    """
    name = _COIN_SLUG[coin]
    month = _MONTH_NAMES[date.month]
    day = date.day
    year = date.year
    h = _format_hour(hour_et)
    return [
        f"{name}-up-or-down-{month}-{day}-{year}-{h}-et",
        f"{name}-up-or-down-{month}-{day}-{h}-et",
    ]


def build_monthly_slugs(coin: Coin, month: int, year: int) -> list[str]:
    """Generate slugs for monthly markets.

    Pattern: what-price-will-{name}-hit-in-{month}
    Example: what-price-will-bitcoin-hit-in-april
    """
    name = _COIN_SLUG[coin]
    m = _MONTH_NAMES[month]
    return [
        f"what-price-will-{name}-hit-in-{m}",
        f"what-price-will-{name}-hit-in-{m}-{year}",
    ]


def build_yearly_slugs(coin: Coin, year: int) -> list[str]:
    """Generate slugs for yearly markets.

    Pattern: what-price-will-{name}-hit-before-{year+1}
    Example: what-price-will-bitcoin-hit-before-2027
    """
    name = _COIN_SLUG[coin]
    return [
        f"what-price-will-{name}-hit-before-{year + 1}",
        f"what-price-will-{name}-hit-in-{year}",
    ]


# ---------------------------------------------------------------------------
# Gamma API client (market discovery)
# ---------------------------------------------------------------------------

class GammaClient:
    """Thin async client for the Polymarket Gamma API (market discovery)."""

    def __init__(self, settings: Settings):
        self._base_url = settings.api.gamma_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15.0)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def get_market_by_slug(self, slug: str) -> Optional[dict]:
        session = await self._ensure_session()
        url = f"{self._base_url}/markets/slug/{slug}"
        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    logger.debug("Gamma %d for slug %s", resp.status, slug)
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Gamma request failed for slug %s: %s", slug, exc)
            return None

    async def get_event_by_slug(self, slug: str) -> Optional[dict]:
        session = await self._ensure_session()
        url = f"{self._base_url}/events/slug/{slug}"
        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Gamma event request failed for slug %s: %s", slug, exc)
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _detect_target_price(text: str) -> Optional[float]:
    match = re.search(r"\$[\s]*([\d,]+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def _parse_json_field(raw: dict, key: str) -> list:
    """Parse a field that may be a JSON string or already a list."""
    val = raw.get(key)
    if val is None:
        return []
    if isinstance(val, str):
        try:
            import json
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(val, list):
        return val
    return []


def _build_instruments_from_gamma(raw: dict) -> list[Instrument]:
    """Build instruments from Gamma API market fields (clobTokenIds, outcomes, outcomePrices)."""
    token_ids = _parse_json_field(raw, "clobTokenIds")
    outcomes = _parse_json_field(raw, "outcomes")
    prices = _parse_json_field(raw, "outcomePrices")

    if not token_ids or len(token_ids) != len(outcomes):
        return []

    slug = raw.get("market_slug") or raw.get("slug", "")
    cond_id = raw.get("condition_id") or raw.get("conditionId", "")
    min_tick = float(raw.get("minimum_tick_size") or raw.get("minimumTickSize", 0.01) or 0.01)
    min_order = float(raw.get("minimum_order_size") or raw.get("minimumOrderSize", 1.0) or 1.0)
    question = raw.get("question", "")

    # For simple YES/NO or Up/Down markets: single instrument
    # For multi-outcome: each outcome becomes an instrument
    if len(token_ids) == 2 and len(outcomes) == 2:
        yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() in ("yes", "up")), 0)
        no_idx = 1 - yes_idx
        return [Instrument(
            condition_id=cond_id,
            yes_token_id=str(token_ids[yes_idx]),
            no_token_id=str(token_ids[no_idx]),
            label=question,
            market_slug=slug,
            target_price=_detect_target_price(question),
            yes_price=float(prices[yes_idx]) if prices else 0.0,
            no_price=float(prices[no_idx]) if prices else 0.0,
            min_tick_size=min_tick,
            min_order_size=min_order,
        )]

    # Multi-outcome: each is its own instrument
    instruments = []
    for i, (tid, outcome) in enumerate(zip(token_ids, outcomes)):
        price = float(prices[i]) if i < len(prices) else 0.0
        instruments.append(Instrument(
            condition_id=cond_id,
            yes_token_id=str(tid),
            label=str(outcome),
            market_slug=slug,
            target_price=_detect_target_price(str(outcome)),
            yes_price=price,
            min_tick_size=min_tick,
            min_order_size=min_order,
        ))
    return instruments


def _parse_gamma_market(raw: dict, coin: Coin, duration: Duration) -> Optional[CryptoMarket]:
    """Parse a Gamma API market response into a CryptoMarket."""
    try:
        instruments = _build_instruments_from_gamma(raw)

        token_ids = _parse_json_field(raw, "clobTokenIds")
        outcome_names = _parse_json_field(raw, "outcomes")
        prices = _parse_json_field(raw, "outcomePrices")

        outcomes = []
        for i, (tid, name) in enumerate(zip(token_ids, outcome_names)):
            outcomes.append(MarketOutcome(
                token_id=str(tid),
                outcome=str(name),
                price=float(prices[i]) if i < len(prices) else 0.0,
            ))

        return CryptoMarket(
            condition_id=raw.get("conditionId") or raw.get("condition_id", ""),
            question=raw.get("question", ""),
            description=raw.get("description", ""),
            outcomes=outcomes,
            active=raw.get("active", True),
            closed=raw.get("closed", False),
            volume=float(raw.get("volume", 0) or 0),
            liquidity=float(raw.get("liquidity", 0) or 0),
            end_date=_parse_dt(raw.get("endDate") or raw.get("end_date_iso")),
            created_at=_parse_dt(raw.get("createdAt") or raw.get("created_at")),
            coin=coin,
            duration=duration,
            instruments=instruments,
            start_date=_parse_dt(raw.get("startDate") or raw.get("created_at")),
        )
    except Exception as exc:
        logger.debug("Failed to parse gamma market: %s", exc)
        return None


def _parse_gamma_event(raw: dict, coin: Coin, duration: Duration) -> Optional[CryptoMarket]:
    """Parse a Gamma API event response (multi-market container) into a CryptoMarket.

    Events like 'What price will Bitcoin hit March 30-April 5?' contain
    multiple sub-markets, each with its own condition_id and tokens.
    We flatten these into instruments on a single CryptoMarket.
    """
    try:
        sub_markets = raw.get("markets") or []
        instruments: list[Instrument] = []
        total_volume = 0.0
        total_liquidity = 0.0

        for sub in sub_markets:
            if not isinstance(sub, dict):
                continue
            slug = sub.get("market_slug") or sub.get("slug", "")
            cond_id = sub.get("condition_id") or sub.get("conditionId", "")
            question = sub.get("question") or sub.get("groupItemTitle", "")
            min_tick = float(sub.get("minimum_tick_size") or sub.get("minimumTickSize", 0.01) or 0.01)
            min_order = float(sub.get("minimum_order_size") or sub.get("minimumOrderSize", 1.0) or 1.0)
            total_volume += float(sub.get("volume", 0) or 0)
            total_liquidity += float(sub.get("liquidity", 0) or 0)

            # Each sub-market uses clobTokenIds/outcomes/outcomePrices (same as single markets)
            sub_instruments = _build_instruments_from_gamma(sub)
            # Override label with the sub-market question (e.g. "Will Bitcoin reach $82,000?")
            for inst in sub_instruments:
                if question:
                    inst.label = question
                    inst.target_price = _detect_target_price(question)
            instruments.extend(sub_instruments)

        instruments.sort(key=lambda i: i.target_price if i.target_price is not None else float("inf"))

        return CryptoMarket(
            condition_id=raw.get("id", raw.get("condition_id", "")),
            question=raw.get("title") or raw.get("question", ""),
            description=raw.get("description", ""),
            outcomes=[],
            active=any(not sub.get("closed", False) for sub in sub_markets),
            closed=all(sub.get("closed", True) for sub in sub_markets),
            volume=total_volume,
            liquidity=total_liquidity,
            end_date=_parse_dt(raw.get("endDate") or raw.get("end_date_iso")),
            created_at=_parse_dt(raw.get("createdAt") or raw.get("created_at")),
            coin=coin,
            duration=duration,
            instruments=instruments,
            start_date=_parse_dt(raw.get("startDate") or raw.get("created_at")),
        )
    except Exception as exc:
        logger.debug("Failed to parse gamma event: %s", exc)
        return None


def _build_instruments(raw: dict) -> list[Instrument]:
    """Build Instrument objects from Gamma API market data."""
    tokens = raw.get("tokens", [])
    if not tokens or not isinstance(tokens, list):
        return []

    groups: dict[str, dict[str, Any]] = {}
    market_slug = raw.get("market_slug") or raw.get("slug", "")
    min_tick = float(raw.get("minimum_tick_size") or raw.get("minimumTickSize", 0.01) or 0.01)
    min_order = float(raw.get("minimum_order_size") or raw.get("minimumOrderSize", 1.0) or 1.0)

    for token in tokens:
        if not isinstance(token, dict):
            continue
        outcome = token.get("outcome", "")
        group_key = token.get("group_item_title") or token.get("groupItemTitle") or token.get("outcome_label") or ""
        cond_id = token.get("condition_id") or token.get("conditionId") or raw.get("condition_id") or raw.get("conditionId", "")

        key = group_key or "_default"
        if key not in groups:
            groups[key] = {
                "label": group_key or raw.get("question", ""),
                "condition_id": cond_id,
                "market_slug": market_slug,
            }

        if outcome.lower() == "yes":
            groups[key]["yes_token_id"] = token.get("token_id", token.get("tokenId", ""))
            groups[key]["yes_price"] = float(token.get("price", 0))
        elif outcome.lower() == "no":
            groups[key]["no_token_id"] = token.get("token_id", token.get("tokenId", ""))
            groups[key]["no_price"] = float(token.get("price", 0))

    instruments = []
    for g in groups.values():
        if "yes_token_id" not in g:
            continue
        label = str(g.get("label", ""))
        instruments.append(Instrument(
            condition_id=str(g.get("condition_id", "")),
            yes_token_id=str(g["yes_token_id"]),
            no_token_id=str(g.get("no_token_id", "")),
            label=label,
            market_slug=str(g.get("market_slug", "")),
            target_price=_detect_target_price(label),
            yes_price=float(g.get("yes_price", 0)),
            no_price=float(g.get("no_price", 0)),
            min_tick_size=min_tick,
            min_order_size=min_order,
        ))

    instruments.sort(key=lambda i: i.target_price if i.target_price is not None else float("inf"))
    return instruments


def _parse_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------

class CryptoMarketSelector:
    """Discovers crypto markets by constructing slugs and fetching from Gamma API."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._gamma = GammaClient(settings)
        self._markets: list[CryptoMarket] = []

    @property
    def markets(self) -> list[CryptoMarket]:
        return list(self._markets)

    async def fetch_daily(self, coins: list[Coin], date: Optional[datetime] = None) -> list[CryptoMarket]:
        """Fetch daily crypto markets for given coins on a date (default: today)."""
        date = date or datetime.utcnow()
        return await self._fetch_by_slugs(
            {coin: build_daily_slugs(coin, date) for coin in coins},
            Duration.DAILY,
        )

    async def fetch_weekly(self, coins: list[Coin], week_start: Optional[datetime] = None) -> list[CryptoMarket]:
        """Fetch weekly crypto markets. week_start defaults to most recent Sunday."""
        if week_start is None:
            today = datetime.utcnow()
            # Polymarket weeks run Monday-Sunday
            week_start = today - timedelta(days=today.weekday())  # Monday
        return await self._fetch_by_slugs(
            {coin: build_weekly_slugs(coin, week_start) for coin in coins},
            Duration.WEEKLY,
            try_events=True,
        )

    async def fetch_5m(self, coins: list[Coin], end_timestamp: int) -> list[CryptoMarket]:
        """Fetch 5-minute markets by end timestamp (unix seconds)."""
        return await self._fetch_by_slugs(
            {coin: build_short_duration_slugs(coin, Duration.MIN_5, end_timestamp) for coin in coins},
            Duration.MIN_5,
        )

    async def fetch_15m(self, coins: list[Coin], end_timestamp: int) -> list[CryptoMarket]:
        """Fetch 15-minute markets by end timestamp (unix seconds)."""
        return await self._fetch_by_slugs(
            {coin: build_short_duration_slugs(coin, Duration.MIN_15, end_timestamp) for coin in coins},
            Duration.MIN_15,
        )

    async def fetch_1h(self, coins: list[Coin], date: Optional[datetime] = None, hour_et: Optional[int] = None) -> list[CryptoMarket]:
        """Fetch 1-hour markets.

        Args:
            date: Date of the market (default: today).
            hour_et: Hour in Eastern Time, 0-23 (default: current hour ET).
        """
        date = date or datetime.utcnow()
        if hour_et is None:
            # Approximate: UTC-4 for EDT, UTC-5 for EST
            hour_et = (datetime.utcnow().hour - 4) % 24
        return await self._fetch_by_slugs(
            {coin: build_hourly_slugs(coin, date, hour_et) for coin in coins},
            Duration.HOUR_1,
        )

    async def fetch_4h(self, coins: list[Coin], end_timestamp: int) -> list[CryptoMarket]:
        """Fetch 4-hour markets by end timestamp (unix seconds)."""
        return await self._fetch_by_slugs(
            {coin: build_short_duration_slugs(coin, Duration.HOUR_4, end_timestamp) for coin in coins},
            Duration.HOUR_4,
        )

    async def fetch_monthly(self, coins: list[Coin], month: Optional[int] = None, year: Optional[int] = None) -> list[CryptoMarket]:
        """Fetch monthly markets (default: current month)."""
        now = datetime.utcnow()
        month = month or now.month
        year = year or now.year
        return await self._fetch_by_slugs(
            {coin: build_monthly_slugs(coin, month, year) for coin in coins},
            Duration.MONTHLY,
            try_events=True,
        )

    async def fetch_yearly(self, coins: list[Coin], year: Optional[int] = None) -> list[CryptoMarket]:
        """Fetch yearly markets (default: current year)."""
        year = year or datetime.utcnow().year
        return await self._fetch_by_slugs(
            {coin: build_yearly_slugs(coin, year) for coin in coins},
            Duration.YEARLY,
            try_events=True,
        )

    async def fetch_all_active(self, coins: Optional[list[Coin]] = None) -> list[CryptoMarket]:
        """Fetch daily + weekly + monthly markets for given coins."""
        coins = coins or [Coin.BTC, Coin.ETH, Coin.SOL]
        daily = await self.fetch_daily(coins)
        weekly = await self.fetch_weekly(coins)
        monthly = await self.fetch_monthly(coins)
        self._markets = daily + weekly + monthly
        return self._markets

    # -- Filters --------------------------------------------------------------

    def filter_by_coin(self, coin: Coin) -> list[CryptoMarket]:
        return [m for m in self._markets if m.coin == coin]

    def filter_by_duration(self, duration: Duration) -> list[CryptoMarket]:
        return [m for m in self._markets if m.duration == duration]

    def filter_by_coin_and_duration(self, coin: Coin, duration: Duration) -> list[CryptoMarket]:
        return [m for m in self._markets if m.coin == coin and m.duration == duration]

    def active_only(self) -> list[CryptoMarket]:
        return [m for m in self._markets if m.active and not m.closed and not m.is_expired]

    def by_liquidity(self, min_liquidity: float = 0.0) -> list[CryptoMarket]:
        return sorted(
            [m for m in self._markets if m.liquidity >= min_liquidity],
            key=lambda m: m.liquidity, reverse=True,
        )

    def by_volume(self, min_volume: float = 0.0) -> list[CryptoMarket]:
        return sorted(
            [m for m in self._markets if m.volume >= min_volume],
            key=lambda m: m.volume, reverse=True,
        )

    def expiring_within_hours(self, hours: float) -> list[CryptoMarket]:
        return sorted(
            [m for m in self._markets if m.hours_remaining is not None and 0 < m.hours_remaining <= hours],
            key=lambda m: m.hours_remaining,
        )

    # -- Internal -------------------------------------------------------------

    async def _fetch_by_slugs(
        self, coin_slugs: dict[Coin, list[str]], duration: Duration, try_events: bool = False,
    ) -> list[CryptoMarket]:
        """Try candidate slugs per coin concurrently, return first hit per coin.

        If try_events=True, also checks the events endpoint (for multi-market events
        like weekly price targets that contain multiple sub-markets/instruments).
        """
        results: list[CryptoMarket] = []

        async def _try_coin(coin: Coin, slugs: list[str]) -> None:
            for slug in slugs:
                # Try market endpoint first
                raw = await self._gamma.get_market_by_slug(slug)
                if raw:
                    market = _parse_gamma_market(raw, coin, duration)
                    if market:
                        results.append(market)
                        logger.info("Found %s %s market: %s", coin.value, duration.value, slug)
                        return

                # Try events endpoint (multi-instrument markets like weekly price targets)
                if try_events:
                    raw = await self._gamma.get_event_by_slug(slug)
                    if raw:
                        market = _parse_gamma_event(raw, coin, duration)
                        if market:
                            results.append(market)
                            logger.info("Found %s %s event: %s", coin.value, duration.value, slug)
                            return

            logger.debug("No %s %s market found (tried %d slugs)", coin.value, duration.value, len(slugs))

        await asyncio.gather(*[_try_coin(coin, slugs) for coin, slugs in coin_slugs.items()])
        self._markets.extend(results)
        return results

    async def close(self) -> None:
        await self._gamma.close()
