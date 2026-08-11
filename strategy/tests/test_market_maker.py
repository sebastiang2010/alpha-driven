# test_market_maker.py
"""
Tests de regresión del orquestador (§0.6) — fixes de la auditoría 2026-08-09:

1. _manage_orders respeta disable_new_entries (§13): con el kill switch
   activo NO se colocan lados faltantes.
2. _manage_orders reemplaza la orden expirada en el MISMO ciclo: has_bid/
   has_ask se recalculan después del loop de expire/replace (sin 1 ciclo
   de latencia → sin orden duplicada).
3. _reduce_or_close en dry-run simula el cierre (§13): inventario a 0.
4. _reduce_or_close en modo real sin API degrada con gracia (no lanza).

Corren con unittest puro (NO pytest):
    python -m unittest strategy.tests.test_market_maker -v
"""
import os
import sys
import pathlib
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from strategy import config
from strategy import execution_engine
from strategy.market_maker import MarketMaker


def _isolate_log_paths(testcase):
    """Redirige los paths de logging de config a un tmpdir (§15).

    MarketMaker.__init__ construye un ExecutionEngine real que lee
    config.LOG_ORDERS / config.LOG_FILLS en construcción, y guarda su
    journal de decisiones en config.LOG_DECISIONS (el kill switch escribe
    además en config.LOG_PNL). Sin este aislamiento, los tests escribirían
    eventos SIMULATED (dry-run) al journal real del día y contaminarían la
    ventana de gate de una corrida mainnet en paralelo ("GATE FAIL: N
    órdenes marcadas como simuladas"). Parchear ANTES de construir el
    MarketMaker: el __init__ lee los paths en construcción.
    """
    tmp = tempfile.mkdtemp(prefix="test_logs_")
    for name in ("LOG_ORDERS", "LOG_FILLS", "LOG_DECISIONS", "LOG_PNL",
                 "LOG_MARKET_DATA"):
        patcher = mock.patch.object(config, name, pathlib.Path(tmp) / f"{name}.jsonl")
        patcher.start()
        testcase.addCleanup(patcher.stop)
    return tmp


def _quotes(bid_ok=True, ask_ok=True, bid_price=2.49, ask_price=2.51,
            bid_size=config.BASE_ORDER_SIZE_XRP, ask_size=config.BASE_ORDER_SIZE_XRP):
    """Dict de quotes con la forma que consume _manage_orders."""
    return {
        "quote_bid_ok": bid_ok,
        "quote_ask_ok": ask_ok,
        "bid_price": bid_price,
        "ask_price": ask_price,
        "bid_size": bid_size,
        "ask_size": ask_size,
    }


