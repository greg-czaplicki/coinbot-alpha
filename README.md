# coinbot-alpha

Research-first auto-trading framework (separate from copy-trading).

## Current Demo: BTC Latency-Divergence (5m/15m)
- Pulls live BTC spot from Binance REST (`BTCUSDT`)
- Auto-resolves active Polymarket BTC `5m` and `15m` rolling markets from Gamma API
- Streams YES price updates from Polymarket CLOB websocket
- Parses YES/NO (or UP/DOWN) prices + strike from market metadata when available
- Computes model-implied probability of finishing above strike
- Emits paper BUY/SELL signals when edge exceeds threshold
- Enforces risk caps and writes trade audit logs

This is a **paper demo**, not production arb.

## Quick Start
```bash
cd ~/Documents/Projects/coinbot-alpha
cp .env.example .env
set -a; source .env; set +a
PYTHONPATH=src python3 -u -m coinbot_alpha.main
```

Refresh demo seeds (optional):
```bash
python3 scripts/resolve_demo_seeds.py
```

## Key Env Vars
- `DEMO_CLOB_API_URL=https://gamma-api.polymarket.com`
- `DEMO_CLOB_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `DEMO_SERIES_5M_PREFIX=btc-updown-5m`
- `DEMO_SERIES_15M_PREFIX=btc-updown-15m`
- `DEMO_SEED_5M_SLUG=btc-updown-5m-1771549800`
- `DEMO_SEED_15M_SLUG=btc-updown-15m-1771551000`
- `DEMO_EDGE_THRESHOLD_BPS=800` (8%)
- `DEMO_MARKET_REFRESH_SEC=5`
- `DEMO_POS_STOP_LOSS_USD=12`
- `DEMO_POS_TAKE_PROFIT_USD=18`
- `DEMO_MIN_HOLD_SEC_5M=45`
- `DEMO_MIN_HOLD_SEC_15M=90`

## Useful Logs
- `market_roll ...` when markets rotate
- `series_snapshot ... edge_bps=...` every loop
- `paper_submit ...` when a signal passes risk checks
- `telemetry_snapshot ... pnl_realized=... pnl_unrealized=...` for paper PnL tracking

## Layout
- `src/coinbot_alpha/data`: market data and Polymarket resolver
- `src/coinbot_alpha/strategy`: strategy interfaces
- `src/coinbot_alpha/risk`: limits and kill switch
- `src/coinbot_alpha/execution`: paper execution
- `src/coinbot_alpha/telemetry`: logs, metrics, and audit
