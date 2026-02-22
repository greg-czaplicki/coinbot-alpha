from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from urllib.error import HTTPError

try:
    import websocket
except ModuleNotFoundError:  # pragma: no cover
    websocket = None


class BinanceSpotClient:
    def __init__(
        self,
        symbol: str,
        base_urls: tuple[str, ...] | None = None,
        ws_urls: tuple[str, ...] | None = None,
    ) -> None:
        self._symbol = symbol.upper()
        self._base_urls = base_urls or (
            "https://api.binance.com",
            "https://api.binance.us",
        )
        self._ws_urls = ws_urls or (
            "wss://stream.binance.com:9443/ws",
            "wss://stream.binance.us:9443/ws",
        )
        self._lock = threading.Lock()
        self._price: Decimal | None = None
        self._price_ts: float = 0.0
        self._started = False
        self._stop = threading.Event()
        self._log = logging.getLogger("coinbot_alpha.binance_ws")

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if websocket is None:
            self._log.warning("binance_ws_unavailable reason=missing_websocket_client")
            return
        thread = threading.Thread(target=self._run_ws, name=f"binance_ws_{self._symbol}", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest_price(self) -> Decimal | None:
        with self._lock:
            return self._price

    def get_price(self, max_age_sec: float = 3.0) -> Decimal:
        self.start()

        now = time.time()
        with self._lock:
            price = self._price
            price_ts = self._price_ts
        if price is not None and (now - price_ts) <= max_age_sec:
            return price

        rest_price = self._fetch_rest_price()
        self._set_price(rest_price)
        return rest_price

    def _set_price(self, price: Decimal) -> None:
        with self._lock:
            self._price = price
            self._price_ts = time.time()

    def _run_ws(self) -> None:
        stream = f"{self._symbol.lower()}@trade"
        while not self._stop.is_set():
            for base in self._ws_urls:
                if self._stop.is_set():
                    break
                ws_url = f"{base.rstrip('/')}/{stream}"
                ws = None
                try:
                    ws = websocket.create_connection(ws_url, timeout=8)
                    while not self._stop.is_set():
                        raw = ws.recv()
                        self._consume_message(raw)
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("binance_ws_error symbol=%s ws=%s err=%s", self._symbol, ws_url, exc)
                    time.sleep(1.0)
                finally:
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception:  # noqa: BLE001
                            pass

    def _consume_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        trade_price = payload.get("p")
        if trade_price is None:
            return
        try:
            self._set_price(Decimal(str(trade_price)))
        except Exception:  # noqa: BLE001
            return

    def _fetch_rest_price(self) -> Decimal:
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