class _MarketMakerTestCase(unittest.TestCase):
    """Base: api=None (offline) para que init_symbol_info y el maker check
    no toquen la red (API_binance_futuros existe en el repo)."""

    def setUp(self):
        patcher = mock.patch.object(execution_engine, "api", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp_logs = _isolate_log_paths(self)
        self.mm = MarketMaker(dry_run=True)


class TestManageOrdersDisableNewEntries(_MarketMakerTestCase):
    """§13: con disable_new_entries activo no se colocan lados faltantes."""

    def test_no_coloca_ordenes_con_kill_switch(self):
        self.mm.disable_new_entries = True
        self.mm._manage_orders({"mid": 2.5}, _quotes())
        self.assertEqual(self.mm.exec.get_open_order_count(), 0)


class TestManageOrdersExpireReplace(_MarketMakerTestCase):
    """§0.6: el reemplazo por expiración ocurre en el mismo ciclo (sin duplicado)."""

    def test_orden_expirada_se_reemplaza_sin_duplicar(self):
        oid, ok, reason = self.mm.exec.place_maker_order("BUY", config.BASE_ORDER_SIZE_XRP, 2.49)
        self.assertTrue(ok, reason)
        # Forzar expiración: edad > MAX_ORDER_LIFETIME_SEC.
        self.mm.quote_age[oid] = time.time() - (config.MAX_ORDER_LIFETIME_SEC + 1)

        # Solo el lado bid cotiza; el ask queda fuera para aislar el caso.
        self.mm._manage_orders({"mid": 2.5}, _quotes(ask_ok=False))

        orders = self.mm.exec.get_orders()
        buys = [o for o in orders.values() if o["side"] == "BUY"]
        # La vieja se canceló y la nueva se colocó en el mismo ciclo:
        # exactamente 1 orden BUY (antes del fix eran 2: reemplazo + duplicado).
        self.assertNotIn(oid, self.mm.exec.open_orders)
        self.assertEqual(len(buys), 1)
        self.assertNotEqual(buys[0]["client_order_id"], oid)


class TestReduceOrClose(_MarketMakerTestCase):
    """§13: reduce/close simulado en dry-run y degradación sin API en real."""

    def test_dry_run_simula_cierre(self):
        self.mm.inventory.record_fill("BUY", 20.0, 2.5, 0.0)
        self.assertEqual(self.mm.inventory.inventory, 20.0)
        self.mm._reduce_or_close({"mid": 2.5})
        self.assertEqual(self.mm.inventory.inventory, 0.0)

    def test_real_sin_api_no_lanza(self):
        """Modo real con API offline: place_maker_order degrada con gracia."""
        self.mm.exec.dry_run = False
        self.mm.inventory.record_fill("BUY", 20.0, 2.5, 0.0)
        # No debe lanzar: el motor devuelve (None, False, razón).
        self.mm._reduce_or_close({"mid": 2.5})
        # Sin API no se pudo cerrar: el inventario queda pendiente.
        self.assertEqual(self.mm.inventory.inventory, 20.0)

    def test_sin_inventario_no_hace_nada(self):
        self.mm._reduce_or_close({"mid": 2.5})
        self.assertEqual(self.mm.inventory.inventory, 0.0)


class TestFixMinNotional(_MarketMakerTestCase):
    """§0.2 fix 2026-08-10 (aprobado por humano): floor de notional mínimo.

    Con MIN_NOTIONAL_USDC=5.0 y QUANTITY_STEP=0.1:
        floor_qty = ceil(MIN_NOTIONAL_USDC / price / QUANTITY_STEP) * QUANTITY_STEP

    (a) sin inventario: tamaño base (BASE_ORDER_SIZE_XRP) tal cual.
    (b) lado por debajo del floor y floor alcanzable (<= max_order_size):
        el tamaño se SUBE al floor.
    (c) floor inalcanzable (> max_order_size): el lado NO cotiza
        (quote_ok=False, reason min_notional_floor_blocked:<side>).
    (d) cierre TOTAL de inventario con floor > max_order_size: se permite
        el tamaño menor (== |inventory|) — la orden va reduce_only y
        Binance exime reduceOnly del minNotional (announcement 2021-01-20).
    """

    def _patch_alpha(self, r=1.0, pnl=0.0001):
        """Fija la decisión de alpha: reserva en r, sin distancias, PnL +."""
        for name, value in (
            ("compute_alpha", 0.0),
            ("reservation_price", r),
            ("quote_distances", (0.0, 0.0)),
            ("expected_net_pnl_estimate", pnl),
        ):
            patcher = mock.patch.object(self.mm.alpha, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _compute(self, mid):
        return self.mm._compute_quotes({"mid": mid, "volatility": 0.0})

    def test_a_sin_inventario_usa_base(self):
        self._patch_alpha(r=1.0)
        q = self._compute(1.0)
        self.assertEqual(q["bid_size"], config.BASE_ORDER_SIZE_XRP)
        self.assertEqual(q["ask_size"], config.BASE_ORDER_SIZE_XRP)
        self.assertTrue(q["quote_bid_ok"])

    def test_b_fix_min_notional_bid_floor_applied(self):
        """LONG 4.9: el bid de reposición (0.5x = 2.5) < floor 5.0 -> 5.0."""
        self.mm.inventory.record_fill("BUY", 4.9, 1.0, 0.0)
        self._patch_alpha(r=1.0)
        q = self._compute(1.0)
        self.assertEqual(q["bid_size"], 5.0)  # floor aplicado
        self.assertTrue(q["quote_bid_ok"])

    def test_c_fix_min_notional_floor_side_unreachable(self):
        """LONG 4.9 con mid 0.95: floor 5.3 > max_order_size 5.0 -> no cotiza."""
        self.mm.inventory.record_fill("BUY", 4.9, 0.95, 0.0)
        self._patch_alpha(r=0.95)
        q = self._compute(0.95)
        self.assertFalse(q["quote_bid_ok"])
        self.assertEqual(q["bid_size"], 0.0)
        self.assertIn("min_notional_floor_blocked:bid", q["reasons"])

    def test_d_fix_min_notional_cierre_total_exento(self):
        """SHORT 4.9 con max_order_size 4.9: bid 4.9 == |inv| (cierre total)
        queda exento del floor (reduce_only) y SÍ cotiza."""
        self.mm.inventory.record_fill("SELL", 4.9, 1.0, 0.0)
        self._patch_alpha(r=1.0)
        with mock.patch.object(self.mm.risk, "max_order_size", 4.9):
            q = self._compute(1.0)
        self.assertEqual(q["bid_size"], 4.9)  # tamaño de cierre, sin floor
        self.assertTrue(q["quote_bid_ok"])
        self.assertNotIn("min_notional_floor_blocked:bid", q["reasons"])


class TestReduceOnlyPropagation(_MarketMakerTestCase):
    """§0.2 fix 2026-08-10: _place_order marca reduce_only SOLO cuando la
    orden es de cierre/reducción (qty <= |inventory| del lado reduce).

    - reduce_only=True  -> Binance exime el minNotional (permite cerrar
      inventario < $5) y el engine salta el check de notional.
    - reduce_only=False -> cuando la orden excede el inventario del lado
      reduce o es del lado agravante: Binance rechaza reduceOnly con
      qty > posición (error -2022).
    """

    def _place(self, side, qty):
        captured = {"reduce_only": None}

        def fake_place(*args, **kwargs):
            captured["reduce_only"] = kwargs.get("reduce_only", False)
            return (123, True, "ok")

        with mock.patch.object(self.mm.exec, "place_maker_order", side_effect=fake_place), \
                mock.patch.object(self.mm.risk, "check_order", return_value=(True, [])):
            result = self.mm._place_order(side, qty, 1.0, mid=1.0)
        # Si el Risk Engine lo rechazó no llegó al engine: el test está mal formado.
        self.assertIsNotNone(result, "la orden no llegó a place_maker_order")
        return captured["reduce_only"]

    def test_sell_cierre_parcial_long(self):
        self.mm.inventory.record_fill("BUY", 4.9, 1.0, 0.0)
        self.assertTrue(self._place("SELL", 4.9))

    def test_sell_que_excede_inventario_no_reduce(self):
        """LONG 4.9, SELL 5.0: excede la posición -> reduceOnly=False (anti -2022)."""
        self.mm.inventory.record_fill("BUY", 4.9, 1.0, 0.0)
        self.assertFalse(self._place("SELL", 5.0))

    def test_buy_cierre_total_short(self):
        self.mm.inventory.record_fill("SELL", 4.9, 1.0, 0.0)
        self.assertTrue(self._place("BUY", 4.9))

    def test_buy_lado_agravante_nunca_reduce(self):
        """LONG: comprar agrava -> reduceOnly siempre False."""
        self.mm.inventory.record_fill("BUY", 4.9, 1.0, 0.0)
        self.assertFalse(self._place("BUY", 4.9))

    def test_sin_inventario_nunca_reduce(self):
        self.assertFalse(self._place("SELL", 5.0))


if __name__ == "__main__":
    unittest.main()