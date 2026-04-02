# 20X — Polymarket Trading Infrastructure

Modular infrastructure for collecting, storing, and analyzing Polymarket crypto prediction market data.

## Project Structure

```
poly/
├── config/              # Settings & secrets management
│   └── settings.py      # Pydantic-based config with dev/paper/live profiles
├── models/              # Shared data models
│   ├── market.py        # Base Market + MarketOutcome
│   ├── crypto_market.py # CryptoMarket, Instrument, Coin, Duration
│   ├── orderbook.py     # OrderBook, PriceLevel, Side
│   ├── order.py         # Order, OrderStatus, OrderType
│   ├── trade.py         # Trade
│   └── position.py      # Position with P&L tracking
├── markets/             # Market discovery
│   └── selector.py      # Slug-based market finder via Gamma API
├── orderbook/           # Live order book management
│   └── manager.py       # WS-fed book with snapshot/delta/trade handling
├── storage/             # Data persistence
│   └── parquet_store.py # Raw message storage in parquet
├── analysis/            # Offline analysis tools
│   └── replay.py        # Reconstruct order books from raw messages
├── clob_client.py       # REST + WebSocket clients for CLOB API
├── collect.py           # Data collection script
├── test_connection.py   # Smoke test for API connectivity
├── .env.example         # Environment variable template
└── .gitignore
```

## Setup

### Install dependencies

```bash
pip install pydantic-settings aiohttp websockets pyarrow pandas
```

### Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

Key variables:
- `POLY_MODE` — `dev`, `paper`, or `live`
- `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` — CLOB API credentials
- `POLY_PRIVATE_KEY`, `POLY_WALLET_ADDRESS` — Polygon wallet (for live execution)

### Verify connectivity

```bash
python test_connection.py
```

This discovers BTC/ETH/SOL daily and weekly markets, prints instrument details, and fetches a live order book.

---

## Collecting Data

`collect.py` connects to the Polymarket WebSocket, subscribes to a market's instruments, and records every raw message to parquet.

### Usage

```bash
# Daily markets
python collect.py --coin BTC --duration daily
python collect.py --coin ETH --duration daily

# Weekly markets (multi-instrument with strike prices)
python collect.py --coin BTC --duration weekly

# Hourly
python collect.py --coin BTC --duration 1h --hour-et 14

# 4-hour (requires end timestamp in unix seconds)
python collect.py --coin BTC --duration 4h --timestamp 1775059200

# 5-minute / 15-minute (requires end timestamp)
python collect.py --coin BTC --duration 5min --timestamp 1775055900
python collect.py --coin ETH --duration 15min --timestamp 1775057400

# Monthly / Yearly
python collect.py --coin BTC --duration monthly
python collect.py --coin BTC --duration yearly
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--coin` | required | Coin ticker: BTC, ETH, SOL, DOGE, XRP, MATIC, AVAX, LINK, ADA, DOT |
| `--duration` | required | Market duration: 5min, 15min, 1h, 4h, daily, weekly, monthly, yearly |
| `--timestamp` | — | End timestamp for 5m/15m/4h markets (unix seconds) |
| `--hour-et` | current hour | Hour in Eastern Time for 1h markets (0-23) |
| `--data-dir` | `data` | Output directory |
| `--flush-every` | 500 | Flush to disk every N messages |
| `--env-file` | — | Path to .env file |

### Output

Raw messages are saved to `data/raw/{market-slug}.parquet` with columns:

| Column | Type | Description |
|--------|------|-------------|
| `received_at_ms` | int64 | Local receipt timestamp (ms) |
| `event_type` | string | `book`, `price_change`, or `last_trade_price` |
| `timestamp_ms` | int64 | Server timestamp from message |
| `raw` | string | Full JSON message |

### Stopping

Press `Ctrl+C` for a clean shutdown. Remaining buffered messages are flushed before exit. The WebSocket auto-reconnects on disconnection.

---

## Analyzing Data

The `analysis` module replays raw messages to reconstruct order books.

### As a Python library

```python
from analysis import load_raw, replay_to_books, replay_to_dataframe

# Load raw messages
df = load_raw("what-price-will-bitcoin-hit-march-30-april-5")

# Get final order book state per token
books = replay_to_books(df)
for token_id, book in books.items():
    print(f"{token_id[:16]}  mid={book.mid}  spread={book.spread}")
    print(f"  best bid: {book.best_bid.price} x {book.best_bid.size}")
    print(f"  best ask: {book.best_ask.price} x {book.best_ask.size}")

# Get full book history as a DataFrame (one row per update)
book_df = replay_to_dataframe(df, levels=20)

# Filter to a single token by prefix
book_df = replay_to_dataframe(df, levels=10, token_filter="4622")
```

### As a CLI script

```bash
# Print summary
python -m analysis.replay --slug what-price-will-bitcoin-hit-march-30-april-5

# Export to CSV with top 10 levels
python -m analysis.replay --slug ... --levels 10 --output book.csv

# Filter to one token
python -m analysis.replay --slug ... --token 4622
```

### Functions

**`load_raw(slug, data_dir="data")`**
Load raw WS messages from parquet. Returns a DataFrame.

**`replay_to_books(df, token_filter=None)`**
Replay all messages and return `dict[token_id, OrderBook]` with the final book state.

