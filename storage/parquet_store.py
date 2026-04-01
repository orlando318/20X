"""Parquet-based historical data store.

Partitioned by coin/duration/date for efficient slicing during backtesting.

Directory layout:
    data_dir/
    ├── book_snapshots/
    │   └── BTC/daily/2026-04-01.parquet
    ├── trades/
    │   └── ETH/weekly/2026-03-30.parquet
    └── ticks/
        └── SOL/daily/2026-04-01.parquet
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from models.crypto_market import Coin, Duration

logger = logging.getLogger(__name__)


class ParquetStore:
    """Append-only parquet store for historical market data."""

    def __init__(self, data_dir: str = "data"):
        self._root = Path(data_dir)

    # -- Writing --------------------------------------------------------------

    def append_book_snapshots(
        self,
        coin: Coin,
        duration: Duration,
        records: list[dict],
        date: Optional[datetime] = None,
    ) -> Path:
        """Append order book snapshot records.

        Expected record keys:
            token_id, timestamp_ms, best_bid, best_ask, mid, spread,
            bid_depth, ask_depth, imbalance, n_bids, n_asks
        """
        return self._append("book_snapshots", coin, duration, records, date)

    def append_trades(
        self,
        coin: Coin,
        duration: Duration,
        records: list[dict],
        date: Optional[datetime] = None,
    ) -> Path:
        """Append trade records.

        Expected record keys:
            id, token_id, side, price, size, timestamp_ms, fee
        """
        return self._append("trades", coin, duration, records, date)

    def append_ticks(
        self,
        coin: Coin,
        duration: Duration,
        records: list[dict],
        date: Optional[datetime] = None,
    ) -> Path:
        """Append tick-level price records.

        Expected record keys:
            token_id, timestamp_ms, yes_price, no_price, label, strike
        """
        return self._append("ticks", coin, duration, records, date)

    # -- Reading --------------------------------------------------------------

    def read_book_snapshots(
        self,
        coin: Coin,
        duration: Duration,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> pd.DataFrame:
        return self._read("book_snapshots", coin, duration, start_date, end_date)

    def read_trades(
        self,
        coin: Coin,
        duration: Duration,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> pd.DataFrame:
        return self._read("trades", coin, duration, start_date, end_date)

    def read_ticks(
        self,
        coin: Coin,
        duration: Duration,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> pd.DataFrame:
        return self._read("ticks", coin, duration, start_date, end_date)

    # -- Internal -------------------------------------------------------------

    def _partition_path(
        self, category: str, coin: Coin, duration: Duration, date: datetime
    ) -> Path:
        return self._root / category / coin.value / duration.value / f"{date.strftime('%Y-%m-%d')}.parquet"

    def _partition_dir(self, category: str, coin: Coin, duration: Duration) -> Path:
        return self._root / category / coin.value / duration.value

    def _append(
        self,
        category: str,
        coin: Coin,
        duration: Duration,
        records: list[dict],
        date: Optional[datetime],
    ) -> Path:
        if not records:
            raise ValueError("No records to write")

        date = date or datetime.utcnow()
        path = self._partition_path(category, coin, duration, date)
        path.parent.mkdir(parents=True, exist_ok=True)

        new_table = pa.Table.from_pylist(records)

        if path.exists():
            existing = pq.read_table(path)
            # Deduplicate by timestamp_ms + token_id if both exist
            combined = pa.concat_tables([existing, new_table])
            df = combined.to_pandas()
            dedup_cols = [c for c in ["timestamp_ms", "token_id", "id"] if c in df.columns]
            if dedup_cols:
                df = df.drop_duplicates(subset=dedup_cols, keep="last")
            df = df.sort_values("timestamp_ms") if "timestamp_ms" in df.columns else df
            table = pa.Table.from_pandas(df, preserve_index=False)
        else:
            table = new_table

        pq.write_table(table, path, compression="snappy")
        logger.debug("Wrote %d rows to %s", table.num_rows, path)
        return path

    def _read(
        self,
        category: str,
        coin: Coin,
        duration: Duration,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> pd.DataFrame:
        partition_dir = self._partition_dir(category, coin, duration)

        if not partition_dir.exists():
            return pd.DataFrame()

        files = sorted(partition_dir.glob("*.parquet"))
        if not files:
            return pd.DataFrame()

        # Filter by date range from filenames
        if start_date or end_date:
            filtered = []
            for f in files:
                try:
                    file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                except ValueError:
                    continue
                if start_date and file_date.date() < start_date.date():
                    continue
                if end_date and file_date.date() > end_date.date():
                    continue
                filtered.append(f)
            files = filtered

        if not files:
            return pd.DataFrame()

        tables = [pq.read_table(f) for f in files]
        combined = pa.concat_tables(tables)
        df = combined.to_pandas()

        # Filter precise timestamps if needed
        if "timestamp_ms" in df.columns:
            if start_date:
                start_ms = int(start_date.timestamp() * 1000)
                df = df[df["timestamp_ms"] >= start_ms]
            if end_date:
                end_ms = int(end_date.timestamp() * 1000)
                df = df[df["timestamp_ms"] <= end_ms]
            df = df.sort_values("timestamp_ms").reset_index(drop=True)

        return df

    def list_available(self, category: str, coin: Optional[Coin] = None) -> list[str]:
        """List available date files for a category (and optionally coin)."""
        base = self._root / category
        if not base.exists():
            return []
        if coin:
            base = base / coin.value
        return sorted(str(f.relative_to(self._root)) for f in base.rglob("*.parquet"))
