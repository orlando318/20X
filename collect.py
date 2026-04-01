"""Collect live data for a specified crypto market via WebSocket stream.

Usage:
    python collect.py --coin BTC --duration weekly
    python collect.py --coin ETH --duration daily
    python collect.py --coin BTC --duration 1h --hour-et 11
    python collect.py --coin BTC --duration 4h --timestamp 1775059200
    python collect.py --coin BTC --duration 5min --timestamp 1775055900
    python collect.py --coin BTC --duration monthly
    python collect.py --coin BTC --duration yearly

Arguments:
    --coin        Coin ticker (BTC, ETH, SOL, DOGE, XRP, etc.)
    --duration    Market duration (5min, 15min, 1h, 4h, daily, weekly, monthly, yearly)
    --timestamp   End timestamp for 5m/15m/4h markets (unix seconds)
    --hour-et     Hour in Eastern Time for 1h markets (0-23)
    --data-dir    Directory for parquet files (default: data)
    --flush-every Flush to parquet every N updates (default: 500)
    --env-file    Path to .env file (optional)
"""

import argparse
import asyncio
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Optional

from config.settings import load_settings
from clob_client import CLOBRestClient
from markets.selector import CryptoMarketSelector
from models.crypto_market import Coin, CryptoMarket, Duration, Instrument
from models.orderbook import OrderBook
from orderbook.manager import OrderBookManager
from storage.parquet_store import ParquetStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("collect")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream live market data to parquet")
    parser.add_argument("--coin", type=str, required=True, help="Coin ticker (BTC, ETH, SOL, ...)")
    parser.add_argument("--duration", type=str, required=True, help="Market duration (daily, weekly)")
    parser.add_argument("--timestamp", type=int, default=None, help="End timestamp for 5m/15m/4h markets (unix seconds)")
    parser.add_argument("--hour-et", type=int, default=None, help="Hour in ET for 1h markets (0-23)")
    parser.add_argument("--data-dir", type=str, default="data", help="Parquet output directory")
    parser.add_argument("--flush-every", type=int, default=500, help="Flush to parquet every N updates")
    parser.add_argument("--env-file", type=str, default=None, help="Path to .env file")
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
    selector: CryptoMarketSelector, coin: Coin, duration: Duration, timestamp: Optional[int] = None, hour_et: Optional[int] = None,
) -> CryptoMarket:
    if duration == Duration.MIN_5:
        if not timestamp:
            raise SystemExit("--timestamp required for 5m markets (unix seconds of market end time)")
        markets = await selector.fetch_5m([coin], timestamp)
    elif duration == Duration.MIN_15:
        if not timestamp:
            raise SystemExit("--timestamp required for 15m markets (unix seconds of market end time)")
        markets = await selector.fetch_15m([coin], timestamp)
    elif duration == Duration.HOUR_1:
        markets = await selector.fetch_1h([coin], hour_et=hour_et)
    elif duration == Duration.HOUR_4:
        if not timestamp:
            raise SystemExit("--timestamp required for 4h markets (unix seconds of market end time)")
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
        raise SystemExit(f"No {coin.value} {duration.value} market found on Polymarket")
    return markets[0]


class StreamCollector:
    """Captures every order book update from the WS feed and flushes to parquet."""

    def __init__(
        self,
        market: CryptoMarket,
        store: ParquetStore,
        flush_every: int = 500,
    ):
        self._market = market
        self._store = store
        self._flush_every = flush_every
        self._buffer: list[dict] = []
        self._update_count = 0
        self._inst_by_token: dict[str, Instrument] = {
            inst.yes_token_id: inst for inst in market.instruments
        }

    def on_book_update(self, token_id: str, book: OrderBook) -> None:
        """Called on every WS book update — records top 20 levels on each side."""
        inst = self._inst_by_token.get(token_id)
        if inst is None:
            return

        record: dict = {
            "token_id": token_id,
            "label": inst.label,
            "strike": inst.target_price,
            "timestamp_ms": book.timestamp_ms or int(time.time() * 1000),
            "mid": book.mid,
            "spread": book.spread,
            "sequence": book.sequence,
        }

        # Top 20 bid levels: bid_price_0, bid_size_0, ..., bid_price_19, bid_size_19
        for i in range(20):
            if i < len(book.bids):
                record[f"bid_price_{i}"] = book.bids[i].price
                record[f"bid_size_{i}"] = book.bids[i].size
            else:
                record[f"bid_price_{i}"] = None
                record[f"bid_size_{i}"] = None

        # Top 20 ask levels
        for i in range(20):
            if i < len(book.asks):
                record[f"ask_price_{i}"] = book.asks[i].price
                record[f"ask_size_{i}"] = book.asks[i].size
            else:
                record[f"ask_price_{i}"] = None
                record[f"ask_size_{i}"] = None

        self._buffer.append(record)

        self._update_count += 1

        if len(self._buffer) >= self._flush_every:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        now = datetime.now(timezone.utc)
        path = self._store.append_book_snapshots(
            self._market.coin, self._market.duration, self._buffer, date=now,
        )
        logger.info("Flushed %d updates to %s (total: %d)",
                    len(self._buffer), path, self._update_count)
        self._buffer.clear()


async def main():
    args = parse_args()
    coin = resolve_coin(args.coin)
    duration = resolve_duration(args.duration)

    settings = load_settings(args.env_file)
    store = ParquetStore(args.data_dir)
    rest = CLOBRestClient(settings)
    selector = CryptoMarketSelector(settings)

    # 1. Discover market
    logger.info("Discovering %s %s market...", coin.value, duration.value)
    market = await discover_market(selector, coin, duration, timestamp=args.timestamp, hour_et=args.hour_et)
    logger.info("Found: %s (%d instruments)", market.question, len(market.instruments))
    for inst in market.instruments:
        logger.info("  %s | strike=%s | token=%s...",
                    inst.label[:50],
                    f"${inst.target_price:,.0f}" if inst.target_price else "n/a",
                    inst.yes_token_id[:16])

    if not market.instruments:
        raise SystemExit("Market has no instruments — nothing to collect")

    # 2. Set up stream collector
    collector = StreamCollector(market, store, flush_every=args.flush_every)
    ob_manager = OrderBookManager(settings, rest_client=rest)
    ob_manager.on_update(collector.on_book_update)

    # 3. Fetch initial snapshots via REST
    yes_token_ids = [inst.yes_token_id for inst in market.instruments]
    logger.info("Fetching initial snapshots for %d tokens...", len(yes_token_ids))
    for tid in yes_token_ids:
        await ob_manager._fetch_snapshot(tid)

    books_ready = sum(1 for tid in yes_token_ids if ob_manager.get_book(tid) is not None)
    logger.info("Order books ready: %d/%d", books_ready, len(yes_token_ids))

    # 4. Start WS stream
    logger.info("Connecting to WebSocket stream (Ctrl+C to stop)...")

    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Run WS connection in background — it calls on_book_update for every message
    ws_task = asyncio.create_task(ob_manager.start(yes_token_ids))

    # Wait for shutdown
    await stop_event.wait()

    # 5. Clean shutdown
    await ob_manager.stop()
    ws_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass

    # Final flush
    collector.flush()

    await selector.close()
    await rest.close()
    logger.info("Done. %d total updates captured, saved to %s/",
                collector._update_count, args.data_dir)


if __name__ == "__main__":
    asyncio.run(main())
