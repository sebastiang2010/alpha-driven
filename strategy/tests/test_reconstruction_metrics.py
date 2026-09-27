# test_reconstruction_metrics.py — F2.5: medición integrada en FinalResult
# costes (promo 0 fees), equity neta, valoración de inventario final,
# fills durante cancelación, tiempo sin cotizar y exposición por lado.
# Motor real, eventos sintéticos; sin credentials ni mercado.

import unittest

from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    ReconstructionConfig,
    ReconstructionMetrics,
)


def _book(ts_ms, bid, ask, update_id, pu):
    return {"ts_ms": ts_ms, "kind": "book", "bids": [[bid, 10]],
            "asks": [[ask, 10]], "update_id": update_id, "pu": pu}


def _submit(ts_ms, order_id, side, price, qty):
    return {"ts_ms": ts_ms, "kind": "submit", "order_id": order_id,
            "side": side, "price_ticks": price, "qty_lots": qty}


def _cancel(ts_ms, order_id):
    return {"ts_ms": ts_ms, "kind": "cancel", "order_id": order_id}


def _trade(ts_ms, trade_id, price, qty, buyer_maker):
    return {"ts_ms": ts_ms, "kind": "trade", "trade_id": trade_id,
            "price_ticks": price, "qty_lots": qty, "is_buyer_maker": buyer_maker}


