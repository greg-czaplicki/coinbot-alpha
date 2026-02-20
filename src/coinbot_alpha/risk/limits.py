from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from coinbot_alpha.schemas import OrderIntent, RiskDecision


@dataclass(frozen=True)
class RiskLimits:
    max_notional_per_symbol_usd: Decimal
    max_daily_notional_usd: Decimal


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits
        self._daily_notional = Decimal("0")
        self._symbol_notional: dict[str, Decimal] = {}

    def check_and_apply(self, intent: OrderIntent) -> RiskDecision:
        sym = intent.symbol
        sym_current = self._symbol_notional.get(sym, Decimal("0"))
        sym_next = sym_current + intent.notional_usd
        if sym_next > self._limits.max_notional_per_symbol_usd:
            return RiskDecision(False, "symbol_cap_exceeded")

        day_next = self._daily_notional + intent.notional_usd
        if day_next > self._limits.max_daily_notional_usd:
            return RiskDecision(False, "daily_cap_exceeded")

        self._symbol_notional[sym] = sym_next
        self._daily_notional = day_next
        return RiskDecision(True, "")
