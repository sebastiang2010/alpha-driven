# test_as_cancel_replace.py — F1.4: cancelación/reemplazo con motor real.
# Cubre: cancelación pendiente, fills durante la espera, reemplazo
# diferido (sin reenvío), ausencia de duplicadas y trazabilidad.

import unittest
from typing import Any, Dict, List

from strategy.as_coordinator import ASCoordinator
from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    ReconstructionConfig,
)


def _depth(ts_ms, bid, ask, update_id, pu, bid_qty=50, ask_qty=50):
    # Dos niveles por lado con colas profundas: las quotes A-S (dentro del
    # spread pero fuera del mejor nivel) descansan (visible 0) en vez de
    # morir con rejected_unknown_depth. Cantidades balanceadas para que el
    # imbalance no suprima un lado por maker guard.
    return {"ts_ms": ts_ms,
            "bids": [[bid, bid_qty], [bid - 100, 200]],
            "asks": [[ask, ask_qty], [ask + 100, 200]],
            "update_id": update_id, "pu": pu}


def _books(start_ts, end_ts, step, bid=10000, ask=10001):
    """Libros encadenados uid/pu cada `step` ms (warm-up denso)."""
    rows = []
    uid = 1
    ts = start_ts
    while ts <= end_ts:
        rows.append(_depth(ts, bid, ask, uid, uid - 1))
        uid += 1
        ts += step
    return rows, uid


def _trade(ts_ms, trade_id, price, qty, buyer_maker):
    return {"ts_ms": ts_ms, "trade_id": trade_id, "price_ticks": price,
            "qty_lots": qty, "is_buyer_maker": buyer_maker}


def _engine(**kw):
    kw.setdefault("max_gap_ms", 60000)
    kw.setdefault("max_book_age_ms", 60000)
    return ExecutionReconstructor(ReconstructionConfig(**kw))


def _coord(depth, trades, engine):
    return ASCoordinator(
        config=ReconstructionConfig(),
        engine=engine,
        depth_csv=depth,
        trades_csv=trades,
        decision_interval_ms=5000,
        warmup_intervals=3,
    )


def _ledger_rows(coord, ts_ms=None, side=None, reason=None):
    rows = coord.quote_ledger
    if ts_ms is not None:
        rows = [r for r in rows if r["ts_ms"] == ts_ms]
    if side is not None:
        rows = [r for r in rows if r["side"] == side]
    if reason is not None:
        rows = [r for r in rows if r["reason"] == reason]
    return rows


class TestCancelPendingAndDeferredReplace(unittest.TestCase):
    """Libro que se mueve tras la primera cotización: cancel diferido."""

    def _run_until(self, shift_ts=71000, tail_ts=76000):
        depth, nxt = _books(1000, 61000, 5000)
        depth.append(_depth(66000, 10000, 10001, nxt, nxt - 1))
        depth.append(_depth(shift_ts, 10010, 10011, nxt + 1, nxt))
        depth.append(_depth(tail_ts, 10010, 10011, nxt + 2, nxt + 1))
        engine = _engine()
        coord = _coord(depth, [], engine)
        result = coord.run()
        return coord, engine, result

    def test_cancel_requested_then_replaced(self):
        coord, engine, _ = self._run_until()
        # 66000: submits iniciales (lado libre, inventario 0)
        first = _ledger_rows(coord, 66000, reason="submitted")
        self.assertEqual(len(first), 2)
        # 71000: libro movido -> ambos lados piden cancel, nada se envía
        canc = _ledger_rows(coord, 71000, reason="cancel_requested")
        self.assertEqual(len(canc), 2)
        self.assertEqual(_ledger_rows(coord, 71000, reason="submitted"), [])
        # 76000: cancelación efectiva drenada -> reemplazo fresco
        repl = _ledger_rows(coord, 76000, reason="submitted")
        self.assertEqual(len(repl), 2)
        # El reemplazo NO reenvía el objetivo viejo: precio distinto
        # (el mercado se movió) e ids nuevos.
        old_prices = {r["price_ticks"] for r in first}
        new_prices = {r["price_ticks"] for r in repl}
        self.assertNotEqual(old_prices, new_prices)
        old_ids = {r["order_id"] for r in first}
        new_ids = {r["order_id"] for r in repl}
        self.assertTrue(old_ids.isdisjoint(new_ids))

    def test_no_duplicate_orders(self):
        coord, engine, _ = self._run_until()
        submitted = [r for r in coord.quote_ledger if r["reason"] == "submitted"]
        ids = [r["order_id"] for r in submitted]
        self.assertEqual(len(ids), len(set(ids)), "order_ids únicos")
        # El motor nunca rechazó una orden del coordinador por lado ocupado
        for j in engine.journal:
            if j.get("event") == "rejected_side_busy":
                self.assertNotIn(j.get("order_id"), ids)

    def test_held_unchanged_without_book_move(self):
        """Sin movimiento del libro: se mantiene, no se cancela ni reenvía."""
        depth, nxt = _books(1000, 61000, 5000)
        depth.append(_depth(66000, 10000, 10001, nxt, nxt - 1))
        depth.append(_depth(71000, 10000, 10001, nxt + 1, nxt))
        engine = _engine()
        coord = _coord(depth, [], engine)
        coord.run()
        self.assertEqual(len(_ledger_rows(coord, 66000, reason="submitted")), 2)
        held = _ledger_rows(coord, 71000, reason="held_unchanged")
        self.assertEqual(len(held), 2)
        self.assertEqual(_ledger_rows(coord, 71000, reason="cancel_requested"), [])
        self.assertEqual(_ledger_rows(coord, 71000, reason="submitted"), [])


