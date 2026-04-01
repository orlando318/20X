"""Order representation for the execution layer."""

import enum
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from models.orderbook import Side


class OrderType(str, enum.Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"      # created locally, not yet submitted
    SUBMITTED = "submitted"  # sent to CLOB
    OPEN = "open"            # acknowledged, resting on book
    PARTIAL = "partial"      # partially filled
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Order(BaseModel):
    """A single order in the system."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    token_id: str
    side: Side
    order_type: OrderType = OrderType.LIMIT
    price: float
    size: float
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    exchange_id: Optional[str] = None  # ID returned by CLOB
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def remaining(self) -> float:
        return round(self.size - self.filled_size, 8)

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.OPEN, OrderStatus.PARTIAL)

    @property
    def notional(self) -> float:
        return self.price * self.size

    @property
    def filled_notional(self) -> float:
        return self.avg_fill_price * self.filled_size

    def record_fill(self, fill_size: float, fill_price: float) -> None:
        """Update order state after a fill event."""
        prev_cost = self.avg_fill_price * self.filled_size
        self.filled_size = round(self.filled_size + fill_size, 8)
        if self.filled_size > 0:
            self.avg_fill_price = round((prev_cost + fill_price * fill_size) / self.filled_size, 8)
        self.status = OrderStatus.FILLED if self.remaining <= 0 else OrderStatus.PARTIAL
        self.updated_at = datetime.utcnow()
