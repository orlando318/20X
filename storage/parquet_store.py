"""Parquet-based historical data store.

Files named by market slug for direct identification.

Directory layout:
    data_dir/
    └── raw/
        └── what-price-will-bitcoin-hit-march-30-april-5.parquet
"""

import logging
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


class ParquetStore:
    """Append-only parquet store for raw WS messages."""

    def __init__(self, data_dir: str = "data"):
        self._root = Path(data_dir)

    def append_raw(self, slug: str, records: list[dict]) -> Path:
        """Append raw WS message records.

        Expected keys: received_at_ms, event_type, timestamp_ms, raw
        """
        if not records:
            raise ValueError("No records to write")

        path = self._root / "raw" / f"{slug}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)

        new_table = pa.Table.from_pylist(records)

        if path.exists():
            existing = pq.read_table(path)
            table = pa.concat_tables([existing, new_table])
        else:
            table = new_table

        pq.write_table(table, path, compression="snappy")
        logger.debug("Wrote %d rows to %s", table.num_rows, path)
        return path

    def read_raw(self, slug: str) -> pd.DataFrame:
        """Read raw messages for a market."""
        path = self._root / "raw" / f"{slug}.parquet"
        if not path.exists():
            return pd.DataFrame()
        df = pq.read_table(path).to_pandas()
        if "received_at_ms" in df.columns:
            df = df.sort_values("received_at_ms").reset_index(drop=True)
        return df

    def list_available(self) -> list[str]:
        """List available market slugs."""
        base = self._root / "raw"
        if not base.exists():
            return []
        return sorted(f.stem for f in base.glob("*.parquet"))
