from __future__ import annotations

import json
import urllib.parse
import urllib.request
from decimal import Decimal
from urllib.error import HTTPError


class BinanceSpotClient:
    def __init__(self, symbol: str, base_urls: tuple[str, ...] | None = None) -> None:
        self._symbol = symbol.upper()
        self._base_urls = base_urls or (
            "https://api.binance.com",
            "https://api.binance.us",
        )

    def get_price(self) -> Decimal:
        query = urllib.parse.urlencode({"symbol": self._symbol})
        last_err: Exception | None = None
        for base in self._base_urls:
            url = f"{base}/api/v3/ticker/price?{query}"
            req = urllib.request.Request(url, headers={"User-Agent": "coinbot-alpha/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=3) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return Decimal(str(payload["price"]))
            except HTTPError as exc:
                last_err = exc
                # Binance global returns 451 in restricted regions; continue to next venue.
                if exc.code == 451:
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        if last_err is not None:
            raise last_err
        raise RuntimeError("No Binance API endpoints configured")
