from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from coinbot_alpha.config import load_settings
from coinbot_alpha.data.binance import BinanceSpotClient
from coinbot_alpha.data.polymarket_clob import ActiveClobMarket, ClobSeriesResolver, ClobYesPriceFeed
from coinbot_alpha.execution.live import LiveExecutor
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


@dataclass
class MakerQuoteState:
    buy_order_id: str | None = None
    sell_order_id: str | None = None
    buy_price: Decimal | None = None
    sell_price: Decimal | None = None


def _clamp_decimal(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def _quote_price(raw_price: Decimal) -> Decimal:
    return raw_price.quantize(Decimal("0.001"))


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
    if cfg.app.mode == "live":
        executor = LiveExecutor(
            fee_bps=cfg.execution.fee_bps,
            dry_run=cfg.execution.dry_run,
            clob_api_url=cfg.execution.clob_api_url,
            private_key=cfg.execution.private_key,
            chain_id=cfg.execution.chain_id,
            signature_type=cfg.execution.signature_type,
            funder=cfg.execution.funder,
            api_key=cfg.execution.api_key,
            api_secret=cfg.execution.api_secret,
            api_passphrase=cfg.execution.api_passphrase,
            order_type=cfg.execution.order_type,
        )
    else:
        executor = PaperExecutor(cfg.execution.fee_bps)

    binance = BinanceSpotClient(cfg.demo.binance_symbol)
    binance.start()
    resolver = ClobSeriesResolver(cfg.demo.clob_api_url)

    tracked: dict[str, ActiveClobMarket] = {}
    yes_feeds: dict[str, ClobYesPriceFeed] = {}
    tracked_lock = threading.Lock()
    last_signal_ts: dict[str, float] = {}
    market_open_spot: dict[str, Decimal] = {}
    last_seen_slug: dict[str, str] = {}
    last_series_yes_price: dict[str, Decimal] = {}
    settled_slug: set[str] = set()
    position_open_ts: dict[str, float] = {}
    last_flat_ts: dict[str, float] = {}
    reentry_armed: dict[str, bool] = {}
    equity_peak: Decimal | None = None
    max_drawdown = Decimal("0")
    drawdown_soft_block = False
    drawdown_hard_triggered = False
    maker_mode = cfg.app.mode == "live" and cfg.demo.maker_enabled and cfg.execution.order_type == "GTC"
    maker_states: dict[str, MakerQuoteState] = {}
    maker_side_cooldown_until: dict[tuple[str, str], float] = {}

    log.info(
        "alpha_latency_demo_start mode=%s maker_mode=%s binance_symbol=%s enable_5m=%s enable_15m=%s series_5m=%s series_15m=%s edge_bps=%s",
        cfg.app.mode,
        maker_mode,
        cfg.demo.binance_symbol,
        cfg.demo.enable_5m,
        cfg.demo.enable_15m,
        cfg.demo.series_5m_prefix,
        cfg.demo.series_15m_prefix,
        cfg.demo.edge_threshold_bps,
    )

    def _cancel_maker_quotes(symbol: str) -> None:
        if not maker_mode:
            return
        state = maker_states.get(symbol)
        if state is None:
            return
        for order_id in (state.buy_order_id, state.sell_order_id):
            if not order_id:
                continue
            try:
                executor.cancel_order(order_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("maker_cancel_error symbol=%s order_id=%s err=%s", symbol, order_id, exc)
        maker_states[symbol] = MakerQuoteState()

    def _sync_maker_quote(symbol: str, side: Side, target_price: Decimal, quote_notional: Decimal) -> None:
        state = maker_states.setdefault(symbol, MakerQuoteState())
        cooldown_key = (symbol, side.value)
        cooldown_until = maker_side_cooldown_until.get(cooldown_key, 0.0)
        if now_s < cooldown_until:
            return
        order_id = state.buy_order_id if side == Side.BUY else state.sell_order_id
        old_price = state.buy_price if side == Side.BUY else state.sell_price
        requote_bps = Decimal(str(cfg.demo.maker_requote_bps))
        should_requote = (
            order_id is None
            or old_price is None
            or abs((target_price - old_price) * Decimal("10000")) >= requote_bps
        )
        if not should_requote:
            return

        if order_id is not None:
            try:
                canceled = executor.cancel_order(order_id)
                if not canceled:
                    if side == Side.BUY:
                        state.buy_order_id = None
                        state.buy_price = None
                    else:
                        state.sell_order_id = None
                        state.sell_price = None
                    cooldown_s = float(cfg.demo.maker_repost_cooldown_sec)
                    if cooldown_s > 0:
                        maker_side_cooldown_until[cooldown_key] = now_s + cooldown_s
                        log.info(
                            "maker_side_cooldown symbol=%s side=%s cooldown_sec=%s reason=matched_or_gone",
                            symbol,
                            side.value,
                            cooldown_s,
                        )
                    return
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "maker_cancel_error symbol=%s side=%s order_id=%s err=%s",
                    symbol,
                    side.value,
                    order_id,
                    exc,
                )
                cooldown_s = float(cfg.demo.maker_repost_cooldown_sec)
                if cooldown_s > 0:
                    maker_side_cooldown_until[cooldown_key] = now_s + cooldown_s
                return

        new_order_id = executor.place_limit_order(
            symbol=symbol,
            side=side,
            price=target_price,
            notional_usd=quote_notional,
        )
        if side == Side.BUY:
            state.buy_order_id = new_order_id
            state.buy_price = target_price
        else:
            state.sell_order_id = new_order_id
            state.sell_price = target_price
        log.info(
            "maker_quote_sync symbol=%s side=%s order_id=%s price=%s notional=%s",
            symbol,
            side.value,
            new_order_id,
            target_price,
            quote_notional,
        )

    def _simulate_maker_shadow_fill(
        *,
        symbol: str,
        series: str,
        market: ActiveClobMarket,
        side: Side,
        quote_price: Decimal,
        trigger_yes_price: Decimal,
    ) -> None:
        intent = OrderIntent(
            intent_id=f"maker_shadow_{uuid4()}",
            symbol=symbol,
            side=side,
            notional_usd=Decimal(str(cfg.demo.maker_notional_usd)),
            slippage_bps=0,
        )
        decision = risk.check_and_apply(intent)
        if not decision.allowed:
            metrics.record_reject()
            audit.write(
                {
                    "intent_id": intent.intent_id,
                    "series": series,
                    "slug": market.slug,
                    "side": side.value,
                    "notional_usd": str(intent.notional_usd),
                    "quote_price": str(quote_price),
                    "trigger_yes_price": str(trigger_yes_price),
                    "blocked_reason": decision.reason,
                    "status": "maker_shadow_reject",
                }
            )
            return

        fill = executor.submit(intent, quote_price)
        latency_ms = (time.perf_counter_ns() - loop_start_ns) / 1_000_000
        metrics.record_submit(latency_ms)
        audit.write(
            {
                "intent_id": intent.intent_id,
                "series": series,
                "slug": market.slug,
                "side": side.value,
                "notional_usd": str(intent.notional_usd),
                "quote_price": str(quote_price),
                "trigger_yes_price": str(trigger_yes_price),
                "fill_price": str(fill.fill_price),
                "qty": str(fill.qty),
                "position_qty_after": str(fill.position_qty_after),
                "avg_entry_price_after": str(fill.avg_entry_price_after),
                "realized_pnl_delta": str(fill.realized_pnl_delta),
                "realized_pnl_total": str(fill.realized_pnl_total),
                "submit_latency_ms": round(latency_ms, 3),
                "status": "maker_shadow_fill",
            }
        )
        log.info(
            "maker_shadow_fill series=%s slug=%s symbol=%s side=%s quote_px=%s trigger_yes_px=%s qty=%s pos_qty_after=%s",
            series,
            market.slug,
            symbol,
            side.value,
            quote_price,
            trigger_yes_price,
            fill.qty,
            fill.position_qty_after,
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
            if cfg.demo.enable_5m:
                _refresh_market("5m", cfg.demo.seed_5m_slug)
            if cfg.demo.enable_15m:
                _refresh_market("15m", cfg.demo.seed_15m_slug)
            time.sleep(cfg.demo.market_refresh_sec)

    thread = threading.Thread(target=_resolver_loop, name="market_resolver", daemon=True)
    thread.start()

    while True:
        loop_start_ns = time.perf_counter_ns()
        metrics.record_loop()

        now_s = time.time()
        edge_snapshot: dict[str, Decimal] = {}
        entry_distance_snapshot: dict[str, Decimal] = {}

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
            executor.bind_symbol(symbol, market.yes_token_id)
            prev_yes_price = last_series_yes_price.get(series)
            last_series_yes_price[series] = yes_price

            prev_slug = last_seen_slug.get(series)
            if prev_slug is not None and prev_slug != market.slug:
                _cancel_maker_quotes(symbol)
                close_px = last_series_yes_price.get(series, yes_price)
                flatten_fill = executor.flatten_symbol(symbol, close_px)
                if flatten_fill is not None:
                    position_open_ts.pop(symbol, None)
                    last_flat_ts[series] = now_s
                    reentry_armed[series] = False
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
                # A new contract window should always start re-armed.
                reentry_armed[series] = True

            last_seen_slug[series] = market.slug

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
                _cancel_maker_quotes(symbol)
                flatten_fill = executor.flatten_symbol(symbol, yes_price)
                settled_slug.add(market.slug)
                if flatten_fill is not None:
                    position_open_ts.pop(symbol, None)
                    last_flat_ts[series] = now_s
                    reentry_armed[series] = False
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

            edge = _edge_bps(model_p, yes_price)
            edge_snapshot[series] = edge
            entry_distance_snapshot[series] = Decimal(str(cfg.demo.edge_threshold_bps)) - abs(edge)
            if maker_mode:
                if kill.check().active or drawdown_soft_block:
                    _cancel_maker_quotes(symbol)
                    continue
                if cfg.demo.maker_notional_usd > cfg.risk.max_notional_per_symbol_usd:
                    log.warning(
                        "maker_disabled_for_symbol symbol=%s reason=notional_exceeds_risk_limit notional=%s risk_limit=%s",
                        symbol,
                        cfg.demo.maker_notional_usd,
                        cfg.risk.max_notional_per_symbol_usd,
                    )
                    _cancel_maker_quotes(symbol)
                    continue

                fair_yes = _clamp_decimal(
                    model_p,
                    Decimal(str(cfg.demo.maker_min_price)),
                    Decimal(str(cfg.demo.maker_max_price)),
                )
                half_spread = Decimal(str(cfg.demo.maker_half_spread_bps)) / Decimal("10000")
                bid_px = _quote_price(
                    _clamp_decimal(
                        fair_yes - half_spread,
                        Decimal(str(cfg.demo.maker_min_price)),
                        Decimal(str(cfg.demo.maker_max_price)),
                    )
                )
                ask_px = _quote_price(
                    _clamp_decimal(
                        fair_yes + half_spread,
                        Decimal(str(cfg.demo.maker_min_price)),
                        Decimal(str(cfg.demo.maker_max_price)),
                    )
                )
                if ask_px <= bid_px:
                    ask_px = bid_px + Decimal("0.001")
                    ask_px = _clamp_decimal(
                        ask_px,
                        Decimal(str(cfg.demo.maker_min_price)),
                        Decimal(str(cfg.demo.maker_max_price)),
                    )

                quote_notional = Decimal(str(cfg.demo.maker_notional_usd))
                max_abs_qty = Decimal(str(cfg.demo.maker_max_abs_position_qty))
                pos_qty = executor.symbol_position_qty(symbol)
                allow_buy_quote = pos_qty < max_abs_qty
                allow_sell_quote = pos_qty > -max_abs_qty
                if cfg.demo.maker_one_sided_by_edge:
                    if edge >= 0:
                        allow_sell_quote = False
                    else:
                        allow_buy_quote = False
                try:
                    if allow_buy_quote:
                        _sync_maker_quote(
                            symbol=symbol,
                            side=Side.BUY,
                            target_price=bid_px,
                            quote_notional=quote_notional,
                        )
                    else:
                        state = maker_states.setdefault(symbol, MakerQuoteState())
                        if state.buy_order_id is not None:
                            executor.cancel_order(state.buy_order_id)
                            state.buy_order_id = None
                            state.buy_price = None

                    if allow_sell_quote:
                        _sync_maker_quote(
                            symbol=symbol,
                            side=Side.SELL,
                            target_price=ask_px,
                            quote_notional=quote_notional,
                        )
                    else:
                        state = maker_states.setdefault(symbol, MakerQuoteState())
                        if state.sell_order_id is not None:
                            executor.cancel_order(state.sell_order_id)
                            state.sell_order_id = None
                            state.sell_price = None
                except Exception as exc:  # noqa: BLE001
                    log.warning("maker_quote_error symbol=%s series=%s err=%s", symbol, series, exc)

                # Shadow-only fill approximation: if market prints through our quote,
                # count it as a fill at the quoted maker price.
                if cfg.execution.dry_run:
                    state = maker_states.get(symbol)
                    if state is not None:
                        if (
                            state.buy_order_id is not None
                            and state.buy_price is not None
                            and prev_yes_price is not None
                            and prev_yes_price > state.buy_price
                            and yes_price <= state.buy_price
                        ):
                            filled_order_id = state.buy_order_id
                            _simulate_maker_shadow_fill(
                                symbol=symbol,
                                series=series,
                                market=market,
                                side=Side.BUY,
                                quote_price=state.buy_price,
                                trigger_yes_price=yes_price,
                            )
                            executor.ack_order_filled(filled_order_id)
                            state.buy_order_id = None
                            state.buy_price = None

                        if (
                            state.sell_order_id is not None
                            and state.sell_price is not None
                            and prev_yes_price is not None
                            and prev_yes_price < state.sell_price
                            and yes_price >= state.sell_price
                        ):
                            filled_order_id = state.sell_order_id
                            _simulate_maker_shadow_fill(
                                symbol=symbol,
                                series=series,
                                market=market,
                                side=Side.SELL,
                                quote_price=state.sell_price,
                                trigger_yes_price=yes_price,
                            )
                            executor.ack_order_filled(filled_order_id)
                            state.sell_order_id = None
                            state.sell_price = None
                continue

            min_hold_sec = cfg.demo.min_hold_sec_5m if series == "5m" else cfg.demo.min_hold_sec_15m
            max_hold_sec = cfg.demo.max_hold_sec_5m if series == "5m" else cfg.demo.max_hold_sec_15m
            if executor.has_open_position(symbol):
                held_s = now_s - position_open_ts.get(symbol, now_s)
                if held_s >= max_hold_sec:
                    flatten_fill = executor.flatten_symbol(symbol, yes_price)
                    if flatten_fill is not None:
                        position_open_ts.pop(symbol, None)
                        last_flat_ts[series] = now_s
                        reentry_armed[series] = False
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
                                "status": "flatten_max_hold",
                                "held_sec": round(held_s, 3),
                            }
                        )
                        log.info(
                            "series_settle series=%s slug=%s reason=max_hold held_s=%.1f px=%s realized_delta=%s",
                            series,
                            market.slug,
                            held_s,
                            yes_price,
                            flatten_fill.realized_pnl_delta,
                        )
                    continue

                if held_s >= min_hold_sec:
                    if abs(edge) <= Decimal(str(cfg.demo.exit_edge_bps)):
                        flatten_fill = executor.flatten_symbol(symbol, yes_price)
                        if flatten_fill is not None:
                            position_open_ts.pop(symbol, None)
                            last_flat_ts[series] = now_s
                            reentry_armed[series] = False
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
                                    "status": "flatten_edge_compress",
                                    "edge_bps": round(float(edge), 2),
                                    "held_sec": round(held_s, 3),
                                }
                            )
                            log.info(
                                "series_settle series=%s slug=%s reason=edge_compress held_s=%.1f edge_bps=%s px=%s realized_delta=%s",
                                series,
                                market.slug,
                                held_s,
                                round(float(edge), 2),
                                yes_price,
                                flatten_fill.realized_pnl_delta,
                            )
                        continue

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
                            last_flat_ts[series] = now_s
                            reentry_armed[series] = False
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

            if executor.has_open_position(symbol):
                # Single-position lifecycle per series; only re-enter after flatten.
                continue
            if drawdown_soft_block:
                metrics.record_reject()
                audit.write({"series": series, "slug": market.slug, "blocked_reason": "max_drawdown_soft"})
                continue

            if kill.check().active:
                metrics.record_reject()
                audit.write({"series": series, "slug": market.slug, "blocked_reason": kill.check().reason})
                continue

            last_for_series = last_signal_ts.get(series, 0.0)
            if now_s - last_for_series < cfg.demo.signal_cooldown_sec:
                continue
            last_flat = last_flat_ts.get(series, 0.0)
            if now_s - last_flat < cfg.demo.signal_cooldown_sec:
                continue
            if not reentry_armed.get(series, True):
                if abs(edge) <= Decimal(str(cfg.demo.reentry_arm_bps)):
                    reentry_armed[series] = True
                else:
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
            reentry_armed[series] = False
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
        if cfg.app.mode == "live" and not cfg.execution.dry_run and hasattr(executor, "reconcile_live_fills"):
            try:
                live_fills = executor.reconcile_live_fills()
            except Exception as exc:  # noqa: BLE001
                log.warning("live_fill_reconcile_error err=%s", exc)
                live_fills = []
            for event in live_fills:
                series = event.symbol.split("_")[-1] if "_" in event.symbol else "unknown"
                slug = tracked_snapshot.get(series).slug if series in tracked_snapshot else "unknown"
                audit.write(
                    {
                        "intent_id": event.paper_fill.intent_id,
                        "series": series,
                        "slug": slug,
                        "order_id": event.order_id,
                        "symbol": event.symbol,
                        "side": event.side,
                        "fill_price": str(event.fill_price),
                        "qty": str(event.fill_qty),
                        "position_qty_after": str(event.paper_fill.position_qty_after),
                        "avg_entry_price_after": str(event.paper_fill.avg_entry_price_after),
                        "realized_pnl_delta": str(event.paper_fill.realized_pnl_delta),
                        "realized_pnl_total": str(event.paper_fill.realized_pnl_total),
                        "status": "live_fill_reconciled",
                    }
                )
                log.info(
                    "live_fill_reconciled order_id=%s symbol=%s side=%s qty=%s px=%s pos_qty_after=%s",
                    event.order_id,
                    event.symbol,
                    event.side,
                    event.fill_qty,
                    event.fill_price,
                    event.paper_fill.position_qty_after,
                )

        snap = metrics.snapshot()
        marks = {}
        for series, market in tracked_snapshot.items():
            with tracked_lock:
                feed = yes_feeds.get(series)
            yes_px = feed.latest_price() if feed is not None else None
            marks[f"btc_updown_{series}"] = yes_px if yes_px is not None else market.yes_price
        ledger = executor.snapshot(marks)
        equity = ledger.realized_pnl_total + ledger.unrealized_pnl_total
        if equity_peak is None or equity > equity_peak:
            equity_peak = equity
        drawdown = equity - equity_peak
        if drawdown < max_drawdown:
            max_drawdown = drawdown

        soft_limit = Decimal(str(cfg.demo.max_drawdown_soft_usd))
        hard_limit = Decimal(str(cfg.demo.max_drawdown_hard_usd))
        drawdown_soft_block = soft_limit > 0 and drawdown <= -soft_limit
        hard_breach = hard_limit > 0 and drawdown <= -hard_limit

        if hard_breach and not drawdown_hard_triggered:
            for series in sorted(tracked_snapshot.keys()):
                symbol = f"btc_updown_{series}"
                _cancel_maker_quotes(symbol)
                close_px = marks.get(symbol)
                if close_px is None:
                    continue
                flatten_fill = executor.flatten_symbol(symbol, close_px)
                if flatten_fill is None:
                    continue
                position_open_ts.pop(symbol, None)
                last_flat_ts[series] = now_s
                reentry_armed[series] = False
                audit.write(
                    {
                        "intent_id": flatten_fill.intent_id,
                        "series": series,
                        "slug": tracked_snapshot[series].slug,
                        "side": flatten_fill.side,
                        "notional_usd": str(flatten_fill.notional_usd),
                        "yes_price": str(close_px),
                        "fill_price": str(flatten_fill.fill_price),
                        "qty": str(flatten_fill.qty),
                        "position_qty_after": str(flatten_fill.position_qty_after),
                        "avg_entry_price_after": str(flatten_fill.avg_entry_price_after),
                        "realized_pnl_delta": str(flatten_fill.realized_pnl_delta),
                        "realized_pnl_total": str(flatten_fill.realized_pnl_total),
                        "status": "flatten_drawdown_hard",
                    }
                )
                log.info(
                    "series_settle series=%s slug=%s reason=drawdown_hard px=%s realized_delta=%s",
                    series,
                    tracked_snapshot[series].slug,
                    close_px,
                    flatten_fill.realized_pnl_delta,
                )
            drawdown_hard_triggered = True
            drawdown_soft_block = True
            kill.activate("max_drawdown_hard")
            ledger = executor.snapshot(marks)
            equity = ledger.realized_pnl_total + ledger.unrealized_pnl_total
            if equity_peak is None or equity > equity_peak:
                equity_peak = equity
            drawdown = equity - equity_peak
            if drawdown < max_drawdown:
                max_drawdown = drawdown

        alert_state = alerts.evaluate(snap)
        if alert_state.reject_spike_breach:
            kill.activate("reject_spike")

        edge_status_parts = []
        for series in sorted(tracked_snapshot.keys()):
            edge = edge_snapshot.get(series)
            dist = entry_distance_snapshot.get(series)
            if edge is None or dist is None:
                continue
            edge_status_parts.append(
                f"{series}:edge_bps={round(float(edge),2)} to_entry_bps={round(float(dist),2)}"
            )
        edge_status = "; ".join(edge_status_parts) if edge_status_parts else "na"

        log.info(
            "telemetry_snapshot loops=%s submits=%s rejects=%s reject_rate=%.4f p95_submit_ms=%s kill_switch=%s tracked=%s pnl_realized=%s pnl_unrealized=%s equity=%s equity_peak=%s drawdown=%s max_drawdown=%s drawdown_soft_block=%s drawdown_hard_triggered=%s open_positions=%s trades_total=%s fee_paid_total=%s entry_edge_bps=%s exit_edge_bps=%s reentry_arm_bps=%s edge_status=%s",
            snap.loops,
            snap.submits,
            snap.rejects,
            snap.reject_rate,
            (snap.decision_to_submit_ms.p95 if snap.decision_to_submit_ms else None),
            kill.check().active,
            sorted(tracked_snapshot.keys()),
            ledger.realized_pnl_total,
            ledger.unrealized_pnl_total,
            equity,
            equity_peak,
            drawdown,
            max_drawdown,
            drawdown_soft_block,
            drawdown_hard_triggered,
            ledger.open_positions,
            ledger.trades_total,
            ledger.fee_paid_total,
            cfg.demo.edge_threshold_bps,
            cfg.demo.exit_edge_bps,
            cfg.demo.reentry_arm_bps,
            edge_status,
        )

        time.sleep(cfg.app.loop_interval_ms / 1000)


if __name__ == "__main__":
    main()
