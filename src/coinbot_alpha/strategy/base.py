from __future__ import annotations

from typing import Protocol

from coinbot_alpha.schemas import MarketTick, Signal


class Strategy(Protocol):
    def on_tick(self, tick: MarketTick) -> Signal | None:
        ...
