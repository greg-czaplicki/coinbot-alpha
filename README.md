# coinbot-alpha

Research-first auto-trading framework (separate from copy-trading).

## Current Demo: BTC Latency-Divergence (5m/15m)
- Pulls live BTC spot from Binance WebSocket (`BTCUSDT`) with REST fallback on stale/missing ticks
- Auto-resolves active Polymarket BTC `5m` and `15m` rolling markets from Gamma API
- Streams YES price updates from Polymarket CLOB websocket
- Parses YES/NO (or UP/DOWN) prices + strike from market metadata when available
- Computes model-implied probability of finishing above strike
- Emits paper BUY/SELL signals when edge exceeds threshold
- Enforces risk caps and writes trade audit logs

This is a **paper demo**, not production arb.

`APP_MODE=live` now uses a live-execution scaffold:
- `EXECUTION_DRY_RUN=true`: shadow-live mode (uses live routing context with local fills/ledger)
- `EXECUTION_DRY_RUN=false`: posts signed live orders via `py-clob-client` (default order type `FOK`)
- `DEMO_MAKER_ENABLED=true` + `EXECUTION_ORDER_TYPE=GTC`: enables maker quote post/cancel/requote loop
  - In dry-run maker mode, a shadow fill simulator marks fills when market price crosses quoted levels to estimate PnL.

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
- `DEMO_EXIT_EDGE_BPS=250`
- `DEMO_REENTRY_ARM_BPS=350`
- `DEMO_MAX_HOLD_SEC_5M=180`
- `DEMO_MAX_HOLD_SEC_15M=540`
- `DEMO_MAX_DRAWDOWN_SOFT_USD=0` (`>0` blocks new entries beyond drawdown)
- `DEMO_MAX_DRAWDOWN_HARD_USD=0` (`>0` flattens and halts beyond drawdown)
- `EXECUTION_FEE_BPS=0` (paper commission model per fill)
- `EXECUTION_CLOB_API_URL=https://clob.polymarket.com`
- `EXECUTION_ORDER_TYPE=FOK`
- `POLYMARKET_PRIVATE_KEY=...` (required for `APP_MODE=live` and `EXECUTION_DRY_RUN=false`)
- `POLYMARKET_CHAIN_ID=137`
- `POLYMARKET_SIGNATURE_TYPE=0`
- `POLYMARKET_FUNDER_ADDRESS=...` (optional; set if your account model needs it)
- `POLYMARKET_API_KEY=...` / `POLYMARKET_API_SECRET=...` / `POLYMARKET_API_PASSPHRASE=...` (optional if API creds can be derived)
- `DEMO_MAKER_ENABLED=false`
- `DEMO_MAKER_NOTIONAL_USD=25`
- `DEMO_MAKER_HALF_SPREAD_BPS=40`
- `DEMO_MAKER_REQUOTE_BPS=12`
- `DEMO_MAKER_MIN_PRICE=0.03`
- `DEMO_MAKER_MAX_PRICE=0.97`

## Useful Logs
- `market_roll ...` when markets rotate
- `series_snapshot ... edge_bps=...` every loop
- `paper_submit ...` when a signal passes risk checks
- `maker_quote_sync ...` when maker quotes are posted/requoted
- `maker_shadow_fill ...` when a simulated maker fill is booked in dry-run mode
- `telemetry_snapshot ... pnl_realized=... pnl_unrealized=...` for paper PnL tracking

## Layout
- `src/coinbot_alpha/data`: market data and Polymarket resolver
- `src/coinbot_alpha/strategy`: strategy interfaces
- `src/coinbot_alpha/risk`: limits and kill switch
- `src/coinbot_alpha/execution`: paper execution
- `src/coinbot_alpha/telemetry`: logs, metrics, and audit
