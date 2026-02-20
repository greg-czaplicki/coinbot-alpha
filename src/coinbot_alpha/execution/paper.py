from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from coinbot_alpha.schemas import OrderIntent


@dataclass(frozen=True)
class PaperFill:
    intent_id: str
    symbol: str
    side: str
    notional_usd: Decimal
    status: str


class PaperExecutor:
    def __init__(self) -> None:
        self._log = logging.getLogger("PaperExecutor")

    def submit(self, intent: OrderIntent) -> PaperFill:
        self._log.info(
            "paper_submit symbol=%s side=%s notional=%s",
            intent.symbol,
            intent.side.value,
            intent.notional_usd,
        )
        return PaperFill(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side.value,
            notional_usd=intent.notional_usd,
            status="filled",
        )
