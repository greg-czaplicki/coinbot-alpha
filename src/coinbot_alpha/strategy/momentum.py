from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from coinbot_alpha.schemas import MarketTick, Side, Signal


@dataclass
class SimpleMomentum:
    lookback: int = 5
    threshold_bps: Decimal = Decimal("8")
    notional_usd: Decimal = Decimal("25")

    def __post_init__(self) -> None:
        self._prices: dict[str, deque[Decimal]] = defaultdict(lambda: deque(maxlen=self.lookback))

    def on_tick(self, tick: MarketTick) -> Signal | None:
        buf = self._prices[tick.symbol]
        buf.append(tick.price)
        if len(buf) < self.lookback:
            return None

        first = buf[0]
        last = buf[-1]
        if first <= 0:
            return None

        move_bps = ((last - first) / first) * Decimal("10000")
        if move_bps >= self.threshold_bps:
            return Signal(str(uuid4()), tick.symbol, Side.BUY, 0.55, self.notional_usd)
        if move_bps <= -self.threshold_bps:
            return Signal(str(uuid4()), tick.symbol, Side.SELL, 0.55, self.notional_usd)
        return None
