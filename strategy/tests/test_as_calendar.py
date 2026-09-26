# test_as_calendar.py — Pruebas sintéticas F1.2: calendario de 5s, ciclos sin
# eventos y drenado de timers vencidos. Motor real (sin mockear A-S);
# solo se registra la secuencia de avances (spy por herencia, no mock).

import unittest
from typing import Any, Dict, List, Sequence

from strategy.as_coordinator import ASCoordinator, ASCoordinatorConfig
from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    FinalResult,
    ReconstructionConfig,
)


def _depth(ts_ms, bid, ask, update_id, pu):
    return {"ts_ms": ts_ms, "bids": [[bid, 10]], "asks": [[ask, 100]],
            "update_id": update_id, "pu": pu}


def _trade(ts_ms, trade_id, price, qty, buyer_maker):
    return {"ts_ms": ts_ms, "trade_id": trade_id, "price_ticks": price,
            "qty_lots": qty, "is_buyer_maker": buyer_maker}


def _big_gap_engine():
    return ExecutionReconstructor(
        ReconstructionConfig(max_gap_ms=60000, max_book_age_ms=60000))


def _make_coord(depth_rows, trades_rows, engine=None, **kw):
    kw.setdefault("decision_interval_ms", 5000)
    kw.setdefault("warmup_intervals", 3)
    return ASCoordinator(
        config=ReconstructionConfig(),
        engine=engine or _big_gap_engine(),
        depth_csv=depth_rows,
        trades_csv=trades_rows,
        **kw,
    )


class RecordingEngine(ExecutionReconstructor):
    """Motor real que registra los ts de advance_to (spy, no mock)."""

    def __init__(self, config):
        super().__init__(config)
        self.advanced_ts: List[int] = []

    def advance_to(self, ts_ms: int, market_events: Sequence[Dict[str, Any]]):
        self.advanced_ts.append(ts_ms)
        return super().advance_to(ts_ms, market_events)


