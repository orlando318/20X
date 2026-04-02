"""Example strategies for forward testing."""

from strategy.base import Strategy, Signal
from models.crypto_market import Instrument
from models.orderbook import OrderBook
from connectors.binance import BookTick


class SimpleSpreadStrategy(Strategy):
    """Buys when spread widens beyond threshold, sells when it narrows.

    A minimal example — not a real trading strategy.
    """

    def __init__(self, spread_threshold_bps: float = 500, size: float = 10.0):
        self._threshold = spread_threshold_bps
        self._size = size
        self._has_position: dict[str, bool] = {}

    def on_book_update(self, token_id: str, inst: Instrument, book: OrderBook):
        if not book.best_bid or not book.best_ask or not book.spread_bps:
            return None

        has_pos = self._has_position.get(token_id, False)

        if not has_pos and book.spread_bps > self._threshold:
            # Wide spread — buy at the bid
            self._has_position[token_id] = True
            return Signal.buy(token_id, price=book.best_bid.price, size=self._size)

        if has_pos and book.spread_bps < self._threshold * 0.5:
            # Spread narrowed — sell at the ask
            self._has_position[token_id] = False
            return Signal.sell(token_id, price=book.best_ask.price, size=self._size)

        return None

    def on_fill(self, token_id, price, size, side):
        if side == "SELL":
            self._has_position[token_id] = False
        elif side == "BUY":
            self._has_position[token_id] = True
