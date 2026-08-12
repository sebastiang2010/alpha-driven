# test_risk_engine.py
"""
Tests unitarios del Risk Engine (§12): verifican que el engine efectivamente
RECHAZA propuestas que violan cada límite y que permite las que están dentro.

Corren con unittest puro (NO pytest):
    python -m unittest strategy.tests.test_risk_engine -v
"""
import sys
import os
import unittest

sys.path.insert(0, r"C:\programas\proyectos\alpha-driven")

from strategy import config, risk_engine


def _snapshot(**overrides):
    base = {
        "inventory": 0.0,
        "mid": 0.5,
        "daily_pnl": 0.0,
        "drawdown": 0.0,
        "unrealized_pnl": 0.0,
        "volatility": 0.001,
        "current_position_notional": 0.0,
        "peak_equity": 1000.0,
        "current_equity": 1000.0,
    }
    base.update(overrides)
    return base


class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        # max_order_size_override SOLO para tests (§12): permite cotizar qty
        # 1.0 en "within limits" aunque el nivel 0 real tenga multiplicador 0.0.
        self.engine = risk_engine.RiskEngine(max_order_size_override=1.0)

    # ── check_order: rechazos por límite (§12) ──────────────────────────

    def test_rejects_order_exceeding_position_notional(self):
        # Posición actual 24.5 + notional 1.0 => 25.5 > 25 => rechaza.
        allowed, reasons = self.engine.check_order(
            config.SYMBOL, "bid", 1.0, 0.5, 1.0, 0,
            _snapshot(current_position_notional=24.5),
        )
        self.assertFalse(allowed)
        self.assertIn(risk_engine.REASON_POSITION_NOTIONAL, reasons)

    def test_rejects_order_exceeding_max_size(self):
        # Nivel 0 (dry-run con simulación §0.2): max order size =
        # BASE_ORDER_SIZE_XRP * SIMULATION_QUOTE_MULTIPLIER = 20.0 XRP.
        # Una qty mayor al tamaño máximo se rechaza.
        eng = risk_engine.RiskEngine()
        self.assertEqual(
            eng.max_order_size,
            config.BASE_ORDER_SIZE_XRP * config.SIMULATION_QUOTE_MULTIPLIER,
        )
        allowed, reasons = eng.check_order(
            config.SYMBOL, "bid", eng.max_order_size + 1.0, 0.5,
            (eng.max_order_size + 1.0) * 0.5, 0, _snapshot(),
        )
        self.assertFalse(allowed)
        self.assertIn(risk_engine.REASON_ORDER_SIZE, reasons)

    def test_level0_simulation_allows_simulated_order(self):
        # Nivel 0 con simulación habilitada: una orden dentro del tamaño
        # simulado SÍ es admitida (antes el multiplicador 0.0 rechazaba toda
        # qty>0, bloqueando la corrida integrada dry-run).
        eng = risk_engine.RiskEngine()
        qty = config.BASE_ORDER_SIZE_XRP * config.SIMULATION_QUOTE_MULTIPLIER / 2.0
        allowed, reasons = eng.check_order(
            config.SYMBOL, "bid", qty, 0.5, qty * 0.5, 0, _snapshot()
        )
        self.assertTrue(allowed, reasons)

    def test_rejects_when_max_open_orders_reached(self):
        allowed, reasons = self.engine.check_order(
            config.SYMBOL, "bid", 1.0, 0.5, 0.5, config.MAX_OPEN_ORDERS,
            _snapshot(),
        )
        self.assertFalse(allowed)
        self.assertIn(risk_engine.REASON_MAX_OPEN_ORDERS, reasons)

    def test_rejects_when_daily_loss_exceeded(self):
        allowed, reasons = self.engine.check_order(
            config.SYMBOL, "bid", 1.0, 0.5, 0.5, 0,
            _snapshot(daily_pnl=-config.MAX_DAILY_LOSS_USDC),
        )
        self.assertFalse(allowed)
        self.assertIn(risk_engine.REASON_DAILY_LOSS, reasons)

    def test_rejects_when_drawdown_exceeded(self):
        allowed, reasons = self.engine.check_order(
            config.SYMBOL, "bid", 1.0, 0.5, 0.5, 0,
            _snapshot(drawdown=config.MAX_DRAWDOWN_PCT),
        )
        self.assertFalse(allowed)
        self.assertIn(risk_engine.REASON_DRAWDOWN, reasons)

    def test_rejects_when_unrealized_loss_exceeded(self):
        # max_unrealized = max(2.0, 0.5*10.0) = 5.0
        allowed, reasons = self.engine.check_order(
            config.SYMBOL, "bid", 1.0, 0.5, 0.5, 0,
            _snapshot(unrealized_pnl=-self.engine.max_unrealized_loss),
        )
        self.assertFalse(allowed)
        self.assertIn(risk_engine.REASON_UNREALIZED_LOSS, reasons)

    def test_rejects_when_volatility_too_high(self):
        allowed, reasons = self.engine.check_order(
            config.SYMBOL, "bid", 1.0, 0.5, 0.5, 0,
            _snapshot(volatility=self.engine.max_volatility + 0.01),
        )
        self.assertFalse(allowed)
        self.assertIn(risk_engine.REASON_VOLATILITY, reasons)

    def test_rejects_when_exposure_exceeded(self):
        # Exposición bruta proyectada 24.5 + 2.0 = 26.5 > 25 => rechaza,
        # aunque la posición neta proyectada (ask) baje a 22.5.
        eng = risk_engine.RiskEngine(max_order_size_override=10.0)
        allowed, reasons = eng.check_order(
            config.SYMBOL, "ask", 4.0, 0.5, 2.0, 0,
            _snapshot(current_position_notional=24.5, inventory=49.0),
        )
        self.assertFalse(allowed)
        self.assertIn(risk_engine.REASON_EXPOSURE, reasons)

    def test_allows_order_within_limits(self):
        allowed, reasons = self.engine.check_order(
            config.SYMBOL, "bid", 1.0, 0.5, 0.5, 0, _snapshot()
        )
        self.assertTrue(allowed, reasons)

    # ── Kill switch (§13) ───────────────────────────────────────────────

    def test_kill_switch_triggers_on_ws_disconnect(self):
        triggered, reasons = self.engine.check_kill_switch(
            _snapshot(), ws_connected=False, error_count=0
        )
        self.assertTrue(triggered)
        self.assertIn(risk_engine.REASON_KS_WS_DISCONNECTED, reasons)

    def test_kill_switch_triggers_on_daily_loss(self):
        triggered, reasons = self.engine.check_kill_switch(
            _snapshot(daily_pnl=-config.MAX_DAILY_LOSS_USDC),
            ws_connected=True, error_count=0,
        )
        self.assertTrue(triggered)
        self.assertIn(risk_engine.REASON_KS_DAILY_LOSS, reasons)

    def test_kill_switch_triggers_on_price_anomaly(self):
        triggered, reasons = self.engine.check_kill_switch(
            _snapshot(mid=0.0), ws_connected=True, error_count=0
        )
        self.assertTrue(triggered)
        self.assertIn(risk_engine.REASON_KS_MID_INVALID, reasons)

    def test_kill_switch_not_triggered_normally(self):
        triggered, reasons = self.engine.check_kill_switch(
            _snapshot(), ws_connected=True, error_count=0
        )
        self.assertFalse(triggered)
        self.assertEqual(reasons, [])

    def test_kill_switch_triggers_on_realized_loss_guard(self):
        # Guard de pérdida realizada ajustado (§0.2 / "el bot no da pérdida"):
        # ante una pérdida neta realizada por debajo de -LOSS_GUARD_USDC el
        # kill switch frena y aplana, sin esperar a -MAX_DAILY_LOSS_USDC.
        triggered, reasons = self.engine.check_kill_switch(
            _snapshot(daily_pnl=-config.LOSS_GUARD_USDC - 1e-6),
            ws_connected=True, error_count=0,
        )
        self.assertTrue(triggered)
        self.assertIn(risk_engine.REASON_KS_REALIZED_LOSS_GUARD, reasons)

    def test_kill_switch_realized_loss_guard_below_threshold_ok(self):
        # Por encima del umbral ajustado (incluso levemente negativo) no frena:
        # el bot opera con normalidad en régimen range-bound (día 11 +0.0045).
        triggered, reasons = self.engine.check_kill_switch(
            _snapshot(daily_pnl=-config.LOSS_GUARD_USDC / 2.0),
            ws_connected=True, error_count=0,
        )
        self.assertFalse(triggered)
        self.assertNotIn(risk_engine.REASON_KS_REALIZED_LOSS_GUARD, reasons)


if __name__ == "__main__":
    unittest.main()
