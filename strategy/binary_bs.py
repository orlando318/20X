"""Binary Black-Scholes pricer and trading strategy.

Polymarket crypto markets are binary options:
    "Will BTC reach $70,000 by April 5?" → pays $1 if yes, $0 if no.

This is a cash-or-nothing digital option priced as:
    - Binary call (reach above K):  P = e^(-rT) * N(d2)
    - Binary put  (dip below K):    P = e^(-rT) * N(-d2)

Where:
    d2 = (ln(S/K) + (r - σ²/2)T) / (σ√T)
    S  = current spot price (from Binance)
    K  = strike price (from Polymarket instrument)
    T  = time to expiry in years
    σ  = annualized volatility
    r  = risk-free rate (0 for crypto)
"""

import math
from dataclasses import dataclass
from typing import Optional

from scipy.stats import norm


@dataclass
class BinaryPrice:
    """Output of the binary BS pricer."""
    spot: float
    strike: float
    vol: float
    time_to_expiry: float  # in years
    fair_value: float      # BS price [0, 1]
    delta: float           # sensitivity to spot
    vega: float            # sensitivity to vol
    theta: float           # time decay per day
    is_call: bool          # True = "reach above K", False = "dip below K"


def price_binary_call(spot: float, strike: float, vol: float, tte_years: float, rate: float = 0.0) -> BinaryPrice:
    """Price a binary call: pays $1 if S > K at expiry."""
    if tte_years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        # Expired or invalid — intrinsic value
        fv = 1.0 if spot >= strike else 0.0
        return BinaryPrice(spot=spot, strike=strike, vol=vol, time_to_expiry=tte_years,
                          fair_value=fv, delta=0, vega=0, theta=0, is_call=True)

    sqrt_t = math.sqrt(tte_years)
    d1 = (math.log(spot / strike) + (rate + vol**2 / 2) * tte_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    discount = math.exp(-rate * tte_years)

    fv = discount * norm.cdf(d2)

    # Greeks
    pdf_d2 = norm.pdf(d2)
    delta = discount * pdf_d2 / (spot * vol * sqrt_t)
    vega = -discount * pdf_d2 * d1 / vol
    theta = discount * pdf_d2 * (d1 / (2 * tte_years) + rate) / 365  # per day

    return BinaryPrice(spot=spot, strike=strike, vol=vol, time_to_expiry=tte_years,
                      fair_value=round(fv, 6), delta=round(delta, 6),
                      vega=round(vega, 6), theta=round(theta, 6), is_call=True)


def price_binary_put(spot: float, strike: float, vol: float, tte_years: float, rate: float = 0.0) -> BinaryPrice:
    """Price a binary put: pays $1 if S < K at expiry."""
    if tte_years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        fv = 1.0 if spot <= strike else 0.0
        return BinaryPrice(spot=spot, strike=strike, vol=vol, time_to_expiry=tte_years,
                          fair_value=fv, delta=0, vega=0, theta=0, is_call=False)

    sqrt_t = math.sqrt(tte_years)
    d1 = (math.log(spot / strike) + (rate + vol**2 / 2) * tte_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    discount = math.exp(-rate * tte_years)

    fv = discount * norm.cdf(-d2)

    pdf_d2 = norm.pdf(d2)
    delta = -discount * pdf_d2 / (spot * vol * sqrt_t)
    vega = discount * pdf_d2 * d1 / vol
    theta = -discount * pdf_d2 * (d1 / (2 * tte_years) + rate) / 365

    return BinaryPrice(spot=spot, strike=strike, vol=vol, time_to_expiry=tte_years,
                      fair_value=round(fv, 6), delta=round(delta, 6),
                      vega=round(vega, 6), theta=round(theta, 6), is_call=False)


def implied_vol_call(market_price: float, spot: float, strike: float, tte_years: float,
                     rate: float = 0.0, tol: float = 1e-6, max_iter: int = 100) -> Optional[float]:
    """Solve for implied vol of a binary call using Newton-Raphson.

    Returns annualized vol, or None if it fails to converge.
    """
    if tte_years <= 0 or spot <= 0 or strike <= 0:
        return None
    if market_price <= 0.001 or market_price >= 0.999:
        return None

    vol = 0.5  # initial guess
    sqrt_t = math.sqrt(tte_years)

    for _ in range(max_iter):
        d1 = (math.log(spot / strike) + (rate + vol**2 / 2) * tte_years) / (vol * sqrt_t)
        d2 = d1 - vol * sqrt_t
        discount = math.exp(-rate * tte_years)
        price = discount * norm.cdf(d2)
        vega = -discount * norm.pdf(d2) * d1 / vol

        diff = price - market_price
        if abs(diff) < tol:
            return round(vol, 6) if vol > 0.01 else None

        if abs(vega) < 1e-12:
            break
        vol -= diff / vega
        vol = max(0.01, min(vol, 20.0))

    return None


def implied_vol_put(market_price: float, spot: float, strike: float, tte_years: float,
                    rate: float = 0.0, tol: float = 1e-6, max_iter: int = 100) -> Optional[float]:
    """Solve for implied vol of a binary put using Newton-Raphson."""
    if tte_years <= 0 or spot <= 0 or strike <= 0:
        return None
    if market_price <= 0.001 or market_price >= 0.999:
        return None

    vol = 0.5
    sqrt_t = math.sqrt(tte_years)

    for _ in range(max_iter):
        d1 = (math.log(spot / strike) + (rate + vol**2 / 2) * tte_years) / (vol * sqrt_t)
        d2 = d1 - vol * sqrt_t
        discount = math.exp(-rate * tte_years)
        price = discount * norm.cdf(-d2)
        vega = discount * norm.pdf(d2) * d1 / vol

        diff = price - market_price
        if abs(diff) < tol:
            return round(vol, 6) if vol > 0.01 else None

        if abs(vega) < 1e-12:
            break
        vol -= diff / vega
        vol = max(0.01, min(vol, 20.0))

    return None


class ImpliedVolTracker:
    """Tracks implied vol across all instruments and provides a market-wide estimate.

    Computes IV for each instrument from its Polymarket price, then takes
    a weighted median as the consensus vol for pricing.
    """

    def __init__(self):
        self._ivs: dict[str, float] = {}  # token_id -> latest IV

    def update(self, token_id: str, market_price: float, spot: float, strike: float,
               tte_years: float, is_call: bool) -> Optional[float]:
        """Compute and store IV for one instrument. Returns the IV or None."""
        if is_call:
            iv = implied_vol_call(market_price, spot, strike, tte_years)
        else:
            iv = implied_vol_put(market_price, spot, strike, tte_years)

        if iv is not None and 0.05 < iv < 15.0:
            self._ivs[token_id] = iv
            return iv

        return None

    @property
    def median_iv(self) -> Optional[float]:
        """Median IV across all instruments — the market's consensus vol."""
        vals = sorted(self._ivs.values())
        if len(vals) < 2:
            return None
        n = len(vals)
        return round(vals[n // 2], 4)

    @property
    def all_ivs(self) -> dict[str, float]:
        return dict(self._ivs)

    @property
    def count(self) -> int:
        return len(self._ivs)


class RealizedVolEstimator:
    """Rolling realized volatility from Binance trade prices.

    Computes annualized vol from log returns over a rolling window.
    """

    def __init__(self, window: int = 200):
        self._window = window
        self._prices: list[float] = []

    def update(self, price: float) -> None:
        self._prices.append(price)
        if len(self._prices) > self._window + 1:
            self._prices = self._prices[-(self._window + 1):]

    @property
    def vol(self) -> Optional[float]:
        if len(self._prices) < 20:
            return None
        returns = []
        for i in range(1, len(self._prices)):
            if self._prices[i - 1] > 0:
                returns.append(math.log(self._prices[i] / self._prices[i - 1]))
        if len(returns) < 10:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        # Annualize: assume ~1 trade per second, ~31.5M seconds per year
        # More conservatively, scale by trades-per-day estimate
        std = math.sqrt(var)
        # Rough annualization: std * sqrt(trades_per_year)
        # With ~1 trade/sec: sqrt(365 * 24 * 3600) ≈ 5615
        trades_per_year = 365 * 24 * 3600
        return round(std * math.sqrt(trades_per_year), 4)