**`replay_to_dataframe(df, levels=20, token_filter=None)`**
Replay all messages and return a DataFrame with the book state captured after every update. Each row includes:
- `token_id`, `received_at_ms`, `timestamp_ms`, `event_type`
- `mid`, `spread`, `n_bids`, `n_asks`
- `bid_price_0` through `bid_price_{N-1}` + `bid_size_0` through `bid_size_{N-1}`
- `ask_price_0` through `ask_price_{N-1}` + `ask_size_0` through `ask_size_{N-1}`

---

## Market Discovery

The selector constructs URL slugs directly and fetches market data from the Gamma API — no pagination through thousands of markets.

```python
from config import load_settings
from markets.selector import CryptoMarketSelector
from models.crypto_market import Coin, Duration

settings = load_settings()
selector = CryptoMarketSelector(settings)

# Fetch by duration
daily = await selector.fetch_daily([Coin.BTC, Coin.ETH, Coin.SOL])
weekly = await selector.fetch_weekly([Coin.BTC])
monthly = await selector.fetch_monthly([Coin.BTC])
yearly = await selector.fetch_yearly([Coin.BTC])
hourly = await selector.fetch_1h([Coin.BTC], hour_et=14)

# Fetch all active (daily + weekly + monthly)
all_markets = await selector.fetch_all_active()

# Filter
btc_weekly = selector.filter_by_coin_and_duration(Coin.BTC, Duration.WEEKLY)
top_liquid = selector.by_liquidity(min_liquidity=10_000)
expiring = selector.expiring_within_hours(6)

await selector.close()
```

### Slug patterns by duration

| Duration | Slug Example | API Endpoint |
|----------|-------------|--------------|
| 5min | `btc-updown-5m-1775055900` | market |
| 15min | `eth-updown-15m-1775057400` | market |
| 1h | `bitcoin-up-or-down-april-1-2026-11am-et` | market |
| 4h | `btc-updown-4h-1775059200` | market |
| daily | `bitcoin-up-or-down-on-april-1-2026` | market |
| weekly | `what-price-will-bitcoin-hit-march-30-april-5` | event |
| monthly | `what-price-will-bitcoin-hit-in-april` | event |
| yearly | `what-price-will-bitcoin-hit-before-2027` | event |

---

## Data Models

### CryptoMarket

A market contains one or more instruments. Daily up/down markets have 1 instrument. Weekly/monthly/yearly markets have many (one per strike price).

```python
market.coin           # Coin.BTC
market.duration       # Duration.WEEKLY
market.slug           # "what-price-will-bitcoin-hit-march-30-april-5"
market.question       # "What price will Bitcoin hit March 30-April 5?"
market.instruments    # [Instrument, Instrument, ...]
market.all_token_ids  # every token across all instruments
market.strikes        # sorted strike prices [54000, 56000, ...]
market.hours_remaining
market.is_expired
```

### Instrument

A single tradeable outcome within a market.

```python
inst.condition_id    # needed for order submission
inst.yes_token_id    # token for YES side
inst.no_token_id     # token for NO side
inst.label           # "Will Bitcoin reach $82,000 March 30-April 5?"
inst.target_price    # 82000.0 (strike)
inst.yes_price       # current YES price
inst.min_tick_size   # minimum price increment
inst.min_order_size  # minimum order quantity
inst.market_slug     # slug for API lookups
```

### OrderBook

```python
book.best_bid        # PriceLevel(price, size)
book.best_ask
book.mid             # midpoint price
book.spread          # absolute spread
book.spread_bps      # spread in basis points
book.bid_depth(5)    # total notional in top 5 bid levels
book.ask_depth(5)
book.imbalance(5)    # [-1, 1] bid/ask imbalance
book.bids            # list[PriceLevel] sorted best-first
book.asks            # list[PriceLevel] sorted best-first
```

---

## Configuration Profiles

Settings are loaded from environment variables with profile-specific defaults.

| Setting | Dev | Paper | Live |
|---------|-----|-------|------|
| Max position USD | $0 | $1,000 | $10,000 |
| Kill switch loss | $0 | $200 | $2,000 |
| Max open orders | 0 | 20 | 50 |
| Log level | DEBUG | INFO | WARNING |

```python
from config import load_settings

settings = load_settings(".env")
settings.mode          # ExecutionMode.DEV
settings.api.api_key   # from POLY_API_KEY env var
settings.risk.max_position_usd  # profile-dependent
```

---

## WebSocket Message Types

The CLOB WebSocket sends three message types that affect the order book:

| Type | When | Effect on Book |
|------|------|----------------|
| `book` | Full snapshot (on subscribe) | Replaces entire book |
| `price_change` | Order placed or cancelled | Updates individual price level |
| `last_trade_price` | Trade executed | Reduces liquidity at traded price |

All three are captured by `collect.py` and replayed by `analysis.replay`.

---

## Architecture Decisions

- **Slug-based discovery** over API pagination — deterministic, fast, no wasted requests
- **Gamma API for discovery, CLOB API for trading** — right tool for each job
- **Raw message storage** over pre-processed books — no data loss, replayable, flexible
- **Parquet with snappy compression** — fast append, columnar reads, ~3-4x compression
- **Files named by market slug** — self-documenting, no date/coin/duration hierarchy
