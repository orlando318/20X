"""Run a forward test with a strategy against live data.

Usage:
    python run_forward_test.py --coin BTC --duration weekly
    python run_forward_test.py --coin ETH --duration daily --print-every 200
"""

import argparse
import asyncio
import logging

from models.crypto_market import Coin, Duration
from strategy import ForwardTest
from strategy.examples import SimpleSpreadStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def parse_args():
    parser = argparse.ArgumentParser(description="Run forward test")
    parser.add_argument("--coin", type=str, required=True)
    parser.add_argument("--duration", type=str, required=True)
    parser.add_argument("--timestamp", type=int, default=None)
    parser.add_argument("--hour-et", type=int, default=None)
    parser.add_argument("--print-every", type=int, default=100)
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

    strategy = SimpleSpreadStrategy(spread_threshold_bps=500, size=10.0)

    test = ForwardTest.create(
        strategy=strategy,
        coin=resolve_coin(args.coin),
        duration=resolve_duration(args.duration),
        timestamp=args.timestamp,
        hour_et=args.hour_et,
        with_binance=not args.no_binance,
        print_interval=args.print_every,
    )

    await test.run()


if __name__ == "__main__":
    asyncio.run(main())
