"""Market and outcome definitions from Polymarket."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MarketOutcome(BaseModel):
    """A single outcome (YES/NO token) within a market."""

    token_id: str
    outcome: str  # e.g. "Yes", "No"
    price: float = 0.0
    winner: Optional[bool] = None


class Market(BaseModel):
    """A Polymarket prediction market (condition)."""

    condition_id: str
    question: str
    description: str = ""
    outcomes: list[MarketOutcome] = []
    active: bool = True
    closed: bool = False
    volume: float = 0.0
    liquidity: float = 0.0
    end_date: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @property
    def spread(self) -> Optional[float]:
        if len(self.outcomes) >= 2:
            prices = sorted(o.price for o in self.outcomes)
            return round(1.0 - sum(prices), 6)
        return None

    @property
    def mid_price(self) -> Optional[float]:
        if self.outcomes:
            return round(sum(o.price for o in self.outcomes) / len(self.outcomes), 6)
        return None
