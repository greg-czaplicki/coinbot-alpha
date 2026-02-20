from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

try:
    import websocket
except ModuleNotFoundError:  # pragma: no cover
    websocket = None


@dataclass(frozen=True)
class ActiveClobMarket:
    slug: str
    condition_id: str
    question: str
    end_ts: datetime
    yes_token_id: str
    no_token_id: str
    yes_price: Decimal
    no_price: Decimal
    strike_price: Decimal | None


class ClobSeriesResolver:
    def __init__(self, clob_api_url: str) -> None:
        self._base = clob_api_url.rstrip("/")

    def resolve_from_seed(self, seed_slug: str) -> ActiveClobMarket | None:
        family = _family_prefix(seed_slug)
        markets = self._fetch_sampling_markets()
        candidates = [
            m
            for m in markets
            if str(m.get("market_slug") or "").startswith(family + "-")
            and bool(m.get("active", True))
            and not bool(m.get("closed", False))
        ]
        if not candidates:
            candidates = [
                m
                for m in markets
                if str(m.get("market_slug") or "") == seed_slug
                and bool(m.get("active", True))
                and not bool(m.get("closed", False))
            ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: _parse_ts(x.get("end_date_iso")) or datetime.min.replace(tzinfo=timezone.utc))
        return _to_active_clob_market(candidates[-1])

    def _fetch_sampling_markets(self) -> list[dict[str, Any]]:
        cursor = "MA=="
        out: list[dict[str, Any]] = []
        for _ in range(30):
            url = f"{self._base}/sampling-markets?{urllib.parse.urlencode({'next_cursor': cursor})}"
            req = urllib.request.Request(url, headers={"User-Agent": "coinbot-alpha/0.1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            data = payload.get("data")
            if isinstance(data, list):
                out.extend([x for x in data if isinstance(x, dict)])

            next_cursor = str(payload.get("next_cursor") or "")
            if not next_cursor or next_cursor == cursor or next_cursor == "LTE=":
                break
            cursor = next_cursor
        return out


class ClobYesPriceFeed:
    def __init__(self, ws_url: str, token_id: str, initial_price: Decimal | None = None) -> None:
        self._ws_url = ws_url
        self._token_id = token_id
        self._price = initial_price
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._log = logging.getLogger("coinbot_alpha.clob_ws")

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name=f"clob_ws_{self._token_id[:8]}", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest_price(self) -> Decimal | None:
        with self._lock:
            return self._price

    def _set_price(self, value: Decimal) -> None:
        with self._lock:
            self._price = value

    def _run(self) -> None:
        if websocket is None:
            self._log.warning("clob_ws_unavailable reason=missing_websocket_client")
            return

        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(self._ws_url, timeout=8)
                ws.send(json.dumps({"type": "market", "assets_ids": [self._token_id]}))
                ws.send(json.dumps({"type": "market", "asset_ids": [self._token_id]}))
                while not self._stop.is_set():
                    raw = ws.recv()
                    self._consume_message(raw)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("clob_ws_error token_id=%s err=%s", self._token_id, exc)
                time.sleep(1.0)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _consume_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        self._walk_message(msg)

    def _walk_message(self, msg: Any) -> None:
        if isinstance(msg, list):
            for item in msg:
                self._walk_message(item)
            return
        if not isinstance(msg, dict):
            return

        asset = str(msg.get("asset_id") or msg.get("asset") or msg.get("token_id") or "")
        if asset and asset == self._token_id:
            price = _extract_price(msg)
            if price is not None:
                self._set_price(price)

        events = msg.get("events")
        if isinstance(events, list):
            for item in events:
                self._walk_message(item)

        changes = msg.get("changes")
        if isinstance(changes, list):
            for item in changes:
                if isinstance(item, dict):
                    payload = dict(item)
                    payload["asset_id"] = asset or str(payload.get("asset_id") or "")
                    self._walk_message(payload)


def _extract_price(msg: dict[str, Any]) -> Decimal | None:
    for key in ("price", "best_bid", "best_ask"):
        raw = msg.get(key)
        if raw is None:
            continue
        try:
            return Decimal(str(raw))
        except Exception:  # noqa: BLE001
            continue
    return None


def _to_active_clob_market(item: dict[str, Any]) -> ActiveClobMarket | None:
    slug = str(item.get("market_slug") or "")
    condition_id = str(item.get("condition_id") or "")
    question = str(item.get("question") or "")
    end_ts = _parse_ts(item.get("end_date_iso"))
    if not slug or not condition_id or end_ts is None:
        return None

    tokens = item.get("tokens")
    if not isinstance(tokens, list) or len(tokens) < 2:
        return None

    yes = _pick_outcome(tokens, "yes")
    no = _pick_outcome(tokens, "no")
    if yes is None or no is None:
        return None

    strike = _parse_strike_price(question)
    return ActiveClobMarket(
        slug=slug,
        condition_id=condition_id,
        question=question,
        end_ts=end_ts,
        yes_token_id=yes[0],
        no_token_id=no[0],
        yes_price=yes[1],
        no_price=no[1],
        strike_price=strike,
    )


def _pick_outcome(tokens: list[Any], name: str) -> tuple[str, Decimal] | None:
    target = name.lower()
    for token in tokens:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome") or "").strip().lower()
        if outcome != target:
            continue
        token_id = str(token.get("token_id") or "")
        if not token_id:
            return None
        try:
            price = Decimal(str(token.get("price")))
        except Exception:  # noqa: BLE001
            return None
        return token_id, price
    return None


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    txt = str(raw)
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        return datetime.fromisoformat(txt).astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_strike_price(question: str) -> Decimal | None:
    m = re.search(r"above\s+\$?([0-9][0-9,]*(?:\.[0-9]+)?[kKmMbB]?)", question, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"hit\s+\$?([0-9][0-9,]*(?:\.[0-9]+)?[kKmMbB]?)", question, flags=re.IGNORECASE)
    if not m:
        return None
    num = m.group(1).replace(",", "").lower()
    mult = Decimal("1")
    if num.endswith("k"):
        mult = Decimal("1000")
        num = num[:-1]
    elif num.endswith("m"):
        mult = Decimal("1000000")
        num = num[:-1]
    elif num.endswith("b"):
        mult = Decimal("1000000000")
        num = num[:-1]
    try:
        return Decimal(num) * mult
    except Exception:  # noqa: BLE001
        return None


def _family_prefix(slug: str) -> str:
    return slug.rsplit("-", 1)[0] if "-" in slug else slug
