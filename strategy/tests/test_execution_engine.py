# test_execution_engine.py
"""
Tests unitarios del Execution Engine (§9-§10, §0.6):

1. _maker_check_ok (§10): una orden maker NO cruza el spread.
   - BUY es maker si price < best_ask (rechaza price >= best_ask).
   - SELL es maker si price > best_bid (rechaza price <= best_bid).
   - Sin libro de órdenes: rechazo por seguridad.
2. process_fills_from_trades (§0.6): semántica correcta de is_buyer_maker.
   - is_buyer_maker=True  → el seller agredió → llena nuestro BUY resting.
   - is_buyer_maker=False → el buyer agredió  → llena nuestro SELL resting.

Corren con unittest puro (NO pytest):
    python -m unittest strategy.tests.test_execution_engine -v
"""
import sys
import os
import pathlib
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from strategy import config
from strategy import execution_engine
from strategy.execution_engine import ExecutionEngine


def _isolate_log_paths(testcase):
    """Redirige los paths de logging de config a un tmpdir (§15).

    ExecutionEngine.__init__ lee config.LOG_ORDERS / config.LOG_FILLS en
    construcción (y hace os.makedirs de esos directorios). Sin este
    aislamiento, los tests escribirían eventos SIMULATED (dry-run) al
    journal real del día y contaminarían la ventana de gate de una corrida
    mainnet en paralelo ("GATE FAIL: N órdenes marcadas como simuladas").
    Parchear ANTES de construir el engine: el __init__ lee los paths en
    construcción. Se incluyen también LOG_DECISIONS / LOG_PNL / LOG_MARKET_DATA
    por defensa (los usan MarketMaker y el kill switch).
    """
    tmp = tempfile.mkdtemp(prefix="test_logs_")
    for name in ("LOG_ORDERS", "LOG_FILLS", "LOG_DECISIONS", "LOG_PNL",
                 "LOG_MARKET_DATA"):
        patcher = mock.patch.object(config, name, pathlib.Path(tmp) / f"{name}.jsonl")
        patcher.start()
        testcase.addCleanup(patcher.stop)
    return tmp


class FakeBookAPI:
    """Stub de API_binance_futuros con un libro fijo (sin red)."""

    def __init__(self, best_bid=None, best_ask=None):
        self.best_bid = best_bid
        self.best_ask = best_ask

    def get_order_book_top(self, symbol):
        return {"best_bid": self.best_bid, "best_ask": self.best_ask}

    def get_symbol(self, symbol):
        """Exchange info mínimo para que init_symbol_info cargue filtros reales."""
        return {
            "symbol": symbol,
            "status": "TRADING",
            "pricePrecision": 8,
            "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.00000001"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }

    def cancel_order(self, symbol, orig_client_id=None, **kwargs):
        """Cancelación stub: devuelve un dict de éxito (los tests lo parchean)."""
        return {"orderId": 999, "clientOrderId": orig_client_id}

    def buy_limit(self, symbol, quantity, price, **kwargs):
        """Colocación stub: devuelve un dict de éxito (los tests lo parchean)."""
        return {"orderId": 1001, "clientOrderId": kwargs.get("new_client_order_id")}

    def sell_limit(self, symbol, quantity, price, **kwargs):
        """Colocación stub: devuelve un dict de éxito (los tests lo parchean)."""
        return {"orderId": 1002, "clientOrderId": kwargs.get("new_client_order_id")}


