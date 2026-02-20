from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TradeAuditConfig:
    out_dir: str = "runs/telemetry"
    jsonl_name: str = "trade_audit.jsonl"


class TradeAuditLogger:
    def __init__(self, cfg: TradeAuditConfig = TradeAuditConfig()) -> None:
        self._path = Path(cfg.out_dir) / cfg.jsonl_name
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        body = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(body, separators=(",", ":"), default=str) + "\n")
