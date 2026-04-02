"""Binance WebSocket feed for bookTicker and trade streams.

Subscribes to pairs on demand — strategies call add_pair() to start receiving data.

Usage:
    from connectors import BinanceFeed

    feed = BinanceFeed()
    feed.on_book_ticker(my_handler)   # (pair, bid, bid_size, ask, ask_size, ts)
    feed.on_trade(my_handler)         # (pair, price, size, side, ts)

    feed.add_pair("BTCUSDT")
    feed.add_pair("ETHUSDT")

    await feed.start()
    # ... running ...
    await feed.stop()
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiohttp
import websockets

logger = logging.getLogger(__name__)

REST_BASE = "https://api.binance.com"
WS_BASE = "wss://stream.binance.com:9443/ws"
WS_COMBINED = "wss://stream.binance.com:9443/stream"


@dataclass
class BookTick:
    pair: str
    bid: float
    bid_size: float
    ask: float
    ask_size: float
    timestamp_ms: int
    local_received_ms: int

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2, 8)

    @property
    def spread(self) -> float:
        return round(self.ask - self.bid, 8)


@dataclass
class Trade:
    pair: str
    price: float
    size: float
    is_buyer_maker: bool
    timestamp_ms: int
    trade_id: int
    local_received_ms: int

    @property
    def side(self) -> str:
        return "SELL" if self.is_buyer_maker else "BUY"


class BinanceFeed:
    """Streams bookTicker and trade data from Binance for any number of pairs."""

    def __init__(self):
        self._pairs: set[str] = set()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False

        self._book_listeners: list[Callable[[BookTick], Any]] = []
        self._trade_listeners: list[Callable[[Trade], Any]] = []
        self._raw_listeners: list[Callable[[dict], Any]] = []

        # Latest book tick per pair for quick access
        self._latest_book: dict[str, BookTick] = {}

    # -- Configuration --------------------------------------------------------

    def add_pair(self, pair: str) -> None:
        """Add a pair to subscribe to (e.g. 'BTCUSDT'). Call before or after start()."""
        pair = pair.upper()
        self._pairs.add(pair)
        # If already running, dynamically subscribe
        if self._running and self._ws:
            asyncio.ensure_future(self._subscribe([pair]))

    def add_pairs(self, pairs: list[str]) -> None:
        for p in pairs:
            self.add_pair(p)

    # -- Callbacks ------------------------------------------------------------

    def on_book_ticker(self, callback: Callable[[BookTick], Any]) -> None:
        self._book_listeners.append(callback)

    def on_trade(self, callback: Callable[[Trade], Any]) -> None:
        self._trade_listeners.append(callback)

    def on_raw_message(self, callback: Callable[[dict], Any]) -> None:
        self._raw_listeners.append(callback)

    # -- State access ---------------------------------------------------------

    def get_book(self, pair: str) -> Optional[BookTick]:
        return self._latest_book.get(pair.upper())

    def get_mid(self, pair: str) -> Optional[float]:
        tick = self._latest_book.get(pair.upper())
        return tick.mid if tick else None

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if not self._pairs:
            raise RuntimeError("No pairs added. Call add_pair() first.")
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        logger.info("Binance feed stopped")

    # -- WebSocket ------------------------------------------------------------

    def _build_streams(self) -> list[str]:
        streams = []
        for pair in self._pairs:
            lower = pair.lower()
            streams.append(f"{lower}@bookTicker")
            streams.append(f"{lower}@trade")
        return streams

    async def _subscribe(self, pairs: list[str]) -> None:
        """Dynamically subscribe to new pairs on an existing connection."""
        if not self._ws:
            return
        streams = []
        for pair in pairs:
            lower = pair.lower()
            streams.append(f"{lower}@bookTicker")
            streams.append(f"{lower}@trade")
        msg = {"method": "SUBSCRIBE", "params": streams, "id": int(time.time())}
        await self._ws.send(json.dumps(msg))
        logger.info("Subscribed to %s", pairs)

    async def _ws_loop(self) -> None:
        attempt = 0

        while self._running:
            try:
                attempt += 1
                streams = self._build_streams()
                stream_param = "/".join(streams)
                url = f"{WS_COMBINED}?streams={stream_param}"

                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    logger.info("Binance WS connected (%d streams for %d pairs)",
                                len(streams), len(self._pairs))
                    attempt = 0

                    async for raw_text in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_text)
                        except json.JSONDecodeError:
                            continue

                                        # Combined stream wraps data in {"stream": "...", "data": {...}}
                        stream_name = msg.get("stream", "")
                        data = msg.get("data", msg)
                        self._process(data, stream_name)

            except websockets.ConnectionClosed as exc:
                logger.warning("Binance WS closed: code=%s reason=%s", exc.code, exc.reason)
            except (OSError, websockets.WebSocketException) as exc:
                logger.warning("Binance WS error: %s", exc)

            self._ws = None
            if not self._running:
                break

            backoff = min(0.5 * (2 ** attempt), 30)
            logger.info("Binance reconnecting in %.1fs...", backoff)
            await asyncio.sleep(backoff)

    # -- Message processing ---------------------------------------------------

    def _process(self, data: dict, stream_name: str = "") -> None:
        now_ms = int(time.time() * 1000)

        # Notify raw listeners
        for cb in self._raw_listeners:
            try:
                cb(data)
            except Exception:
                logger.exception("Error in Binance raw listener")

        # Detect event type from stream name or "e" field
        event = data.get("e", "")
        if not event and "bookTicker" in stream_name:
            event = "bookTicker"
        elif not event and "@trade" in stream_name:
            event = "trade"

        if event == "bookTicker":
            tick = BookTick(
                pair=data.get("s", ""),
                bid=float(data.get("b", 0)),
                bid_size=float(data.get("B", 0)),
                ask=float(data.get("a", 0)),
                ask_size=float(data.get("A", 0)),
                timestamp_ms=int(data.get("T", data.get("E", data.get("u", 0)))),
                local_received_ms=now_ms,
            )
            self._latest_book[tick.pair] = tick
            for cb in self._book_listeners:
                try:
                    cb(tick)
                except Exception:
                    logger.exception("Error in Binance book listener")

        elif event == "trade":
            trade = Trade(
                pair=data.get("s", ""),
                price=float(data.get("p", 0)),
                size=float(data.get("q", 0)),
                is_buyer_maker=data.get("m", False),
                timestamp_ms=int(data.get("T", 0)),
                trade_id=int(data.get("t", 0)),
                local_received_ms=now_ms,
            )
            for cb in self._trade_listeners:
                try:
                    cb(trade)
                except Exception:
                    logger.exception("Error in Binance trade listener")


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

async def fetch_candle_open(pair: str, start_time_ms: int, interval: str = "1m") -> Optional[float]:
    """Fetch the open price of a Binance kline at a specific time.

    Args:
        pair: e.g. "BTCUSDT"
        start_time_ms: Unix ms of candle open
        interval: Binance kline interval ("1m", "5m", "15m", "1h", "4h")

    Returns:
        Open price as float, or None on failure.
    """
    url = f"{REST_BASE}/api/v3/klines"
    params = {
        "symbol": pair,
        "interval": interval,
        "startTime": str(start_time_ms),
        "limit": "1",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data and len(data) > 0:
                    # Kline format: [open_time, open, high, low, close, ...]
                    return float(data[0][1])
    except Exception as exc:
        logger.warning("Failed to fetch Binance candle: %s", exc)
    return None


async def fetch_reference_price(pair: str, event_start_time_ms: int, duration_minutes: int) -> Optional[float]:
    """Get the Binance open price for an up/down market's reference candle.

    Picks the appropriate Binance kline interval based on market duration,
    then fetches the open price at the event start time.
    """
    # Map market duration to Binance interval
    if duration_minutes <= 5:
        interval = "1m"
    elif duration_minutes <= 15:
        interval = "5m"
    elif duration_minutes <= 60:
        interval = "1h"
    elif duration_minutes <= 240:
        interval = "4h"
    else:
        interval = "1d"

    return await fetch_candle_open(pair, event_start_time_ms, interval)
