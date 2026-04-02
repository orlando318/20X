"""Paper executor — simulates order fills against the live order book.

Resting limit orders are checked on every book update.
Market-crossing orders fill immediately at the best available price.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from models.orderbook import OrderBook, Side
from models.position import Position
from strategy.base import Signal, SignalType

logger = logging.getLogger(__name__)

FEE_RATE = 0.015  # 1.5% effective fee on Polymarket


@dataclass
class PaperOrder:
    id: str
    token_id: str
    side: str        # "BUY" or "SELL"
    price: float
    size: float
    remaining: float
    timestamp_ms: int
    status: str = "OPEN"  # OPEN, FILLED, PARTIALLY_FILLED, CANCELLED


@dataclass
class Fill:
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    fee: float
    timestamp_ms: int


class PaperExecutor:
    """Simulates order execution against a live book.

    - BUY orders rest on the bid side; they fill when the ask drops to or below the order price.
    - SELL orders rest on the ask side; they fill when the bid rises to or at/above the order price.
    - Orders that cross the book on submission fill immediately.
    """

    def __init__(self, fee_rate: float = FEE_RATE):
        self._fee_rate = fee_rate
        self._orders: dict[str, PaperOrder] = {}
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._fill_callbacks: list[Callable[[Fill], None]] = []

    # -- Public API -----------------------------------------------------------

    @property
    def open_orders(self) -> list[PaperOrder]:
        return [o for o in self._orders.values() if o.status == "OPEN"]

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    def get_position(self, token_id: str) -> Position:
        if token_id not in self._positions:
            self._positions[token_id] = Position(token_id=token_id)
        return self._positions[token_id]

    def total_pnl(self, books: dict[str, OrderBook]) -> float:
        total = 0.0
        for tid, pos in self._positions.items():
            book = books.get(tid)
            mark = book.mid if book and book.mid else pos.avg_entry
            total += pos.total_pnl(mark)
        return round(total, 4)

    def on_fill(self, callback: Callable[[Fill], None]) -> None:
        self._fill_callbacks.append(callback)

    # -- Signal processing ----------------------------------------------------

    def submit(self, signal: Signal) -> Optional[str]:
        if signal.type == SignalType.CANCEL:
            return self._cancel(signal.order_id)

        order_id = uuid.uuid4().hex[:12]
        order = PaperOrder(
            id=order_id,
            token_id=signal.token_id,
            side=signal.type.value,
            price=signal.price,
            size=signal.size,
            remaining=signal.size,
            timestamp_ms=int(time.time() * 1000),
        )
        self._orders[order_id] = order
        logger.debug("Paper order %s: %s %s %.4f x %.2f",
                     order_id, order.side, order.token_id[:16], order.price, order.size)
        return order_id

    def _cancel(self, order_id: str) -> Optional[str]:
        order = self._orders.get(order_id)
        if order and order.status == "OPEN":
            order.status = "CANCELLED"
            return order_id
        return None

    # -- Book update — check for fills ----------------------------------------

    def check_fills(self, token_id: str, book: OrderBook) -> list[Fill]:
        new_fills: list[Fill] = []
        for order in list(self._orders.values()):
            if order.token_id != token_id or order.status != "OPEN":
                continue
            fill = self._try_fill(order, book)
            if fill:
                new_fills.append(fill)
        return new_fills

    def _try_fill(self, order: PaperOrder, book: OrderBook) -> Optional[Fill]:
        if order.side == "BUY":
            best_ask = book.best_ask
            if not best_ask or best_ask.price > order.price:
                return None
            fill_price = best_ask.price
            fill_size = min(order.remaining, best_ask.size)
            pos_side = Side.BID
        else:
            best_bid = book.best_bid
            if not best_bid or best_bid.price < order.price:
                return None
            fill_price = best_bid.price
            fill_size = min(order.remaining, best_bid.size)
            pos_side = Side.ASK

        if fill_size <= 0:
            return None

        fee = round(fill_price * fill_size * self._fee_rate, 8)
        now_ms = int(time.time() * 1000)

        fill = Fill(
            order_id=order.id,
            token_id=order.token_id,
            side=order.side,
            price=fill_price,
            size=fill_size,
            fee=fee,
            timestamp_ms=now_ms,
        )

        order.remaining = round(order.remaining - fill_size, 8)
        if order.remaining <= 0:
            order.status = "FILLED"
        else:
            order.status = "PARTIALLY_FILLED"

        pos = self.get_position(order.token_id)
        pos.apply_fill(pos_side, fill_size, fill_price, fee)

        self._fills.append(fill)
        logger.info("FILL %s %s %.4f x %.2f fee=%.4f | pos=%.2f avg=%.4f rpnl=%.4f",
                    fill.side, fill.token_id[:16], fill.price, fill.size, fill.fee,
                    pos.size, pos.avg_entry, pos.realized_pnl)

        for cb in self._fill_callbacks:
            try:
                cb(fill)
            except Exception:
                logger.exception("Error in fill callback")

        return fill

    # -- Summary --------------------------------------------------------------

    def summary(self, books: dict[str, OrderBook] | None = None) -> str:
        lines = ["Paper Executor Summary"]
        lines.append(f"  Orders: {len(self._orders)} total, {len(self.open_orders)} open")
        lines.append(f"  Fills: {len(self._fills)}")

        if self._positions:
            lines.append("  Positions:")
            for tid, pos in self._positions.items():
                mark = None
                if books:
                    book = books.get(tid)
                    mark = book.mid if book else None
                upnl = pos.unrealized_pnl(mark) if mark else 0.0
                lines.append(f"    {tid[:16]}... size={pos.size:.2f} avg={pos.avg_entry:.4f} "
                           f"rpnl={pos.realized_pnl:.4f} upnl={upnl:.4f} fees={pos.total_fees:.4f}")

        if books:
            lines.append(f"  Total P&L: {self.total_pnl(books):.4f}")

        return "\n".join(lines)

    def print_summary(self, books: dict[str, OrderBook] | None = None) -> None:
        print(self.summary(books))
