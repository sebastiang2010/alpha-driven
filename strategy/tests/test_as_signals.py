# test_as_signals.py — Pruebas sintéticas F1.3: señales causales reales,
# conversiones ticks↔USDC/XRP, tiempo simulado y determinismo.
# Motor + MarketState + AlphaModel reales (nada mockeado).

import math
import statistics
import unittest

from strategy.as_coordinator import ASCoordinator
from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    ReconstructionConfig,
)


def _depth(ts_ms, bid, ask, update_id, pu, bid_qty=10, ask_qty=100):
    return {"ts_ms": ts_ms, "bids": [[bid, bid_qty]], "asks": [[ask, ask_qty]],
            "update_id": update_id, "pu": pu}


def _trade(ts_ms, trade_id, price, qty, buyer_maker):
    return {"ts_ms": ts_ms, "trade_id": trade_id, "price_ticks": price,
            "qty_lots": qty, "is_buyer_maker": buyer_maker}


def _engine(tick_size=0.0001, qty_step=1.0):
    return ExecutionReconstructor(ReconstructionConfig(
        tick_size=tick_size, qty_step=qty_step,
        max_gap_ms=60000, max_book_age_ms=60000))


def _coord(depth, trades, engine, **kw):
    kw.setdefault("decision_interval_ms", 5000)
    kw.setdefault("warmup_intervals", 3)
    return ASCoordinator(config=ReconstructionConfig(), engine=engine,
                         depth_csv=depth, trades_csv=trades, **kw)


class TestCausalSignals(unittest.TestCase):
    """Las señales vienen del flujo causal (no ceros hardcodeados)."""

    def test_imbalance_and_microprice_from_real_books(self):
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2)]
        coord = _coord(depth, [], _engine())
        coord.run()
        snap = coord._market_state.get_snapshot()
        # bids [[10000,10]], asks [[10001,100]] (lots; qty_step=1)
        self.assertAlmostEqual(snap["imbalance"], (10 - 100) / 110, places=12)
        # microprice ticks = (10001*10 + 10000*100)/110, en USDC
        self.assertAlmostEqual(
            snap["microprice"], ((10001 * 10 + 10000 * 100) / 110) * 0.0001,
            places=12)
        self.assertAlmostEqual(snap["mid"], 10000.5 * 0.0001, places=12)
        self.assertAlmostEqual(snap["momentum"], 0.0, places=12)

    def test_trade_flow_sides_from_real_trades(self):
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2)]
        # is_buyer_maker=False -> agresor compra -> buy; True -> sell
        trades = [_trade(1500, "b1", 10001, 4, False),
                  _trade(2500, "s1", 10000, 6, True)]
        coord = _coord(depth, trades, _engine())
        coord.run()
        snap = coord._market_state.get_snapshot()
        self.assertAlmostEqual(snap["buy_volume_60s"], 4.0, places=12)
        self.assertAlmostEqual(snap["sell_volume_60s"], 6.0, places=12)


class TestUnitConversions(unittest.TestCase):
    """tick_size/qty_step no unitarios: el estado ve USDC/XRP, no ticks."""

    def test_nonunit_tick_conversions(self):
        # tick_size=0.01, qty_step=10: 100/101 ticks -> mid 1.005 USDC
        depth = [_depth(1000, 100, 101, 1, 0),
                 _depth(2000, 100, 101, 2, 1),
                 _depth(3000, 100, 101, 3, 2)]
        trades = [_trade(1500, "b1", 101, 2, False)]
        eng = _engine(tick_size=0.01, qty_step=10.0)
        coord = _coord(depth, trades, eng)
        coord.run()
        snap = coord._market_state.get_snapshot()
        # Si pasaran ticks crudos, mid sería 100.5; en USDC es 1.005
        self.assertAlmostEqual(snap["mid"], 1.005, places=12)
        # qty 2 lots * 10 -> 20 XRP en el flujo (no 2.0)
        self.assertAlmostEqual(snap["buy_volume_60s"], 20.0, places=12)
        self.assertAlmostEqual(
            snap["microprice"], ((101 * 10 + 100 * 100) / 110) * 0.01,
            places=9)

    def test_emitted_quotes_are_ticks_and_maker_valid(self):
        depth = [_depth(1000, 100, 101, 1, 0),
                 _depth(2000, 100, 101, 2, 1),
                 _depth(3000, 100, 101, 3, 2),
                 _depth(7000, 100, 101, 4, 3),
                 _depth(12000, 100, 101, 5, 4)]
        eng = _engine(tick_size=0.01, qty_step=10.0)
        coord = _coord(depth, [], eng)
        coord.run()
        orders = list(eng.orders.values())
        self.assertGreater(len(orders), 0, "la política debe emitir")
        for o in orders:
            p = o["price_ticks"]
            self.assertIsInstance(p, int)
            self.assertGreater(p, 0)
            if o["side"] == "BUY":
                self.assertLess(p, 101, "BUY maker: no cruza el ask")
            else:
                self.assertGreater(p, 100, "SELL maker: no cruza el bid")


class TestSimulatedVolatilityScaling(unittest.TestCase):
    """Un solo escalado sqrt(ref_s/dt_bar), exacto a 2s de muestreo.

    Nota: espaciado 2s (no 10s) porque MarketState comparte una sola deque
    de mids entre momentum (ventana 15s, poda destructiva) y volatilidad
    (ventana 60s): con muestras separadas >15s el poda de momentum recorta
    el historial de vol. Con 5 muestras en 8s nada se poda y el factor
    sqrt(5/2)!=1 sigue fijando el escalado único (doble raíz daría 5/2).
    La interacción de ventanas se reporta al diseñador (conducta heredada
    del módulo reutilizado, no del coordinador).
    """

    def test_single_sqrt_scaling_at_2s_spacing(self):
        # Mids alternados 10000/10100 ticks cada 2s (dt_bar=2, ref_s=5):
        # sigma_ref = stdev(retornos) * sqrt(5/2), UN solo factor.
        mids = [10000, 10100, 10000, 10100, 10000]
        depth = [_depth(i * 2000, m - 1, m + 1, i + 1, i)
                 for i, m in enumerate(mids)]
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord.run()
        snap = coord._market_state.get_snapshot()
        self.assertEqual(len(coord._market_state._mid_samples), 5)
        rets = [math.log(mids[i] / mids[i - 1]) for i in range(1, 5)]
        expected = statistics.stdev(rets) * math.sqrt(5.0 / 2.0)
        self.assertAlmostEqual(snap["volatility"], expected, places=12)


class TestDeterminism(unittest.TestCase):
    """Doble corrida idéntica: mismos comandos (tiempo simulado explícito)."""

    def _run_orders(self):
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10002, 10003, 3, 2),
                 _depth(7000, 10002, 10003, 4, 3),
                 _depth(12000, 9998, 9999, 5, 4)]
        trades = [_trade(2500, "t1", 10001, 3, False)]
        eng = _engine()
        _coord(depth, trades, eng).run()
        return sorted((o["order_id"], o["side"], o["price_ticks"], o["qty_lots"])
                      for o in eng.orders.values())

    def test_double_run_identical_commands(self):
        self.assertEqual(self._run_orders(), self._run_orders())


if __name__ == "__main__":
    unittest.main()
