"""Latency profiler for the live engine.

Tracks three latency components:
    1. Network latency    — time from server timestamp to local receipt
    2. Processing latency — time to process a message and update the book
    3. End-to-end latency — server timestamp to book update callback fired

Also tracks WS ping/pong RTT separately.

Usage:
    from monitoring import LatencyTracker

    tracker = LatencyTracker()
    tracker.attach(engine)   # hooks into the live engine

    # Later
    tracker.summary()
    tracker.reset()
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LatencyStats:
    """Computed stats from a latency sample."""
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


class LatencyTracker:
    """Tracks latency across the live engine pipeline.

    Stores the last `window_size` samples per metric for rolling stats.
    """

    def __init__(self, window_size: int = 10000):
        self._window = window_size
        # Network: server_ts → local receipt (ms converted to µs)
        self._network: deque[float] = deque(maxlen=window_size)
        # Processing: time spent in _process_message (µs)
        self._processing: deque[float] = deque(maxlen=window_size)
        # End-to-end: server_ts → after book callback (µs)
        self._e2e: deque[float] = deque(maxlen=window_size)
        # REST snapshot fetch time (µs)
        self._rest: deque[float] = deque(maxlen=window_size)

        self._msg_count = 0
        self._engine = None

    def attach(self, engine) -> None:
        """Attach to a LiveEngine by monkey-patching its _process_message."""
        self._engine = engine
        original_process = engine._process_message

        def instrumented_process(msg: dict) -> None:
            self._msg_count += 1
            now_ms = time.time() * 1000
            server_ts = int(msg.get("timestamp", 0) or 0)

            # Network latency: server → local receipt
            if server_ts > 0:
                network_us = (now_ms - server_ts) * 1000
                if network_us >= 0:  # ignore clock skew negatives
                    self._network.append(network_us)

            # Processing latency
            t0 = time.perf_counter_ns()
            original_process(msg)
            t1 = time.perf_counter_ns()
            proc_us = (t1 - t0) / 1000
            self._processing.append(proc_us)

            # End-to-end: server → after processing
            if server_ts > 0:
                e2e_us = (time.time() * 1000 - server_ts) * 1000
                if e2e_us >= 0:
                    self._e2e.append(e2e_us)

        engine._process_message = instrumented_process

        # Also instrument REST snapshot fetches
        original_fetch = engine._fetch_snapshot

        async def instrumented_fetch(yes_token_id: str, tick_size: float = 0.01) -> None:
            t0 = time.perf_counter_ns()
            await original_fetch(yes_token_id, tick_size)
            t1 = time.perf_counter_ns()
            self._rest.append((t1 - t0) / 1000)

        engine._fetch_snapshot = instrumented_fetch

    @property
    def network(self) -> LatencyStats:
        return _compute_stats(list(self._network))

    @property
    def processing(self) -> LatencyStats:
        return _compute_stats(list(self._processing))

    @property
    def e2e(self) -> LatencyStats:
        return _compute_stats(list(self._e2e))

    @property
    def rest(self) -> LatencyStats:
        return _compute_stats(list(self._rest))

    def summary(self) -> str:
        lines = [
            f"Latency Report ({self._msg_count} messages)",
            f"  Network (server→local):  {self.network}",
            f"  Processing (msg→book):   {self.processing}",
            f"  End-to-end (server→cb):  {self.e2e}",
            f"  REST snapshots:          {self.rest}",
        ]
        return "\n".join(lines)

    def print_summary(self) -> None:
        print(self.summary())

    def reset(self) -> None:
        self._network.clear()
        self._processing.clear()
        self._e2e.clear()
        self._rest.clear()
        self._msg_count = 0
