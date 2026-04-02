"""Latency profiler for Polymarket and Binance feeds.

Tracks per-source:
    1. Network latency    — server timestamp to local receipt
    2. Processing latency — time to process a message
    3. End-to-end latency — server timestamp to after callback

Usage:
    from monitoring import LatencyTracker

    tracker = LatencyTracker()
    tracker.attach_engine(engine)     # Polymarket live engine
    tracker.attach_binance(feed)      # Binance feed

    tracker.print_summary()
    tracker.reset()
"""

import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LatencyStats:
    count: int = 0
    min_us: float = 0.0
    max_us: float = 0.0
    mean_us: float = 0.0
    p50_us: float = 0.0
    p95_us: float = 0.0
    p99_us: float = 0.0

    def __str__(self) -> str:
        if self.count == 0:
            return "no samples"
        return (
            f"n={self.count} min={self.min_us:.0f}µs mean={self.mean_us:.0f}µs "
            f"p50={self.p50_us:.0f}µs p95={self.p95_us:.0f}µs p99={self.p99_us:.0f}µs "
            f"max={self.max_us:.0f}µs"
        )


def _compute_stats(samples: list[float]) -> LatencyStats:
    if not samples:
        return LatencyStats()
    s = sorted(samples)
    n = len(s)
    return LatencyStats(
        count=n,
        min_us=s[0],
        max_us=s[-1],
        mean_us=sum(s) / n,
        p50_us=s[n // 2],
        p95_us=s[int(n * 0.95)],
        p99_us=s[int(n * 0.99)],
    )


class _SourceMetrics:
    """Latency metrics for a single data source."""

    def __init__(self, name: str, window: int):
        self.name = name
        self.network: deque[float] = deque(maxlen=window)
        self.processing: deque[float] = deque(maxlen=window)
        self.e2e: deque[float] = deque(maxlen=window)
        self.msg_count = 0

    def record_network(self, server_ms: float, local_ms: float) -> None:
        latency_us = (local_ms - server_ms) * 1000
        if latency_us >= 0:
            self.network.append(latency_us)

    def record_processing(self, duration_ns: int) -> None:
        self.processing.append(duration_ns / 1000)

    def record_e2e(self, server_ms: float) -> None:
        latency_us = (time.time() * 1000 - server_ms) * 1000
        if latency_us >= 0:
            self.e2e.append(latency_us)

    def summary_lines(self) -> list[str]:
        return [
            f"  [{self.name}] ({self.msg_count} messages)",
            f"    Network (server→local):  {_compute_stats(list(self.network))}",
            f"    Processing:              {_compute_stats(list(self.processing))}",
            f"    End-to-end:              {_compute_stats(list(self.e2e))}",
        ]

    def reset(self) -> None:
        self.network.clear()
        self.processing.clear()
        self.e2e.clear()
        self.msg_count = 0


class LatencyTracker:
    """Tracks latency for Polymarket and Binance feeds."""

    def __init__(self, window_size: int = 10000):
        self._window = window_size
        self._poly = _SourceMetrics("Polymarket", window_size)
        self._binance = _SourceMetrics("Binance", window_size)
        self._rest = deque(maxlen=window_size)

    def attach_engine(self, engine) -> None:
        """Instrument a Polymarket LiveEngine."""
        original_process = engine._process_message

        def instrumented(msg: dict) -> None:
            self._poly.msg_count += 1
            now_ms = time.time() * 1000
            server_ts = int(msg.get("timestamp", 0) or 0)

            if server_ts > 0:
                self._poly.record_network(server_ts, now_ms)

            t0 = time.perf_counter_ns()
            original_process(msg)
            t1 = time.perf_counter_ns()
            self._poly.record_processing(t1 - t0)

            if server_ts > 0:
                self._poly.record_e2e(server_ts)

        engine._process_message = instrumented

        # REST snapshots
        original_fetch = engine._fetch_snapshot

        async def instrumented_fetch(yes_token_id: str, tick_size: float = 0.01) -> None:
            t0 = time.perf_counter_ns()
            await original_fetch(yes_token_id, tick_size)
            t1 = time.perf_counter_ns()
            self._rest.append((t1 - t0) / 1000)

        engine._fetch_snapshot = instrumented_fetch

    def attach_binance(self, feed) -> None:
        """Instrument a BinanceFeed."""
        original_process = feed._process

        def instrumented(data: dict, stream_name: str = "") -> None:
            self._binance.msg_count += 1
            now_ms = time.time() * 1000

            # Binance trade has "T" (trade time), bookTicker may have "T" or "E"
            server_ts = int(data.get("T", data.get("E", 0)) or 0)
            if server_ts > 0:
                self._binance.record_network(server_ts, now_ms)

            t0 = time.perf_counter_ns()
            original_process(data, stream_name)
            t1 = time.perf_counter_ns()
            self._binance.record_processing(t1 - t0)

            if server_ts > 0:
                self._binance.record_e2e(server_ts)

        feed._process = instrumented

    def summary(self) -> str:
        lines = ["Latency Report"]
        lines.extend(self._poly.summary_lines())
        lines.extend(self._binance.summary_lines())
        rest_stats = _compute_stats(list(self._rest))
        lines.append(f"  [REST snapshots] {rest_stats}")
        return "\n".join(lines)

    def print_summary(self) -> None:
        print(self.summary())

    def reset(self) -> None:
        self._poly.reset()
        self._binance.reset()
        self._rest.clear()
