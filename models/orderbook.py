"""Order book representation with price-level aggregation."""

import enum
from typing import Optional

from pydantic import BaseModel, Field


class Side(str, enum.Enum):
    BID = "bid"
    ASK = "ask"


class PriceLevel(BaseModel):
    """A single price level in the book."""

    price: float
    size: float

    @property
    def notional(self) -> float:
        return self.price * self.size


class OrderBook(BaseModel):
    """Local order book state for a single token."""

    token_id: str
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)
    sequence: int = 0
    timestamp_ms: int = 0

    @property
    def best_bid(self) -> Optional[PriceLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[PriceLevel]:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return round((self.best_bid.price + self.best_ask.price) / 2, 6)
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return round(self.best_ask.price - self.best_bid.price, 6)
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        if self.mid and self.spread:
            return round((self.spread / self.mid) * 10_000, 2)
        return None

    def bid_depth(self, levels: int = 0) -> float:
        slc = self.bids[:levels] if levels else self.bids
        return sum(l.notional for l in slc)

    def ask_depth(self, levels: int = 0) -> float:
        slc = self.asks[:levels] if levels else self.asks
        return sum(l.notional for l in slc)

    def imbalance(self, levels: int = 5) -> Optional[float]:
        """Bid/ask imbalance ratio. >0 = bid heavy, <0 = ask heavy, range [-1, 1]."""
        bd = self.bid_depth(levels)
        ad = self.ask_depth(levels)
        total = bd + ad
        if total == 0:
            return None
        return round((bd - ad) / total, 6)

    def apply_snapshot(self, bids: list[list[float]], asks: list[list[float]], sequence: int, timestamp_ms: int = 0) -> None:
        """Replace entire book from a snapshot message."""
        self.bids = sorted([PriceLevel(price=p, size=s) for p, s in bids], key=lambda l: -l.price)
        self.asks = sorted([PriceLevel(price=p, size=s) for p, s in asks], key=lambda l: l.price)
        self.sequence = sequence
        self.timestamp_ms = timestamp_ms

    def apply_delta(self, side: Side, price: float, size: float, sequence: int, timestamp_ms: int = 0) -> None:
        """Apply an incremental update. size=0 removes the level."""
        if sequence <= self.sequence:
            return  # stale
        self.sequence = sequence
        self.timestamp_ms = timestamp_ms

        levels = self.bids if side == Side.BID else self.asks
        # Remove existing level at this price
        levels[:] = [l for l in levels if l.price != price]
        # Insert if size > 0
        if size > 0:
            levels.append(PriceLevel(price=price, size=size))
        # Re-sort
        if side == Side.BID:
            levels.sort(key=lambda l: -l.price)
        else:
            levels.sort(key=lambda l: l.price)