class TestMakerCheck(unittest.TestCase):
    """§10: la orden maker no cruza el spread (bug corregido)."""

    def setUp(self):
        self.api = FakeBookAPI(best_bid=0.45, best_ask=0.55)
        patcher = mock.patch.object(execution_engine, "api", self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp_logs = _isolate_log_paths(self)
        self.engine = ExecutionEngine("XRPUSDC", real=True, dry_run=False)

    # ── BUY: maker si price < best_ask ────────────────────────────────
    def test_buy_inside_spread_es_maker(self):
        """BUY @ 0.50 con ask 0.55 NO cruza el spread → se acepta."""
        self.assertTrue(self.engine._maker_check_ok("BUY", 0.50))

    def test_buy_en_best_bid_es_maker(self):
        """BUY @ 0.45 (mejor bid) es maker."""
        self.assertTrue(self.engine._maker_check_ok("BUY", 0.45))

    def test_buy_en_best_ask_se_rechaza(self):
        """BUY @ 0.55 (== best_ask) cruzaría → se rechaza."""
        self.assertFalse(self.engine._maker_check_ok("BUY", 0.55))

    def test_buy_encima_de_ask_se_rechaza(self):
        """BUY @ 0.60 (> best_ask) cruzaría → se rechaza."""
        self.assertFalse(self.engine._maker_check_ok("BUY", 0.60))

    # ── SELL: maker si price > best_bid ───────────────────────────────
    def test_sell_inside_spread_es_maker(self):
        """SELL @ 0.50 con bid 0.45 NO cruza el spread → se acepta."""
        self.assertTrue(self.engine._maker_check_ok("SELL", 0.50))

    def test_sell_en_best_ask_es_maker(self):
        """SELL @ 0.55 (mejor ask) es maker."""
        self.assertTrue(self.engine._maker_check_ok("SELL", 0.55))

    def test_sell_en_best_bid_se_rechaza(self):
        """SELL @ 0.45 (== best_bid) cruzaría → se rechaza."""
        self.assertFalse(self.engine._maker_check_ok("SELL", 0.45))

    def test_sell_debajo_de_bid_se_rechaza(self):
        """SELL @ 0.40 (< best_bid) cruzaría → se rechaza."""
        self.assertFalse(self.engine._maker_check_ok("SELL", 0.40))

    # ── Sin libro: seguridad ───────────────────────────────────────────
    def test_sin_libro_se_rechaza(self):
        no_book = FakeBookAPI(best_bid=None, best_ask=None)
        with mock.patch.object(execution_engine, "api", no_book):
            self.assertFalse(self.engine._maker_check_ok("BUY", 0.50))


class TestProcessFillsFromTrades(unittest.TestCase):
    """§0.6: semántica de is_buyer_maker en el matching de fills (bug corregido)."""

    def setUp(self):
        # dry_run=True: agrega órdenes SIMULATED sin tocar la API.
        # init_symbol_info se mockea para no intentar get_symbol con client=None.
        patcher = mock.patch.object(ExecutionEngine, "init_symbol_info", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp_logs = _isolate_log_paths(self)
        self.engine = ExecutionEngine("XRPUSDC", real=False, dry_run=True)

    def _place(self, side, price, qty=1.0):
        oid, ok, reason = self.engine.place_maker_order(side, qty, price)
        self.assertTrue(ok, reason)
        return oid

    def test_buy_resting_se_llena_cuando_seller_agrede(self):
        """Nuestro BUY resting se llena con is_buyer_maker=True (seller agredió)."""
        oid = self._place("BUY", 0.50)
        filled = self.engine.process_fills_from_trades([
            {"price": "0.50", "qty": "1.0", "is_buyer_maker": True}
        ])
        self.assertIn(oid, filled)
        # La orden se purga de open_orders (§0.6) y el fill queda registrado.
        self.assertNotIn(oid, self.engine.open_orders)
        self.assertEqual(self.engine.fills[-1]["side"], "BUY")
        self.assertEqual(self.engine.fills[-1]["status"], "FILLED")

    def test_buy_resting_no_se_llena_cuando_buyer_agrede(self):
        """is_buyer_maker=False (buyer agredió) NO llena nuestro BUY resting."""
        oid = self._place("BUY", 0.50)
        filled = self.engine.process_fills_from_trades([
            {"price": "0.50", "qty": "1.0", "is_buyer_maker": False}
        ])
        self.assertNotIn(oid, filled)
        self.assertIn(oid, self.engine.open_orders)

    def test_sell_resting_se_llena_cuando_buyer_agrede(self):
        """Nuestro SELL resting se llena con is_buyer_maker=False (buyer agredió)."""
        oid = self._place("SELL", 0.50)
        filled = self.engine.process_fills_from_trades([
            {"price": "0.50", "qty": "1.0", "is_buyer_maker": False}
        ])
        self.assertIn(oid, filled)
        # La orden se purga de open_orders (§0.6) y el fill queda registrado.
        self.assertNotIn(oid, self.engine.open_orders)
        self.assertEqual(self.engine.fills[-1]["side"], "SELL")
        self.assertEqual(self.engine.fills[-1]["status"], "FILLED")

    def test_sell_resting_no_se_llena_cuando_seller_agrede(self):
        """is_buyer_maker=True (seller agredió) NO llena nuestro SELL resting."""
        oid = self._place("SELL", 0.50)
        filled = self.engine.process_fills_from_trades([
            {"price": "0.50", "qty": "1.0", "is_buyer_maker": True}
        ])
        self.assertNotIn(oid, filled)
        self.assertIn(oid, self.engine.open_orders)

    def test_precio_distinto_no_llena(self):
        """Trade a precio distinto del tick de la orden no llena."""
        oid = self._place("BUY", 0.50)
        filled = self.engine.process_fills_from_trades([
            {"price": "0.51", "qty": "1.0", "is_buyer_maker": True}
        ])
        self.assertNotIn(oid, filled)
        self.assertIn(oid, self.engine.open_orders)


class _FakeBinanceError(Exception):
    """Sentinel para simular BinanceAPIException sin depender de su firma.

    Se parchea sobre execution_engine.BinanceAPIException en el test: el motor
    usa isinstance(result, BinanceAPIException) en tiempo de llamada, así que
    cualquier clase que parcheemos ahí funciona (robusto a versiones de binance).
    """


class TestCancelOrderById(unittest.TestCase):
    """§0.6: cancel_order_by_id es API-first en modo real (evita órdenes fantasma)."""

    def setUp(self):
        self.api = FakeBookAPI(best_bid=0.45, best_ask=0.55)
        self.cancel_mock = mock.patch.object(
            self.api, "cancel_order", return_value={"orderId": 123}
        ).start()
        self.addCleanup(mock.patch.stopall)
        patcher = mock.patch.object(execution_engine, "api", self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp_logs = _isolate_log_paths(self)
        self.engine = ExecutionEngine("XRPUSDC", real=True, dry_run=False)

    def _place(self, side="BUY", price=0.50, qty=12.0):
        oid, ok, reason = self.engine.place_maker_order(side, qty, price)
        self.assertTrue(ok, reason)
        return oid

    def test_real_cancel_ok_purga_local(self):
        """Cancel exitoso en la API → la orden se purga localmente."""
        oid = self._place()
        self.assertTrue(self.engine.cancel_order_by_id(oid))
        self.assertNotIn(oid, self.engine.open_orders)
        self.cancel_mock.assert_called_once()

    def test_real_cancel_falla_no_purga_local(self):
        """Si la API falla, la orden NO se purga (evita orden fantasma §0.6)."""
        with mock.patch.object(execution_engine, "BinanceAPIException",
                               _FakeBinanceError):
            with mock.patch.object(self.api, "cancel_order",
                                   return_value=_FakeBinanceError("Unknown order sent.")):
                oid = self._place()
                self.assertFalse(self.engine.cancel_order_by_id(oid))
                self.assertIn(oid, self.engine.open_orders)

    def test_dry_run_purga_sin_api(self):
        """En dry-run no hay API: se purga directamente."""
        _isolate_log_paths(self)
        engine = ExecutionEngine("XRPUSDC", real=False, dry_run=True)
        oid, ok, reason = engine.place_maker_order("BUY", 12.0, 0.50)
        self.assertTrue(ok, reason)
        self.assertTrue(engine.cancel_order_by_id(oid))
        self.assertNotIn(oid, engine.open_orders)


if __name__ == "__main__":
    unittest.main()
