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
        rec.advance_to(2000, [_book(2000, 10000, 10001, 3, 2)])
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
        rec.advance_to(2000, [_book(2000, 10000, 10001, 3, 2)])
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
        rec.advance_to(3000, [_book(3000, 10000, 10001, 3, 2)])
        res = rec.finish(3000)
        m = res.metrics
        assert m is not None
        self.assertEqual(m.n_fills, 1)
        self.assertEqual(m.fills_during_cancel, 1)
        self.assertTrue(res.fills[0].filled_during_cancel)
        self.assertEqual(rec.orders["o1"]["status"], "filled")

    def test_unquoted_ms_no_orders(self):
        """Sin órdenes: todo el intervalo observado queda sin cotizar.
        finish() rechaza extensión más allá del último público (F2.5 #3)."""
        rec = self._rec()
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.advance_to(2000, [_book(2000, 10000, 10001, 2, 1)])
        res = rec.finish(2000)  # termina en el último público
        m = res.metrics
        assert m is not None
        self.assertEqual(m.coverage_ms, 1000)
        self.assertEqual(m.unquoted_ms, 1000)
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

    # ── Ajustes del veredicto F2.5 ─────────────────────────────────────

    def test_unquoted_excludes_cancel_wait(self):
        """#1: la espera de cancelación cuenta como cotizado (orden viva)."""
        rec = self._rec(cancel_latency_ms=2000)
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(1000, [_submit(1000, "b1", "BUY", 10000, 5)])
        rec.advance_to(1040, [])  # live
        rec.advance_to(1500, [_book(1500, 10000, 10001, 2, 1)])
        rec.apply_commands(1500, [_cancel(1500, "b1")])  # efectiva 3500
        rec.advance_to(3000, [_book(3000, 10000, 10001, 3, 2)])  # aún viva
        rec.advance_to(3500, [_book(3500, 10000, 10001, 4, 3)])  # effective -> cancelled
        rec.advance_to(4000, [_book(4000, 10000, 10001, 5, 4)])
        res = rec.finish(4000)
        m = res.metrics
        assert m is not None
        # huecos sin cotizar: 1000->1040 (arrival) + 3500->4000 (post-cancel) = 540
        self.assertEqual(m.unquoted_ms, 540)
        self.assertEqual(m.coverage_ms, 3000)
        self.assertEqual(rec.orders["b1"]["status"], "cancelled")
        self.assertEqual(m.n_fills, 0)

    def test_unquoted_two_sides_still_quoted_until_both_gone(self):
        """#1: mientras un lado siga vivo tras cancel, sigue contando como cotizado."""
        rec = self._rec(cancel_latency_ms=500)
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(1000, [_submit(1000, "b1", "BUY", 10000, 5),
                                  _submit(1000, "s1", "SELL", 10001, 5)])
        rec.advance_to(1040, [])  # ambas live
        rec.advance_to(1500, [_book(1500, 10000, 10001, 2, 1)])
        rec.apply_commands(1500, [_cancel(1500, "b1")])  # b1 efectiva 2000; s1 sigue
        rec.advance_to(2000, [_book(2000, 10000, 10001, 3, 2)])  # b1 cancelled aquí
        rec.advance_to(2500, [_book(2500, 10000, 10001, 4, 3)])  # s1 viva
        res = rec.finish(2500)
        m = res.metrics
        assert m is not None
        # solo el arrival gap 1000->1040 queda sin cotizar (s1 siempre live)
        self.assertEqual(m.unquoted_ms, 40)
        self.assertEqual(m.coverage_ms, 1500)

    def test_partial_fill_during_cancel_wait_still_quoted(self):
        """#1: fill parcial durante la espera; el remanente sigue cotizando."""
        rec = self._rec(cancel_latency_ms=1500)
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(1000, [_submit(1000, "b1", "BUY", 10000, 5)])
        rec.advance_to(1040, [])
        rec.advance_to(1500, [_book(1500, 10000, 10001, 2, 1)])
        rec.apply_commands(1500, [_cancel(1500, "b1")])  # efectiva 3000
        # fill parcial mientras la cancel está pendiente (trade 12 > queue 10 -> 2)
        rec.advance_to(2000, [_book(2000, 10000, 10001, 3, 2),
                              _trade(2000, "t1", 10000, 12, True)])
        rec.advance_to(3000, [_book(3000, 10000, 10001, 4, 3)])  # cancelled
        res = rec.finish(3000)
        m = res.metrics
        assert m is not None
        self.assertEqual(m.n_fills, 1)
        self.assertEqual(m.fills_during_cancel, 1)
        self.assertEqual(rec.orders["b1"]["status"], "cancelled")
        # huecos: 1000->1040 arrival = 40; después siempre live hasta 3000
        self.assertEqual(m.unquoted_ms, 1040 - 1000)  # 40ms
        self.assertEqual(m.coverage_ms, 2000)

    def test_costs_nonzero_fees_and_funding(self):
        """#2: con maker_fee y funding != 0, net < gross y campos separados."""
        # 2 fills @ 10000 de 5 lots -> turnover 10.0 USDC; fee 2 bps -> 0.002
        # funding 0.01 por 8h sobre |inv|=10 (XRP) ~1.0 -> ~3.47e-8 por 1000ms
        rec = self._rec(maker_fee_rate=0.0002, funding_rate_per_8h=0.01)
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.apply_commands(1000, [_submit(1000, "b1", "BUY", 10000, 5)])
        rec.advance_to(1040, [])
        rec.advance_to(1100, [_book(1100, 10000, 10001, 2, 1),
                              _trade(1100, "t1", 10000, 20, True)])
        rec.advance_to(2000, [_book(2000, 10000, 10001, 3, 2)])
        res = rec.finish(2000)
        m = res.metrics
        assert m is not None
        self.assertGreater(m.fees_usdc, 0.0)
        self.assertAlmostEqual(m.fees_usdc, m.turnover_usdc * 0.0002, places=12)
        self.assertGreater(m.funding_usdc, 0.0)
        assert m.gross_equity_usdc is not None and m.net_equity_usdc is not None
        self.assertAlmostEqual(
            m.net_equity_usdc,
            m.gross_equity_usdc - m.fees_usdc - m.funding_usdc,
            places=12)
        self.assertEqual(m.slippage_usdc, 0.0)  # ya incluido en precios maker

    def test_costs_zero_is_explicit_scenario_not_default_conclusion(self):
        """#2: con config por defecto (0) fees/funding son 0 pero REPORTADOS,
        y gross == net exactamente; el caller elige el escenario."""
        res = self._filled_buy_rec().finish(2000)
        m = res.metrics
        assert m is not None
        self.assertEqual(m.fees_usdc, 0.0)
        self.assertEqual(m.funding_usdc, 0.0)
        assert m.gross_equity_usdc is not None and m.net_equity_usdc is not None
        self.assertAlmostEqual(m.net_equity_usdc, m.gross_equity_usdc, places=12)

    def test_finish_rejects_extension_beyond_last_public(self):
        """#3: finish(>último público) se rechaza antes de drenar timers."""
        rec = self._rec()
        rec.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec.advance_to(2000, [_book(2000, 10000, 10001, 2, 1)])
        with self.assertRaises(ValueError) as cm:
            rec.finish(3000)
        self.assertIn("beyond last public event", str(cm.exception))
        # se permite terminar exactamente con el último público
        rec2 = self._rec()
        rec2.advance_to(1000, [_book(1000, 10000, 10001, 1, 0)])
        rec2.advance_to(2000, [_book(2000, 10000, 10001, 2, 1)])
        res = rec2.finish(2000)
        self.assertIsNotNone(res.metrics)


if __name__ == "__main__":
    unittest.main()
