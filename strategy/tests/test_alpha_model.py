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

    def test_funding_reduce_pnl(self):
        """§18: el funding resta del NetPnL (fix auditoría 2026-08-09)."""
        sin_funding = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0, funding_rate=0.0
        )
        con_funding = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0, funding_rate=0.0001
        )
        self.assertGreater(sin_funding, con_funding)

    def test_slippage_reduce_pnl(self):
        """§18: el slippage resta del NetPnL (fix auditoría 2026-08-09)."""
        sin_slip = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0, slippage_rate=0.0
        )
        con_slip = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0, slippage_rate=0.0001
        )
        self.assertGreater(sin_slip, con_slip)

    def test_costos_combinados_netpnl_menor(self):
        """§18: NetPnL = GrossPnL - fees - funding - slippage (los tres restan)."""
        gross = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0,
            maker_fee=0.0, funding_rate=0.0, slippage_rate=0.0,
        )
        net = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0,
            maker_fee=0.0002, funding_rate=0.0001, slippage_rate=0.0001,
        )
        self.assertGreater(gross, net)
        # Verificación numérica exacta: costos = price*qty*(fee+funding+slippage).
        expected_costs = 2.49 * 20.0 * (0.0002 + 0.0001 + 0.0001)
        self.assertAlmostEqual(gross - net, expected_costs, places=12)


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


class TestAlphaSinDobleConteoEnQuotes(unittest.TestCase):
    """Regresión del hallazgo gate 2026-08-09 (bug §8).

    §8 define r = S + alpha: el alpha ya desplaza el precio de reserva
    (reservation_price). Con ALPHA_QUOTE_FACTOR=1.0 (bug corregido a 0.0) el
    |alpha| se SUMABA ademas a la distancia de quotes -> con alpha=±0.01 el
    lado alejado quedaba a ±2% del mid y el cercano sin borde (NetPnL≤0,
    gate falló en vivo: run 22:26 UTC, ask=1.0622 con mid=1.04215).

    Estos tests fallan si alguien vuelve a ensanchar las distancias con alpha.
    """

    def setUp(self):
        self.am = alpha_model.AlphaModel()
        self.snap = _snapshot(mid=2.5, spread=0.002, volatility=0.001,
                              imbalance=0.0, momentum=0.0, microprice=0.0)

    def test_alpha_no_ensancha_las_distancias(self):
        """Las distancias de quote deben ser independientes de alpha (§8)."""
        d0_bid, d0_ask = self.am.quote_distances(self.snap, 0.0, 0.0, 0.001)
        dmax_bid, dmax_ask = self.am.quote_distances(
            self.snap, alpha_model.ALPHA_MAX, 0.0, 0.001
        )
        self.assertAlmostEqual(d0_bid, dmax_bid, places=12)
        self.assertAlmostEqual(d0_ask, dmax_ask, places=12)

    def test_ask_se_desplaza_solo_por_reservacion(self):
        """La única influencia de alpha en el ask es via reservation_price (§8).

        ask(alpha) - ask(0) debe ser exactamente 'alpha' (desplazamiento de la
        reserva). Con el bug viejo (ALPHA_QUOTE_FACTOR=1.0) el |alpha| ademas
        ensanchaba la distancia -> el delta era 2*alpha.
        """
        alpha = alpha_model.ALPHA_MAX
        r_alpha = self.am.reservation_price(self.snap, alpha, 0.0, 0.001)
        _, ask_dist_alpha = self.am.quote_distances(self.snap, alpha, 0.0, 0.001)
        r_cero = self.am.reservation_price(self.snap, 0.0, 0.0, 0.001)
        _, ask_dist_cero = self.am.quote_distances(self.snap, 0.0, 0.0, 0.001)
        delta = (r_alpha + ask_dist_alpha) - (r_cero + ask_dist_cero)
        self.assertAlmostEqual(delta, alpha, places=9)


if __name__ == "__main__":
    unittest.main()
