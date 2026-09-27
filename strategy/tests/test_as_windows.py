# test_as_windows.py — Veredicto F1.3 (V1–V4): cobertura de ventanas,
# vencimiento explícito, consultas no destructivas, un libro por grupo,
# volatilidad sobre ventana real de 60s. Motor + MarketState + AlphaModel
# reales (nada mockeado).

import math
import statistics
import unittest

from strategy.as_coordinator import ASCoordinator
from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    ReconstructionConfig,
)
from strategy.market_state import MarketState


def _depth(ts_ms, bid, ask, update_id, pu, bid_qty=10, ask_qty=100):
    return {"ts_ms": ts_ms, "bids": [[bid, bid_qty]],
            "asks": [[ask, ask_qty]], "update_id": update_id, "pu": pu}


def _trade(ts_ms, trade_id, price, qty, buyer_maker):
    return {"ts_ms": ts_ms, "trade_id": trade_id, "price_ticks": price,
            "qty_lots": qty, "is_buyer_maker": buyer_maker}


def _engine():
    return ExecutionReconstructor(ReconstructionConfig(
        max_gap_ms=60000, max_book_age_ms=60000))


def _coord(depth, trades, engine):
    return ASCoordinator(config=ReconstructionConfig(), engine=engine,
                         depth_csv=depth, trades_csv=trades,
                         decision_interval_ms=5000, warmup_intervals=3)


def _dense(t_start=1000, t_end=61000, step=5000, bid=10000, ask=10001):
    rows = []
    uid = 1
    prev = 0
    ts = t_start
    while ts <= t_end:
        rows.append(_depth(ts, bid, ask, uid, prev))
        prev = uid
        uid += 1
        ts += step
    return rows, uid


class TestWindowCoverageGate(unittest.TestCase):
    """V1: sin cobertura de ventanas no hay submits, pero el historial
    AlphaModel ya se alimenta durante el warm-up."""

    def test_no_submits_before_coverage_but_history_fed(self):
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2)]
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord.run()
        self.assertFalse(coord._warmup_complete)
        self.assertEqual(list(eng.orders.values()), [])
        # ...pero el warm-up best-effort ya alimentó el filtro §14
        hist = list(coord._alpha_model._mid_history)
        self.assertGreater(len(hist), 0)
        self.assertAlmostEqual(hist[-1][1], 10000.5 * 0.0001, places=12)

    def test_submits_after_60s_with_fed_history(self):
        depth, nxt = _dense()
        depth += [_depth(66000, 10000, 10001, nxt, nxt - 1),
                  _depth(71000, 10000, 10001, nxt + 1, nxt)]
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord.run()
        self.assertTrue(coord._warmup_complete)
        self.assertGreater(len(eng.orders), 0)
        hist = list(coord._alpha_model._mid_history)
        self.assertGreater(len(hist), 0)
        # Alimentación continua (warm-up + decisiones) con la ventana
        # propia §14 de 30s del filtro: el último es del cierre (71s) y
        # el historial cubre ~30s hacia atrás, nunca vacío al decidir.
        self.assertAlmostEqual(hist[-1][0], 71.0, places=9)
        self.assertGreater(hist[-1][0] - hist[0][0], 0.0)
        self.assertLessEqual(hist[-1][0] - hist[0][0], 30.0 + 1e-9)


class TestExplicitExpiry(unittest.TestCase):
    """V2: con tiempo explícito las observaciones vencen (MarketState
    directo, sin coordinador)."""

    def test_stale_observations_expire(self):
        ms = MarketState(symbol="xrpusdc", real=False)
        ms.update_bookticker(1.0, 10.0, 1.0001, 100.0, 1000)
        ms.update_depth([[1.0, 10.0]], [[1.0001, 100.0]], 1, 1000)
        ms.update_trade(1.0001, 4.0, False, 2000)
        ms.update_trade(1.0, 6.0, True, 3000)
        fresh = ms.get_snapshot(3.0)
        self.assertAlmostEqual(float(fresh["buy_volume_60s"] or 0.0), 4.0,
                               places=12)
        self.assertAlmostEqual(float(fresh["sell_volume_60s"] or 0.0), 6.0,
                               places=12)
        # A los 100s todo flujo/momentum/volatilidad expiró (mid persiste:
        # es nivel, no ventana)
        old = ms.get_snapshot(100.0)
        self.assertEqual(float(old["buy_volume_60s"] or 0.0), 0.0)
        self.assertEqual(float(old["sell_volume_60s"] or 0.0), 0.0)
        self.assertEqual(float(old["momentum"] or 0.0), 0.0)
        self.assertEqual(float(old["volatility"] or 0.0), 0.0)
        self.assertGreater(float(old["mid"] or 0.0), 0.0)


