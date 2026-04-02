"""Live engine — discovers a market, maintains correct book state, and provides real-time access.

Usage:
    from live import LiveEngine
    from models.crypto_market import Coin, Duration

    engine = LiveEngine.from_env()
    await engine.start(Coin.BTC, Duration.WEEKLY)

    # Access current book state
    for inst in engine.instruments:
        book = engine.get_book(inst.yes_token_id)
        print(f"{inst.label}: mid={book.mid} spread={book.spread}")

    # Register callbacks
    engine.on_book_update(my_handler)

    await engine.stop()
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

import websockets

from config.settings import Settings, load_settings
from clob_client import CLOBRestClient
from markets.selector import CryptoMarketSelector
from models.crypto_market import Coin, CryptoMarket, Duration, Instrument
from models.orderbook import OrderBook, Side

logger = logging.getLogger(__name__)


def _parse_book_levels(raw_levels: list) -> list[list[float]]:
    result = []
    for level in raw_levels:
        if isinstance(level, dict):
            result.append([float(level["price"]), float(level["size"])])
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            result.append([float(level[0]), float(level[1])])
    return result


def _flip_price(price: float) -> float:
    return round(1.0 - price, 10)


def _flip_side(side: Side) -> Side:
    return Side.ASK if side == Side.BID else Side.BID


class LiveEngine:
    """Self-contained live engine for a single market.

    Handles discovery, WS connection, book state, and raw message recording.
    All book updates flow through a single code path with validation.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._rest = CLOBRestClient(settings)
        self._selector = CryptoMarketSelector(settings)

        self._market: Optional[CryptoMarket] = None
        self._books: dict[str, OrderBook] = {}      # YES token -> book
        self._no_to_yes: dict[str, str] = {}         # NO token -> YES token
        self._inst_by_yes: dict[str, Instrument] = {}

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False
        self._msg_count = 0

        self._book_listeners: list[Callable[[str, Instrument, OrderBook], Any]] = []
        self._trade_listeners: list[Callable[[str, Instrument, dict], Any]] = []
        self._raw_listeners: list[Callable[[dict], Any]] = []

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "LiveEngine":
        return cls(load_settings(env_file))

    # -- Properties -----------------------------------------------------------

    @property
    def market(self) -> Optional[CryptoMarket]:
        return self._market

    @property
    def instruments(self) -> list[Instrument]:
        return self._market.instruments if self._market else []

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def message_count(self) -> int:
        return self._msg_count

    # -- Book access ----------------------------------------------------------

    def get_book(self, yes_token_id: str) -> Optional[OrderBook]:
        return self._books.get(yes_token_id)

    def get_all_books(self) -> dict[str, OrderBook]:
        return dict(self._books)

    def get_book_for_strike(self, strike: float) -> Optional[OrderBook]:
        if not self._market:
            return None
        inst = self._market.get_instrument_by_strike(strike)
        if inst:
            return self._books.get(inst.yes_token_id)
        return None

    # -- Callbacks ------------------------------------------------------------

    def on_book_update(self, callback: Callable[[str, Instrument, OrderBook], Any]) -> None:
        """Register book update listener. Signature: (yes_token_id, instrument, book)."""
        self._book_listeners.append(callback)

    def on_trade(self, callback: Callable[[str, Instrument, dict], Any]) -> None:
        """Register trade listener. Signature: (yes_token_id, instrument, trade_msg)."""
        self._trade_listeners.append(callback)

    def on_raw_message(self, callback: Callable[[dict], Any]) -> None:
        """Register raw message listener. Called for every WS message before processing."""
        self._raw_listeners.append(callback)

    # -- Lifecycle ------------------------------------------------------------

    async def start(
        self,
        coin: Coin,
        duration: Duration,
        timestamp: Optional[int] = None,
        hour_et: Optional[int] = None,
    ) -> None:
        """Discover market, fetch snapshots, connect WS, and start streaming."""
        # 1. Discover
        logger.info("Discovering %s %s market...", coin.value, duration.value)
        self._market = await self._discover(coin, duration, timestamp, hour_et)
        logger.info("Found: %s (%d instruments)", self._market.question, len(self._market.instruments))

        if not self._market.instruments:
            raise RuntimeError("Market has no instruments")

        # 2. Build token mappings
        all_token_ids = []
        for inst in self._market.instruments:
            self._inst_by_yes[inst.yes_token_id] = inst
            all_token_ids.append(inst.yes_token_id)
            if inst.no_token_id:
                self._no_to_yes[inst.no_token_id] = inst.yes_token_id
                all_token_ids.append(inst.no_token_id)

        logger.info("Registered %d instruments (%d YES + %d NO tokens)",
                    len(self._market.instruments), len(self._inst_by_yes),
                    len(self._no_to_yes))

        # 3. Fetch initial snapshots for YES tokens via REST
        logger.info("Fetching initial book snapshots...")
        for inst in self._market.instruments:
            await self._fetch_snapshot(inst.yes_token_id, inst.min_tick_size)

        ready = sum(1 for tid in self._inst_by_yes if self._books.get(tid) is not None)
        logger.info("Books ready: %d/%d", ready, len(self._inst_by_yes))

        # 4. Start WS
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop(all_token_ids))
        logger.info("Live engine started")

    async def stop(self) -> None:
        """Stop WS, flush remaining raw messages, clean up."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        await self._selector.close()
        await self._rest.close()
        logger.info("Live engine stopped. %d messages processed.", self._msg_count)

    # -- Discovery ------------------------------------------------------------

    async def _discover(
        self, coin: Coin, duration: Duration,
        timestamp: Optional[int], hour_et: Optional[int],
    ) -> CryptoMarket:
        fetch_map = {
            Duration.MIN_5: lambda: self._selector.fetch_5m([coin], timestamp),
            Duration.MIN_15: lambda: self._selector.fetch_15m([coin], timestamp),
            Duration.HOUR_1: lambda: self._selector.fetch_1h([coin], hour_et=hour_et),
            Duration.HOUR_4: lambda: self._selector.fetch_4h([coin], timestamp),
            Duration.DAILY: lambda: self._selector.fetch_daily([coin]),
            Duration.WEEKLY: lambda: self._selector.fetch_weekly([coin]),
            Duration.MONTHLY: lambda: self._selector.fetch_monthly([coin]),
            Duration.YEARLY: lambda: self._selector.fetch_yearly([coin]),
        }
        fetcher = fetch_map.get(duration)
        if not fetcher:
            raise ValueError(f"Unknown duration: {duration}")
        markets = await fetcher()
        if not markets:
            raise RuntimeError(f"No {coin.value} {duration.value} market found")
        return markets[0]

    # -- REST snapshot --------------------------------------------------------

    async def _fetch_snapshot(self, yes_token_id: str, tick_size: float = 0.01) -> None:
        try:
            data = await self._rest.get_order_book(yes_token_id)
            bids = _parse_book_levels(data.get("bids") or [])
            asks = _parse_book_levels(data.get("asks") or [])
            ts = int(data.get("timestamp", 0) or 0)
            tick = float(data.get("tick_size") or data.get("minimum_tick_size") or tick_size or 0.01)

            book = OrderBook(token_id=yes_token_id, tick_size=tick)
            book.apply_snapshot(bids, asks, sequence=ts, timestamp_ms=ts)
            self._books[yes_token_id] = book

            logger.debug("Snapshot for %s: %d bids, %d asks, tick=%s",
                        yes_token_id[:16], len(book.bids), len(book.asks), tick)
        except Exception:
            logger.exception("Failed to fetch snapshot for %s", yes_token_id[:16])

    # -- WebSocket loop -------------------------------------------------------

    async def _ws_loop(self, token_ids: list[str]) -> None:
        ws_url = self._settings.api.clob_ws_url
        attempt = 0

        while self._running:
            try:
                attempt += 1
                async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
                    logger.info("WS connected, streaming %d tokens", len(token_ids))
                    attempt = 0

                    async for raw_text in ws:
                        if not self._running:
                            break
                        try:
                            parsed = json.loads(raw_text)
                        except json.JSONDecodeError:
                            continue

                        if isinstance(parsed, list):
                            for msg in parsed:
                                if isinstance(msg, dict):
                                    self._process_message(msg)
                        elif isinstance(parsed, dict):
                            self._process_message(parsed)

            except websockets.ConnectionClosed as exc:
                logger.warning("WS closed: code=%s reason=%s", exc.code, exc.reason)
            except (OSError, websockets.WebSocketException) as exc:
                logger.warning("WS error: %s", exc)

            self._ws = None
            if not self._running:
                break

            backoff = min(0.5 * (2 ** attempt), 30)
            logger.info("Reconnecting in %.1fs...", backoff)
            await asyncio.sleep(backoff)

    # -- Message processing ---------------------------------------------------

    def _process_message(self, msg: dict) -> None:
        """Single entry point for all WS messages."""
        self._msg_count += 1
        event_type = msg.get("event_type", "")

        # Notify raw listeners
        for cb in self._raw_listeners:
            try:
                cb(msg)
            except Exception:
                logger.exception("Error in raw message listener")

        # Route to handler
        if event_type == "book":
            self._on_book_snapshot(msg)
        elif event_type == "price_change":
            self._on_price_change(msg)
        elif event_type == "last_trade_price":
            self._on_trade(msg)
        elif event_type == "tick_size_change":
            self._on_tick_size_change(msg)

    def _resolve(self, token_id: str) -> tuple[str, bool]:
        """Resolve token to (yes_token_id, is_no_token)."""
        if token_id in self._no_to_yes:
            return self._no_to_yes[token_id], True
        return token_id, False

    def _on_book_snapshot(self, msg: dict) -> None:
        token_id = msg.get("asset_id", "")
        if not token_id:
            return

        yes_id, is_no = self._resolve(token_id)
        book = self._books.get(yes_id)
        if book is None:
            return

        bids = _parse_book_levels(msg.get("bids") or [])
        asks = _parse_book_levels(msg.get("asks") or [])
        ts = int(msg.get("timestamp", 0) or 0)

        if is_no:
            for price, size in bids:
                book.apply_delta(Side.ASK, _flip_price(price), size, sequence=ts, timestamp_ms=ts)
            for price, size in asks:
                book.apply_delta(Side.BID, _flip_price(price), size, sequence=ts, timestamp_ms=ts)
        else:
            book.apply_snapshot(bids, asks, sequence=ts, timestamp_ms=ts)

        self._notify_book(yes_id, book)

    def _on_price_change(self, msg: dict) -> None:
        ts = int(msg.get("timestamp", 0) or 0)

        for change in msg.get("price_changes") or []:
            if not isinstance(change, dict):
                continue

            token_id = change.get("asset_id", "")
            if not token_id:
                continue

            yes_id, is_no = self._resolve(token_id)
            book = self._books.get(yes_id)
            if book is None:
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
                side = _flip_side(side)
                price = _flip_price(price)

            book.apply_delta(side, price, size, sequence=ts, timestamp_ms=ts)
            self._notify_book(yes_id, book)

    def _on_trade(self, msg: dict) -> None:
        token_id = msg.get("asset_id", "")
        if not token_id:
            return

        yes_id, is_no = self._resolve(token_id)
        book = self._books.get(yes_id)
        if book is None:
            return

        trade_price = float(msg.get("price", 0))
        trade_size = float(msg.get("size", 0))
        raw_side = msg.get("side", "").upper()
        ts = int(msg.get("timestamp", 0) or 0)

        if not trade_price or not trade_size:
            return

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
        self._notify_book(yes_id, book)

        # Notify trade listeners
        inst = self._inst_by_yes.get(yes_id)
        if inst:
            for cb in self._trade_listeners:
                try:
                    cb(yes_id, inst, {
                        "price": trade_price,
                        "size": trade_size,
                        "side": raw_side if not is_no else ("SELL" if raw_side == "BUY" else "BUY"),
                        "timestamp_ms": ts,
                    })
                except Exception:
                    logger.exception("Error in trade listener")

    def _on_tick_size_change(self, msg: dict) -> None:
        asset_id = msg.get("asset_id", "")
        new_tick = msg.get("new_tick_size") or msg.get("tick_size")
        if not asset_id or not new_tick:
            return

        yes_id, _ = self._resolve(asset_id)
        book = self._books.get(yes_id)
        if book is None:
            return

        old = book.tick_size
        book.set_tick_size(float(new_tick))
        logger.info("Tick size: %s -> %s for %s", old, new_tick, yes_id[:16])

    # -- Notifications --------------------------------------------------------

    def _notify_book(self, yes_id: str, book: OrderBook) -> None:
        inst = self._inst_by_yes.get(yes_id)
        if not inst:
            return
        for cb in self._book_listeners:
            try:
                cb(yes_id, inst, book)
            except Exception:
                logger.exception("Error in book listener")

    # -- Diagnostics ----------------------------------------------------------

    def print_books(self, levels: int = 5) -> None:
        """Print current book state for all instruments."""
        if not self._market:
            print("No market loaded")
            return
        for inst in self._market.instruments:
            book = self._books.get(inst.yes_token_id)
            if not book:
                print(f"  {inst.label}: no book")
                continue
            bb = book.best_bid
            ba = book.best_ask
            print(f"\n  {inst.label} (strike={inst.target_price})")
            print(f"    mid={book.mid}  spread={book.spread}  tick={book.tick_size}")
            print(f"    ASKS ({len(book.asks)} levels):")
            for lvl in list(reversed(book.asks[:levels])):
                print(f"      {lvl.price:.4f}  {lvl.size:>12.2f}")
            print(f"    ---- mid={book.mid} ----")
            print(f"    BIDS ({len(book.bids)} levels):")
            for lvl in book.bids[:levels]:
                print(f"      {lvl.price:.4f}  {lvl.size:>12.2f}")
