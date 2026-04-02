"""Order book manager — maintains unified YES books from both YES and NO token feeds.

Each instrument has one OrderBook keyed by its YES token_id. Updates from the
NO token are flipped (price → 1-price, BUY ↔ SELL) and merged into the YES book.
"""

import asyncio
import logging
from typing import Any, Callable, Optional

from config.settings import Settings
from clob_client import CLOBRestClient, CLOBWebSocketClient, CLOBDataError
from models.orderbook import OrderBook, PriceLevel, Side

logger = logging.getLogger(__name__)


def _parse_book_levels(raw_levels: list) -> list[list[float]]:
    result = []
    for level in raw_levels:
        parsed = _parse_book_level(level)
        if parsed:
            result.append(parsed)
    return result


def _parse_book_level(level) -> Optional[list[float]]:
    if isinstance(level, dict):
        return [float(level["price"]), float(level["size"])]
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        return [float(level[0]), float(level[1])]
    return None


def _flip_price(price: float) -> float:
    """Convert NO price to YES price: 1 - price."""
    return round(1.0 - price, 10)


def _flip_side(side: Side) -> Side:
    """NO BID = YES ASK, NO ASK = YES BID."""
    return Side.ASK if side == Side.BID else Side.BID


class OrderBookManager:
    """Manages unified YES order books from both YES and NO token WS feeds.

    Each instrument produces one book keyed by YES token_id. The manager
    subscribes to both YES and NO tokens and merges updates into a single view.
    """

    def __init__(self, settings: Settings, rest_client: Optional[CLOBRestClient] = None):
        self._settings = settings
        self._rest = rest_client or CLOBRestClient(settings)
        self._ws: Optional[CLOBWebSocketClient] = None
        self._books: dict[str, OrderBook] = {}  # keyed by YES token_id
        self._no_to_yes: dict[str, str] = {}    # NO token_id -> YES token_id
        self._all_tokens: set[str] = set()       # all subscribed token_ids
        self._listeners: list[Callable[[str, OrderBook], Any]] = []
        self._syncing: set[str] = set()

    # -- Public API -----------------------------------------------------------

    def register_instrument(self, yes_token_id: str, no_token_id: str = "") -> None:
        """Register a YES/NO token pair. Call before start()."""
        self._all_tokens.add(yes_token_id)
        if no_token_id:
            self._no_to_yes[no_token_id] = yes_token_id
            self._all_tokens.add(no_token_id)

    def get_book(self, yes_token_id: str) -> Optional[OrderBook]:
        return self._books.get(yes_token_id)

    def get_all_books(self) -> dict[str, OrderBook]:
        return dict(self._books)

    def on_update(self, callback: Callable[[str, OrderBook], Any]) -> None:
        """Register a listener. Signature: (yes_token_id, book)."""
        self._listeners.append(callback)

    async def start(self, token_ids: list[str]) -> None:
        """Initialize books and subscribe to WS. token_ids should be YES tokens only.

        NO tokens are subscribed automatically if registered via register_instrument().
        """
        # Ensure all YES tokens are tracked
        for tid in token_ids:
            self._all_tokens.add(tid)

        # Fetch YES book snapshots
        snapshot_tasks = [self._fetch_snapshot(tid) for tid in token_ids]
        await asyncio.gather(*snapshot_tasks, return_exceptions=True)

        # Subscribe to all tokens (YES + NO)
        all_sub = list(self._all_tokens)
        self._ws = CLOBWebSocketClient(self._settings, on_message=self._handle_ws_message)
        await self._ws.connect(assets=all_sub)

    async def stop(self) -> None:
        if self._ws:
            await self._ws.stop()

    def _resolve_token(self, token_id: str) -> tuple[str, bool]:
        """Resolve a token_id to (yes_token_id, is_no_token)."""
        if token_id in self._no_to_yes:
            return self._no_to_yes[token_id], True
        return token_id, False

    # -- Snapshot fetching ----------------------------------------------------

    async def _fetch_snapshot(self, token_id: str) -> None:
        """Fetch full book from REST and apply as snapshot."""
        self._syncing.add(token_id)
        try:
            data = await self._rest.get_order_book(token_id)
            bids = _parse_book_levels(data.get("bids") or [])
            asks = _parse_book_levels(data.get("asks") or [])
            ts = int(data.get("timestamp", 0) or 0)
            seq = ts

            tick = float(data.get("tick_size") or data.get("minimum_tick_size", 0.01) or 0.01)
            book = self._books.get(token_id) or OrderBook(token_id=token_id, tick_size=tick)
            if book.tick_size != tick:
                book.set_tick_size(tick)
            book.apply_snapshot(bids, asks, sequence=seq, timestamp_ms=ts)
            self._books[token_id] = book

            logger.info("Snapshot applied for %s: %d bids, %d asks",
                        token_id[:16], len(book.bids), len(book.asks))
            self._notify(token_id, book)
        except Exception:
            logger.exception("Failed to fetch snapshot for %s", token_id[:16])
        finally:
            self._syncing.discard(token_id)

    # -- WS message handling --------------------------------------------------

    async def _handle_ws_message(self, msg: Any) -> None:
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    await self._handle_ws_message(item)
            return

        if not isinstance(msg, dict):
            return

        event_type = msg.get("event_type") or msg.get("type", "")

        if event_type == "book":
            self._handle_book_snapshot(msg)
        elif event_type == "price_change":
            await self._handle_price_change(msg)
        elif event_type == "last_trade_price":
            self._handle_trade(msg)
        elif event_type == "tick_size_change":
            self._handle_tick_size_change(msg)
        elif event_type == "best_bid_ask":
            pass
        else:
            logger.debug("Unhandled WS event: %s", event_type)

    def _handle_book_snapshot(self, msg: dict) -> None:
        """Handle full book snapshot. If from a NO token, flip and merge into YES book."""
        token_id = msg.get("asset_id", "")
        if not token_id:
            return

        yes_id, is_no = self._resolve_token(token_id)
        bids = _parse_book_levels(msg.get("bids") or [])
        asks = _parse_book_levels(msg.get("asks") or [])
        ts = int(msg.get("timestamp", 0) or 0)

        if is_no:
            # NO snapshot → flip into YES book
            # NO bids at price P → YES asks at price (1-P)
            # NO asks at price P → YES bids at price (1-P)
            book = self._books.get(yes_id)
            if book is None:
                return
            for price, size in bids:
                book.apply_delta(Side.ASK, _flip_price(price), size, sequence=ts, timestamp_ms=ts)
            for price, size in asks:
                book.apply_delta(Side.BID, _flip_price(price), size, sequence=ts, timestamp_ms=ts)
        else:
            # YES snapshot → direct apply
            book = self._books.get(yes_id) or OrderBook(token_id=yes_id)
            book.apply_snapshot(bids, asks, sequence=ts, timestamp_ms=ts)
            self._books[yes_id] = book

        logger.info("WS snapshot for %s%s: %d bids, %d asks",
                    token_id[:16], " (NO→YES)" if is_no else "", len(book.bids), len(book.asks))
        self._notify(yes_id, book)

    async def _handle_price_change(self, msg: dict) -> None:
        """Handle price_change. NO token changes are flipped into the YES book."""
        ts = int(msg.get("timestamp", 0) or 0)
        changes = msg.get("price_changes") or []

        for change in changes:
            if not isinstance(change, dict):
                continue

            token_id = change.get("asset_id", "")
            if not token_id:
                continue

            yes_id, is_no = self._resolve_token(token_id)

            if yes_id in self._syncing or token_id in self._syncing:
                continue

            book = self._books.get(yes_id)
            if book is None:
                if not is_no:
                    await self._fetch_snapshot(yes_id)
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

            if is_no:
                # Flip: NO BUY at P → YES ASK at (1-P), NO SELL at P → YES BID at (1-P)
                side = _flip_side(side)
                price = _flip_price(price)

            book.apply_delta(side, price, size, sequence=ts, timestamp_ms=ts)
            self._notify(yes_id, book)

    def _handle_trade(self, msg: dict) -> None:
        """Handle trade. NO token trades are flipped into YES book."""
        token_id = msg.get("asset_id", "")
        if not token_id:
            return

        yes_id, is_no = self._resolve_token(token_id)

        if yes_id in self._syncing or token_id in self._syncing:
            return

        book = self._books.get(yes_id)
        if book is None:
            return

        trade_price = float(msg.get("price", 0))
        trade_size = float(msg.get("size", 0))
        raw_side = msg.get("side", "").upper()
        ts = int(msg.get("timestamp", 0) or 0)

        if not trade_price or not trade_size:
            return

        # Taker BUY consumes ASK, taker SELL consumes BID
        if raw_side == "BUY":
            consumed_side = Side.ASK
        elif raw_side == "SELL":
            consumed_side = Side.BID
        else:
            return

        if is_no:
            consumed_side = _flip_side(consumed_side)
            trade_price = _flip_price(trade_price)

        book.reduce_level(consumed_side, trade_price, trade_size)
        book.timestamp_ms = ts
        self._notify(yes_id, book)

    def _handle_tick_size_change(self, msg: dict) -> None:
        asset_id = msg.get("asset_id", "")
        new_tick = msg.get("new_tick_size") or msg.get("tick_size")

        if not asset_id or not new_tick:
            return

        yes_id, _ = self._resolve_token(asset_id)
        new_tick = float(new_tick)
        book = self._books.get(yes_id)
        if book is None:
            return

        old_tick = book.tick_size
        book.set_tick_size(new_tick)
        logger.info("Tick size changed for %s: %s -> %s", yes_id[:16], old_tick, new_tick)

    # -- Listener notification ------------------------------------------------

    def _notify(self, yes_token_id: str, book: OrderBook) -> None:
        for cb in self._listeners:
            try:
                result = cb(yes_token_id, book)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                logger.exception("Error in book update listener")
