from __future__ import annotations

from dataclasses import dataclass

from coinbot_alpha.telemetry.metrics import DashboardSnapshot


@dataclass(frozen=True)
class AlertThresholds:
    max_reject_rate: float = 0.1
    max_p95_submit_latency_ms: int = 1200


@dataclass(frozen=True)
class AlertState:
    reject_spike_breach: bool
    p95_latency_breach: bool


class AlertEvaluator:
    def __init__(self, thresholds: AlertThresholds) -> None:
        self._thresholds = thresholds

    def evaluate(self, snapshot: DashboardSnapshot) -> AlertState:
        p95 = snapshot.decision_to_submit_ms.p95 if snapshot.decision_to_submit_ms else 0
        return AlertState(
            reject_spike_breach=snapshot.reject_rate > self._thresholds.max_reject_rate,
            p95_latency_breach=p95 > self._thresholds.max_p95_submit_latency_ms,
        )
