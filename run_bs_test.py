"""Forward test the Black-Scholes mispricing strategy.

Usage:
    python run_bs_test.py --coin BTC --duration weekly
    python run_bs_test.py --coin BTC --duration weekly --edge 0.03 --vol 0.8 --size 100
    python run_bs_test.py --coin ETH --duration monthly --edge 0.05
"""

import argparse
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from connectors.binance import fetch_candle_open
from models.crypto_market import Coin, Duration
from strategy import ForwardTest
from strategy.bs_strategy import BSMispricingStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_bs_test")


def parse_args():
    parser = argparse.ArgumentParser(description="Forward test BS mispricing strategy")
    parser.add_argument("--coin", type=str, required=True)
    parser.add_argument("--duration", type=str, required=True)
    parser.add_argument("--timestamp", type=int, default=None)
    parser.add_argument("--hour-et", type=int, default=None)
    parser.add_argument("--edge", type=float, default=0.05, help="Min mispricing to trade (default 0.05)")
    parser.add_argument("--vol", type=float, default=None, help="Override vol (annualized, e.g. 0.8)")
    parser.add_argument("--size", type=float, default=50.0, help="Order size per trade")
    parser.add_argument("--max-pos", type=float, default=200.0, help="Max position per instrument")
    parser.add_argument("--cooldown", type=float, default=30.0, help="Seconds between trades per instrument")
    parser.add_argument("--print-every", type=int, default=200)
    parser.add_argument("--no-binance", action="store_true")
    return parser.parse_args()


def resolve_coin(raw: str) -> Coin:
    try:
        return Coin(raw.upper())
    except ValueError:
        raise SystemExit(f"Unknown coin: {raw}")


def resolve_duration(raw: str) -> Duration:
    lookup = {d.value: d for d in Duration}
    if raw.lower() in lookup:
        return lookup[raw.lower()]
    raise SystemExit(f"Unknown duration: {raw}")


async def main():
    args = parse_args()
    coin = resolve_coin(args.coin)
    duration = resolve_duration(args.duration)

    # Create test — we need the engine to discover the market first to get expiry
    test = ForwardTest.create(
        strategy=BSMispricingStrategy(),  # placeholder, replaced below
        coin=coin,
        duration=duration,
        timestamp=args.timestamp,
        hour_et=args.hour_et,
        with_binance=not args.no_binance,
        print_interval=args.print_every,
    )

    # Discover market to get expiry date
    engine = test.engine
    market = await engine._discover(coin, duration, args.timestamp, args.hour_et)

    expiry = market.end_date
    if not expiry:
        raise SystemExit("Market has no end_date — cannot compute time-to-expiry")

    # For up/down markets, fetch the reference price from Binance
    COIN_TO_PAIR = {
        Coin.BTC: "BTCUSDT", Coin.ETH: "ETHUSDT", Coin.SOL: "SOLUSDT",
        Coin.DOGE: "DOGEUSDT", Coin.XRP: "XRPUSDT", Coin.MATIC: "MATICUSDT",
        Coin.AVAX: "AVAXUSDT", Coin.LINK: "LINKUSDT", Coin.ADA: "ADAUSDT",
        Coin.DOT: "DOTUSDT",
    }
    reference_price = None
    has_strikes = any(inst.target_price for inst in market.instruments)

    if not has_strikes:
        pair = COIN_TO_PAIR.get(coin)
        if pair:
            now_utc = datetime.now(timezone.utc)
            if market.event_start_time and now_utc >= market.event_start_time:
                # Market already started — fetch the actual candle open from Binance
                event_ms = int(market.event_start_time.timestamp() * 1000)
                dur_minutes = duration.max_minutes
                if dur_minutes <= 5:
                    interval = "1m"
                elif dur_minutes <= 15:
                    interval = "5m"
                elif dur_minutes <= 60:
                    interval = "1h"
                elif dur_minutes <= 240:
                    interval = "4h"
                else:
                    interval = "1d"
                reference_price = await fetch_candle_open(pair, event_ms, interval)
                if reference_price:
                    logger.info("Reference price (Binance %s %s candle open): %.2f", pair, interval, reference_price)

            if reference_price is None:
                # Market hasn't started yet or candle fetch failed — use current spot
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(f"https://api.binance.com/api/v3/ticker/price?symbol={pair}") as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            reference_price = float(data.get("price", 0))
                            logger.info("Reference price (Binance %s current spot): %.2f", pair, reference_price)

            if not reference_price:
                logger.warning("Could not get reference price — BS pricing won't work for up/down markets")

    logger.info("Market: %s", market.question)
    logger.info("Expiry: %s", expiry)
    logger.info("Instruments: %d (strikes: %s)", len(market.instruments),
                "yes" if has_strikes else f"no, ref_price={reference_price}")
    logger.info("Edge threshold: %.3f", args.edge)
    logger.info("Vol: %s", f"fixed at {args.vol}" if args.vol else "implied (median across instruments)")

    # Now create the real strategy with the expiry
    strategy = BSMispricingStrategy(
        edge_threshold=args.edge,
        size=args.size,
        max_position=args.max_pos,
        vol_override=args.vol,
        expiry_utc=expiry,
        reference_price=reference_price,
        cooldown_seconds=args.cooldown,
    )

    # Set strategy and pre-discovered market to avoid re-discovery
    test._strategy = strategy
    test.set_market(market)

    await test.run()


if __name__ == "__main__":
    asyncio.run(main())
