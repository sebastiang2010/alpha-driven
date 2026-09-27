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


def _coord(depth, trades, engine, qty_lots=1):
    return ASCoordinator(
        config=ReconstructionConfig(),
        engine=engine,
        depth_csv=depth,
        trades_csv=trades,
        decision_interval_ms=5000,
        warmup_intervals=3,
        qty_lots=qty_lots,
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
    """Libro que se mueve tras la primera cotización: cancel diferido.

    Permanencia mínima (espejo prod §8/§10, MIN_ORDER_LIFETIME_SEC=15):
    submits en 66000 solo pueden cancelarse desde 81000 (frontera
    inclusiva). El movimiento llega en 81000; 71000/76000 mantienen.
    """

    def _run_until(self, shift_ts=81000, tail_ts=86000):
        depth, nxt = _books(1000, 61000, 5000)
        depth.append(_depth(66000, 10000, 10001, nxt, nxt - 1))
        # Libros intermedios sin movimiento (gap rule: eventos <= 10000ms;
        # ciclos 71000/76000 mantienen por libro quieto).
        depth.append(_depth(71000, 10000, 10001, nxt + 1, nxt))
        depth.append(_depth(76000, 10000, 10001, nxt + 2, nxt + 1))
        depth.append(_depth(shift_ts, 10010, 10011, nxt + 3, nxt + 2))
        depth.append(_depth(tail_ts, 10010, 10011, nxt + 4, nxt + 3))
        engine = _engine()
        coord = _coord(depth, [], engine)
        result = coord.run()
        return coord, engine, result

    def test_cancel_requested_then_replaced(self):
        coord, engine, _ = self._run_until()
        # 66000: submits iniciales (lado libre, inventario 0)
        first = _ledger_rows(coord, 66000, reason="submitted")
        self.assertEqual(len(first), 2)
        # 71000/76000: libro quieto -> se mantiene (edad irrelevante)
        for ts in (71000, 76000):
            self.assertEqual(len(_ledger_rows(coord, ts, reason="held_unchanged")), 2)
        # 81000: libro movido + edad exactamente 15s (frontera inclusiva)
        # -> ambos lados piden cancel, nada se envía
        canc = _ledger_rows(coord, 81000, reason="cancel_requested")
        self.assertEqual(len(canc), 2)
        self.assertEqual(_ledger_rows(coord, 81000, reason="submitted"), [])
        # 86000: cancelación efectiva drenada -> reemplazo fresco
        repl = _ledger_rows(coord, 86000, reason="submitted")
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


class TestMinLifetimeBoundary(unittest.TestCase):
    """Fronteras temporales de la permanencia mínima (15s, inclusiva).

    El libro se mueve en 71000 (edad 5s) y sigue movido: 71000/76000
    deben mantener por lifetime (held_min_lifetime, sin comandos) y
    81000 (edad exactamente 15s) debe cancelar.
    """

    def _run_early_move(self):
        depth, nxt = _books(1000, 61000, 5000)
        depth.append(_depth(66000, 10000, 10001, nxt, nxt - 1))
        depth.append(_depth(71000, 10010, 10011, nxt + 1, nxt))
        depth.append(_depth(76000, 10010, 10011, nxt + 2, nxt + 1))
        depth.append(_depth(81000, 10010, 10011, nxt + 3, nxt + 2))
        depth.append(_depth(86000, 10010, 10011, nxt + 4, nxt + 3))
        engine = _engine()
        coord = _coord(depth, [], engine)
        coord.run()
        return coord, engine

    def test_hold_below_15s_cancel_at_15s(self):
        coord, engine = self._run_early_move()
        self.assertEqual(len(_ledger_rows(coord, 66000, reason="submitted")), 2)
        # 71000 (5s) y 76000 (10s): precio cambió pero la orden es joven
        for ts in (71000, 76000):
            held = _ledger_rows(coord, ts, reason="held_min_lifetime")
            self.assertEqual(len(held), 2)
            self.assertEqual(_ledger_rows(coord, ts, reason="cancel_requested"), [])
            self.assertEqual(_ledger_rows(coord, ts, reason="submitted"), [])
        # 81000 (exactamente 15s): frontera inclusiva -> cancela
        canc = _ledger_rows(coord, 81000, reason="cancel_requested")
        self.assertEqual(len(canc), 2)
        # Sin comandos de cancel antes de la frontera
        early_cancels = [j for j in engine.journal
                         if j.get("event") == "cancel_requested"
                         and j.get("ts_ms", 0) < 81000]
        self.assertEqual(early_cancels, [])
        # 86000: reemplazo fresco tras la cancelación efectiva
        repl = _ledger_rows(coord, 86000, reason="submitted")
        self.assertEqual(len(repl), 2)


class TestFillDuringCancelWait(unittest.TestCase):
    """Cancel con latencia larga: el fill llega en la espera y el lado se
    libera por terminal (filled), no por cancelación efectiva.

    La cancelación solo puede pedirse con edad >= 15s: el movimiento llega
    en 81000; la cancel lenta (6000ms) vence en 87000 y el trade cae en
    86500 (fuera de grilla, durante la espera).
    """

    def test_fill_before_effective_frees_side(self):
        depth, nxt = _books(1000, 61000, 5000)
        depth.append(_depth(66000, 10000, 10001, nxt, nxt - 1))
        depth.append(_depth(71000, 10000, 10001, nxt + 1, nxt))
        depth.append(_depth(76000, 10000, 10001, nxt + 2, nxt + 1))
        depth.append(_depth(81000, 10010, 10011, nxt + 3, nxt + 2))
        # Motor con cancelación lenta (6000ms > ciclo 5000ms): la cancel
        # pedida en 81000 vence en 87000, más de un ciclo después.
        engine = _engine(cancel_latency_ms=6000)
        coord = _coord(depth, [], engine)
        # Se necesita el precio BUY emitido en 66000 para el trade;
        # corrida de sondeo con motor gemelo para leer el ledger.
        probe = _coord(list(depth), [], _engine())
        probe.run()
        buy_price = _ledger_rows(probe, 66000, side="BUY",
                                 reason="submitted")[0]["price_ticks"]
        # Trade en 86500 (fuera de grilla: no hay decisión ese ts) al precio
        # de la orden BUY (cola vacía: visible 0 fuera de nivel → llena).
        # Ocurre DURANTE la espera: cancel pedida en 81000, efectiva en
        # 87000. En 86000 (grilla, sin trades) el lado debe verse pendiente.
        depth.append(_depth(86000, 10010, 10011, nxt + 4, nxt + 3))
        trades = [_trade(86500, "fillbuy", buy_price, 5, True)]
        depth.append(_depth(91000, 10010, 10011, nxt + 5, nxt + 4))
        engine2 = _engine(cancel_latency_ms=6000)
        coord2 = _coord(depth, trades, engine2)
        coord2.run()
        # 86000: lado BUY en espera de cancelación (ocupado, sin envío)
        pend = _ledger_rows(coord2, 86000, side="BUY", reason="cancel_pending")
        self.assertEqual(len(pend), 1)
        # El fill se procesó durante la espera
        fills = [f for f in engine2.fills if f.side == "BUY"]
        self.assertEqual(len(fills), 1)
        # 91000: BUY terminal-filled libera el lado; inventario +1 → BUY
        # suprimido por regla de inventario y SELL reemplazado fresco.
        self.assertEqual(engine2.inventory_lots, 1)
        self.assertEqual(
            len(_ledger_rows(coord2, 91000, side="BUY",
                             reason="suppressed_inventory_side")), 1)
        repl_sell = _ledger_rows(coord2, 91000, side="SELL",
                                 reason="submitted")
        self.assertEqual(len(repl_sell), 1)


class TestPartialFillDuringCancelWait(unittest.TestCase):
    """Coordinador con qty_lots=3: un trade chico durante la espera deja
    remanente pendiente (fill parcial). El lado sigue ocupado (sin
    reemplazo prematuro), la contabilidad refleja el remanente y el
    reemplazo —tras la cancelación efectiva— se recalcula con el mercado
    movido durante la espera (no reenvía el precio guardado)."""

    def test_partial_fill_keeps_side_busy_then_recalculates(self):
        depth, nxt = _books(1000, 61000, 5000)
        depth.append(_depth(66000, 10000, 10001, nxt, nxt - 1))
        depth.append(_depth(71000, 10000, 10001, nxt + 1, nxt))
        depth.append(_depth(76000, 10000, 10001, nxt + 2, nxt + 1))
        depth.append(_depth(81000, 10010, 10011, nxt + 3, nxt + 2))
        depth.append(_depth(86000, 10010, 10011, nxt + 4, nxt + 3))
        # Cancel lenta (11000ms): efectiva en 92000. El ciclo de grilla
        # 91000 cae dentro de la espera (86500 < 91000 < 92000) y es el
        # ciclo observacional post-fill.
        engine = _engine(cancel_latency_ms=11000)
        coord = _coord(depth, [], engine, qty_lots=3)
        probe = _coord(list(depth), [], _engine(), qty_lots=3)
        probe.run()
        buy_price = _ledger_rows(probe, 66000, side="BUY",
                                 reason="submitted")[0]["price_ticks"]
        # El mercado se mueve DURANTE la espera (91000 < efectiva 92000):
        # el reemplazo debe usar este libro, no el precio guardado.
        depth.append(_depth(91000, 10020, 10021, nxt + 5, nxt + 4))
        depth.append(_depth(96000, 10020, 10021, nxt + 6, nxt + 5))
        # Trade chico (1 < 3): parcial contra la orden BUY de 3.
        trades = [_trade(86500, "partbuy", buy_price, 1, True)]
        engine2 = _engine(cancel_latency_ms=11000)
        coord2 = _coord(depth, trades, engine2, qty_lots=3)
        coord2.run()
        # 1. Cancel pedida en 81000 (edad 15s) con qty 3
        canc = _ledger_rows(coord2, 81000, side="BUY",
                            reason="cancel_requested")
        self.assertEqual(len(canc), 1)
        self.assertEqual(canc[0]["qty_lots"], 3)
        old_oid = canc[0]["order_id"]
        # 2. En 86000 (pre-fill) el lado sigue ocupado: cancel_pending
        # con el total 3, sin submits del lado.
        pend = _ledger_rows(coord2, 86000, side="BUY",
                            reason="cancel_pending")
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["qty_lots"], 3)
        self.assertEqual(
            _ledger_rows(coord2, 86000, side="BUY", reason="submitted"), [])
        # 3. Ciclo observacional 91000: después del parcial (86500) y
        # antes de la cancelación efectiva (92000). Remanente 2, lado
        # ocupado, sin reemplazo, cash/inventario exactos.
        pend9 = _ledger_rows(coord2, 91000, side="BUY",
                             reason="cancel_pending")
        self.assertEqual(len(pend9), 1)
        self.assertEqual(pend9[0]["qty_lots"], 2)
        self.assertEqual(pend9[0]["order_id"], old_oid)
        self.assertEqual(
            _ledger_rows(coord2, 91000, side="BUY", reason="submitted"), [])
        fills = [f for f in engine2.fills if f.side == "BUY"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].qty, 1.0)
        self.assertEqual(engine2.inventory_lots, 1)
        self.assertEqual(engine2.cash_units, -buy_price)
        live = engine2.orders[old_oid]
        self.assertEqual(live["remaining_lots"], 2)
        # 4. Contabilidad del remanente: fill de 1, quedan 2 pendientes,
        # inventario +1.
        # La orden muere cancelada (no filled): el remanente nunca se
        # llenó del todo y no hubo reemplazo prematuro.
        self.assertEqual(live["status"], "cancelled")
        # 5. Tras la cancelación efectiva (92000, drenada en 96000) el
        # inventario es +1 (fill parcial): BUY ya no puede reemplazarse
        # (regla de lados: en largo solo SELL) y SELL se reemplaza
        # recalculado con el mercado nuevo — precio distinto del guardado
        # e id nuevo.
        supp = _ledger_rows(coord2, 96000, side="BUY",
                            reason="suppressed_inventory_side")
        self.assertEqual(len(supp), 1)
        repl = _ledger_rows(coord2, 96000, side="SELL",
                            reason="submitted")
        self.assertEqual(len(repl), 1)
        old_sell = _ledger_rows(coord2, 81000, side="SELL",
                                reason="cancel_requested")[0]
        self.assertNotEqual(repl[0]["order_id"], old_sell["order_id"])
        self.assertNotEqual(repl[0]["price_ticks"],
                            old_sell["price_ticks"])
        cancelled_evts = [j for j in engine2.journal
                          if j.get("event") == "cancelled"
                          and j.get("order_id") == old_oid]
        self.assertEqual(len(cancelled_evts), 1)


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
                    "submitted", "held_unchanged", "held_min_lifetime",
                    "cancel_requested", "cancel_pending", "suppressed_maker",
                    "suppressed_inventory_side", "suppressed_no_book",
                    "suppressed_no_mid"})
        # 71000/76000: precio movido pero órdenes jóvenes (5s/10s < 15s)
        for cycle in (71000, 76000):
            held = _ledger_rows(coord, cycle, reason="held_min_lifetime")
            self.assertEqual(len(held), 2)
            self.assertEqual(
                _ledger_rows(coord, cycle, reason="cancel_requested"), [])
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