class TestFiveSecondCalendar(unittest.TestCase):
    """F1.2: default 5s, ciclos sin eventos, sin saltar eventos."""

    def test_default_interval_is_5s(self):
        self.assertEqual(ASCoordinatorConfig().decision_interval_ms, 5000)
        coord = ASCoordinator(
            config=ReconstructionConfig(),
            engine=_big_gap_engine(),
            depth_csv=[_depth(1000, 10000, 10001, 1, 0)],
            trades_csv=[],
        )
        self.assertEqual(coord.decision_interval_ms, 5000)

    def test_merged_steps_include_empty_cycles_in_order(self):
        """Libros@1000,2000,3000,7000,12000 (D=5s, t0=1000): pasos
        1000,2000,3000,6000(vacío),7000,11000(vacío),12000 — sin saltos,
        sin regresión, fin en último evento."""
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2),
                 _depth(7000, 10000, 10001, 4, 3),
                 _depth(12000, 10000, 10001, 5, 4)]
        engine = RecordingEngine(
            ReconstructionConfig(max_gap_ms=60000, max_book_age_ms=60000))
        coord = _make_coord(depth, [], engine=engine)
        result = coord.run()
        self.assertIsInstance(result, FinalResult)
        self.assertEqual(engine.advanced_ts,
                         [1000, 2000, 3000, 6000, 7000, 11000, 12000])
        self.assertEqual(result.observed_end_ms, 12000)

    def test_empty_cycle_decides_and_drains_arrival(self):
        """El ciclo vacío 6000 decide (warm-up completo) y su arrival@6040
        ya está drenado en el paso 7000: ninguna orden queda 'pending'."""
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2),
                 _depth(7000, 10000, 10001, 4, 3),
                 _depth(12000, 10000, 10001, 5, 4)]
        coord = _make_coord(depth, [])
        result = coord.run()
        self.assertIsInstance(result, FinalResult)
        # Hubo decisión en ciclo sin eventos (órdenes emitidas en ts=6000)
        by_empty_cycle = [o for o in coord.engine.orders.values()
                          if o.get("submit_ts_ms") == 6000]
        self.assertGreater(len(by_empty_cycle), 0)
        # ...y sus arrivals se drenaron (nada pendiente)
        pending = [o for o in coord.engine.orders.values()
                   if o["status"] == "pending"]
        self.assertEqual(pending, [])

    def test_off_grid_trade_is_consumed(self):
        """Trade@6500 (fuera de grilla) se procesa: el libro posterior lo
        refleja y el motor lo vio (sin saltar eventos)."""
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2),
                 _depth(7000, 9990, 9991, 4, 3)]
        trades = [_trade(6500, "tx", 9995, 5, True)]
        engine = RecordingEngine(
            ReconstructionConfig(max_gap_ms=60000, max_book_age_ms=60000))
        coord = _make_coord(depth, trades, engine=engine)
        coord.run()
        self.assertIn(6500, engine.advanced_ts)
        self.assertIn("tx", engine.seen_trades)

    def test_event_gap_invalidates_despite_cycles(self):
        """Hueco entre EVENTOS > 2*D invalida aunque los ciclos intermedios
        existan. El inventario NO NULO del motor no se resetea: si algo lo
        pusiera a cero, este test lo detectaría (comprobar cero no detecta)."""
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2),
                 _depth(30000, 10000, 10001, 4, 3)]
        coord = _make_coord(depth, [])
        # Inventario no nulo previo al hueco (siembra de estado: la vía que
        # invalida no debe tocar el inventario del motor)
        coord.engine.inventory_lots = 5
        with self.assertRaises(ValueError) as ctx:
            coord.run()
        self.assertIn("Gap", str(ctx.exception))
        self.assertEqual(coord.engine.inventory_lots, 5)

    def test_t0_anchors_to_first_book_despite_earlier_trade(self):
        """Un trade anterior al primer libro no desplaza la grilla (t0 = primer
        libro) y el trade igual se procesa."""
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2)]
        trades = [_trade(500, "early", 10000, 5, True)]
        engine = RecordingEngine(
            ReconstructionConfig(max_gap_ms=60000, max_book_age_ms=60000))
        coord = _make_coord(depth, trades, engine=engine)
        self.assertEqual(coord._t0, 1000)
        coord.run()
        self.assertIn("early", engine.seen_trades)
        self.assertEqual(engine.advanced_ts[0], 500)

    def test_invalid_intervals_rejected(self):
        """Intervalo cero, negativo, fraccionario, no-entero o bool se rechaza
        antes de construir la grilla (no debe colgar ni truncar)."""
        for bad in (0, -1000, 1500.5, "5000", True, None):
            with self.assertRaises(ValueError, msg=f"interval={bad!r}"):
                _make_coord([_depth(1000, 10000, 10001, 1, 0)], [],
                            decision_interval_ms=bad)
        # Entero positivo válido pasa
        coord = _make_coord([_depth(1000, 10000, 10001, 1, 0)],
                            [], decision_interval_ms=5000)
        self.assertEqual(coord.decision_interval_ms, 5000)

    def test_empty_cycle_with_stale_book_rejected_before_policy(self):
        """Ciclo vacío con libro obsoleto se invalida ANTES de llamar a la
        política, aunque esta no emitiría comandos (cobertura incondicional)."""
        from strategy.execution_reconstruction import ExecutionSnapshot

        calls: List[int] = []

        class QuietCoordinator(ASCoordinator):
            def _as_decision(self, snapshot: ExecutionSnapshot):
                calls.append(self._current_ts)
                return []

        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2),
                 _depth(7000, 10000, 10001, 4, 3)]
        # Motor con max_book_age default (1000): en el ciclo vacío 6000 el
        # libro@3000 tiene 3000ms -> obsoleto. Gap de libros 4000 <= 2*D=10000:
        # la invalidez viene por cobertura, no por hueco.
        engine = ExecutionReconstructor(ReconstructionConfig())
        coord = QuietCoordinator(
            config=ReconstructionConfig(),
            engine=engine,
            depth_csv=depth,
            trades_csv=[],
            decision_interval_ms=5000,
            warmup_intervals=3,
        )
        with self.assertRaises(ValueError) as ctx:
            coord.run()
        self.assertIn("obsoleto", str(ctx.exception))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
