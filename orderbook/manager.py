"""Order book manager — maintains local book state from WS feed and REST snapshots."""

import asyncio
import logging
from typing import Any, Callable, Optional

from config.settings import Settings
from clob_client import CLOBRestClient, CLOBWebSocketClient, CLOBDataError
from models.orderbook import OrderBook, PriceLevel, Side

logger = logging.getLogger(__name__)


def _parse_book_levels(raw_levels: list) -> list[list[float]]:
    """Parse book levels from CLOB API — handles both dict and list formats."""
    result = []
    for level in raw_levels:
        parsed = _parse_book_level(level)
        if parsed:
            result.append(parsed)
    return result


def _parse_book_level(level) -> Optional[list[float]]:
    """Parse a single book level — dict or list/tuple."""
    if isinstance(level, dict):
        return [float(level["price"]), float(level["size"])]
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        return [float(level[0]), float(level[1])]
    return None


class OrderBookManager:
    """Manages local order book state for one or more tokens.

    Responsibilities:
    - Subscribes to WS feed for real-time deltas
    - Fetches REST snapshots to initialize / resync
    - Detects sequence gaps and triggers resync
    - Notifies listeners on every book update
    """

    def __init__(self, settings: Settings, rest_client: Optional[CLOBRestClient] = None):
        self._settings = settings
        self._rest = rest_client or CLOBRestClient(settings)
        self._ws: Optional[CLOBWebSocketClient] = None
        self._books: dict[str, OrderBook] = {}
        self._listeners: list[Callable[[str, OrderBook], Any]] = []
        self._syncing: set[str] = set()

    # -- Public API -----------------------------------------------------------

    def get_book(self, token_id: str) -> Optional[OrderBook]:
        return self._books.get(token_id)

    def get_all_books(self) -> dict[str, OrderBook]:
        return dict(self._books)

    def on_update(self, callback: Callable[[str, OrderBook], Any]) -> None:
        """Register a listener called on every book update. Signature: (token_id, book)."""
        self._listeners.append(callback)

    async def start(self, token_ids: list[str]) -> None:
        """Initialize books from REST snapshots, then subscribe to WS deltas."""
        # Fetch snapshots concurrently
        snapshot_tasks = [self._fetch_snapshot(tid) for tid in token_ids]
        await asyncio.gather(*snapshot_tasks, return_exceptions=True)

        # Start WS feed
        self._ws = CLOBWebSocketClient(self._settings, on_message=self._handle_ws_message)
        await self._ws.connect(assets=token_ids)

    async def stop(self) -> None:
        if self._ws:
            await self._ws.stop()

    # -- Snapshot fetching ----------------------------------------------------

    async def _fetch_snapshot(self, token_id: str) -> None:
        """Fetch full book from REST and apply as snapshot."""
        self._syncing.add(token_id)
        try:
            data = await self._rest.get_order_book(token_id)
            bids = _parse_book_levels(data.get("bids") or [])
            asks = _parse_book_levels(data.get("asks") or [])
            ts = int(data.get("timestamp", 0) or 0)
            # CLOB hash is hex, not a numeric sequence — use timestamp as sequence
            seq = ts

            book = self._books.get(token_id) or OrderBook(token_id=token_id)
            book.apply_snapshot(bids, asks, sequence=seq, timestamp_ms=ts)
            self._books[token_id] = book

            logger.info("Snapshot applied for %s: %d bids, %d asks, seq=%d",
                        token_id, len(book.bids), len(book.asks), seq)
            self._notify(token_id, book)
        except Exception:
            logger.exception("Failed to fetch snapshot for %s", token_id)
        finally:
            self._syncing.discard(token_id)

    # -- WS message handling --------------------------------------------------

    async def _handle_ws_message(self, msg: Any) -> None:
        """Route incoming WS messages to appropriate handlers."""
        # WS can send batched messages as a list
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    await self._handle_ws_message(item)
            return

        if not isinstance(msg, dict):
            logger.debug("Skipping non-dict WS message: %s", type(msg).__name__)
            return

        event_type = msg.get("event_type") or msg.get("type", "")

        if event_type == "book":
            self._handle_book_snapshot(msg)
        elif event_type == "price_change":
            await self._handle_price_change(msg)
        elif event_type == "last_trade_price":
            await self._handle_trade(msg)
        elif event_type == "best_bid_ask":
            pass  # Informational only — full book is maintained via price_change
        else:
            logger.debug("Unhandled WS event: %s", event_type)

    def _handle_book_snapshot(self, msg: dict) -> None:
        """Handle a full order book snapshot from WS (event_type: 'book')."""
        token_id = msg.get("asset_id", "")
        if not token_id:
            return

        bids = _parse_book_levels(msg.get("bids") or [])
        asks = _parse_book_levels(msg.get("asks") or [])
        ts = int(msg.get("timestamp", 0) or 0)

        book = self._books.get(token_id) or OrderBook(token_id=token_id)
        book.apply_snapshot(bids, asks, sequence=ts, timestamp_ms=ts)
        self._books[token_id] = book

        logger.info("WS snapshot for %s: %d bids, %d asks", token_id[:16], len(book.bids), len(book.asks))
        self._notify(token_id, book)

    async def _handle_price_change(self, msg: dict) -> None:
        """Handle price_change events — individual level updates.

        Format:
            {
                "event_type": "price_change",
                "market": "<condition_id>",
                "price_changes": [
                    {"asset_id": "...", "price": "0.5", "size": "200", "side": "BUY", ...},
                    ...
                ],
                "timestamp": "1757908892351"
            }
        """
        ts = int(msg.get("timestamp", 0) or 0)
        changes = msg.get("price_changes") or []

        for change in changes:
            if not isinstance(change, dict):
                continue

            token_id = change.get("asset_id", "")
            if not token_id or token_id in self._syncing:
                continue

            book = self._books.get(token_id)
            if book is None:
                await self._fetch_snapshot(token_id)
                continue

            price = float(change.get("price", 0))
            size = float(change.get("size", 0))
            raw_side = change.get("side", "").upper()

            if raw_side == "BUY":
                side = Side.BID
            elif raw_side == "SELL":
                side = Side.ASK
            else:
                continue

            book.apply_delta(side, price, size, sequence=ts, timestamp_ms=ts)
            self._notify(token_id, book)

    async def _handle_trade(self, msg: dict) -> None:
        """Handle last_trade_price — reduce liquidity at the traded price level.

        Format:
            {
                "event_type": "last_trade_price",
                "asset_id": "...",
                "price": "0.456",
                "size": "219.217767",
                "side": "BUY",  // taker side
                "timestamp": "1750428146322"
            }

        A BUY taker consumes ASK liquidity. A SELL taker consumes BID liquidity.
        """
        token_id = msg.get("asset_id", "")
        if not token_id or token_id in self._syncing:
            return

        book = self._books.get(token_id)
        if book is None:
            return

        trade_price = float(msg.get("price", 0))
        trade_size = float(msg.get("size", 0))
        raw_side = msg.get("side", "").upper()
        ts = int(msg.get("timestamp", 0) or 0)

        if not trade_price or not trade_size:
            return

        # Taker BUY consumes ASK side, taker SELL consumes BID side
        if raw_side == "BUY":
            levels = book.asks
        elif raw_side == "SELL":
            levels = book.bids
        else:
            return

        # Find and reduce the level at the trade price.
        # If trade_size >= level size, the trade swept through this level
        # entirely (and possibly others). Remove it — price_change messages
        # will correct adjacent levels.
        for level in levels:
            if level.price == trade_price:
                if trade_size >= level.size:
                    levels.remove(level)
                else:
                    level.size = round(level.size - trade_size, 8)
                break

        book.timestamp_ms = ts
        self._notify(token_id, book)

    # -- Listener notification ------------------------------------------------

    def _notify(self, token_id: str, book: OrderBook) -> None:
        for cb in self._listeners:
            try:
                result = cb(token_id, book)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                logger.exception("Error in book update listener")
