from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KillSwitchState:
    active: bool = False
    reason: str = ""


class KillSwitch:
    def __init__(self) -> None:
        self._state = KillSwitchState()

    def activate(self, reason: str) -> None:
        self._state.active = True
        self._state.reason = reason

    def deactivate(self) -> None:
        self._state.active = False
        self._state.reason = ""

    def check(self) -> KillSwitchState:
        return KillSwitchState(active=self._state.active, reason=self._state.reason)
