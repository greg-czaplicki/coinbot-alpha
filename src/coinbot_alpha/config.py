from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    def load_dotenv() -> None:
        return None


@dataclass(frozen=True)
class AppConfig:
    mode: str = "paper"
    loop_interval_ms: int = 1000


@dataclass(frozen=True)
class RiskConfig:
    max_notional_per_symbol_usd: float = 1000.0
    max_daily_notional_usd: float = 10000.0


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool = True
    slippage_bps: int = 10


@dataclass(frozen=True)
class DemoConfig:
    enabled: bool = True
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    binance_symbol: str = "BTCUSDT"
    series_5m_prefix: str = "btc-updown-5m"
    series_15m_prefix: str = "btc-updown-15m"
    market_refresh_sec: int = 30
    edge_threshold_bps: int = 800
    signal_notional_usd: float = 25.0
    model_sigma_annual: float = 0.8
    signal_cooldown_sec: int = 20


@dataclass(frozen=True)
class Settings:
    app: AppConfig
    risk: RiskConfig
    execution: ExecutionConfig
    demo: DemoConfig


def _get_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    load_dotenv()

    settings = Settings(
        app=AppConfig(
            mode=os.getenv("APP_MODE", AppConfig.mode),
            loop_interval_ms=int(os.getenv("APP_LOOP_INTERVAL_MS", AppConfig.loop_interval_ms)),
        ),
        risk=RiskConfig(
            max_notional_per_symbol_usd=float(
                os.getenv("RISK_MAX_NOTIONAL_PER_SYMBOL_USD", RiskConfig.max_notional_per_symbol_usd)
            ),
            max_daily_notional_usd=float(
                os.getenv("RISK_MAX_DAILY_NOTIONAL_USD", RiskConfig.max_daily_notional_usd)
            ),
        ),
        execution=ExecutionConfig(
            dry_run=_get_bool("EXECUTION_DRY_RUN", ExecutionConfig.dry_run),
            slippage_bps=int(os.getenv("EXECUTION_SLIPPAGE_BPS", ExecutionConfig.slippage_bps)),
        ),
        demo=DemoConfig(
            enabled=_get_bool("DEMO_ENABLED", DemoConfig.enabled),
            gamma_api_url=os.getenv("DEMO_GAMMA_API_URL", DemoConfig.gamma_api_url),
            binance_symbol=os.getenv("DEMO_BINANCE_SYMBOL", DemoConfig.binance_symbol),
            series_5m_prefix=os.getenv("DEMO_SERIES_5M_PREFIX", DemoConfig.series_5m_prefix),
            series_15m_prefix=os.getenv("DEMO_SERIES_15M_PREFIX", DemoConfig.series_15m_prefix),
            market_refresh_sec=int(os.getenv("DEMO_MARKET_REFRESH_SEC", DemoConfig.market_refresh_sec)),
            edge_threshold_bps=int(os.getenv("DEMO_EDGE_THRESHOLD_BPS", DemoConfig.edge_threshold_bps)),
            signal_notional_usd=float(os.getenv("DEMO_SIGNAL_NOTIONAL_USD", DemoConfig.signal_notional_usd)),
            model_sigma_annual=float(os.getenv("DEMO_MODEL_SIGMA_ANNUAL", DemoConfig.model_sigma_annual)),
            signal_cooldown_sec=int(os.getenv("DEMO_SIGNAL_COOLDOWN_SEC", DemoConfig.signal_cooldown_sec)),
        ),
    )
    validate_settings(settings)
    return settings


def validate_settings(settings: Settings) -> None:
    if settings.app.mode not in {"paper", "live"}:
        raise ValueError("APP_MODE must be paper|live")
    if settings.app.loop_interval_ms <= 0:
        raise ValueError("APP_LOOP_INTERVAL_MS must be > 0")
    if settings.risk.max_notional_per_symbol_usd <= 0:
        raise ValueError("RISK_MAX_NOTIONAL_PER_SYMBOL_USD must be > 0")
    if settings.risk.max_daily_notional_usd <= 0:
        raise ValueError("RISK_MAX_DAILY_NOTIONAL_USD must be > 0")
    if settings.execution.slippage_bps < 0:
        raise ValueError("EXECUTION_SLIPPAGE_BPS must be >= 0")
    if settings.demo.market_refresh_sec <= 0:
        raise ValueError("DEMO_MARKET_REFRESH_SEC must be > 0")
    if settings.demo.edge_threshold_bps <= 0:
        raise ValueError("DEMO_EDGE_THRESHOLD_BPS must be > 0")
    if settings.demo.signal_notional_usd <= 0:
        raise ValueError("DEMO_SIGNAL_NOTIONAL_USD must be > 0")
    if settings.demo.model_sigma_annual <= 0:
        raise ValueError("DEMO_MODEL_SIGMA_ANNUAL must be > 0")
    if settings.demo.signal_cooldown_sec < 0:
        raise ValueError("DEMO_SIGNAL_COOLDOWN_SEC must be >= 0")
