"""Forward test runner — runs a strategy against live data with paper execution.

Wires together: LiveEngine + BinanceFeed + Strategy + PaperExecutor + LatencyTracker

Usage:
    from strategy import ForwardTest, Strategy, Signal

    class MyStrategy(Strategy):
        def on_book_update(self, token_id, inst, book):
            if book.mid and book.mid < 0.3:
                return Signal.buy(token_id, price=book.best_ask.price, size=10)

    test = ForwardTest.create(
        strategy=MyStrategy(),
        coin=Coin.BTC,
        duration=Duration.WEEKLY,
    )
    await test.run()  # blocks until Ctrl+C
"""

import asyncio
import logging
import signal
import time
from typing import Optional

from connectors.binance import BinanceFeed, BookTick, Trade as BinanceTrade
from live import LiveEngine
from models.crypto_market import Coin, Duration, Instrument
from models.orderbook import OrderBook
from monitoring import LatencyTracker
from strategy.base import Strategy, Signal
from strategy.paper_executor import PaperExecutor, Fill

logger = logging.getLogger(__name__)

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


class ForwardTest:
    """Runs a strategy against live market data with paper execution."""

    def __init__(
        self,
        strategy: Strategy,
        engine: LiveEngine,
        executor: PaperExecutor,
        binance: Optional[BinanceFeed] = None,
        tracker: Optional[LatencyTracker] = None,
        print_interval: int = 100,
    ):
        self._strategy = strategy
        self._engine = engine
        self._executor = executor
        self._binance = binance
        self._tracker = tracker
        self._print_interval = print_interval
        self._update_count = 0
        self._start_time = 0.0
        self._market = None  # set via set_market() to skip re-discovery

    @classmethod
    def create(
        cls,
        strategy: Strategy,
        coin: Coin,
        duration: Duration,
        timestamp: Optional[int] = None,
        hour_et: Optional[int] = None,
        with_binance: bool = True,
        with_profiler: bool = True,
        print_interval: int = 100,
        fee_rate: float = 0.015,
        env_file: Optional[str] = None,
    ) -> "ForwardTest":
        """Convenience factory that wires everything together."""
        engine = LiveEngine.from_env(env_file)
        executor = PaperExecutor(fee_rate=fee_rate)
        tracker = LatencyTracker() if with_profiler else None
        binance = None

        if with_binance:
            pair = COIN_TO_PAIR.get(coin)
            if pair:
                binance = BinanceFeed()
                binance.add_pair(pair)

        # Store discovery params for start()
        test = cls(strategy, engine, executor, binance, tracker, print_interval)
        test._coin = coin
        test._duration = duration
        test._timestamp = timestamp
        test._hour_et = hour_et
        return test

    # -- Properties -----------------------------------------------------------

    def set_market(self, market) -> None:
        """Set a pre-discovered market to avoid re-discovery on run()."""
        self._market = market

    @property
    def executor(self) -> PaperExecutor:
        return self._executor

    @property
    def engine(self) -> LiveEngine:
        return self._engine

    @property
    def binance(self) -> Optional[BinanceFeed]:
        return self._binance

    # -- Run ------------------------------------------------------------------

    async def run(self) -> None:
        """Start all feeds, run strategy, block until Ctrl+C, then print results."""
        # Attach profiler
        if self._tracker:
            self._tracker.attach_engine(self._engine)
            if self._binance:
                self._tracker.attach_binance(self._binance)

        # Wire callbacks
        self._engine.on_book_update(self._on_poly_book)
        self._engine.on_trade(self._on_poly_trade)

        if self._binance:
            self._binance.on_book_ticker(self._on_binance_tick)
            self._binance.on_trade(self._on_binance_trade)

        # Notify strategy of fills
        self._executor.on_fill(self._on_fill)

        # Start feeds — use pre-discovered market if available, otherwise discover
        if self._market:
            await self._engine.start_with_market(self._market)
        else:
            await self._engine.start(
                self._coin, self._duration,
                timestamp=self._timestamp, hour_et=self._hour_et,
            )
        if self._binance:
            await self._binance.start()

        self._strategy.on_start()
        self._start_time = time.time()
        logger.info("Forward test started")

        # Wait for shutdown with periodic PnL printing
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        async def pnl_printer():
            while not stop.is_set():
                await asyncio.sleep(1)
                books = self._engine.get_all_books()
                pnl = self._executor.total_pnl(books)
                positions = self._executor.positions
                if positions:
                    parts = []
                    for tid, pos in positions.items():
                        book = books.get(tid)
                        mark = book.mid if book and book.mid else pos.avg_entry
                        upnl = pos.unrealized_pnl(mark)
                        if pos.size != 0:
                            parts.append(f"{tid[:8]}={pos.size:+.0f}@{pos.avg_entry:.4f}({upnl:+.4f})")
                    logger.info("PNL total=%.4f | %s", pnl, " ".join(parts))

        pnl_task = asyncio.create_task(pnl_printer())
        await stop.wait()
        pnl_task.cancel()
        try:
            await pnl_task
        except asyncio.CancelledError:
            pass

        # Shutdown
        self._strategy.on_stop()
        if self._binance:
            await self._binance.stop()
        await self._engine.stop()

        # Print results
        self._print_results()

    # -- Callbacks ------------------------------------------------------------

    def _on_poly_book(self, token_id: str, inst: Instrument, book: OrderBook) -> None:
        self._update_count += 1

        # Check for paper fills
        self._executor.check_fills(token_id, book)

        # Run strategy
        signals = self._strategy.on_book_update(token_id, inst, book)
        self._process_signals(signals)

        # Periodic status
        if self._update_count % self._print_interval == 0:
            self._log_status(inst, book)

    def _on_poly_trade(self, token_id: str, inst: Instrument, trade: dict) -> None:
        signals = self._strategy.on_polymarket_trade(token_id, inst, trade)
        self._process_signals(signals)

    def _on_binance_tick(self, tick: BookTick) -> None:
        signals = self._strategy.on_binance_tick(tick)
        self._process_signals(signals)

    def _on_binance_trade(self, trade: BinanceTrade) -> None:
        signals = self._strategy.on_binance_trade(trade)
        self._process_signals(signals)

    def _on_fill(self, fill: Fill) -> None:
        self._strategy.on_fill(fill.token_id, fill.price, fill.size, fill.side)

    # -- Signal processing ----------------------------------------------------

    def _process_signals(self, signals: Optional[Signal | list[Signal]]) -> None:
        if signals is None:
            return
        if isinstance(signals, Signal):
            signals = [signals]
        for sig in signals:
            self._executor.submit(sig)

    # -- Logging --------------------------------------------------------------

    def _log_status(self, inst: Instrument, book: OrderBook) -> None:
        bb = book.best_bid
        ba = book.best_ask
        pnl = self._executor.total_pnl(self._engine.get_all_books())
        binance_mid = ""
        if self._binance:
            pair = COIN_TO_PAIR.get(self._coin, "")
            mid = self._binance.get_mid(pair)
            if mid:
                binance_mid = f" | {pair}={mid}"

        logger.info(
            "[%d] %s | mid=%s spread=%s | orders=%d fills=%d pnl=%.4f%s",
            self._update_count,
            (inst.label if inst.label else ""),
            book.mid, book.spread,
            len(self._executor.open_orders), len(self._executor.fills), pnl,
            binance_mid,
        )

    def _print_results(self) -> None:
        elapsed = time.time() - self._start_time
        print(f"\n{'='*60}")
        print(f"Forward Test Results ({elapsed:.1f}s, {self._update_count} updates)")
        print(f"{'='*60}")

        self._executor.print_summary(self._engine.get_all_books())

        if self._tracker:
            print()
            self._tracker.print_summary()

        print(f"{'='*60}")
