"""Run the live engine with Binance feed and latency profiling.

Usage:
    python run_live.py --coin BTC --duration weekly
    python run_live.py --coin ETH --duration daily --print-every 50
    python run_live.py --coin BTC --duration 1h
"""

import argparse
import asyncio
import logging
import signal

from connectors import BinanceFeed
from live import LiveEngine
from models.crypto_market import Coin, Duration, Instrument
from models.orderbook import OrderBook
from monitoring import LatencyTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_live")

# Coin → Binance USDT pair
COIN_TO_PAIR: dict[Coin, str] = {
    Coin.BTC: "BTCUSDT",
    Coin.ETH: "ETHUSDT",
    Coin.SOL: "SOLUSDT",
    Coin.DOGE: "DOGEUSDT",
    Coin.XRP: "XRPUSDT",
    Coin.MATIC: "MATICUSDT",
    Coin.AVAX: "AVAXUSDT",
    Coin.LINK: "LINKUSDT",
    Coin.ADA: "ADAUSDT",
    Coin.DOT: "DOTUSDT",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Run live engine with Binance feed")
    parser.add_argument("--coin", type=str, required=True)
    parser.add_argument("--duration", type=str, required=True)
    parser.add_argument("--timestamp", type=int, default=None)
    parser.add_argument("--hour-et", type=int, default=None)
    parser.add_argument("--print-every", type=int, default=100,
                        help="Print book summary every N Polymarket updates")
    parser.add_argument("--no-binance", action="store_true", help="Skip Binance feed")
    return parser.parse_args()


def resolve_coin(raw: str) -> Coin:
    try:
        return Coin(raw.upper())
    except ValueError:
        raise SystemExit(f"Unknown coin: {raw}. Available: {', '.join(c.value for c in Coin)}")


def resolve_duration(raw: str) -> Duration:
    lookup = {d.value: d for d in Duration}
    if raw.lower() in lookup:
        return lookup[raw.lower()]
    raise SystemExit(f"Unknown duration: {raw}. Available: {', '.join(lookup.keys())}")


async def main():
    args = parse_args()
    coin = resolve_coin(args.coin)
    duration = resolve_duration(args.duration)
    binance_pair = COIN_TO_PAIR.get(coin)

    engine = LiveEngine.from_env()
    tracker = LatencyTracker()
    tracker.attach_engine(engine)

    # Binance feed
    binance: BinanceFeed | None = None
    if not args.no_binance and binance_pair:
        binance = BinanceFeed()
        tracker.attach_binance(binance)
        binance.add_pair(binance_pair)

        def on_binance_book(tick):
            pass  # tracked by profiler, available via binance.get_mid()

        binance.on_book_ticker(on_binance_book)

    # Polymarket callbacks
    update_count = 0

    def on_book(yes_id: str, inst: Instrument, book: OrderBook):
        nonlocal update_count
        update_count += 1
        if update_count % args.print_every == 0:
            bb = book.best_bid
            ba = book.best_ask
            binance_mid = binance.get_mid(binance_pair) if binance and binance_pair else None
            logger.info(
                "[%d] %s | mid=%s spread=%s | bid=%s x %s | ask=%s x %s | levels=%d/%d%s",
                update_count,
                (inst.label if inst.label else yes_id[:16]),
                book.mid, book.spread,
                bb.price if bb else "-", f"{bb.size:.1f}" if bb else "-",
                ba.price if ba else "-", f"{ba.size:.1f}" if ba else "-",
                len(book.bids), len(book.asks),
                f" | {binance_pair}={binance_mid}" if binance_mid else "",
            )

    def on_trade(yes_id: str, inst: Instrument, trade: dict):
        logger.info(
            "TRADE %s | %s %.4f x %.2f",
            (inst.label if inst.label else yes_id[:16]),
            trade["side"], trade["price"], trade["size"],
        )

    engine.on_book_update(on_book)
    engine.on_trade(on_trade)

    # Start both
    await engine.start(coin, duration, timestamp=args.timestamp, hour_et=args.hour_et)
    if binance:
        await binance.start()
        logger.info("Binance feed started for %s", binance_pair)

    engine.print_books(levels=5)

    # Wait for shutdown
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logger.info("Running (Ctrl+C to stop)...")
    await stop.wait()

    # Shutdown
    if binance:
        await binance.stop()
    await engine.stop()

    logger.info("Final book state:")
    engine.print_books(levels=5)

    if binance and binance_pair:
        tick = binance.get_book(binance_pair)
        if tick:
            print(f"\nBinance {binance_pair}: bid={tick.bid} ask={tick.ask} mid={tick.mid} spread={tick.spread}")

    print()
    tracker.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
