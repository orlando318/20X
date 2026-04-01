"""Order book manager — maintains local book state from WS feed and REST snapshots."""

import asyncio
import logging
from typing import Any, Callable, Optional

from config.settings import Settings
from clob_client import CLOBRestClient, CLOBWebSocketClient, CLOBDataError
from models.orderbook import OrderBook, PriceLevel, Side

logger = logging.getLogger(__name__)


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
            bids = [[float(p), float(s)] for p, s in (data.get("bids") or [])]
            asks = [[float(p), float(s)] for p, s in (data.get("asks") or [])]
            seq = int(data.get("hash", data.get("sequence", 0)) or 0)
            ts = int(data.get("timestamp", 0) or 0)

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

    async def _handle_ws_message(self, msg: dict) -> None:
        """Route incoming WS messages to appropriate handlers."""
        event_type = msg.get("event_type") or msg.get("type", "")

        if event_type in ("book", "book_update"):
            await self._handle_book_update(msg)
        elif event_type == "book_snapshot":
            self._handle_snapshot_message(msg)
        elif event_type in ("trade", "last_trade_price"):
            pass  # Trade events handled by a separate consumer if needed
        else:
            logger.debug("Unhandled WS event: %s", event_type)

    async def _handle_book_update(self, msg: dict) -> None:
        """Apply an incremental book delta from WS."""
        token_id = msg.get("asset_id") or msg.get("market", "")
        if not token_id:
            logger.warning("Book update missing asset_id: %s", msg)
            return

        # Skip updates while syncing
        if token_id in self._syncing:
            return

        book = self._books.get(token_id)
        if book is None:
            # No snapshot yet — fetch one
            logger.info("No book for %s, fetching snapshot", token_id)
            await self._fetch_snapshot(token_id)
            return

        new_seq = int(msg.get("sequence", msg.get("hash", 0)) or 0)

        # Gap detection
        if new_seq > 0 and book.sequence > 0 and new_seq > book.sequence + 1:
            logger.warning("Sequence gap for %s: expected %d, got %d — resyncing",
                           token_id, book.sequence + 1, new_seq)
            await self._fetch_snapshot(token_id)
            return

        ts = int(msg.get("timestamp", 0) or 0)

        # Apply bid changes
        for change in msg.get("bids", []):
            price, size = float(change[0]), float(change[1])
            book.apply_delta(Side.BID, price, size, sequence=new_seq, timestamp_ms=ts)

        # Apply ask changes
        for change in msg.get("asks", []):
            price, size = float(change[0]), float(change[1])
            book.apply_delta(Side.ASK, price, size, sequence=new_seq, timestamp_ms=ts)

        self._notify(token_id, book)

    def _handle_snapshot_message(self, msg: dict) -> None:
        """Handle a full snapshot delivered over WS."""
        token_id = msg.get("asset_id") or msg.get("market", "")
        if not token_id:
            return

        bids = [[float(p), float(s)] for p, s in (msg.get("bids") or [])]
        asks = [[float(p), float(s)] for p, s in (msg.get("asks") or [])]
        seq = int(msg.get("sequence", msg.get("hash", 0)) or 0)
        ts = int(msg.get("timestamp", 0) or 0)

        book = self._books.get(token_id) or OrderBook(token_id=token_id)
        book.apply_snapshot(bids, asks, sequence=seq, timestamp_ms=ts)
        self._books[token_id] = book

        logger.info("WS snapshot for %s: %d bids, %d asks", token_id, len(book.bids), len(book.asks))
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
