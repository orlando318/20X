"""Position tracking per token."""

from pydantic import BaseModel

from models.orderbook import Side


class Position(BaseModel):
    """Tracks net position and P&L for a single token."""

    token_id: str
    size: float = 0.0          # positive = long, negative = short
    avg_entry: float = 0.0     # average entry price
    realized_pnl: float = 0.0  # closed P&L
    total_fees: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.size == 0.0

    @property
    def side(self) -> Side | None:
        if self.size > 0:
            return Side.BID
        elif self.size < 0:
            return Side.ASK
        return None

    @property
    def notional(self) -> float:
        return abs(self.size * self.avg_entry)

    def unrealized_pnl(self, mark_price: float) -> float:
        return round(self.size * (mark_price - self.avg_entry), 8)

    def total_pnl(self, mark_price: float) -> float:
        return round(self.realized_pnl + self.unrealized_pnl(mark_price) - self.total_fees, 8)

    def apply_fill(self, side: Side, size: float, price: float, fee: float = 0.0) -> None:
        """Update position from a fill. Handles increasing, reducing, and flipping."""
        self.total_fees += fee
        signed = size if side == Side.BID else -size

        # Same direction — increase position
        if self.size == 0 or (self.size > 0 and signed > 0) or (self.size < 0 and signed < 0):
            total_cost = self.avg_entry * abs(self.size) + price * size
            self.size = round(self.size + signed, 8)
            if self.size != 0:
                self.avg_entry = round(total_cost / abs(self.size), 8)
            return

        # Opposite direction — reduce or flip
        reduce_qty = min(abs(signed), abs(self.size))
        pnl = reduce_qty * (price - self.avg_entry) * (1 if self.size > 0 else -1)
        self.realized_pnl = round(self.realized_pnl + pnl, 8)
        self.size = round(self.size + signed, 8)

        # If flipped to other side, new avg_entry is the fill price
        if self.size != 0 and ((self.size > 0 and signed > 0) is False):
            self.avg_entry = price
        elif self.size == 0:
            self.avg_entry = 0.0
