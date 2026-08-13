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


class TestMinNotionalGuard(_MarketMakerTestCase):
    """§3, fix 2026-08-11: un lado cuyo notional quede bajo minNotional NO se
    cotiza (antes: rechazo ~5/s en loop "notional 2.52 < minNotional 5.0"
    con el lado agravante 2.5 XRP del skew §11). El guard es DINÁMICO con el
    precio: minNotional está en USDC, así que si XRP sube, el mismo tamaño
    cumple el piso y el lado vuelve a cotizar solo (sin intervención)."""

    def _setup(self, mid, inventory):
        """Alpha mockeado (reservación = mid, distancias ±0.01, NetPnL > 0)
        y filtros del símbolo con minNotional 5.0."""
        self.mm.alpha.compute_alpha = mock.Mock(return_value=0.0)
        self.mm.alpha.reservation_price = mock.Mock(return_value=mid)
        self.mm.alpha.quote_distances = mock.Mock(return_value=(0.01, 0.01))
        self.mm.alpha.expected_net_pnl_estimate = mock.Mock(return_value=1.0)
        self.mm.exec.symbol_info = {
            "min_notional": 5.0, "tick_size": 0.0001, "step_size": 0.1,
        }
        if inventory > 0:
            self.mm.inventory.record_fill("BUY", inventory, mid, 0.0)
        elif inventory < 0:
            self.mm.inventory.record_fill("SELL", abs(inventory), mid, 0.0)
        return self.mm._compute_quotes(
            {"mid": mid, "inventory": inventory, "volatility": 0.0}
        )

    def test_lado_agravante_bajo_notional_se_sube_al_piso(self):
        """Corto −5.0 @ ~$1.2: el SELL agrava (skew 0.5x → 2.5 XRP →
        $3.03 < $5). Fix 2026-08-12: en vez de rechazar (one-sided), se sube
        el tamaño al mínimo que cumple notional (ceil(5/1.21/0.1)*0.1 = 4.2
        XRP → $5.08 ≥ $5) y AMBOS lados cotizan two-sided para capturar el
        spread en vez de salir en precio adverso."""
        q = self._setup(mid=1.2, inventory=-5.0)
        self.assertTrue(q["quote_bid_ok"], q["reasons"])
        self.assertTrue(q["quote_ask_ok"], q["reasons"])
        self.assertNotIn("below_min_notional:ask", q["reasons"])
        # el ask se subió al piso notional en vez de descartarse
        self.assertGreaterEqual(q["ask_size"] * q["ask_price"], 5.0 - 1e-9)

    def test_xrp_subio_y_el_lado_vuelve_a_cotizar(self):
        """Futuro: XRP sube a ~$2.2 → el mismo tamaño 2.5 XRP ya cumple
        minNotional ($5.53 ≥ $5) → el lado agravante vuelve a cotizar solo,
        sin intervención manual (minNotional en USDC, precio en XRP)."""
        q = self._setup(mid=2.2, inventory=-5.0)
        self.assertTrue(q["quote_bid_ok"], q["reasons"])
        self.assertTrue(q["quote_ask_ok"], q["reasons"])
        self.assertNotIn("below_min_notional:ask", q["reasons"])

    def test_flat_con_precio_bajo_no_cotiza_ningun_lado(self):
        """Inventario 0 y XRP en $0.6: 8.0 XRP = $4.80 < $5 → ningún
        lado se cotiza (evita el spam de órdenes que el exchange rechazaría
        aunque el NetPnL estimado sea positivo)."""
        q = self._setup(mid=0.6, inventory=0.0)
        self.assertFalse(q["quote_bid_ok"], q["reasons"])
        self.assertFalse(q["quote_ask_ok"], q["reasons"])
        self.assertIn("below_min_notional:bid", q["reasons"])
        self.assertIn("below_min_notional:ask", q["reasons"])


if __name__ == "__main__":
    unittest.main()