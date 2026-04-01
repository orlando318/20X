"""Centralized configuration with profile support (dev / paper / live)."""

import enum
import os
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class ExecutionMode(str, enum.Enum):
    DEV = "dev"
    PAPER = "paper"
    LIVE = "live"


class APISettings(BaseSettings):
    """Polymarket CLOB API credentials and endpoints."""

    clob_rest_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_url: str = "https://gamma-api.polymarket.com"

    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None

    # Polygon wallet for signing orders
    private_key: Optional[str] = None
    wallet_address: Optional[str] = None

    model_config = {"env_prefix": "POLY_"}


class ConnectionSettings(BaseSettings):
    """Timeouts, retries, rate limits."""

    request_timeout: float = 10.0
    max_retries: int = 5
    base_backoff: float = 0.5
    max_backoff: float = 30.0
    heartbeat_interval: float = 30.0
    rate_limit_per_second: int = 10

    model_config = {"env_prefix": "POLY_CONN_"}


class RiskSettings(BaseSettings):
    """Risk management limits — tighter defaults for non-live profiles."""

    max_position_usd: float = 100.0
    max_drawdown_pct: float = 5.0
    max_open_orders: int = 10
    kill_switch_loss_usd: float = 50.0

    model_config = {"env_prefix": "POLY_RISK_"}


class LogSettings(BaseSettings):
    """Logging configuration."""

    level: str = "DEBUG"
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    file: Optional[str] = None

    model_config = {"env_prefix": "POLY_LOG_"}


class Settings(BaseSettings):
    """Root settings — aggregates all sub-settings and applies profile overrides."""

    mode: ExecutionMode = ExecutionMode.DEV
    api: APISettings = Field(default_factory=APISettings)
    connection: ConnectionSettings = Field(default_factory=ConnectionSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    log: LogSettings = Field(default_factory=LogSettings)

    model_config = {"env_prefix": "POLY_"}

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, v: str) -> str:
        return v.lower().strip() if isinstance(v, str) else v


# ---------------------------------------------------------------------------
# Profile overrides — applied on top of env / defaults
# ---------------------------------------------------------------------------

_PROFILE_OVERRIDES: dict[ExecutionMode, dict] = {
    ExecutionMode.DEV: {
        "log": {"level": "DEBUG"},
        "risk": {
            "max_position_usd": 0.0,
            "max_open_orders": 0,
            "kill_switch_loss_usd": 0.0,
        },
    },
    ExecutionMode.PAPER: {
        "log": {"level": "INFO"},
        "risk": {
            "max_position_usd": 1000.0,
            "max_open_orders": 20,
            "kill_switch_loss_usd": 200.0,
        },
    },
    ExecutionMode.LIVE: {
        "log": {"level": "WARNING"},
        "risk": {
            "max_position_usd": 10000.0,
            "max_drawdown_pct": 3.0,
            "max_open_orders": 50,
            "kill_switch_loss_usd": 2000.0,
        },
    },
}


def _apply_overrides(settings: Settings) -> Settings:
    """Merge profile-specific defaults into settings (env vars take priority)."""
    overrides = _PROFILE_OVERRIDES.get(settings.mode, {})
    for section, values in overrides.items():
        sub = getattr(settings, section)
        for key, default_val in values.items():
            env_key = f"{sub.model_config.get('env_prefix', '')}{key}".upper()
            if os.environ.get(env_key) is None:
                setattr(sub, key, default_val)
    return settings


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_settings(env_file: Optional[str] = None) -> Settings:
    """Load settings from environment and optional .env file, then apply profile overrides.

    Priority: env vars > .env file > profile defaults > field defaults.
    """
    if env_file:
        _load_dotenv(Path(env_file))

    settings = Settings()
    settings = _apply_overrides(settings)
    return settings


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — sets os.environ for keys not already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key not in os.environ:
            os.environ[key] = value
