"""Collect raw WebSocket messages for a specified crypto market.

Stores every message (book snapshots, price_change, last_trade_price) as-is
into parquet. A separate replay tool reconstructs the order book from these.

Usage:
    python collect.py --coin BTC --duration weekly
    python collect.py --coin ETH --duration daily
    python collect.py --coin BTC --duration 1h --hour-et 11
    python collect.py --coin BTC --duration 4h --timestamp 1775059200
    python collect.py --coin BTC --duration 5min --timestamp 1775055900
    python collect.py --coin BTC --duration monthly
    python collect.py --coin BTC --duration yearly
"""

import argparse
import asyncio
import json
import logging
import signal
import time
from typing import Optional

import aiohttp
import websockets

from config.settings import load_settings
from markets.selector import CryptoMarketSelector
from models.crypto_market import Coin, CryptoMarket, Duration
from storage.parquet_store import ParquetStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("collect")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream raw WS messages to parquet")
    parser.add_argument("--coin", type=str, required=True)
    parser.add_argument("--duration", type=str, required=True)
    parser.add_argument("--timestamp", type=int, default=None)
    parser.add_argument("--hour-et", type=int, default=None)
    parser.add_argument("--data-dir", type=str, default="data")
    parser.add_argument("--flush-every", type=int, default=500)
    parser.add_argument("--env-file", type=str, default=None)
    return parser.parse_args()


def resolve_coin(raw: str) -> Coin:
    try:
        return Coin(raw.upper())
    except ValueError:
        raise SystemExit(f"Unknown coin: {raw}. Available: {', '.join(c.value for c in Coin)}")


def resolve_duration(raw: str) -> Duration:
    lookup = {d.value: d for d in Duration}
    key = raw.lower().strip()
    if key in lookup:
        return lookup[key]
    raise SystemExit(f"Unknown duration: {raw}. Available: {', '.join(lookup.keys())}")


async def discover_market(
    selector: CryptoMarketSelector, coin: Coin, duration: Duration,
    timestamp: Optional[int] = None, hour_et: Optional[int] = None,
) -> CryptoMarket:
    if duration == Duration.MIN_5:
        if not timestamp:
            raise SystemExit("--timestamp required for 5m markets")
        markets = await selector.fetch_5m([coin], timestamp)
    elif duration == Duration.MIN_15:
        if not timestamp:
            raise SystemExit("--timestamp required for 15m markets")
        markets = await selector.fetch_15m([coin], timestamp)
    elif duration == Duration.HOUR_1:
        markets = await selector.fetch_1h([coin], hour_et=hour_et)
    elif duration == Duration.HOUR_4:
        if not timestamp:
            raise SystemExit("--timestamp required for 4h markets")
        markets = await selector.fetch_4h([coin], timestamp)
    elif duration == Duration.DAILY:
        markets = await selector.fetch_daily([coin])
    elif duration == Duration.WEEKLY:
        markets = await selector.fetch_weekly([coin])
    elif duration == Duration.MONTHLY:
        markets = await selector.fetch_monthly([coin])
    elif duration == Duration.YEARLY:
        markets = await selector.fetch_yearly([coin])
    else:
        raise SystemExit(f"Unknown duration: {duration.value}")
    if not markets:
        raise SystemExit(f"No {coin.value} {duration.value} market found")
    return markets[0]


class RawMessageCollector:
    """Buffers raw WS messages and flushes to parquet."""

    def __init__(self, slug: str, store: ParquetStore, flush_every: int = 500):
        self._slug = slug
        self._store = store
        self._flush_every = flush_every
        self._buffer: list[dict] = []
        self._count = 0

    def record(self, msg: dict) -> None:
        """Store a single raw message."""
        self._buffer.append({
            "received_at_ms": int(time.time() * 1000),
            "event_type": msg.get("event_type", ""),
            "timestamp_ms": int(msg.get("timestamp", 0) or 0),
            "raw": json.dumps(msg),
        })
        self._count += 1

        if len(self._buffer) >= self._flush_every:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        path = self._store.append_raw(self._slug, self._buffer)
        logger.info("Flushed %d messages to %s (total: %d)", len(self._buffer), path, self._count)
        self._buffer.clear()


async def main():
    args = parse_args()
    coin = resolve_coin(args.coin)
    duration = resolve_duration(args.duration)

    settings = load_settings(args.env_file)
    store = ParquetStore(args.data_dir)
    selector = CryptoMarketSelector(settings)

    # 1. Discover market
    logger.info("Discovering %s %s market...", coin.value, duration.value)
    market = await discover_market(selector, coin, duration, timestamp=args.timestamp, hour_et=args.hour_et)
    logger.info("Found: %s (%d instruments)", market.question, len(market.instruments))

    if not market.instruments:
        raise SystemExit("Market has no instruments — nothing to collect")

    # Subscribe to both YES and NO tokens for full book depth
    all_token_ids = []
    for inst in market.instruments:
        all_token_ids.append(inst.yes_token_id)
        if inst.no_token_id:
            all_token_ids.append(inst.no_token_id)
    logger.info("Subscribing to %d tokens (%d YES + %d NO)",
                len(all_token_ids), len(market.instruments),
                len(all_token_ids) - len(market.instruments))

    # 2. Set up collector
    collector = RawMessageCollector(market.slug, store, flush_every=args.flush_every)

    # 3. Connect to WS and record everything
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    ws_url = settings.api.clob_ws_url
    attempt = 0

    logger.info("Connecting to WebSocket (Ctrl+C to stop)...")

    while not stop_event.is_set():
        try:
            attempt += 1
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                await ws.send(json.dumps({"assets_ids": all_token_ids, "type": "market"}))
                logger.info("WebSocket connected, streaming...")
                attempt = 0

                async for raw_text in ws:
                    if stop_event.is_set():
                        break
                    try:
                        parsed = json.loads(raw_text)
                    except json.JSONDecodeError:
                        continue

                    # Handle batched messages
                    if isinstance(parsed, list):
                        for msg in parsed:
                            if isinstance(msg, dict):
                                collector.record(msg)
                    elif isinstance(parsed, dict):
                        collector.record(parsed)

        except websockets.ConnectionClosed as exc:
            logger.warning("WS closed: code=%s reason=%s", exc.code, exc.reason)
        except (OSError, websockets.WebSocketException) as exc:
            logger.warning("WS error: %s", exc)

        if stop_event.is_set():
            break

        backoff = min(0.5 * (2 ** attempt), 30)
        logger.info("Reconnecting in %.1fs...", backoff)
        await asyncio.sleep(backoff)

    # 4. Final flush
    collector.flush()
    await selector.close()
    logger.info("Done. %d messages captured, saved to %s/", collector._count, args.data_dir)


if __name__ == "__main__":
    asyncio.run(main())
