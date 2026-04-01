"""Smoke test — verify Gamma API connection, slug-based market discovery, and order books."""

import asyncio
import logging
import sys

from config.settings import load_settings
from clob_client import CLOBRestClient
from markets.selector import CryptoMarketSelector
from models.crypto_market import Coin, Duration

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_connection")

COINS = [Coin.BTC, Coin.ETH, Coin.SOL]


async def main():
    settings = load_settings()
    logger.info("Mode: %s", settings.mode.value)

    selector = CryptoMarketSelector(settings)
    rest = CLOBRestClient(settings)

    try:
        # 1. Fetch daily markets
        logger.info("--- Fetching daily markets ---")
        daily = await selector.fetch_daily(COINS)
        logger.info("Found %d daily markets", len(daily))

        # 2. Fetch weekly markets
        logger.info("--- Fetching weekly markets ---")
        weekly = await selector.fetch_weekly(COINS)
        logger.info("Found %d weekly markets", len(weekly))

        # 3. Show what we found
        all_markets = selector.markets
        if not all_markets:
            logger.warning("No crypto markets found — slug patterns may need updating")
            return

        logger.info("--- Found %d total crypto markets ---", len(all_markets))
        for m in all_markets:
            logger.info("  [%s %s] %s", m.coin.value, m.duration.value, m.question[:70])
            logger.info("    liq=$%.0f  vol=$%.0f  instruments=%d  expires=%s",
                        m.liquidity, m.volume, len(m.instruments),
                        m.hours_remaining and f"{m.hours_remaining:.1f}h" or "n/a")
            for inst in m.instruments[:5]:
                logger.info("      -> %s | yes=%.3f | strike=%s | token=%s...",
                            inst.label[:40], inst.yes_price,
                            f"${inst.target_price:,.0f}" if inst.target_price else "n/a",
                            inst.yes_token_id[:16])

        # 4. Fetch an order book from the CLOB for the first instrument
        first_with_instruments = next((m for m in all_markets if m.instruments), None)
        if first_with_instruments:
            token_id = first_with_instruments.instruments[0].yes_token_id
            logger.info("--- Fetching order book for token %s... ---", token_id[:16])
            book_data = await rest.get_order_book(token_id)
            n_bids = len(book_data.get("bids", []))
            n_asks = len(book_data.get("asks", []))
            logger.info("Order book: %d bids, %d asks", n_bids, n_asks)
            if n_bids:
                logger.info("  Best bid: %s", book_data["bids"][0])
            if n_asks:
                logger.info("  Best ask: %s", book_data["asks"][0])

        logger.info("--- All checks passed ---")

    except Exception:
        logger.exception("Test failed")
        sys.exit(1)
    finally:
        await selector.close()
        await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