class TestNonDestructiveQueries(unittest.TestCase):
    """V3: snapshots repetidos no se alteran entre sí ni podan la deque."""

    def test_repeated_snapshots_stable_and_deque_intact(self):
        ms = MarketState(symbol="xrpusdc", real=False)
        for i in range(36):
            ts = 1000 + i * 2000  # 1s..71s cada 2s
            m = 1.0 + (0.001 if i % 2 else 0.0)
            ms.update_bookticker(m - 0.00005, 10.0, m + 0.00005, 100.0, ts)
        n0 = len(ms._mid_samples)
        s1 = ms.get_snapshot(71.0)
        n1 = len(ms._mid_samples)
        s2 = ms.get_snapshot(71.0)
        self.assertEqual(n0, n1)
        self.assertEqual(s1["volatility"], s2["volatility"])
        self.assertEqual(s1["momentum"], s2["momentum"])
        # 36 muestras intactas: ninguna poda entre consultas
        self.assertEqual(n1, 36)


class TestOneSamplePerGroup(unittest.TestCase):
    """V4: varios libros del mismo ts se validan todos pero incorporan uno."""

    def test_same_ts_books_yield_single_mid_sample(self):
        eng = _engine()
        coord = _coord([], [], eng)
        n0 = len(coord._market_state._mid_samples)
        coord._feed_market_state(1000, [
            {"ts_ms": 1000, "kind": "book", "bids": [[10000, 10]],
             "asks": [[10001, 100]], "update_id": 1, "pu": 0},
            {"ts_ms": 1000, "kind": "book", "bids": [[10002, 10]],
             "asks": [[10003, 100]], "update_id": 2, "pu": 1},
            {"ts_ms": 1000, "kind": "book", "bids": [],
             "asks": [[10003, 100]], "update_id": 3, "pu": 2},
        ])
        samples = list(coord._market_state._mid_samples)
        self.assertEqual(len(samples), n0 + 1)
        # El último VÁLIDO del grupo (el vacío se salta): mid 10002.5 ticks
        self.assertAlmostEqual(samples[-1][1], 10002.5 * 0.0001, places=12)
        self.assertAlmostEqual(float(coord._market_state.best_bid or 0.0),
                               10002 * 0.0001, places=12)


class TestSixtySecondVolWindow(unittest.TestCase):
    """La ventana de vol es 60s reales: 70s de datos con 60s finales
    planos dan sigma 0 (el pasado variable queda fuera)."""

    def test_vol_uses_only_60s_window(self):
        ms = MarketState(symbol="xrpusdc", real=False)
        # t=1..9s alternados, luego plano 1.0 hasta t=71s (cada 2s)
        mids = [1.0, 1.001, 1.0, 1.001, 1.0] + [1.0] * 31
        for i, m in enumerate(mids):
            ts = 1000 + i * 2000
            ms.update_bookticker(m - 0.00005, 10.0, m + 0.00005, 100.0, ts)
        snap = ms.get_snapshot(71.0)
        # Ventana (11s, 71s]: todo plano -> solo retornos 0 -> sigma 0
        self.assertEqual(snap["volatility"], 0.0)
        # ...y la deque conserva las 36 muestras (sin poda destructiva)
        self.assertEqual(len(ms._mid_samples), 36)
        # Contrapeso: a t=12s la ventana aún incluye variación -> sigma > 0
        early = ms.get_snapshot(12.0)
        self.assertGreater(early["volatility"], 0.0)


if __name__ == "__main__":
    unittest.main()
