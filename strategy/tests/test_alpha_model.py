# test_alpha_model.py
"""
Tests del AlphaModel (§6, §8, §11) — enfocados en la convención de side y en
el cálculo de NetPnL esperado (§9/§18).

Corren con unittest puro (NO pytest):
    python -m unittest strategy.tests.test_alpha_model -v
"""
import sys
import unittest

sys.path.insert(0, r"C:\programas\proyectos\alpha-driven")

from strategy import alpha_model


def _snapshot(mid=2.5, **overrides):
    base = {
        "mid": mid,
        "spread": 0.002,
        "imbalance": 0.1,
        "microprice": 2.501,
        "momentum": 0.001,
        "buy_volume_60s": 500.0,
        "sell_volume_60s": 400.0,
        "volatility": 0.001,
        "inventory": 0.0,
    }
    base.update(overrides)
    return base


class TestExpectedNetPnlEstimate(unittest.TestCase):
    """§0.6: la convención de side no debe romper el cálculo (bug de 0.0)."""

    def setUp(self):
        self.am = alpha_model.AlphaModel()
        self.snap = _snapshot()

    def test_acepta_convencion_bid_ask(self):
        # bid a 2.49 con mid 2.5 => spread capturado 0.01 * 20 = 0.2, fees 0.01
        pnl = self.am.expected_net_pnl_estimate(self.snap, "bid", 2.49, 20.0)
        self.assertGreater(pnl, 0.0)

    def test_acepta_convencion_BUY_SELL(self):
        # "BUY" debe ser equivalente a "bid" (era el bug: devolvía 0.0).
        pnl = self.am.expected_net_pnl_estimate(self.snap, "BUY", 2.49, 20.0)
        self.assertGreater(pnl, 0.0)
        pnl_ask = self.am.expected_net_pnl_estimate(self.snap, "SELL", 2.51, 20.0)
        self.assertGreater(pnl_ask, 0.0)

    def test_bid_y_BUY_equivalentes(self):
        a = self.am.expected_net_pnl_estimate(self.snap, "bid", 2.49, 20.0)
        b = self.am.expected_net_pnl_estimate(self.snap, "BUY", 2.49, 20.0)
        self.assertAlmostEqual(a, b, places=12)

    def test_ask_y_SELL_equivalentes(self):
        a = self.am.expected_net_pnl_estimate(self.snap, "ask", 2.51, 20.0)
        b = self.am.expected_net_pnl_estimate(self.snap, "SELL", 2.51, 20.0)
        self.assertAlmostEqual(a, b, places=12)

    def test_side_invalido_devuelve_cero(self):
        pnl = self.am.expected_net_pnl_estimate(self.snap, "HOLD", 2.49, 20.0)
        self.assertEqual(pnl, 0.0)

    def test_fees_reducen_pnl(self):
        sin_fee = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0
        )
        con_fee = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0002
        )
        self.assertGreater(sin_fee, con_fee)


class TestComputeAlpha(unittest.TestCase):
    """§6: señal acotada y con términos nulos bien manejados."""

    def setUp(self):
        self.am = alpha_model.AlphaModel()

    def test_alpha_sin_datos_devuelve_cero(self):
        self.assertEqual(self.am.compute_alpha({"mid": 0.0}), 0.0)
        self.assertEqual(self.am.compute_alpha({}), 0.0)

    def test_alpha_acotada_por_ALPHA_MAX(self):
        snap = _snapshot(mid=2.5, momentum=1.0, imbalance=1.0,
                         microprice=100.0, buy_volume_60s=1e9, sell_volume_60s=1.0)
        alpha = self.am.compute_alpha(snap)
        self.assertLessEqual(abs(alpha), alpha_model.ALPHA_MAX)

    def test_microprice_cero_anula_termino(self):
        snap = _snapshot(microprice=0.0)
        alpha = self.am.compute_alpha(snap)
        # Sin microprice, la señal sigue siendo finita y acotada.
        self.assertLessEqual(abs(alpha), alpha_model.ALPHA_MAX)


if __name__ == "__main__":
    unittest.main()