class TestReconstructionMetrics(unittest.TestCase):
    """Métricas integradas por finish(): F2.5."""

    def _rec(self, **kw):
        kw.setdefault("max_gap_ms", 60000)
        kw.setdefault("max_book_age_ms", 60000)
        return ExecutionReconstructor(ReconstructionConfig(**kw))

    def _filled_buy_rec(self) -> ExecutionReconstructor:
        """BUY o1 qty 5 @10000 llenada por trade 20 a 1100; último book 9990/9991 @2000."""
        rec = self._rec()
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(1000, [_submit(1000, "o1", "BUY", 10000, 5)])
        rec.advance_to(1040, [])
        rec.advance_to(1100, [_book(1100, 10000, 10001, 2, 1),
                              _trade(1100, "t1", 10000, 20, True)])
        rec.advance_to(2000, [_book(2000, 9990, 9991, 3, 2)])
        return rec

    def test_equity_cash_and_valuation(self):
        """Equity neta = cash + inventario valorado al último mid (USDC)."""
        res = self._filled_buy_rec().finish(2000)
        m = res.metrics
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.n_fills, 1)
        # turnover: 10000 ticks * 5 lots * tick 0.0001 * step 1 = 5.0 USDC
        self.assertAlmostEqual(m.turnover_usdc, 5.0, places=9)
        self.assertAlmostEqual(m.cash_usdc, -5.0, places=9)
        self.assertAlmostEqual(m.cash_usdc, res.cash, places=12)
        # promo 0 fees: el motor no modela comisiones
        self.assertEqual(m.fees_usdc, 0.0)
        # último mid 9990.5 ticks * 0.0001 = 0.99905
        self.assertIsNotNone(m.final_mid_usdc)
        assert m.final_mid_usdc is not None
        self.assertAlmostEqual(m.final_mid_usdc, 0.99905, places=9)
        self.assertIsNotNone(m.final_inventory_usdc)
        self.assertIsNotNone(m.net_equity_usdc)
        assert m.final_inventory_usdc is not None and m.net_equity_usdc is not None
        self.assertAlmostEqual(m.final_inventory_xrp, 5.0, places=9)
        self.assertAlmostEqual(m.final_inventory_usdc, 5 * 0.99905, places=9)
        # equity = -5.0 + 4.99525
        self.assertAlmostEqual(m.net_equity_usdc, -5.0 + 5 * 0.99905, places=9)

    def test_exposure_by_side(self):
        """Exposición: máximos long/short a lo largo de la corrida (base 0)."""
        rec = self._rec()
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(1000, [_submit(1000, "b1", "BUY", 10000, 7)])
        rec.advance_to(1040, [])
        rec.advance_to(1100, [_book(1100, 10000, 10001, 2, 1),
                              _trade(1100, "t1", 10000, 17, True)])  # +7
        # cerramos con máximos
        res = rec.finish(2000)
        m = res.metrics
        assert m is not None
        self.assertEqual(m.max_long_lots, 7)
        self.assertEqual(m.max_short_lots, 0)
        self.assertEqual(m.buy_qty_lots, 7)
        self.assertEqual(m.sell_qty_lots, 0)

    def test_short_exposure(self):
        """Short: SELL resting llena con buyer agresor -> max_short negativo."""
        rec = self._rec()
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(1000, [_submit(1000, "s1", "SELL", 10001, 4)])
        rec.advance_to(1040, [])
        rec.advance_to(1100, [_book(1100, 10000, 10001, 2, 1),
                              _trade(1100, "t1", 10001, 14, False)])
        res = rec.finish(2000)
        m = res.metrics
        assert m is not None
        self.assertEqual(m.max_long_lots, 0)
        self.assertEqual(m.max_short_lots, -4)
        self.assertEqual(res.inventory_lots, -4)
        self.assertAlmostEqual(m.final_inventory_xrp, -4.0, places=9)

    def test_fill_during_cancel_counts(self):
        """Fill con cancel ya solicitada: flag en_FILL_ y conteo en métricas."""
        rec = self._rec(cancel_latency_ms=2500)
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(1000, [_submit(1000, "o1", "BUY", 10000, 5)])
        rec.advance_to(1040, [])
        rec.advance_to(2000, [])
        rec.apply_commands(2000, [_cancel(2000, "o1")])  # efectiva 4500
        rec.advance_to(2500, [_book(2500, 10000, 10001, 2, 1),
                              _trade(2500, "t1", 10000, 20, True)])
        res = rec.finish(3000)
        m = res.metrics
        assert m is not None
        self.assertEqual(m.n_fills, 1)
        self.assertEqual(m.fills_during_cancel, 1)
        self.assertTrue(res.fills[0].filled_during_cancel)
        self.assertEqual(rec.orders["o1"]["status"], "filled")

    def test_unquoted_ms_no_orders(self):
        """Sin órdenes: todo el intervalo observado queda sin cotizar."""
        rec = self._rec()
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.advance_to(2000, [_book(2000, 10000, 10001, 2, 1)])
        res = rec.finish(3000)
        m = res.metrics
        assert m is not None
        self.assertEqual(m.coverage_ms, 2000)
        self.assertEqual(m.unquoted_ms, 2000)
        self.assertEqual(m.n_fills, 0)
        # equity con cero inventario: cash 0 + inv 0 = 0
        self.assertAlmostEqual(m.net_equity_usdc or 0.0, 0.0, places=12)

    def test_unquoted_ms_arrival_gap(self):
        """Fuera de cotización: latencia de llegada y el tramo post-fill hasta el fin."""
        res = self._filled_buy_rec().finish(2000)
        m = res.metrics
        assert m is not None
        # pending 1000->1040 (40ms) + tras fill completo 1100->finish 2000 (900ms)
        self.assertEqual(m.unquoted_ms, 940)
        self.assertEqual(m.coverage_ms, 1000)

    def test_no_books_none_fields(self):
        """Sin books: valoración/equity None y coverage 0 (sin adversar)."""
        rec = self._rec()
        # submit sin libro ya falla por _fresh; usamos solo finish sin books
        res = rec.finish(0)
        m = res.metrics
        assert m is not None
        self.assertIsNone(m.final_mid_usdc)
        self.assertIsNone(m.final_inventory_usdc)
        self.assertIsNone(m.net_equity_usdc)
        self.assertEqual(m.coverage_ms, 0)
        self.assertEqual(m.unquoted_ms, 0)
        self.assertEqual(m.n_fills, 0)

    def test_metrics_type(self):
        """FinalResult.metrics es ReconstructionMetrics (API estable)."""
        res = self._filled_buy_rec().finish(2000)
        self.assertIsInstance(res.metrics, ReconstructionMetrics)


if __name__ == "__main__":
    unittest.main()
