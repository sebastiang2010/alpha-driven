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
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from strategy import config
from strategy import execution_engine
from strategy.market_maker import MarketMaker


def _quotes(bid_ok=True, ask_ok=True, bid_price=2.49, ask_price=2.51,
            bid_size=5.0, ask_size=5.0):
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
        oid, ok, reason = self.mm.exec.place_maker_order("BUY", 5.0, 2.49)
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


if __name__ == "__main__":
    unittest.main()