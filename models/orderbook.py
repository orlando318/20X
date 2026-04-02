"""Array-based order book for O(1) price level updates.

Prices on Polymarket range from 0.00 to 1.00. With a tick size of 0.01,
that's 101 possible price levels (0.00, 0.01, ..., 1.00). We store sizes
in a flat array indexed by price tick.

When tick size changes (e.g. 0.01 → 0.001), the array resizes automatically.
"""

import enum
import math
from typing import Optional

from pydantic import BaseModel, Field


class Side(str, enum.Enum):
    BID = "bid"
    ASK = "ask"


class PriceLevel(BaseModel):
    """A single price level — used for output, not internal storage."""

    price: float
    size: float

    @property
    def notional(self) -> float:
        return self.price * self.size


class OrderBook(BaseModel):
    """Array-based order book for a single token.

    Internal storage: two arrays (bid_sizes, ask_sizes) indexed by price tick.
    Index 0 = price 0.00, index 1 = price tick_size, ..., index N = price 1.00.

    All delta operations are O(1). Sorted level lists are generated on demand.
    """

    token_id: str
    tick_size: float = 0.01
    bid_sizes: list[float] = Field(default_factory=list)
    ask_sizes: list[float] = Field(default_factory=list)
    sequence: int = 0
    timestamp_ms: int = 0

    def model_post_init(self, __context) -> None:
        n = self._num_ticks()
        if not self.bid_sizes:
            self.bid_sizes = [0.0] * n
        if not self.ask_sizes:
            self.ask_sizes = [0.0] * n

    # -- Price ↔ Index conversion ---------------------------------------------

    def _num_ticks(self) -> int:
        return round(1.0 / self.tick_size) + 1

    def _price_to_index(self, price: float) -> int:
        return round(price / self.tick_size)

    def _index_to_price(self, index: int) -> float:
        return round(index * self.tick_size, 10)

    # -- Tick size changes ----------------------------------------------------

    def set_tick_size(self, new_tick: float) -> None:
        """Resize arrays when tick size changes. Existing levels are remapped."""
        if new_tick == self.tick_size:
            return

        old_tick = self.tick_size
        old_bids = self.bid_sizes
        old_asks = self.ask_sizes

        self.tick_size = new_tick
        n = self._num_ticks()
        self.bid_sizes = [0.0] * n
        self.ask_sizes = [0.0] * n

        # Remap old levels to new indices
        for old_i, size in enumerate(old_bids):
            if size > 0:
                price = round(old_i * old_tick, 10)
                new_i = self._price_to_index(price)
                if 0 <= new_i < n:
                    self.bid_sizes[new_i] += size

        for old_i, size in enumerate(old_asks):
            if size > 0:
                price = round(old_i * old_tick, 10)
                new_i = self._price_to_index(price)
                if 0 <= new_i < n:
                    self.ask_sizes[new_i] += size

    # -- Properties -----------------------------------------------------------

    @property
    def bids(self) -> list[PriceLevel]:
        """Bid levels sorted best (highest) first."""
        return [
            PriceLevel(price=self._index_to_price(i), size=s)
            for i, s in reversed(list(enumerate(self.bid_sizes)))
            if s > 0
        ]

    @property
    def asks(self) -> list[PriceLevel]:
        """Ask levels sorted best (lowest) first."""
        return [
            PriceLevel(price=self._index_to_price(i), size=s)
            for i, s in enumerate(self.ask_sizes)
            if s > 0
        ]

    @property
    def best_bid(self) -> Optional[PriceLevel]:
        for i in range(len(self.bid_sizes) - 1, -1, -1):
            if self.bid_sizes[i] > 0:
                return PriceLevel(price=self._index_to_price(i), size=self.bid_sizes[i])
        return None

    @property
    def best_ask(self) -> Optional[PriceLevel]:
        for i in range(len(self.ask_sizes)):
            if self.ask_sizes[i] > 0:
                return PriceLevel(price=self._index_to_price(i), size=self.ask_sizes[i])
        return None

    @property
    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb and ba:
            return round((bb.price + ba.price) / 2, 6)
        return None

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb and ba:
            return round(ba.price - bb.price, 6)
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        m = self.mid
        s = self.spread
        if m and m > 0 and s is not None:
            return round((s / m) * 10_000, 2)
        return None

    def bid_depth(self, levels: int = 0) -> float:
        bids = self.bids
        slc = bids[:levels] if levels else bids
        return sum(l.notional for l in slc)

    def ask_depth(self, levels: int = 0) -> float:
        asks = self.asks
        slc = asks[:levels] if levels else asks
        return sum(l.notional for l in slc)

    def imbalance(self, levels: int = 5) -> Optional[float]:
        """Bid/ask imbalance ratio. >0 = bid heavy, <0 = ask heavy, range [-1, 1]."""
        bd = self.bid_depth(levels)
        ad = self.ask_depth(levels)
        total = bd + ad
        if total == 0:
            return None
        return round((bd - ad) / total, 6)

    # -- Mutations (O(1) per update) ------------------------------------------

    def apply_snapshot(self, bids: list[list[float]], asks: list[list[float]], sequence: int, timestamp_ms: int = 0) -> None:
        """Replace entire book from a snapshot message."""
        n = self._num_ticks()
        self.bid_sizes = [0.0] * n
        self.ask_sizes = [0.0] * n

        for price, size in bids:
            idx = self._price_to_index(price)
            if 0 <= idx < n:
                self.bid_sizes[idx] = size

        for price, size in asks:
            idx = self._price_to_index(price)
            if 0 <= idx < n:
                self.ask_sizes[idx] = size

        self.sequence = sequence
        self.timestamp_ms = timestamp_ms

    def apply_delta(self, side: Side, price: float, size: float, sequence: int, timestamp_ms: int = 0) -> None:
        """Apply an incremental update. size=0 removes the level. O(1)."""
        if sequence <= self.sequence:
            return
        self.sequence = sequence
        self.timestamp_ms = timestamp_ms

        idx = self._price_to_index(price)
        arr = self.bid_sizes if side == Side.BID else self.ask_sizes

        if 0 <= idx < len(arr):
            arr[idx] = size

    def reduce_level(self, side: Side, price: float, amount: float) -> None:
        """Reduce size at a price level by amount. Used for trade processing."""
        idx = self._price_to_index(price)
        arr = self.bid_sizes if side == Side.BID else self.ask_sizes

        if 0 <= idx < len(arr):
            arr[idx] = max(0.0, round(arr[idx] - amount, 8))
