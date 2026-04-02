"""Crypto-specific market model with multi-instrument support."""

import enum
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, computed_field

from models.market import Market


class Coin(str, enum.Enum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    DOGE = "DOGE"
    XRP = "XRP"
    MATIC = "MATIC"
    AVAX = "AVAX"
    LINK = "LINK"
    ADA = "ADA"
    DOT = "DOT"


class Duration(str, enum.Enum):
    """Market duration buckets matching Polymarket's crypto offerings."""
    MIN_5 = "5min"
    MIN_15 = "15min"
    HOUR_1 = "1h"
    HOUR_4 = "4h"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"

    @property
    def max_minutes(self) -> int:
        return {
            Duration.MIN_5: 5,
            Duration.MIN_15: 15,
            Duration.HOUR_1: 60,
            Duration.HOUR_4: 240,
            Duration.DAILY: 1440,
            Duration.WEEKLY: 10080,
            Duration.MONTHLY: 43200,
            Duration.YEARLY: 525600,
        }[self]


class Instrument(BaseModel):
    """A single tradeable instrument within a crypto market.

    Each instrument represents one outcome (e.g. "BTC above $100k")
    with its own YES/NO token pair and order books.
    """

    condition_id: str = ""                # unique condition for this instrument on Polymarket
    yes_token_id: str
    no_token_id: str = ""
    label: str                            # e.g. "Above $100,000"
    market_slug: str = ""                 # URL slug for API lookups
    target_price: Optional[float] = None  # strike price extracted from label
    yes_price: float = 0.0
    no_price: float = 0.0
    volume: float = 0.0
    liquidity: float = 0.0
    min_tick_size: float = 0.01           # minimum price increment
    min_order_size: float = 1.0           # minimum order quantity

    @property
    def mid(self) -> float:
        return round((self.yes_price + (1 - self.no_price)) / 2, 6) if self.no_price else self.yes_price

    @property
    def implied_prob(self) -> float:
        """YES price as implied probability."""
        return self.yes_price

    @property
    def token_ids(self) -> list[str]:
        ids = [self.yes_token_id]
        if self.no_token_id:
            ids.append(self.no_token_id)
        return ids


class CryptoMarket(Market):
    """A Polymarket prediction market for a specific coin and time duration.

    A market can contain multiple instruments (e.g. multiple strike prices).
    Single-outcome markets have one instrument. Multi-outcome markets
    (like "What price will BTC hit?") have several.
    """

    coin: Coin
    duration: Duration
    slug: str = ""
    instruments: list[Instrument] = []
    start_date: Optional[datetime] = None
    event_start_time: Optional[datetime] = None  # when the candle/window opens (for up/down markets)

    @computed_field
    @property
    def time_to_expiry(self) -> Optional[timedelta]:
        if self.end_date:
            return self.end_date - datetime.now(timezone.utc)
        return None

    @property
    def is_expired(self) -> bool:
        if self.end_date:
            return datetime.now(timezone.utc) > self.end_date
        return False

    @property
    def hours_remaining(self) -> Optional[float]:
        ttl = self.time_to_expiry
        if ttl:
            return round(ttl.total_seconds() / 3600, 2)
        return None

    @property
    def is_multi_instrument(self) -> bool:
        return len(self.instruments) > 1

    @property
    def all_token_ids(self) -> list[str]:
        """Every token ID across all instruments — for subscribing to order books."""
        ids = []
        for inst in self.instruments:
            ids.extend(inst.token_ids)
        return ids

    @property
    def strikes(self) -> list[float]:
        """Sorted list of strike prices across instruments."""
        return sorted(p for inst in self.instruments if (p := inst.target_price) is not None)

    def get_instrument(self, token_id: str) -> Optional[Instrument]:
        """Look up an instrument by any of its token IDs."""
        for inst in self.instruments:
            if token_id in inst.token_ids:
                return inst
        return None

    def get_instrument_by_strike(self, strike: float) -> Optional[Instrument]:
        for inst in self.instruments:
            if inst.target_price == strike:
                return inst
        return None

    def nearest_strike(self, price: float) -> Optional[Instrument]:
        """Find the instrument with strike closest to a given price."""
        if not self.instruments:
            return None
        return min(
            (inst for inst in self.instruments if inst.target_price is not None),
            key=lambda inst: abs(inst.target_price - price),
            default=None,
        )
