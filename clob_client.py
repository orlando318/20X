"""Polymarket CLOB API client with error handling, reconnection, and rate limiting."""

import asyncio
import enum
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import aiohttp
import websockets

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
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class CLOBConfig:
    rest_url: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    request_timeout: float = 10.0
    max_retries: int = 5
    base_backoff: float = 0.5
    max_backoff: float = 30.0
    heartbeat_interval: float = 30.0
    rate_limit_per_second: int = 10

    def validate(self) -> None:
        if not self.rest_url:
            raise CLOBConfigError("rest_url is required")
        if not self.ws_url:
            raise CLOBConfigError("ws_url is required")


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------

class CLOBRestClient:
    """Async REST client for Polymarket CLOB API."""

    def __init__(self, config: CLOBConfig):
        config.validate()
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_times: list[float] = []

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._config.api_key:
                headers["POLY_API_KEY"] = self._config.api_key
            if self._config.api_secret:
                headers["POLY_API_SECRET"] = self._config.api_secret
            if self._config.api_passphrase:
                headers["POLY_PASSPHRASE"] = self._config.api_passphrase
            timeout = aiohttp.ClientTimeout(total=self._config.request_timeout)
            self._session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        return self._session

    async def _throttle(self) -> None:
        now = time.monotonic()
        window = 1.0
        self._request_times = [t for t in self._request_times if now - t < window]
        if len(self._request_times) >= self._config.rate_limit_per_second:
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
        url = f"{self._config.rest_url}{path}"
        last_err: Optional[Exception] = None

        for attempt in range(1, self._config.max_retries + 1):
            await self._throttle()
            session = await self._ensure_session()
            try:
                logger.debug("REQ %s %s attempt=%d", method, path, attempt)
                async with session.request(method, url, params=params, json=body) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", self._config.base_backoff * attempt))
                        logger.warning("Rate limited on %s %s, retry after %.1fs", method, path, retry_after)
                        await asyncio.sleep(retry_after)
                        last_err = CLOBRateLimitError(retry_after)
                        continue

                    if resp.status >= 500:
                        text = await resp.text()
                        logger.warning("Server error %d on %s %s: %s", resp.status, method, path, text[:200])
                        last_err = CLOBConnectionError(f"HTTP {resp.status}: {text[:200]}")
                        await asyncio.sleep(min(self._config.base_backoff * (2 ** (attempt - 1)), self._config.max_backoff))
                        continue

                    if resp.status >= 400:
                        text = await resp.text()
                        raise CLOBConnectionError(f"HTTP {resp.status}: {text[:500]}")

                    data = await resp.json()
                    return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                backoff = min(self._config.base_backoff * (2 ** (attempt - 1)), self._config.max_backoff)
                logger.warning("Network error on %s %s (attempt %d/%d): %s — retrying in %.1fs",
                               method, path, attempt, self._config.max_retries, exc, backoff)
                last_err = CLOBConnectionError(str(exc))
                await asyncio.sleep(backoff)

        raise CLOBConnectionError(f"All {self._config.max_retries} retries exhausted for {method} {path}") from last_err

    # -- convenience helpers --------------------------------------------------

    async def get_markets(self) -> list[dict]:
        return await self.request("GET", "/markets")

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
        config: CLOBConfig,
        on_message: Optional[Callable[[dict], Any]] = None,
    ):
        config.validate()
        self._config = config
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
                    self._config.ws_url,
                    ping_interval=self._config.heartbeat_interval,
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

            backoff = min(self._config.base_backoff * (2 ** attempt), self._config.max_backoff)
            logger.info("Reconnecting in %.1fs (attempt %d)", backoff, attempt)
            await asyncio.sleep(backoff)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        self._set_state(ConnState.DISCONNECTED)
