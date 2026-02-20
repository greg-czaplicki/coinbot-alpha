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
    fill_price: Decimal
    qty: Decimal
    position_qty_after: Decimal
    avg_entry_price_after: Decimal
    realized_pnl_delta: Decimal
    realized_pnl_total: Decimal
    status: str


@dataclass(frozen=True)
class PaperLedgerSnapshot:
    realized_pnl_total: Decimal
    unrealized_pnl_total: Decimal
    open_positions: int


class PaperExecutor:
    def __init__(self) -> None:
        self._log = logging.getLogger("PaperExecutor")
        self._position_qty: dict[str, Decimal] = {}
        self._avg_entry_price: dict[str, Decimal] = {}
        self._marks: dict[str, Decimal] = {}
        self._realized_pnl_total = Decimal("0")

    def submit(self, intent: OrderIntent, fill_price: Decimal) -> PaperFill:
        price = max(fill_price, Decimal("0.0001"))
        qty = intent.notional_usd / price
        signed_qty = qty if intent.side.value == "BUY" else -qty

        current_qty = self._position_qty.get(intent.symbol, Decimal("0"))
        current_avg = self._avg_entry_price.get(intent.symbol, Decimal("0"))
        realized_delta = Decimal("0")

        if current_qty == 0 or (current_qty > 0 and signed_qty > 0) or (current_qty < 0 and signed_qty < 0):
            next_qty = current_qty + signed_qty
            next_avg = _weighted_avg(current_qty, current_avg, signed_qty, price)
        else:
            closing_qty = min(abs(current_qty), abs(signed_qty))
            if current_qty > 0:
                realized_delta = (price - current_avg) * closing_qty
            else:
                realized_delta = (current_avg - price) * closing_qty

            if abs(current_qty) > abs(signed_qty):
                next_qty = current_qty + signed_qty
                next_avg = current_avg
            elif abs(current_qty) == abs(signed_qty):
                next_qty = Decimal("0")
                next_avg = Decimal("0")
            else:
                next_qty = current_qty + signed_qty
                next_avg = price

        self._position_qty[intent.symbol] = next_qty
        self._avg_entry_price[intent.symbol] = next_avg
        self._marks[intent.symbol] = price
        self._realized_pnl_total += realized_delta

        self._log.info(
            "paper_submit symbol=%s side=%s notional=%s px=%s qty=%s pos_qty=%s realized_delta=%s realized_total=%s",
            intent.symbol,
            intent.side.value,
            intent.notional_usd,
            price,
            qty,
            next_qty,
            realized_delta,
            self._realized_pnl_total,
        )
        return PaperFill(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side.value,
            notional_usd=intent.notional_usd,
            fill_price=price,
            qty=qty,
            position_qty_after=next_qty,
            avg_entry_price_after=next_avg,
            realized_pnl_delta=realized_delta,
            realized_pnl_total=self._realized_pnl_total,
            status="filled",
        )

    def snapshot(self, marks: dict[str, Decimal] | None = None) -> PaperLedgerSnapshot:
        if marks:
            self._marks.update(marks)

        unrealized_total = Decimal("0")
        open_positions = 0
        for symbol, qty in self._position_qty.items():
            if qty == 0:
                continue
            mark = self._marks.get(symbol)
            if mark is None:
                continue
            avg = self._avg_entry_price.get(symbol, Decimal("0"))
            if qty > 0:
                unrealized_total += (mark - avg) * qty
            else:
                unrealized_total += (avg - mark) * abs(qty)
            open_positions += 1

        return PaperLedgerSnapshot(
            realized_pnl_total=self._realized_pnl_total,
            unrealized_pnl_total=unrealized_total,
            open_positions=open_positions,
        )

    def flatten_symbol(self, symbol: str, fill_price: Decimal) -> PaperFill | None:
        current_qty = self._position_qty.get(symbol, Decimal("0"))
        if current_qty == 0:
            return None

        price = max(fill_price, Decimal("0.0001"))
        avg = self._avg_entry_price.get(symbol, Decimal("0"))
        qty = abs(current_qty)
        notional = qty * price

        if current_qty > 0:
            side = "SELL"
            realized_delta = (price - avg) * qty
        else:
            side = "BUY"
            realized_delta = (avg - price) * qty

        self._position_qty[symbol] = Decimal("0")
        self._avg_entry_price[symbol] = Decimal("0")
        self._marks[symbol] = price
        self._realized_pnl_total += realized_delta

        self._log.info(
            "paper_flatten symbol=%s side=%s px=%s qty=%s realized_delta=%s realized_total=%s",
            symbol,
            side,
            price,
            qty,
            realized_delta,
            self._realized_pnl_total,
        )

        return PaperFill(
            intent_id="system_flatten",
            symbol=symbol,
            side=side,
            notional_usd=notional,
            fill_price=price,
            qty=qty,
            position_qty_after=Decimal("0"),
            avg_entry_price_after=Decimal("0"),
            realized_pnl_delta=realized_delta,
            realized_pnl_total=self._realized_pnl_total,
            status="filled",
        )


def _weighted_avg(current_qty: Decimal, current_avg: Decimal, new_qty: Decimal, new_price: Decimal) -> Decimal:
    total_abs = abs(current_qty) + abs(new_qty)
    if total_abs == 0:
        return Decimal("0")
    return ((abs(current_qty) * current_avg) + (abs(new_qty) * new_price)) / total_abs
