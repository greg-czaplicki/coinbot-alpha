from __future__ import annotations

import json
import urllib.parse
import urllib.request
from decimal import Decimal


class BinanceSpotClient:
    def __init__(self, symbol: str) -> None:
        self._symbol = symbol.upper()

    def get_price(self) -> Decimal:
        url = f"https://api.binance.com/api/v3/ticker/price?{urllib.parse.urlencode({'symbol': self._symbol})}"
        req = urllib.request.Request(url, headers={"User-Agent": "coinbot-alpha/0.1"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return Decimal(str(payload["price"]))
