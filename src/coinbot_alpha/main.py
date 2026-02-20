from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from coinbot_alpha.config import load_settings
from coinbot_alpha.data.binance import BinanceSpotClient
from coinbot_alpha.data.polymarket_clob import ActiveClobMarket, ClobSeriesResolver, ClobYesPriceFeed
from coinbot_alpha.execution.paper import PaperExecutor
from coinbot_alpha.risk.kill_switch import KillSwitch
from coinbot_alpha.risk.limits import RiskEngine, RiskLimits
from coinbot_alpha.schemas import OrderIntent, Side
from coinbot_alpha.telemetry.alerts import AlertEvaluator, AlertThresholds
from coinbot_alpha.telemetry.audit import TradeAuditLogger
from coinbot_alpha.telemetry.logging import setup_logging
from coinbot_alpha.telemetry.metrics import MetricsCollector


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _model_prob_up(spot: Decimal, strike: Decimal, time_to_expiry_s: float, sigma_annual: float) -> Decimal:
    if time_to_expiry_s <= 0:
        return Decimal("1") if spot > strike else Decimal("0")
    t_years = max(time_to_expiry_s, 1.0) / (365.0 * 24.0 * 3600.0)
    vol_t = sigma_annual * math.sqrt(t_years)
    if vol_t <= 0:
        return Decimal("0.5")
    z = math.log(float(strike / spot)) / vol_t
    prob = 1.0 - _normal_cdf(z)
    return Decimal(str(max(0.0, min(1.0, prob))))


def _edge_bps(model_prob: Decimal, yes_price: Decimal) -> Decimal:
    return (model_prob - yes_price) * Decimal("10000")


def _maybe_signal_side(edge_bps: Decimal, threshold_bps: int) -> Side | None:
    if edge_bps >= Decimal(threshold_bps):
        return Side.BUY
    if edge_bps <= Decimal(-threshold_bps):
        return Side.SELL
    return None


def _is_updown_market(market: ActiveClobMarket) -> bool:
    slug = market.slug.lower()
    q = market.question.lower()
    return "updown" in slug or "up or down" in q


