#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any


GAMMA_API_URL = "https://gamma-api.polymarket.com"


def _fetch_market(base_url: str, slug: str) -> dict[str, Any] | None:
    url = f"{base_url.rstrip('/')}/markets?{urllib.parse.urlencode({'slug': slug})}"
    req = urllib.request.Request(url, headers={"User-Agent": "coinbot-alpha/0.1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, list):
        return None
    for item in payload:
        if isinstance(item, dict) and str(item.get("slug") or "") == slug:
            return item
    return None


def _find_latest_open_slug(base_url: str, family: str, interval_sec: int, lookback_bars: int = 48) -> str | None:
    now_ts = int(time.time())
    now_bucket = now_ts - (now_ts % interval_sec)

    for i in range(0, lookback_bars + 1):
        ts = now_bucket - (i * interval_sec)
        slug = f"{family}-{ts}"
        item = _fetch_market(base_url, slug)
        if not item:
            continue
        if bool(item.get("active", True)) and not bool(item.get("closed", False)):
            return slug
    return None


def main() -> int:
    seed_5m = _find_latest_open_slug(GAMMA_API_URL, "btc-updown-5m", 5 * 60)
    seed_15m = _find_latest_open_slug(GAMMA_API_URL, "btc-updown-15m", 15 * 60)

    if not seed_5m or not seed_15m:
        print("Failed to resolve one or both seed slugs.")
        return 1

    print(f"DEMO_SEED_5M_SLUG={seed_5m}")
    print(f"DEMO_SEED_15M_SLUG={seed_15m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