class TestFillDuringCancelWait(unittest.TestCase):
    """Cancel con latencia larga: el fill llega en la espera y el lado se
    libera por terminal (filled), no por cancelación efectiva."""

    def test_fill_before_effective_frees_side(self):
        depth, nxt = _books(1000, 61000, 5000)
        depth.append(_depth(66000, 10000, 10001, nxt, nxt - 1))
        depth.append(_depth(71000, 10010, 10011, nxt + 1, nxt))
        # Motor con cancelación lenta (6000ms > ciclo 5000ms): la cancel
        # pedida en 71000 vence en 77000, un ciclo completo después.
        engine = _engine(cancel_latency_ms=6000)
        coord = _coord(depth, [], engine)
        # Se necesita el precio BUY emitido en 66000 para el trade;
        # corrida de sondeo con motor gemelo para leer el ledger.
        probe = _coord(list(depth), [], _engine())
        probe.run()
        buy_price = _ledger_rows(probe, 66000, side="BUY",
                                 reason="submitted")[0]["price_ticks"]
        # Trade en 76500 (fuera de grilla: no hay decisión ese ts) al precio
        # de la orden BUY (cola vacía: visible 0 fuera de nivel → llena).
        # Ocurre DURANTE la espera: cancel pedida en 71000, efectiva en
        # 77000. En 76000 (grilla, sin trades) el lado debe verse pendiente.
        depth.append(_depth(76000, 10010, 10011, nxt + 2, nxt + 1))
        trades = [_trade(76500, "fillbuy", buy_price, 5, True)]
        depth.append(_depth(81000, 10010, 10011, nxt + 3, nxt + 2))
        engine2 = _engine(cancel_latency_ms=6000)
        coord2 = _coord(depth, trades, engine2)
        coord2.run()
        # 76000: lado BUY en espera de cancelación (ocupado, sin envío)
        pend = _ledger_rows(coord2, 76000, side="BUY", reason="cancel_pending")
        self.assertEqual(len(pend), 1)
        # El fill se procesó durante la espera
        fills = [f for f in engine2.fills if f.side == "BUY"]
        self.assertEqual(len(fills), 1)
        # 81000: BUY terminal-filled libera el lado; inventario +1 → BUY
        # suprimido por regla de inventario y SELL reemplazado fresco.
        self.assertEqual(engine2.inventory_lots, 1)
        self.assertEqual(
            len(_ledger_rows(coord2, 81000, side="BUY",
                             reason="suppressed_inventory_side")), 1)
        repl_sell = _ledger_rows(coord2, 81000, side="SELL",
                                 reason="submitted")
        self.assertEqual(len(repl_sell), 1)


class TestQuoteLedgerTraceability(unittest.TestCase):
    """Cada comando del motor tiene su fila en el ledger y cada ciclo
    decide ambos lados."""

    def test_every_command_traced_every_cycle_covered(self):
        depth, nxt = _books(1000, 61000, 5000)
        depth.append(_depth(66000, 10000, 10001, nxt, nxt - 1))
        depth.append(_depth(71000, 10010, 10011, nxt + 1, nxt))
        depth.append(_depth(76000, 10010, 10011, nxt + 2, nxt + 1))
        engine = _engine()
        coord = _coord(depth, [], engine)
        coord.run()
        for cycle in (66000, 71000, 76000):
            rows = _ledger_rows(coord, cycle)
            self.assertEqual({r["side"] for r in rows}, {"BUY", "SELL"})
            for r in rows:
                self.assertIn(r["reason"], {
                    "submitted", "held_unchanged", "cancel_requested",
                    "cancel_pending", "suppressed_maker",
                    "suppressed_inventory_side", "suppressed_no_book",
                    "suppressed_no_mid"})
        # Comandos del journal ↔ filas del ledger
        ledger_by_id = {}
        for r in coord.quote_ledger:
            if r["order_id"]:
                ledger_by_id.setdefault(r["order_id"], []).append(r)
        for j in engine.journal:
            if j.get("event") in ("submit", "cancel_requested",
                                  "cancelled", "live", "fill"):
                oid = j.get("order_id")
                if oid and oid.startswith(("buy_", "sell_")):
                    self.assertIn(oid, ledger_by_id)


if __name__ == "__main__":
    unittest.main()
