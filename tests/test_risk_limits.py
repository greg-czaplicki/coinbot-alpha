from __future__ import annotations

from decimal import Decimal
import unittest

from coinbot_alpha.risk.limits import RiskEngine, RiskLimits
from coinbot_alpha.schemas import OrderIntent, Side


class RiskLimitsTests(unittest.TestCase):
    def test_symbol_cap_blocks(self) -> None:
        engine = RiskEngine(RiskLimits(Decimal("100"), Decimal("1000")))
        ok = engine.check_and_apply(OrderIntent("1", "BTC-USD", Side.BUY, Decimal("90"), 10))
        blocked = engine.check_and_apply(OrderIntent("2", "BTC-USD", Side.BUY, Decimal("20"), 10))
        self.assertTrue(ok.allowed)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "symbol_cap_exceeded")


if __name__ == "__main__":
    unittest.main()
