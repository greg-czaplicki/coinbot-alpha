from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class MarketTick:
    symbol: str
    price: Decimal
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class Signal:
    signal_id: str
    symbol: str
    side: Side
    confidence: float
    target_notional_usd: Decimal
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    symbol: str
    side: Side
    notional_usd: Decimal
    slippage_bps: int
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""
