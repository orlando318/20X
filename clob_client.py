"""Polymarket CLOB API client with error handling, reconnection, and rate limiting."""

import asyncio
import enum
import json
import logging
import time
from typing import Any, Callable, Optional

import aiohttp
import websockets

from config.settings import Settings, load_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class CLOBBaseError(Exception):
    """Base error for all CLOB client errors."""


class CLOBConnectionError(CLOBBaseError):
    """Network issues, auth failures, connection drops."""


class CLOBDataError(CLOBBaseError):
    """Malformed messages, unexpected schemas, sequence gaps."""


class CLOBRateLimitError(CLOBConnectionError):
    """Rate limit hit — caller should back off."""

    def __init__(self, retry_after: float = 1.0):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s")


class CLOBConfigError(CLOBBaseError):
    """Missing or invalid configuration."""


# ---------------------------------------------------------------------------
# Connection state machine
# ---------------------------------------------------------------------------

class ConnState(enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    SYNCING = "SYNCING"
    READY = "READY"


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------

class CLOBRestClient:
    """Async REST client for Polymarket CLOB API."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._api = settings.api
        self._conn = settings.connection
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_times: list[float] = []

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._api.api_key:
                headers["POLY_API_KEY"] = self._api.api_key
            if self._api.api_secret:
                headers["POLY_API_SECRET"] = self._api.api_secret
            if self._api.api_passphrase:
                headers["POLY_PASSPHRASE"] = self._api.api_passphrase
            timeout = aiohttp.ClientTimeout(total=self._conn.request_timeout)
            self._session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        return self._session

    async def _throttle(self) -> None:
        now = time.monotonic()
        window = 1.0
        self._request_times = [t for t in self._request_times if now - t < window]
        if len(self._request_times) >= self._conn.rate_limit_per_second:
            sleep_for = window - (now - self._request_times[0])
            if sleep_for > 0:
                logger.debug("Throttling for %.3fs", sleep_for)
                await asyncio.sleep(sleep_for)
        self._request_times.append(time.monotonic())

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> Any:
        """Send an HTTP request with retries and exponential backoff."""
        url = f"{self._api.clob_rest_url}{path}"
        last_err: Optional[Exception] = None

        for attempt in range(1, self._conn.max_retries + 1):
            await self._throttle()
            session = await self._ensure_session()
            try:
                logger.debug("REQ %s %s attempt=%d", method, path, attempt)
                async with session.request(method, url, params=params, json=body) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", self._conn.base_backoff * attempt))
                        logger.warning("Rate limited on %s %s, retry after %.1fs", method, path, retry_after)
                        await asyncio.sleep(retry_after)
                        last_err = CLOBRateLimitError(retry_after)
                        continue

                    if resp.status >= 500:
                        text = await resp.text()
                        logger.warning("Server error %d on %s %s: %s", resp.status, method, path, text[:200])
                        last_err = CLOBConnectionError(f"HTTP {resp.status}: {text[:200]}")
                        await asyncio.sleep(min(self._conn.base_backoff * (2 ** (attempt - 1)), self._conn.max_backoff))
                        continue

                    if resp.status >= 400:
                        text = await resp.text()
                        raise CLOBConnectionError(f"HTTP {resp.status}: {text[:500]}")

                    data = await resp.json()
                    return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                backoff = min(self._conn.base_backoff * (2 ** (attempt - 1)), self._conn.max_backoff)
                logger.warning("Network error on %s %s (attempt %d/%d): %s — retrying in %.1fs",
                               method, path, attempt, self._conn.max_retries, exc, backoff)
                last_err = CLOBConnectionError(str(exc))
                await asyncio.sleep(backoff)

        raise CLOBConnectionError(f"All {self._conn.max_retries} retries exhausted for {method} {path}") from last_err

    # -- convenience helpers --------------------------------------------------

    async def get_markets(self, tag: Optional[str] = None, active: bool = True, max_pages: int = 20) -> list[dict]:
        """Fetch markets with cursor-based pagination.

        Args:
            tag: Filter by tag (e.g. "crypto").
            active: Only return active markets.
            max_pages: Safety limit on pages to fetch.
        """
        all_markets: list[dict] = []
        cursor = None

        for _ in range(max_pages):
            params: dict[str, str] = {}
            if tag:
                params["tag"] = tag
            if active:
                params["active"] = "true"
            if cursor:
                params["next_cursor"] = cursor

            resp = await self.request("GET", "/markets", params=params)

            if isinstance(resp, dict):
                data = resp.get("data") or []
                all_markets.extend(d for d in data if isinstance(d, dict))
                cursor = resp.get("next_cursor")
                if not cursor or not data:
                    break
            elif isinstance(resp, list):
                all_markets.extend(resp)
                break
            else:
                break

        return all_markets

    async def get_market(self, condition_id: str) -> dict:
        return await self.request("GET", f"/markets/{condition_id}")

    async def get_order_book(self, token_id: str) -> dict:
        return await self.request("GET", "/book", params={"token_id": token_id})

    async def get_price(self, token_id: str) -> dict:
        return await self.request("GET", "/price", params={"token_id": token_id})

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class CLOBWebSocketClient:
    """Async WebSocket client with reconnection and heartbeat."""

    def __init__(
        self,
        settings: Settings,
        on_message: Optional[Callable[[dict], Any]] = None,
    ):
        self._settings = settings
        self._api = settings.api
        self._conn = settings.connection
        self._on_message = on_message
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._state = ConnState.DISCONNECTED
        self._running = False
        self._subscriptions: list[dict] = []

    @property
    def state(self) -> ConnState:
        return self._state

    def _set_state(self, new: ConnState) -> None:
        logger.info("WS state: %s -> %s", self._state.value, new.value)
        self._state = new

    async def connect(self, assets: list[str]) -> None:
        """Start the connection loop. Blocks until stop() is called."""
        self._running = True
        self._subscriptions = [{"assets_ids": assets, "type": "market"}]
        attempt = 0

        while self._running:
            try:
                self._set_state(ConnState.CONNECTING)
                attempt += 1
                async with websockets.connect(
                    self._api.clob_ws_url,
                    ping_interval=self._conn.heartbeat_interval,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._set_state(ConnState.CONNECTED)

                    # Subscribe
                    for sub in self._subscriptions:
                        await ws.send(json.dumps(sub))
                    self._set_state(ConnState.READY)
                    attempt = 0  # reset on successful connection

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            logger.error("Malformed WS message: %s", exc)
                            raise CLOBDataError(f"Bad JSON from WS: {exc}") from exc

                        if self._on_message:
                            try:
                                await self._on_message(msg) if asyncio.iscoroutinefunction(self._on_message) else self._on_message(msg)
                            except Exception:
                                logger.exception("Error in message handler")

            except websockets.ConnectionClosed as exc:
                logger.warning("WS closed: code=%s reason=%s", exc.code, exc.reason)
            except (OSError, websockets.WebSocketException) as exc:
                logger.warning("WS connection error: %s", exc)
            except CLOBDataError:
                logger.warning("Data error — reconnecting")

            self._ws = None
            self._set_state(ConnState.DISCONNECTED)

            if not self._running:
                break

            backoff = min(self._conn.base_backoff * (2 ** attempt), self._conn.max_backoff)
            logger.info("Reconnecting in %.1fs (attempt %d)", backoff, attempt)
            await asyncio.sleep(backoff)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        self._set_state(ConnState.DISCONNECTED)
