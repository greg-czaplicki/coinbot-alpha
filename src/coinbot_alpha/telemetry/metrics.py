from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class PercentileSummary:
    p50: float
    p95: float
    p99: float


@dataclass(frozen=True)
class DashboardSnapshot:
    decision_to_submit_ms: PercentileSummary | None
    loops: int
    submits: int
    rejects: int
    reject_rate: float


class MetricsCollector:
    def __init__(self) -> None:
        self._decision_to_submit_ms: list[float] = []
        self._loops = 0
        self._submits = 0
        self._rejects = 0

    def record_loop(self) -> None:
        self._loops += 1

    def record_submit(self, latency_ms: float) -> None:
        self._submits += 1
        self._decision_to_submit_ms.append(latency_ms)

    def record_reject(self) -> None:
        self._rejects += 1

    def snapshot(self) -> DashboardSnapshot:
        denom = self._submits + self._rejects
        return DashboardSnapshot(
            decision_to_submit_ms=_summary(self._decision_to_submit_ms),
            loops=self._loops,
            submits=self._submits,
            rejects=self._rejects,
            reject_rate=(self._rejects / denom) if denom > 0 else 0.0,
        )


def _summary(values: list[float]) -> PercentileSummary | None:
    if not values:
        return None
    ordered = sorted(values)
    return PercentileSummary(
        p50=median(ordered),
        p95=_percentile(ordered, 95),
        p99=_percentile(ordered, 99),
    )


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = int(round((p / 100) * (len(sorted_values) - 1)))
    return sorted_values[max(0, min(index, len(sorted_values) - 1))]
