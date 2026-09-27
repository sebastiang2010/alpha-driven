# test_execution_reconstruction_markout_backfill.py
# Point 6: evaluar markouts vencidos y consolidar ventana.
# adverse_5s = signo * (future_mid - fill_price) / fill_price, donde
# future_mid es el mid del PRIMER book con ts >= fill.ts_ms + 5000.

import unittest

from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    ReconstructionConfig,
)


def _book(ts_ms, bid, ask, update_id, pu):
    return {
        "ts_ms": ts_ms,
        "kind": "book",
        "bids": [[bid, 10]],
        "asks": [[ask, 100]],
        "update_id": update_id,
        "pu": pu,
    }


def _submit(ts_ms, order_id="o1", side="BUY", price=10000, qty=10):
    return {
        "ts_ms": ts_ms,
        "kind": "submit",
        "order_id": order_id,
        "side": side,
        "price_ticks": price,
        "qty_lots": qty,
    }


def _trade(ts_ms, trade_id="t1", price=10000, qty=10, buyer_maker=True):
    return {
        "ts_ms": ts_ms,
        "kind": "trade",
        "trade_id": trade_id,
        "price_ticks": price,
        "qty_lots": qty,
        "is_buyer_maker": buyer_maker,
    }


class TestMarkoutBackfill(unittest.TestCase):
    """Markouts 5s: evaluación diferida al llegar el book (vencidos) y
    consolidación de ventana (primer book >= firing_time, no el último)."""

    def _make_rec(self) -> ExecutionReconstructor:
        # Ventana de 5s excede los defaults (max_gap 2000 / max_book_age 1000);
        # se amplían solo para este escenario de markouts.
        config = ReconstructionConfig(max_gap_ms=60000, max_book_age_ms=60000)
        return ExecutionReconstructor(config)

    def _submit_and_fill(self, rec, fill_ts, order_id="o1", side="BUY",
                         price=10000, qty=10, trade_id="t1",
                         buyer_maker=True, uid_start=1, trade_qty=None):
        """Flujo completo: book@1000 -> submit@1000 -> arrival@1040 ->
        trade@fill_ts (orden ya live). Retorna el fill registrado."""
        rec.advance_to(1000, [_book(1000, 10000, 10001, uid_start, uid_start - 1)])
        rec.apply_commands(1000, [_submit(1000, order_id, side, price, qty)])
        # arrival (place_latency 40ms) -> orden live en 1040
        rec.advance_to(1040, [])
        # Cola FIFO: qa = visible + our_qty - our_qty = visible (10 en bids,
        # 100 en asks) -> el trade debe exceder qa para que nos llene.
        if trade_qty is None:
            trade_qty = 25 if side == "BUY" else 150
        trades = [_trade(fill_ts, trade_id, price, trade_qty, buyer_maker)]
        if fill_ts == 1040:
            rec.advance_to(1040, trades)
        else:
            # book intermedio encadenado para mantener frescura/secuencia
            rec.advance_to(
                fill_ts,
                [_book(fill_ts, 10000, 10001, uid_start + 1, uid_start)] + trades,
            )
        fills = [f for f in rec.fills if f.order_id == order_id]
        self.assertGreater(len(fills), 0, "Fill should have been recorded")
        return fills[0]

    def test_adverse_5s_computed_when_fill_arrives_on_time(self):
        """Fill@1100 (firing 6100); book@6100 llega después -> adverse valuado."""
        rec = self._make_rec()
        fill = self._submit_and_fill(rec, 1100, uid_start=1)
        self.assertEqual(fill.ts_ms, 1100)
        self.assertIsNone(fill.adverse_5s, "sin book futuro aún -> pendiente")
        # firing = 1100 + 5000 = 6100; book encadenado (uid3, pu=uid2)
        rec.advance_to(6100, [_book(6100, 9990, 9991, 3, 2)])
        expected = (9990.5 - 10000) / 10000  # BUY: signo +1
        self.assertIsNotNone(fill.adverse_5s)
        assert fill.adverse_5s is not None
        self.assertAlmostEqual(fill.adverse_5s, expected, places=10)

    def test_adverse_5s_uses_first_book_at_or_after_firing(self):
        """Ventana consolidada: books en 6100 (mid 9990.5) y 7000 (mid 9980.5);
        el markout del fill@1100 usa el PRIMERO >= firing (6100), no el último."""
        rec = self._make_rec()
        fill = self._submit_and_fill(rec, 1100, uid_start=1)
        rec.advance_to(6100, [_book(6100, 9990, 9991, 3, 2)])
        first_val = fill.adverse_5s
        self.assertIsNotNone(first_val)
        rec.advance_to(7000, [_book(7000, 9980, 9981, 4, 3)])
        # No se re-evalúa: queda el valor del primer book >= firing
        self.assertEqual(fill.adverse_5s, first_val)
        assert fill.adverse_5s is not None
        self.assertAlmostEqual(fill.adverse_5s, (9990.5 - 10000) / 10000, places=10)

    def test_adverse_5s_none_when_no_future_book_available(self):
        """Sin book >= firing_time, adverse_5s queda None (válido)."""
        rec = self._make_rec()
        fill = self._submit_and_fill(rec, 1100, uid_start=1)
        # Solo books antes del firing (6100): el de 1100 y uno en 3000
        rec.advance_to(3000, [_book(3000, 9990, 9991, 3, 2)])
        self.assertIsNone(fill.adverse_5s)

    def test_adverse_5s_sell_side_sign(self):
        """SELL: adverse = -(future_mid - price)/price."""
        rec = self._make_rec()
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(
            1000, [_submit(1000, "s1", "SELL", 10001, 10)])
        rec.advance_to(1040, [])
        # SELL resting llena con buyer agresor (is_buyer_maker=False)
        rec.advance_to(
            1100,
            [_book(1100, 10000, 10001, 2, 1),
             _trade(1100, "ts1", 10001, 150, False)],
        )
        fills = [f for f in rec.fills if f.order_id == "s1"]
        self.assertGreater(len(fills), 0, "SELL fill should have been recorded")
        fill = fills[0]
        rec.advance_to(6100, [_book(6100, 9990, 9991, 3, 2)])
        expected = -((9990.5 - 10001) / 10001)
        self.assertIsNotNone(fill.adverse_5s)
        assert fill.adverse_5s is not None
        self.assertAlmostEqual(fill.adverse_5s, expected, places=10)

    def test_late_book_marks_reason_without_value(self):
        """Primer book >= firing llega 900ms tarde (> tolerancia 500):
        motivo 'late_book', adverse_5s None (criterio Replay.markouts)."""
        rec = self._make_rec()
        fill = self._submit_and_fill(rec, 1100, uid_start=1)
        # firing = 6100; primer book >= firing en 7000 (900ms tarde)
        rec.advance_to(7000, [_book(7000, 9980, 9981, 3, 2)])
        self.assertEqual(fill.markout_reason, "late_book")
        self.assertIsNone(fill.adverse_5s)

    def test_end_of_data_marks_reason_at_finish(self):
        """Sin book >= firing al cerrar la captura: 'end_of_data'."""
        rec = self._make_rec()
        fill = self._submit_and_fill(rec, 1100, uid_start=1)
        rec.advance_to(3000, [_book(3000, 9990, 9991, 3, 2)])
        self.assertEqual(fill.markout_reason, "pending")
        rec.finish(3000)
        self.assertEqual(fill.markout_reason, "end_of_data")
        self.assertIsNone(fill.adverse_5s)

    def test_ok_reason_on_timely_book(self):
        """Book exactamente en firing -> motivo 'ok' con valor."""
        rec = self._make_rec()
        fill = self._submit_and_fill(rec, 1100, uid_start=1)
        rec.advance_to(6100, [_book(6100, 9990, 9991, 3, 2)])
        self.assertEqual(fill.markout_reason, "ok")
        self.assertIsNotNone(fill.adverse_5s)

    def test_tolerance_boundary_plus_500ms_is_ok(self):
        """Q2: book exactamente a +500ms del firing (límite inclusivo) ->
        motivo 'ok' con adverse valuado."""
        rec = self._make_rec()
        fill = self._submit_and_fill(rec, 1100, uid_start=1)
        # firing = 6100; book en 6600 = +500ms exactos (tolerancia inclusiva)
        rec.advance_to(6600, [_book(6600, 9990, 9991, 3, 2)])
        self.assertEqual(fill.markout_reason, "ok")
        self.assertIsNotNone(fill.adverse_5s)
        assert fill.adverse_5s is not None
        self.assertAlmostEqual(fill.adverse_5s, (9990.5 - 10000) / 10000,
                               places=10)

    def test_tolerance_boundary_plus_501ms_is_late_book(self):
        """Q2: book a +501ms del firing -> 'late_book', adverse None."""
        rec = self._make_rec()
        fill = self._submit_and_fill(rec, 1100, uid_start=1)
        # firing = 6100; book en 6601 = +501ms (fuera de tolerancia)
        rec.advance_to(6601, [_book(6601, 9990, 9991, 3, 2)])
        self.assertEqual(fill.markout_reason, "late_book")
        self.assertIsNone(fill.adverse_5s)


if __name__ == "__main__":
    unittest.main()