def main() -> None:
    setup_logging()
    log = logging.getLogger("coinbot_alpha.main")
    cfg = load_settings()

    metrics = MetricsCollector()
    alerts = AlertEvaluator(AlertThresholds())
    audit = TradeAuditLogger()

    risk = RiskEngine(
        RiskLimits(
            max_notional_per_symbol_usd=Decimal(str(cfg.risk.max_notional_per_symbol_usd)),
            max_daily_notional_usd=Decimal(str(cfg.risk.max_daily_notional_usd)),
        )
    )
    kill = KillSwitch()
    executor = PaperExecutor()

    binance = BinanceSpotClient(cfg.demo.binance_symbol)
    resolver = ClobSeriesResolver(cfg.demo.clob_api_url)

    tracked: dict[str, ActiveClobMarket] = {}
    yes_feeds: dict[str, ClobYesPriceFeed] = {}
    tracked_lock = threading.Lock()
    last_signal_ts: dict[str, float] = {}
    market_open_spot: dict[str, Decimal] = {}
    last_seen_slug: dict[str, str] = {}
    last_series_yes_price: dict[str, Decimal] = {}
    settled_slug: set[str] = set()
    traded_slug: dict[str, str] = {}
    position_open_ts: dict[str, float] = {}

    log.info(
        "alpha_latency_demo_start mode=%s binance_symbol=%s series_5m=%s series_15m=%s edge_bps=%s",
        cfg.app.mode,
        cfg.demo.binance_symbol,
        cfg.demo.series_5m_prefix,
        cfg.demo.series_15m_prefix,
        cfg.demo.edge_threshold_bps,
    )

    def _refresh_market(series: str, seed_slug: str) -> None:
        try:
            market = resolver.resolve_from_seed(seed_slug)
            if market is None:
                return
            with tracked_lock:
                prev = tracked.get(series)
                tracked[series] = market
            if prev is None or prev.slug != market.slug:
                with tracked_lock:
                    prev_feed = yes_feeds.get(series)
                    if prev_feed is not None:
                        prev_feed.stop()
                    feed = ClobYesPriceFeed(cfg.demo.clob_ws_url, market.yes_token_id, market.yes_price)
                    feed.start()
                    yes_feeds[series] = feed
                log.info(
                    "market_roll series=%s slug=%s condition_id=%s yes_token=%s no_token=%s end=%s",
                    series,
                    market.slug,
                    market.condition_id,
                    market.yes_token_id,
                    market.no_token_id,
                    market.end_ts.isoformat(),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("market_refresh_error series=%s seed_slug=%s err=%s", series, seed_slug, exc)

    def _resolver_loop() -> None:
        while True:
            _refresh_market("5m", cfg.demo.seed_5m_slug)
            _refresh_market("15m", cfg.demo.seed_15m_slug)
            time.sleep(cfg.demo.market_refresh_sec)

    thread = threading.Thread(target=_resolver_loop, name="market_resolver", daemon=True)
    thread.start()

    while True:
        loop_start_ns = time.perf_counter_ns()
        metrics.record_loop()

        now_s = time.time()

        try:
            spot = binance.get_price()
        except Exception as exc:  # noqa: BLE001
            log.warning("binance_price_error symbol=%s err=%s", cfg.demo.binance_symbol, exc)
            time.sleep(cfg.app.loop_interval_ms / 1000)
            continue

        with tracked_lock:
            tracked_snapshot = dict(tracked)

        for series, market in tracked_snapshot.items():
            now = datetime.now(timezone.utc)
            tte_s = max(0.0, (market.end_ts - now).total_seconds())
            with tracked_lock:
                feed = yes_feeds.get(series)
            yes_px = feed.latest_price() if feed is not None else None
            yes_price = yes_px if yes_px is not None else market.yes_price
            symbol = f"btc_updown_{series}"

            prev_slug = last_seen_slug.get(series)
            if prev_slug is not None and prev_slug != market.slug:
                close_px = last_series_yes_price.get(series, yes_price)
                flatten_fill = executor.flatten_symbol(symbol, close_px)
                if flatten_fill is not None:
                    position_open_ts.pop(symbol, None)
                    audit.write(
                        {
                            "intent_id": flatten_fill.intent_id,
                            "series": series,
                            "slug": prev_slug,
                            "side": flatten_fill.side,
                            "notional_usd": str(flatten_fill.notional_usd),
                            "yes_price": str(close_px),
                            "fill_price": str(flatten_fill.fill_price),
                            "qty": str(flatten_fill.qty),
                            "position_qty_after": str(flatten_fill.position_qty_after),
                            "avg_entry_price_after": str(flatten_fill.avg_entry_price_after),
                            "realized_pnl_delta": str(flatten_fill.realized_pnl_delta),
                            "realized_pnl_total": str(flatten_fill.realized_pnl_total),
                            "status": "flatten_roll",
                        }
                    )
                    log.info(
                        "series_settle series=%s from_slug=%s to_slug=%s reason=roll px=%s realized_delta=%s",
                        series,
                        prev_slug,
                        market.slug,
                        close_px,
                        flatten_fill.realized_pnl_delta,
                    )
                market_open_spot.pop(prev_slug, None)

            last_seen_slug[series] = market.slug
            last_series_yes_price[series] = yes_price

            model_strike = market.strike_price
            if model_strike is None and _is_updown_market(market):
                open_spot = market_open_spot.get(market.slug)
                if open_spot is None:
                    open_spot = spot
                    market_open_spot[market.slug] = open_spot
                model_strike = open_spot

            if model_strike is None:
                log.info(
                    "series_snapshot series=%s slug=%s spot=%s yes_px=%s strike=na tte_s=%.1f note=no_strike_parse",
                    series,
                    market.slug,
                    spot,
                    yes_price,
                    tte_s,
                )
                continue

            model_p = _model_prob_up(spot, model_strike, tte_s, cfg.demo.model_sigma_annual)
            if tte_s <= 0 and market.slug not in settled_slug:
                flatten_fill = executor.flatten_symbol(symbol, yes_price)
                settled_slug.add(market.slug)
                if flatten_fill is not None:
                    position_open_ts.pop(symbol, None)
                    audit.write(
                        {
                            "intent_id": flatten_fill.intent_id,
                            "series": series,
                            "slug": market.slug,
                            "side": flatten_fill.side,
                            "notional_usd": str(flatten_fill.notional_usd),
                            "yes_price": str(yes_price),
                            "fill_price": str(flatten_fill.fill_price),
                            "qty": str(flatten_fill.qty),
                            "position_qty_after": str(flatten_fill.position_qty_after),
                            "avg_entry_price_after": str(flatten_fill.avg_entry_price_after),
                            "realized_pnl_delta": str(flatten_fill.realized_pnl_delta),
                            "realized_pnl_total": str(flatten_fill.realized_pnl_total),
                            "status": "flatten_expiry",
                        }
                    )
                    log.info(
                        "series_settle series=%s slug=%s reason=expiry px=%s realized_delta=%s",
                        series,
                        market.slug,
                        yes_price,
                        flatten_fill.realized_pnl_delta,
                    )
                continue
            if tte_s <= 0:
                # Never trade expired markets.
                continue

            min_hold_sec = cfg.demo.min_hold_sec_5m if series == "5m" else cfg.demo.min_hold_sec_15m
            if executor.has_open_position(symbol):
                held_s = now_s - position_open_ts.get(symbol, now_s)
                if held_s >= min_hold_sec:
                    unrealized = executor.symbol_unrealized(symbol, yes_price)
                    stop_loss = Decimal(str(cfg.demo.pos_stop_loss_usd))
                    take_profit = Decimal(str(cfg.demo.pos_take_profit_usd))
                    settle_reason = ""
                    if unrealized <= -stop_loss:
                        settle_reason = "stop_loss"
                    elif unrealized >= take_profit:
                        settle_reason = "take_profit"

                    if settle_reason:
                        flatten_fill = executor.flatten_symbol(symbol, yes_price)
                        if flatten_fill is not None:
                            position_open_ts.pop(symbol, None)
                            audit.write(
                                {
                                    "intent_id": flatten_fill.intent_id,
                                    "series": series,
                                    "slug": market.slug,
                                    "side": flatten_fill.side,
                                    "notional_usd": str(flatten_fill.notional_usd),
                                    "yes_price": str(yes_price),
                                    "fill_price": str(flatten_fill.fill_price),
                                    "qty": str(flatten_fill.qty),
                                    "position_qty_after": str(flatten_fill.position_qty_after),
                                    "avg_entry_price_after": str(flatten_fill.avg_entry_price_after),
                                    "realized_pnl_delta": str(flatten_fill.realized_pnl_delta),
                                    "realized_pnl_total": str(flatten_fill.realized_pnl_total),
                                    "status": f"flatten_{settle_reason}",
                                    "unrealized_pnl_at_exit": str(unrealized),
                                    "held_sec": round(held_s, 3),
                                }
                            )
                            log.info(
                                "series_settle series=%s slug=%s reason=%s held_s=%.1f px=%s unrealized=%s realized_delta=%s",
                                series,
                                market.slug,
                                settle_reason,
                                held_s,
                                yes_price,
                                unrealized,
                                flatten_fill.realized_pnl_delta,
                            )
                        continue

            edge = _edge_bps(model_p, yes_price)
            side = _maybe_signal_side(edge, cfg.demo.edge_threshold_bps)

            log.info(
                "series_snapshot series=%s slug=%s spot=%s strike=%s yes_px=%s model_yes=%s edge_bps=%s tte_s=%.1f",
                series,
                market.slug,
                spot,
                model_strike,
                yes_price,
                model_p,
                round(float(edge), 2),
                tte_s,
            )

            if side is None:
                continue

            if kill.check().active:
                metrics.record_reject()
                audit.write({"series": series, "slug": market.slug, "blocked_reason": kill.check().reason})
                continue

            last_for_series = last_signal_ts.get(series, 0.0)
            if now_s - last_for_series < cfg.demo.signal_cooldown_sec:
                continue
            if traded_slug.get(series) == market.slug:
                # Prevent repeated stacking in the same contract window.
                continue

            intent = OrderIntent(
                intent_id=str(uuid4()),
                symbol=symbol,
                side=side,
                notional_usd=Decimal(str(cfg.demo.signal_notional_usd)),
                slippage_bps=cfg.execution.slippage_bps,
            )

            decision = risk.check_and_apply(intent)
            if not decision.allowed:
                metrics.record_reject()
                audit.write(
                    {
                        "intent_id": intent.intent_id,
                        "series": series,
                        "slug": market.slug,
                        "edge_bps": round(float(edge), 2),
                        "blocked_reason": decision.reason,
                    }
                )
                continue

            fill = executor.submit(intent, yes_price)
            last_signal_ts[series] = now_s
            traded_slug[series] = market.slug
            if fill.position_qty_after != 0 and symbol not in position_open_ts:
                position_open_ts[symbol] = now_s
            elif fill.position_qty_after == 0:
                position_open_ts.pop(symbol, None)
            latency_ms = (time.perf_counter_ns() - loop_start_ns) / 1_000_000
            metrics.record_submit(latency_ms)
            audit.write(
                {
                    "intent_id": intent.intent_id,
                    "series": series,
                    "slug": market.slug,
                    "side": intent.side.value,
                    "notional_usd": str(intent.notional_usd),
                    "spot": str(spot),
                    "strike": str(model_strike),
                    "yes_price": str(yes_price),
                    "fill_price": str(fill.fill_price),
                    "qty": str(fill.qty),
                    "position_qty_after": str(fill.position_qty_after),
                    "avg_entry_price_after": str(fill.avg_entry_price_after),
                    "realized_pnl_delta": str(fill.realized_pnl_delta),
                    "realized_pnl_total": str(fill.realized_pnl_total),
                    "model_yes": str(model_p),
                    "edge_bps": round(float(edge), 2),
                    "submit_latency_ms": round(latency_ms, 3),
                    "status": "submitted",
                }
            )

        snap = metrics.snapshot()
        marks = {}
        for series, market in tracked_snapshot.items():
            with tracked_lock:
                feed = yes_feeds.get(series)
            yes_px = feed.latest_price() if feed is not None else None
            marks[f"btc_updown_{series}"] = yes_px if yes_px is not None else market.yes_price
        ledger = executor.snapshot(marks)
        alert_state = alerts.evaluate(snap)
        if alert_state.reject_spike_breach:
            kill.activate("reject_spike")

        log.info(
            "telemetry_snapshot loops=%s submits=%s rejects=%s reject_rate=%.4f p95_submit_ms=%s kill_switch=%s tracked=%s pnl_realized=%s pnl_unrealized=%s open_positions=%s",
            snap.loops,
            snap.submits,
            snap.rejects,
            snap.reject_rate,
            (snap.decision_to_submit_ms.p95 if snap.decision_to_submit_ms else None),
            kill.check().active,
            sorted(tracked_snapshot.keys()),
            ledger.realized_pnl_total,
            ledger.unrealized_pnl_total,
            ledger.open_positions,
        )

        time.sleep(cfg.app.loop_interval_ms / 1000)


if __name__ == "__main__":
    main()
