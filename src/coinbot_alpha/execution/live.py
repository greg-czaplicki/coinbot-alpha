from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import uuid4

from coinbot_alpha.execution.paper import PaperExecutor, PaperFill, PaperLedgerSnapshot
from coinbot_alpha.schemas import Side
from coinbot_alpha.schemas import OrderIntent


class LiveExecutor:
    """Live-execution scaffold.

    In dry-run mode this keeps a local ledger identical to PaperExecutor while
    producing logs with token context that will be required for real posting.
    """

    def __init__(
        self,
        fee_bps: int = 0,
        dry_run: bool = True,
        clob_api_url: str = "https://clob.polymarket.com",
        private_key: str = "",
        chain_id: int = 137,
        signature_type: int = 0,
        funder: str = "",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        order_type: str = "FOK",
    ) -> None:
        self._log = logging.getLogger("coinbot_alpha.live_execution")
        self._dry_run = dry_run
        self._clob_api_url = clob_api_url.rstrip("/")
        self._private_key = private_key
        self._chain_id = chain_id
        self._signature_type = signature_type
        self._funder = funder or None
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._order_type = order_type.upper()
        self._paper = PaperExecutor(fee_bps=fee_bps)
        self._symbol_to_token: dict[str, str] = {}
        self._client: Any = None
        self._clob_types: dict[str, Any] = {}
        self._shadow_order_ids: set[str] = set()

        self._log.info(
            "live_executor_init dry_run=%s clob_api_url=%s chain_id=%s signature_type=%s order_type=%s",
            self._dry_run,
            self._clob_api_url,
            self._chain_id,
            self._signature_type,
            self._order_type,
        )
        if not self._dry_run:
            self._init_client()

    def bind_symbol(self, symbol: str, token_id: str) -> None:
        if not symbol or not token_id:
            return
        self._symbol_to_token[symbol] = token_id

    def submit(self, intent: OrderIntent, fill_price: Decimal) -> PaperFill:
        token_id = self._symbol_to_token.get(intent.symbol)
        if not token_id:
            raise RuntimeError(f"Missing token binding for symbol={intent.symbol}")

        qty = intent.notional_usd / max(fill_price, Decimal("0.0001"))
        if self._dry_run:
            self._log.info(
                "live_submit_shadow intent_id=%s symbol=%s token_id=%s side=%s notional=%s px=%s qty=%s",
                intent.intent_id,
                intent.symbol,
                token_id,
                intent.side.value,
                intent.notional_usd,
                fill_price,
                qty,
            )
        else:
            self._post_live_order(intent=intent, token_id=token_id, limit_price=fill_price, qty=qty)

        fill = self._paper.submit(intent, fill_price)
        status = "live_shadow_filled" if self._dry_run else "live_submitted"
        return replace(fill, status=status)

    def snapshot(self, marks: dict[str, Decimal] | None = None) -> PaperLedgerSnapshot:
        return self._paper.snapshot(marks)

    def flatten_symbol(self, symbol: str, fill_price: Decimal) -> PaperFill | None:
        if not self._dry_run:
            token_id = self._symbol_to_token.get(symbol)
            if not token_id:
                raise RuntimeError(f"Missing token binding for symbol={symbol}")
            qty = self._paper.symbol_position_qty(symbol)
            if qty != 0:
                flatten_side = Side.SELL if qty > 0 else Side.BUY
                flatten_intent = OrderIntent(
                    intent_id="system_flatten_live",
                    symbol=symbol,
                    side=flatten_side,
                    notional_usd=abs(qty) * max(fill_price, Decimal("0.0001")),
                    slippage_bps=0,
                )
                self._post_live_order(
                    intent=flatten_intent,
                    token_id=token_id,
                    limit_price=fill_price,
                    qty=abs(qty),
                )

        fill = self._paper.flatten_symbol(symbol, fill_price)
        if fill is None:
            return None
        status = "live_shadow_flattened" if self._dry_run else "live_flatten_submitted"
        return replace(fill, status=status)

    def has_open_position(self, symbol: str) -> bool:
        return self._paper.has_open_position(symbol)

    def symbol_unrealized(self, symbol: str, mark: Decimal) -> Decimal:
        return self._paper.symbol_unrealized(symbol, mark)

    def _post_live_order(self, intent: OrderIntent, token_id: str, limit_price: Decimal, qty: Decimal) -> None:
        _ = self._post_live_order_raw(intent=intent, token_id=token_id, limit_price=limit_price, qty=qty)

    def place_limit_order(self, symbol: str, side: Side, price: Decimal, notional_usd: Decimal) -> str:
        token_id = self._symbol_to_token.get(symbol)
        if not token_id:
            raise RuntimeError(f"Missing token binding for symbol={symbol}")

        px = max(price, Decimal("0.0001"))
        qty = notional_usd / px
        if qty <= 0:
            raise RuntimeError(f"Quote qty must be > 0 (symbol={symbol}, notional={notional_usd}, px={px})")

        if self._dry_run:
            order_id = f"shadow-{uuid4()}"
            self._shadow_order_ids.add(order_id)
            self._log.info(
                "live_quote_post_shadow order_id=%s symbol=%s token_id=%s side=%s notional=%s qty=%s px=%s",
                order_id,
                symbol,
                token_id,
                side.value,
                notional_usd,
                qty,
                px,
            )
            return order_id

        intent = OrderIntent(
            intent_id=f"maker_quote_{uuid4()}",
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            slippage_bps=0,
        )
        resp = self._post_live_order_raw(intent=intent, token_id=token_id, limit_price=px, qty=qty)
        order_id = self._extract_order_id(resp)
        if not order_id:
            raise RuntimeError(f"post_order succeeded but missing order id: {resp}")
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        if not order_id:
            return False
        if self._dry_run:
            existed = order_id in self._shadow_order_ids
            self._shadow_order_ids.discard(order_id)
            self._log.info("live_quote_cancel_shadow order_id=%s existed=%s", order_id, existed)
            return existed

        if self._client is None:
            self._init_client()

        cancel_fn = getattr(self._client, "cancel", None)
        resp: Any = None
        if cancel_fn is not None:
            try:
                resp = cancel_fn(order_id)
            except TypeError:
                resp = cancel_fn(order_id=order_id)
        elif hasattr(self._client, "cancel_order"):
            cancel_order_fn = getattr(self._client, "cancel_order")
            resp = cancel_order_fn(order_id)
        elif hasattr(self._client, "cancel_orders"):
            cancel_orders_fn = getattr(self._client, "cancel_orders")
            resp = cancel_orders_fn([order_id])
        else:
            raise RuntimeError("py-clob-client missing cancel method")

        self._log.info("live_quote_cancel order_id=%s resp=%s", order_id, resp)
        if isinstance(resp, dict) and (resp.get("error") or resp.get("errorMsg")):
            raise RuntimeError(f"cancel rejected: {resp}")
        return True

    def ack_order_filled(self, order_id: str) -> None:
        if not order_id:
            return
        if self._dry_run:
            self._shadow_order_ids.discard(order_id)

    def _post_live_order_raw(
        self, intent: OrderIntent, token_id: str, limit_price: Decimal, qty: Decimal
    ) -> dict[str, Any] | Any:
        if self._client is None:
            self._init_client()

        if qty <= 0:
            raise RuntimeError(f"Order qty must be > 0 (symbol={intent.symbol}, qty={qty})")

        side_const = self._clob_types["BUY"] if intent.side == Side.BUY else self._clob_types["SELL"]
        order_args = self._clob_types["OrderArgs"](
            token_id=token_id,
            price=float(limit_price),
            size=float(qty),
            side=side_const,
        )
        signed_order = self._client.create_order(order_args)

        order_type_value = self._resolve_order_type()
        resp = self._client.post_order(signed_order, order_type_value)
        self._validate_post_response(resp)
        self._log.info(
            "live_order_posted intent_id=%s symbol=%s token_id=%s side=%s qty=%s px=%s order_type=%s resp=%s",
            intent.intent_id,
            intent.symbol,
            token_id,
            intent.side.value,
            qty,
            limit_price,
            self._order_type,
            resp,
        )
        return resp

    def _validate_post_response(self, resp: Any) -> None:
        if not isinstance(resp, dict):
            return
        if resp.get("error") or resp.get("errorMsg"):
            raise RuntimeError(f"post_order rejected: {resp}")
        if ("success" in resp) and (resp.get("success") is False):
            raise RuntimeError(f"post_order unsuccessful: {resp}")

    def _extract_order_id(self, resp: Any) -> str | None:
        if not isinstance(resp, dict):
            return None
        for key in ("orderID", "orderId", "id"):
            raw = resp.get(key)
            if raw:
                return str(raw)
        order = resp.get("order")
        if isinstance(order, dict):
            for key in ("orderID", "orderId", "id"):
                raw = order.get(key)
                if raw:
                    return str(raw)
        return None

    def _resolve_order_type(self) -> Any:
        order_type_enum = self._clob_types["OrderType"]
        if hasattr(order_type_enum, self._order_type):
            return getattr(order_type_enum, self._order_type)
        return self._order_type

    def _init_client(self) -> None:
        if self._client is not None:
            return
        if not self._private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is required for APP_MODE=live")

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "Missing dependency: py-clob-client. Install it and rerun (e.g. pip install py-clob-client)."
            ) from exc

        client = ClobClient(
            self._clob_api_url,
            key=self._private_key,
            chain_id=self._chain_id,
            signature_type=self._signature_type,
            funder=self._funder,
        )
        self._clob_types = {
            "ApiCreds": ApiCreds,
            "OrderArgs": OrderArgs,
            "OrderType": OrderType,
            "BUY": BUY,
            "SELL": SELL,
        }

        creds = self._get_or_create_api_creds(client, ApiCreds)
        client.set_api_creds(creds)
        self._client = client

    def _get_or_create_api_creds(self, client: Any, api_creds_type: Any) -> Any:
        if self._api_key and self._api_secret and self._api_passphrase:
            return api_creds_type(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            )

        if hasattr(client, "create_or_derive_api_creds"):
            return client.create_or_derive_api_creds()
        raise RuntimeError(
            "No API creds provided and client cannot derive creds. Set POLYMARKET_API_KEY/SECRET/PASSPHRASE."
        )
