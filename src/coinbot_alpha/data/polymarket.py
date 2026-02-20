from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ActiveSeriesMarket:
    slug: str
    market_id: str
    condition_id: str
    question: str
    end_ts: datetime
    yes_token_id: str
    no_token_id: str
    yes_price: Decimal
    no_price: Decimal
    strike_price: Decimal | None


class GammaSeriesResolver:
    def __init__(self, gamma_api_url: str) -> None:
        self._base = gamma_api_url.rstrip("/")

    def resolve_latest(self, series_prefix: str) -> ActiveSeriesMarket | None:
        items = self._fetch_active_markets()
        candidates = [
            item
            for item in items
            if str(item.get("slug") or "").startswith(series_prefix + "-")
            and not bool(item.get("closed", False))
            and bool(item.get("active", True))
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda x: _parse_ts(x.get("endDate")) or datetime.min.replace(tzinfo=timezone.utc))
        chosen = candidates[-1]
        return _to_active_series_market(chosen)

    def _fetch_active_markets(self) -> list[dict[str, Any]]:
        q = urllib.parse.urlencode({"active": "true", "closed": "false", "limit": 5000})
        urls = [
            f"{self._base}/markets?{q}",
            f"{self._base}/api/markets?{q}",
        ]
        last_err: Exception | None = None
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "coinbot-alpha/0.1"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if isinstance(payload, list):
                    return [x for x in payload if isinstance(x, dict)]
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if last_err is not None:
            raise last_err
        return []


def _to_active_series_market(item: dict[str, Any]) -> ActiveSeriesMarket | None:
    slug = str(item.get("slug") or "")
    market_id = str(item.get("id") or "")
    condition_id = str(item.get("conditionId") or "")
    question = str(item.get("question") or "")
    end_ts = _parse_ts(item.get("endDate"))
    if not slug or not market_id or not condition_id or end_ts is None:
        return None

    labels = _extract_labels(item.get("outcomes"))
    prices = _extract_prices(item.get("outcomePrices"))
    token_ids = _extract_token_ids(item)
    if len(labels) < 2 or len(prices) < 2 or len(token_ids) < 2:
        return None

    mapping = {
        labels[i].strip().lower(): (token_ids[i], prices[i])
        for i in range(min(len(labels), len(token_ids), len(prices)))
    }
    yes = mapping.get("yes")
    no = mapping.get("no")
    if yes is None or no is None:
        return None

    strike = _parse_strike_price(question)

    return ActiveSeriesMarket(
        slug=slug,
        market_id=market_id,
        condition_id=condition_id,
        question=question,
        end_ts=end_ts,
        yes_token_id=yes[0],
        no_token_id=no[0],
        yes_price=yes[1],
        no_price=no[1],
        strike_price=strike,
    )


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


def _extract_labels(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for v in raw:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            out.append(str(v.get("name") or v.get("outcome") or ""))
    return [x for x in out if x]


def _extract_prices(raw: Any) -> list[Decimal]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        return []
    out: list[Decimal] = []
    for v in raw:
        try:
            out.append(Decimal(str(v)))
        except Exception:  # noqa: BLE001
            continue
    return out


def _extract_token_ids(item: dict[str, Any]) -> list[str]:
    raw = item.get("clobTokenIds") or item.get("tokenIds") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x)]


def _parse_strike_price(question: str) -> Decimal | None:
    m = re.search(r"above\s+\$?([0-9][0-9,]*(?:\.[0-9]+)?)", question, flags=re.IGNORECASE)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        return Decimal(num)
    except Exception:  # noqa: BLE001
        return None
