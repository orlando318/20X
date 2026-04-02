"""Base strategy interface.

Subclass Strategy and implement on_book_update() to emit signals.
The forward test runner calls these methods as data arrives.

Example:
    class MyStrategy(Strategy):
        def on_book_update(self, token_id, inst, book):
            if book.mid and book.mid < 0.3:
                return Signal.buy(token_id, price=book.best_ask.price, size=100)
            return None
"""

import enum
from dataclasses import dataclass, field
from typing import Optional

from connectors.binance import BookTick, Trade as BinanceTrade
from models.crypto_market import Instrument
from models.orderbook import OrderBook


class SignalType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    CANCEL = "CANCEL"


@dataclass
class Signal:
    """Order signal emitted by a strategy."""

    type: SignalType
    token_id: str
    price: float = 0.0
    size: float = 0.0
    order_id: str = ""  # for CANCEL signals

    @classmethod
    def buy(cls, token_id: str, price: float, size: float) -> "Signal":
        return cls(type=SignalType.BUY, token_id=token_id, price=price, size=size)

    @classmethod
    def sell(cls, token_id: str, price: float, size: float) -> "Signal":
        return cls(type=SignalType.SELL, token_id=token_id, price=price, size=size)

    @classmethod
    def cancel(cls, token_id: str, order_id: str) -> "Signal":
        return cls(type=SignalType.CANCEL, token_id=token_id, order_id=order_id)


class Strategy:
    """Base strategy class. Override the on_* methods you need."""

    def on_book_update(
        self, token_id: str, inst: Instrument, book: OrderBook
    ) -> Optional[Signal | list[Signal]]:
        """Called on every Polymarket book update. Return signal(s) or None."""
        return None

    def on_polymarket_trade(
        self, token_id: str, inst: Instrument, trade: dict
    ) -> Optional[Signal | list[Signal]]:
        """Called on every Polymarket trade."""
        return None

    def on_binance_tick(self, tick: BookTick) -> Optional[Signal | list[Signal]]:
        """Called on every Binance bookTicker update."""
        return None

    def on_binance_trade(self, trade: BinanceTrade) -> Optional[Signal | list[Signal]]:
        """Called on every Binance trade."""
        return None

    def on_fill(self, token_id: str, price: float, size: float, side: str) -> None:
        """Called when a paper/live fill occurs. Update internal state here."""
        pass

    def on_start(self) -> None:
        """Called when the forward test starts."""
        pass

    def on_stop(self) -> None:
        """Called when the forward test stops."""
        pass
