"""Black-Scholes mispricing strategy for Polymarket binary options.

Uses per-instrument implied volatility. On each update:
1. Compute IV for this instrument from its current Polymarket price
2. Re-price using its previous IV + updated Binance spot
3. Trade when market price diverges from the repriced fair value

The edge: when spot moves on Binance, BS fair value shifts immediately,
but Polymarket market prices may lag. The strategy captures that lag.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from connectors.binance import BookTick, Trade as BinanceTrade
from models.crypto_market import Instrument
from models.orderbook import OrderBook
from strategy.base import Strategy, Signal
from strategy.binary_bs import (
    BinaryPrice,
    ImpliedVolTracker,
    price_binary_call,
    price_binary_put,
)

logger = logging.getLogger(__name__)


def _is_call(label: str) -> Optional[bool]:
    """Determine if instrument is a call or put from its label."""
    lower = label.lower()
    if "reach" in lower or "above" in lower or "hit" in lower or "up" in lower:
        return True
    if "dip" in lower or "below" in lower or "drop" in lower or "down" in lower:
        return False
    return None


class BSMispricingStrategy(Strategy):
    """Trade Polymarket binary options when they diverge from BS fair value.

    Uses each instrument's own implied vol (from previous update) + current
    Binance spot to compute fair value. Trades when market lags spot moves.
    """

    def __init__(
        self,
        edge_threshold: float = 0.05,
        size: float = 50.0,
        max_position: float = 200.0,
        vol_override: Optional[float] = None,
        expiry_utc: Optional[datetime] = None,
        reference_price: Optional[float] = None,
        min_instruments: int = 1,
        cooldown_seconds: float = 30.0,
    ):
        self._edge = edge_threshold
        self._size = size
        self._max_pos = max_position
        self._vol_override = vol_override
        self._expiry = expiry_utc
        self._reference_price = reference_price
        self._min_instruments = min_instruments
        self._cooldown = cooldown_seconds

        self._iv_tracker = ImpliedVolTracker()
        self._spot: Optional[float] = None
        self._positions: dict[str, float] = {}
        self._last_trade_time: dict[str, float] = {}
        self._pricing_cache: dict[str, BinaryPrice] = {}

    # -- Binance callbacks ----------------------------------------------------

    def on_binance_trade(self, trade: BinanceTrade) -> None:
        self._spot = trade.price

    def on_binance_tick(self, tick: BookTick) -> None:
        self._spot = tick.mid

    # -- Polymarket callback --------------------------------------------------

    def on_book_update(
        self, token_id: str, inst: Instrument, book: OrderBook
    ) -> Optional[Signal | list[Signal]]:
        if not self._spot or not book.best_bid or not book.best_ask:
            return None

        strike = inst.target_price or self._reference_price
        if not strike or not self._expiry:
            return None

        now = datetime.now(timezone.utc)
        tte_seconds = (self._expiry - now).total_seconds()
        if tte_seconds <= 0:
            return None
        tte_years = tte_seconds / (365.25 * 24 * 3600)

        is_call = _is_call(inst.label)
        if is_call is None:
            return None

        market_mid = book.mid
        if market_mid is None:
            return None

        # Step 1: Get PREVIOUS IV for this instrument (before updating)
        if self._vol_override:
            vol = self._vol_override
        else:
            vol = self._iv_tracker.all_ivs.get(token_id)
            if vol is None:
                # No previous IV — compute and store for next time, skip trading
                self._iv_tracker.update(token_id, market_mid, self._spot, strike, tte_years, is_call)
                return None

        # Step 2: Reprice with PREVIOUS IV + CURRENT spot (spot moved, IV lagged)
        if is_call:
            bp = price_binary_call(self._spot, strike, vol, tte_years)
        else:
            bp = price_binary_put(self._spot, strike, vol, tte_years)

        self._pricing_cache[token_id] = bp
        fair = bp.fair_value
        mispricing = fair - market_mid

        # Step 3: NOW update IV from current market price (for next cycle)
        self._iv_tracker.update(token_id, market_mid, self._spot, strike, tte_years, is_call)

        # Step 4: Trade if mispriced beyond edge
        now_ts = time.time()
        last = self._last_trade_time.get(token_id, 0)
        if now_ts - last < self._cooldown:
            return None

        current_pos = self._positions.get(token_id, 0)
        signals = []

        if mispricing > self._edge and current_pos < self._max_pos:
            buy_size = min(self._size, self._max_pos - current_pos)
            if buy_size > 0:
                signals.append(Signal.buy(token_id, price=book.best_ask.price, size=buy_size))
                self._last_trade_time[token_id] = now_ts
                logger.info(
                    "BUY %s | spot=%.0f K=%.0f iv=%.4f | fair=%.4f mkt=%.4f edge=%.4f",
                    inst.label, self._spot, strike, vol,
                    fair, market_mid, mispricing,
                )

        elif mispricing < -self._edge and current_pos > -self._max_pos:
            sell_size = min(self._size, self._max_pos + current_pos)
            if sell_size > 0:
                signals.append(Signal.sell(token_id, price=book.best_bid.price, size=sell_size))
                self._last_trade_time[token_id] = now_ts
                logger.info(
                    "SELL %s | spot=%.0f K=%.0f iv=%.4f | fair=%.4f mkt=%.4f edge=%.4f",
                    inst.label, self._spot, strike, vol,
                    fair, market_mid, mispricing,
                )

        return signals if signals else None

    # -- Fill tracking --------------------------------------------------------

    def on_fill(self, token_id: str, price: float, size: float, side: str) -> None:
        current = self._positions.get(token_id, 0)
        if side == "BUY":
            self._positions[token_id] = current + size
        else:
            self._positions[token_id] = current - size

    # -- Diagnostics ----------------------------------------------------------

    def on_start(self) -> None:
        logger.info("BS Strategy started | edge=%.3f size=%.0f max_pos=%.0f vol=%s",
                    self._edge, self._size, self._max_pos,
                    self._vol_override or "per-instrument implied")

    def on_stop(self) -> None:
        logger.info("BS Strategy stopped")
        ivs = self._iv_tracker.all_ivs
        if ivs:
            logger.info("IV per instrument:")
            for tid, iv in sorted(ivs.items(), key=lambda x: x[1]):
                bp = self._pricing_cache.get(tid)
                if bp:
                    logger.info("  K=%7.0f iv=%.4f fair=%.4f %s %s...",
                               bp.strike, iv, bp.fair_value,
                               "CALL" if bp.is_call else "PUT", tid[:16])
