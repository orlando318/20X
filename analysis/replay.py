"""Replay raw WS messages to reconstruct order books.

As a library:
    from analysis import load_raw, replay_to_books, replay_to_dataframe

    df = load_raw("what-price-will-bitcoin-hit-march-30-april-5")
    books = replay_to_books(df)
    book_df = replay_to_dataframe(df, levels=20)

As a script:
    python -m analysis.replay --slug what-price-will-bitcoin-hit-march-30-april-5
    python -m analysis.replay --slug ... --levels 10 --output book.csv
"""

import argparse
import json
import logging
import sys
from typing import Optional

import pandas as pd

from models.orderbook import OrderBook, Side
from storage.parquet_store import ParquetStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_raw(slug: str, data_dir: str = "data") -> pd.DataFrame:
    """Load raw WS messages for a market slug."""
    store = ParquetStore(data_dir)
    df = store.read_raw(slug)
    if df.empty:
        available = store.list_available()
        raise ValueError(f"No data for '{slug}'. Available: {available}")
    return df


def replay_to_books(
    df: pd.DataFrame,
    token_filter: Optional[str] = None,
) -> dict[str, OrderBook]:
    """Replay all messages and return final book state per token.

    Args:
        df: Raw messages from load_raw().
        token_filter: Only process tokens starting with this prefix.

    Returns:
        Dict of token_id -> OrderBook with final state.
    """
    books: dict[str, OrderBook] = {}

    for _, row in df.iterrows():
        msg = json.loads(row["raw"])
        _apply_message(books, msg, token_filter)

    return books


def replay_to_dataframe(
    df: pd.DataFrame,
    levels: int = 20,
    token_filter: Optional[str] = None,
) -> pd.DataFrame:
    """Replay all messages and capture book state after every update.

    Args:
        df: Raw messages from load_raw().
        levels: Number of price levels per side to include.
        token_filter: Only process tokens starting with this prefix.

    Returns:
        DataFrame with one row per book update, including top N levels.
    """
    books: dict[str, OrderBook] = {}
    snapshots: list[dict] = []

    for _, row in df.iterrows():
        msg = json.loads(row["raw"])
        received = row["received_at_ms"]
        event_type = msg.get("event_type", "")

        updated_tokens = _apply_message(books, msg, token_filter)

        for token_id in updated_tokens:
            snapshots.append(_capture(books[token_id], received, event_type, levels))

    return pd.DataFrame(snapshots)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _parse_book_levels(raw_levels: list) -> list[list[float]]:
    result = []
    for level in raw_levels:
        if isinstance(level, dict):
            result.append([float(level["price"]), float(level["size"])])
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            result.append([float(level[0]), float(level[1])])
    return result


def _apply_message(
    books: dict[str, OrderBook],
    msg: dict,
    token_filter: Optional[str],
) -> list[str]:
    """Apply a single WS message to book state. Returns list of updated token_ids."""
    event_type = msg.get("event_type", "")
    updated: list[str] = []

    if event_type == "book":
        token_id = msg.get("asset_id", "")
        if not token_id:
            return []
        if token_filter and not token_id.startswith(token_filter):
            return []

        bids = _parse_book_levels(msg.get("bids", []))
        asks = _parse_book_levels(msg.get("asks", []))
        ts = int(msg.get("timestamp", 0) or 0)

        book = books.get(token_id) or OrderBook(token_id=token_id)
        book.apply_snapshot(bids, asks, sequence=ts, timestamp_ms=ts)
        books[token_id] = book
        updated.append(token_id)

    elif event_type == "price_change":
        ts = int(msg.get("timestamp", 0) or 0)
        for change in msg.get("price_changes", []):
            if not isinstance(change, dict):
                continue
            token_id = change.get("asset_id", "")
            if not token_id:
                continue
            if token_filter and not token_id.startswith(token_filter):
                continue

            book = books.get(token_id)
            if book is None:
                book = OrderBook(token_id=token_id)
                books[token_id] = book

            price = float(change.get("price", 0))
            size = float(change.get("size", 0))
            raw_side = change.get("side", "").upper()
            side = Side.BID if raw_side == "BUY" else Side.ASK if raw_side == "SELL" else None
            if side is None:
                continue

            book.apply_delta(side, price, size, sequence=ts, timestamp_ms=ts)
            updated.append(token_id)

    elif event_type == "last_trade_price":
        token_id = msg.get("asset_id", "")
        if not token_id:
            return []
        if token_filter and not token_id.startswith(token_filter):
            return []

        book = books.get(token_id)
        if book is None:
            return []

        trade_price = float(msg.get("price", 0))
        trade_size = float(msg.get("size", 0))
        raw_side = msg.get("side", "").upper()
        ts = int(msg.get("timestamp", 0) or 0)

        if raw_side == "BUY":
            target_levels = book.asks
        elif raw_side == "SELL":
            target_levels = book.bids
        else:
            return []

        for level in target_levels:
            if level.price == trade_price:
                if trade_size >= level.size:
                    target_levels.remove(level)
                else:
                    level.size = round(level.size - trade_size, 8)
                break

        book.timestamp_ms = ts
        updated.append(token_id)

    return updated


def _capture(book: OrderBook, received_ms: int, event_type: str, levels: int) -> dict:
    record: dict = {
        "token_id": book.token_id,
        "received_at_ms": received_ms,
        "timestamp_ms": book.timestamp_ms,
        "event_type": event_type,
        "mid": book.mid,
        "spread": book.spread,
        "n_bids": len(book.bids),
        "n_asks": len(book.asks),
    }

    for i in range(levels):
        if i < len(book.bids):
            record[f"bid_price_{i}"] = book.bids[i].price
            record[f"bid_size_{i}"] = book.bids[i].size
        else:
            record[f"bid_price_{i}"] = None
            record[f"bid_size_{i}"] = None

    for i in range(levels):
        if i < len(book.asks):
            record[f"ask_price_{i}"] = book.asks[i].price
            record[f"ask_size_{i}"] = book.asks[i].size
        else:
            record[f"ask_price_{i}"] = None
            record[f"ask_size_{i}"] = None

    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Replay raw messages into order book snapshots")
    parser.add_argument("--slug", type=str, required=True)
    parser.add_argument("--data-dir", type=str, default="data")
    parser.add_argument("--levels", type=int, default=20)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--token", type=str, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    df = load_raw(args.slug, args.data_dir)
    logger.info("Loaded %d raw messages", len(df))
    logger.info("Event types: %s", df["event_type"].value_counts().to_dict())

    result = replay_to_dataframe(df, levels=args.levels, token_filter=args.token)
    logger.info("Generated %d book snapshots", len(result))

    if args.output:
        result.to_csv(args.output, index=False)
        logger.info("Saved to %s", args.output)
    else:
        tokens = result["token_id"].unique()
        logger.info("Tokens: %d", len(tokens))
        for tid in tokens[:5]:
            sub = result[result["token_id"] == tid]
            logger.info("  %s...: %d updates, mid range [%s - %s]",
                        tid[:16], len(sub), sub["mid"].min(), sub["mid"].max())


if __name__ == "__main__":
    main()
