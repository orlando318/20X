"""Trade (fill) records."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from models.orderbook import Side


class Trade(BaseModel):
    """A single executed trade."""

    id: str
    token_id: str
    side: Side
    price: float
    size: float
    timestamp_ms: int
    order_id: Optional[str] = None
    fee: float = 0.0

    @property
    def notional(self) -> float:
        return self.price * self.size

    @property
    def net_notional(self) -> float:
        return self.notional - self.fee

    @property
    def timestamp(self) -> datetime:
        return datetime.utcfromtimestamp(self.timestamp_ms / 1000)
