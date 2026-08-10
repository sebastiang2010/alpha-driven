# test_alpha_model.py
"""
Tests del AlphaModel (§6, §8, §11) — enfocados en la convención de side y en
el cálculo de NetPnL esperado (§9/§18).

Corren con unittest puro (NO pytest):
    python -m unittest strategy.tests.test_alpha_model -v
"""
import math
import sys
import unittest

sys.path.insert(0, r"C:\programas\proyectos\alpha-driven")

from strategy import alpha_model


def _snapshot(mid=2.5, **overrides):
    base = {
        "mid": mid,
        "spread": 0.002,
        "imbalance": 0.1,
        "microprice": 2.501,
        "momentum": 0.001,
        "buy_volume_60s": 500.0,
        "sell_volume_60s": 400.0,
        "volatility": 0.001,
        "inventory": 0.0,
    }
    base.update(overrides)
    return base


class TestExpectedNetPnlEstimate(unittest.TestCase):
    """§0.6: la convención de side no debe romper el cálculo (bug de 0.0)."""

    def setUp(self):
        self.am = alpha_model.AlphaModel()
        self.snap = _snapshot()

    def test_acepta_convencion_bid_ask(self):
        # bid a 2.49 con mid 2.5 => spread capturado 0.01 * 20 = 0.2, fees 0.01
        pnl = self.am.expected_net_pnl_estimate(self.snap, "bid", 2.49, 20.0)
        self.assertGreater(pnl, 0.0)

    def test_acepta_convencion_BUY_SELL(self):
        # "BUY" debe ser equivalente a "bid" (era el bug: devolvía 0.0).
        pnl = self.am.expected_net_pnl_estimate(self.snap, "BUY", 2.49, 20.0)
        self.assertGreater(pnl, 0.0)
        pnl_ask = self.am.expected_net_pnl_estimate(self.snap, "SELL", 2.51, 20.0)
        self.assertGreater(pnl_ask, 0.0)

    def test_bid_y_BUY_equivalentes(self):
        a = self.am.expected_net_pnl_estimate(self.snap, "bid", 2.49, 20.0)
        b = self.am.expected_net_pnl_estimate(self.snap, "BUY", 2.49, 20.0)
        self.assertAlmostEqual(a, b, places=12)

    def test_ask_y_SELL_equivalentes(self):
        a = self.am.expected_net_pnl_estimate(self.snap, "ask", 2.51, 20.0)
        b = self.am.expected_net_pnl_estimate(self.snap, "SELL", 2.51, 20.0)
        self.assertAlmostEqual(a, b, places=12)

    def test_side_invalido_devuelve_cero(self):
        pnl = self.am.expected_net_pnl_estimate(self.snap, "HOLD", 2.49, 20.0)
        self.assertEqual(pnl, 0.0)

    def test_fees_reducen_pnl(self):
        sin_fee = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0
        )
        con_fee = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0002
        )
        self.assertGreater(sin_fee, con_fee)

    def test_funding_reduce_pnl(self):
        """§18: el funding resta del NetPnL, proporcional al hold time."""
        # hold = 28,800 s (1 intervalo de 8 h) => funding = tasa completa.
        sin_funding = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0,
            funding_rate_per_8h=0.0001, expected_hold_sec=0.0,
        )
        con_funding = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0,
            funding_rate_per_8h=0.0001, expected_hold_sec=28800.0,
        )
        self.assertGreater(sin_funding, con_funding)

    def test_slippage_reduce_pnl(self):
        """§18: el slippage resta del NetPnL (solo si se configura > 0)."""
        sin_slip = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0, slippage_rate=0.0
        )
        con_slip = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0, maker_fee=0.0, slippage_rate=0.0001
        )
        self.assertGreater(sin_slip, con_slip)

    def test_costos_combinados_netpnl_menor(self):
        """§18: NetPnL = GrossPnL - fees - funding - slippage (los tres restan).

        Funding proporcional: con hold=28,800 s (1 intervalo de 8 h) la tasa
        completa entra en el cálculo. Fórmula verificada numéricamente:
        costos = price*qty*(fee + funding_rate_per_8h*(hold/28800) + slippage).
        """
        gross = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0,
            maker_fee=0.0, funding_rate_per_8h=0.0, slippage_rate=0.0,
        )
        net = self.am.expected_net_pnl_estimate(
            self.snap, "bid", 2.49, 20.0,
            maker_fee=0.0002, funding_rate_per_8h=0.0001,
            expected_hold_sec=28800.0, slippage_rate=0.0001,
        )
        self.assertGreater(gross, net)
        expected_costs = 2.49 * 20.0 * (0.0002 + 0.0001 * (28800.0 / 28800.0) + 0.0001)
        self.assertAlmostEqual(gross - net, expected_costs, places=12)


class TestModeloDeCostos(unittest.TestCase):
    """Modelo de costos §18 (decisión 2026-08-09, pendiente de confirmación).

    - funding: proporcional al hold time (Binance cobra cada 8 h sobre notional,
      NO por fill) — antes 0.0001 fijo por trade sobreestimaba ~1000x.
    - slippage: 0 para maker (GTX post-only nunca cruza el spread).
    - Una quote con edge real dentro del spread es operables en mercado tranquilo.
    """

    def setUp(self):
        self.am = alpha_model.AlphaModel()

    def test_funding_hold_cero_es_cero(self):
        """hold=0 => sin exposición al funding: no debe restar nada."""
        base = self.am.expected_net_pnl_estimate(
            _snapshot(mid=1.0), "bid", 0.9997, 20.0, maker_fee=0.0,
            funding_rate_per_8h=0.0001, expected_hold_sec=0.0,
        )
        self.assertGreater(base, 0.0)

    def test_funding_proporcional_al_hold(self):
        """A mayor hold time, mayor costo de funding (proporcionalidad §18)."""
        snap = _snapshot(mid=1.0)
        hold_300 = self.am.expected_net_pnl_estimate(
            snap, "bid", 0.9997, 20.0, maker_fee=0.0,
            funding_rate_per_8h=0.0001, expected_hold_sec=300.0,
        )
        hold_28800 = self.am.expected_net_pnl_estimate(
            snap, "bid", 0.9997, 20.0, maker_fee=0.0,
            funding_rate_per_8h=0.0001, expected_hold_sec=28800.0,
        )
        self.assertGreater(hold_300, hold_28800)
        # Con hold=0 el funding no existe: la diferencia hold_0 - hold_300 es
        # exactamente price*qty*tasa*(300/28800) (única diferencia: funding).
        hold_0 = self.am.expected_net_pnl_estimate(
            snap, "bid", 0.9997, 20.0, maker_fee=0.0,
            funding_rate_per_8h=0.0001, expected_hold_sec=0.0,
        )
        delta = hold_0 - hold_300
        self.assertAlmostEqual(delta, 0.9997 * 20.0 * 0.0001 * (300.0 / 28800.0), places=12)

    def test_slippage_maker_cero_por_defecto(self):
        """El default de slippage maker es 0 (GTX post-only nunca cruza)."""
        self.assertEqual(alpha_model.config.SLIPPAGE_MAKER_BPS, 0.0)
        con_default = self.am.expected_net_pnl_estimate(
            _snapshot(mid=1.0), "bid", 0.9997, 20.0, maker_fee=0.0,
        )
        con_cero = self.am.expected_net_pnl_estimate(
            _snapshot(mid=1.0), "bid", 0.9997, 20.0, maker_fee=0.0, slippage_rate=0.0,
        )
        self.assertAlmostEqual(con_default, con_cero, places=12)

    def test_quote_dentro_del_spread_operable_mercado_tranquilo(self):
        """Gate pasa en mercado tranquilo: quote con edge > costos maker.

        mid=1.0, spread=0.0006, bid a 3 ticks del mid: gross=0.0003*20=0.006;
        fees=1.0*20*0.0002=0.004; funding≈20*0.0001*(300/28800)≈0.00002;
        slippage=0 => NetPnL>0. Con el modelo viejo (fee+funding+slippage=0.0004
        fijos) el mismo quote daba NetPnL<0 y el gate lo rechazaba.
        """
        snap = _snapshot(mid=1.0, spread=0.0006)
        pnl_bid = self.am.expected_net_pnl_estimate(snap, "bid", 0.9997, 20.0)
        self.assertGreater(pnl_bid, 0.0)
        # Contraste con el modelo viejo: costos fijos 0.0004 => NetPnL negativo.
        pnl_viejo = self.am.expected_net_pnl_estimate(
            snap, "bid", 0.9997, 20.0,
            maker_fee=0.0002, funding_rate_per_8h=0.0001,
            expected_hold_sec=28800.0, slippage_rate=0.0001,
        )
        self.assertLess(pnl_viejo, 0.0)


class TestComputeAlpha(unittest.TestCase):
    """§6: señal acotada y con términos nulos bien manejados."""

    def setUp(self):
        self.am = alpha_model.AlphaModel()

    def test_alpha_sin_datos_devuelve_cero(self):
        self.assertEqual(self.am.compute_alpha({"mid": 0.0}), 0.0)
        self.assertEqual(self.am.compute_alpha({}), 0.0)

    def test_alpha_acotada_por_ALPHA_MAX(self):
        snap = _snapshot(mid=2.5, momentum=1.0, imbalance=1.0,
                         microprice=100.0, buy_volume_60s=1e9, sell_volume_60s=1.0)
        alpha = self.am.compute_alpha(snap)
        self.assertLessEqual(abs(alpha), alpha_model.ALPHA_MAX)

    def test_microprice_cero_anula_termino(self):
        snap = _snapshot(microprice=0.0)
        alpha = self.am.compute_alpha(snap)
        # Sin microprice, la señal sigue siendo finita y acotada.
        self.assertLessEqual(abs(alpha), alpha_model.ALPHA_MAX)


class TestAlphaSinDobleConteoEnQuotes(unittest.TestCase):
    """Regresión del hallazgo gate 2026-08-09 (bug §8).

    §8 define r = S + alpha: el alpha ya desplaza el precio de reserva
    (reservation_price). Con ALPHA_QUOTE_FACTOR=1.0 (bug corregido a 0.0) el
    |alpha| se SUMABA ademas a la distancia de quotes -> con alpha=±0.01 el
    lado alejado quedaba a ±2% del mid y el cercano sin borde (NetPnL≤0,
    gate falló en vivo: run 22:26 UTC, ask=1.0622 con mid=1.04215).

    Estos tests fallan si alguien vuelve a ensanchar las distancias con alpha.
    """

    def setUp(self):
        self.am = alpha_model.AlphaModel()
        self.snap = _snapshot(mid=2.5, spread=0.002, volatility=0.001,
                              imbalance=0.0, momentum=0.0, microprice=0.0)

    def test_alpha_no_ensancha_las_distancias(self):
        """Las distancias de quote deben ser independientes de alpha (§8)."""
        d0_bid, d0_ask = self.am.quote_distances(self.snap, 0.0, 0.0, 0.001)
        dmax_bid, dmax_ask = self.am.quote_distances(
            self.snap, alpha_model.ALPHA_MAX, 0.0, 0.001
        )
        self.assertAlmostEqual(d0_bid, dmax_bid, places=12)
        self.assertAlmostEqual(d0_ask, dmax_ask, places=12)

    def test_ask_se_desplaza_solo_por_reservacion(self):
        """La única influencia de alpha en el ask es via reservation_price (§8).

        ask(alpha) - ask(0) debe ser exactamente 'alpha' (desplazamiento de la
        reserva). Con el bug viejo (ALPHA_QUOTE_FACTOR=1.0) el |alpha| ademas
        ensanchaba la distancia -> el delta era 2*alpha.
        """
        alpha = alpha_model.ALPHA_MAX
        r_alpha = self.am.reservation_price(self.snap, alpha, 0.0, 0.001)
        _, ask_dist_alpha = self.am.quote_distances(self.snap, alpha, 0.0, 0.001)
        r_cero = self.am.reservation_price(self.snap, 0.0, 0.0, 0.001)
        _, ask_dist_cero = self.am.quote_distances(self.snap, 0.0, 0.0, 0.001)
        delta = (r_alpha + ask_dist_alpha) - (r_cero + ask_dist_cero)
        self.assertAlmostEqual(delta, alpha, places=9)


class TestPisoDeSpread(unittest.TestCase):
    """Piso de spread MIN_SPREAD_TICKS=8 (§11, aprobado §0.2 2026-08-10).

    Breakeven contra fees maker: N = 2*f_maker*P/tick = 2*0.0002*1.026/0.0001
    = 4.1 ticks -> piso de 8 ticks = 1.95x las fees (decisión tras round
    trip mainnet con adverse selection -0.0113 USDC: no existía piso).

    Verifica:
    (a) sigma->0            => spread resultante == 8 ticks exacto.
    (b) sigma alto          => spread ya supera el piso: comportamiento
                               previo intacto (sin cambios).
    (c) skew de inventario  => el piso se mantiene y el skew se aplica
                               encima (bid - ask == 2*skew esperado).
    """

    def setUp(self):
        self.am = alpha_model.AlphaModel()
        self.tick = alpha_model.config.TICK_SIZE_XRPUSDC
        self.floor = alpha_model.config.MIN_SPREAD_TICKS * self.tick

    def _snap_quieta(self, mid=2.5, **overrides):
        base = {"spread": 0.0, "volatility": 0.0, "imbalance": 0.0,
                "momentum": 0.0, "microprice": 0.0, "inventory": 0.0}
        base.update(overrides)
        return _snapshot(mid=mid, **base)

    def test_sigma_cero_spread_igual_piso_exacto(self):
        """(a) sigma->0: el piso domina -> bid_dist + ask_dist == 8 ticks."""
        snap = self._snap_quieta()
        bid, ask = self.am.quote_distances(snap, 0.0, 0.0, 0.0)
        self.assertAlmostEqual(bid + ask, self.floor, places=12)
        # Ensanche simétrico desde el mid: ambos lados iguales.
        self.assertAlmostEqual(bid, ask, places=12)
        self.assertAlmostEqual(bid, self.floor / 2.0, places=12)

    def test_sigma_alto_no_alterado_por_piso(self):
        """(b) sigma alto: spread >> piso -> el cálculo previo queda intacto."""
        snap = self._snap_quieta(mid=2.5, spread=0.002, volatility=0.001)
        bid, ask = self.am.quote_distances(snap, 0.0, 0.0, 0.001)
        self.assertGreater(bid + ask, self.floor)
        # Referencia explícita de la fórmula previa (sin piso):
        # base = spread/2 + K*sigma_efectiva, con sigma_ef = sigma*sqrt(12)*sqrt(5)
        sigma_ef = 0.001 * math.sqrt(12.0) * math.sqrt(5.0)
        base = 0.001 + sigma_ef
        self.assertAlmostEqual(bid, base, places=12)
        self.assertAlmostEqual(ask, base, places=12)

    def test_skew_inventario_se_aplica_sobre_piso(self):
        """(c) inventario long + sigma>0 con base < piso:
        - el spread total queda en el piso (el piso se mantiene);
        - el skew se aplica ENCIMA del piso (bid - ask == 2*skew)."""
        mid = 2.5
        sigma = 0.00002
        inv = 10.0  # nocional = 25 USDC = MAX_POSITION_NOTIONAL_USDC -> ratio 1.0
        snap = self._snap_quieta(mid=mid, volatility=sigma, inventory=inv)
        bid, ask = self.am.quote_distances(snap, 0.0, inv, sigma)
        # Piso se mantiene pese al skew.
        self.assertAlmostEqual(bid + ask, self.floor, places=12)
        # Inventario long -> bid más lejos del mid que el ask.
        self.assertGreater(bid, ask)
        # El skew (2*skew = bid - ask) se preserva tal cual lo calcula el modelo.
        sigma_ef = sigma * math.sqrt(12.0) * math.sqrt(5.0)
        skew = ((inv * mid) / alpha_model.config.MAX_POSITION_NOTIONAL_USDC
                * alpha_model.INVENTORY_SKEW_MULTIPLIER * sigma_ef)
        self.assertAlmostEqual(bid - ask, 2.0 * skew, places=12)


class TestFiltroMomentum(unittest.TestCase):
    """Filtro de momentum anti-adverse-selection (§14, config MOMENTUM_*).

    Evidencia (2026-08-10, mainnet): con piso de 8 ticks el mercado cayó
    ~0.9% en ~50 min; el bid se llenó primero (compras en caída) y el ask
    no (RTs -0.0118/-0.0128 USDC). Regla implementada:

        Si |mid_actual - mid_inicio_ventana| >= MOMENTUM_MAX_TICKS * tick_size
        dentro de MOMENTUM_WINDOW_SECONDS (o el cooldown de
        MOMENTUM_COOLDOWN_SECONDS sigue vigente), el piso efectivo pasa de
        MIN_SPREAD_TICKS (8) a MIN_SPREAD_TICKS * MOMENTUM_SPREAD_MULTIPLIER
        (16). SIMÉTRICO (usa |Δmid|) y el skew de inventario se aplica igual.

    Verifica (sin red, con historial sintético vía record_mid + now_sec):
    (a) mid estable  -> piso normal (8 ticks).
    (b) movimiento >= umbral -> piso ampliado (16 ticks).
    (c) cooldown: aunque el mid se calme, el piso sigue ampliado hasta
        superar MOMENTUM_COOLDOWN_SECONDS; luego vuelve a 8.
    (d) movimiento < umbral -> sin ampliación.
    (e) simetría: subida y bajada activan el filtro por igual.
    (f) el piso ampliado convive con el skew de inventario (se preserva).
    (g) sin historial / filtro desactivado -> comportamiento previo.
    """

    def setUp(self):
        self.am = alpha_model.AlphaModel()
        self.tick = alpha_model.config.TICK_SIZE_XRPUSDC
        self.floor = alpha_model.config.MIN_SPREAD_TICKS * self.tick
        self.floor_momentum = (
            alpha_model.config.MIN_SPREAD_TICKS
            * alpha_model.config.MOMENTUM_SPREAD_MULTIPLIER
            * self.tick
        )
        self.threshold = alpha_model.config.MOMENTUM_MAX_TICKS * self.tick

    def _snap_quieta(self, mid=2.5, **overrides):
        base = {"spread": 0.0, "volatility": 0.0, "imbalance": 0.0,
                "momentum": 0.0, "microprice": 0.0, "inventory": 0.0}
        base.update(overrides)
        return _snapshot(mid=mid, **base)

    def _dist(self, snap, now_sec, alpha=0.0, inv=0.0, sigma=0.0):
        return self.am.quote_distances(snap, alpha, inv, sigma, now_sec=now_sec)

    # (a) mid estable
    def test_mid_estable_spread_normal(self):
        """Mid sin movimiento en la ventana -> piso normal (8 ticks)."""
        for ts, mid in [(0.0, 2.5), (10.0, 2.5), (20.0, 2.5), (30.0, 2.5)]:
            self.am.record_mid(ts, mid)
        snap = self._snap_quieta()
        bid, ask = self._dist(snap, now_sec=35.0)
        self.assertAlmostEqual(bid + ask, self.floor, places=12)
        self.assertAlmostEqual(bid, ask, places=12)
        self.assertFalse(self.am.last_momentum_active)

    # (b) movimiento >= umbral
    def test_movimiento_mayor_umbral_amplia_spread(self):
        """Mid moviéndose >= MOMENTUM_MAX_TICKS en la ventana -> 16 ticks."""
        # 9 ticks (0.0009) en 20 s: por encima del umbral de 8 ticks.
        self.am.record_mid(0.0, 2.5)
        self.am.record_mid(20.0, 2.5 + 9.0 * self.tick)
        snap = self._snap_quieta()
        bid, ask = self._dist(snap, now_sec=25.0)
        self.assertAlmostEqual(bid + ask, self.floor_momentum, places=12)
        self.assertAlmostEqual(bid, ask, places=12)
        self.assertTrue(self.am.last_momentum_active)

    def test_movimiento_igual_al_umbral_amplia_spread(self):
        """Movimiento == MOMENTUM_MAX_TICKS exacto también activa (>=)."""
        self.am.record_mid(0.0, 2.5)
        self.am.record_mid(20.0, 2.5 + self.threshold)
        snap = self._snap_quieta()
        bid, ask = self._dist(snap, now_sec=25.0)
        self.assertAlmostEqual(bid + ask, self.floor_momentum, places=12)

    # (c) cooldown
    def test_cooldown_mantiene_spread_ampliado(self):
        """Tras detectar momentum, el piso ampliado persiste aunque el mid
        se estabilice, hasta superar MOMENTUM_COOLDOWN_SECONDS."""
        self.am.record_mid(0.0, 2.5)
        self.am.record_mid(20.0, 2.5 + 9.0 * self.tick)
        snap = self._snap_quieta()

        # t=25: detección fresca -> 16 ticks.
        bid, ask = self._dist(snap, now_sec=25.0)
        self.assertAlmostEqual(bid + ask, self.floor_momentum, places=12)

        # t=40: el mid ya se estabilizó (muestra vieja de la ventana
        # podada), pero el cooldown (60 s desde t=25) sigue corriendo.
        self.am.record_mid(40.0, 2.5 + 9.0 * self.tick)
        bid, ask = self._dist(snap, now_sec=40.0)
        self.assertAlmostEqual(bid + ask, self.floor_momentum, places=12)

        # t=90: cooldown vencido (90-25=65 >= 60) y sin nueva detección
        # (muestras estables dentro de la ventana) -> vuelve a 8 ticks.
        self.am.record_mid(90.0, 2.5 + 9.0 * self.tick)
        bid, ask = self._dist(snap, now_sec=90.0)
        self.assertAlmostEqual(bid + ask, self.floor, places=12)
        self.assertFalse(self.am.last_momentum_active)

    # (d) movimiento < umbral
    def test_movimiento_menor_umbral_sin_ampliacion(self):
        """3 ticks (< 8) en la ventana -> piso normal (8 ticks)."""
        self.am.record_mid(0.0, 2.5)
        self.am.record_mid(20.0, 2.5 + 3.0 * self.tick)
        snap = self._snap_quieta()
        bid, ask = self._dist(snap, now_sec=25.0)
        self.assertAlmostEqual(bid + ask, self.floor, places=12)
        self.assertFalse(self.am.last_momentum_active)

    # (e) simetría
    def test_simetrico_subida_y_bajada(self):
        """Tanto subida como bajada del mid activan el filtro por igual."""
        for direccion in (+1.0, -1.0):
            am = alpha_model.AlphaModel()  # estado limpio por dirección
            am.record_mid(0.0, 2.5)
            am.record_mid(20.0, 2.5 + direccion * 10.0 * self.tick)
            snap = self._snap_quieta()
            bid, ask = am.quote_distances(snap, 0.0, 0.0, 0.0, now_sec=25.0)
            self.assertAlmostEqual(bid + ask, self.floor_momentum, places=12)
            self.assertTrue(am.last_momentum_active)

    # (f) convivencia con skew de inventario
    def test_skew_inventario_convive_con_piso_ampliado(self):
        """Con momentum activo: piso 16 ticks se mantiene y el skew de
        inventario se aplica encima (bid - ask == 2*skew)."""
        mid = 2.5
        sigma = 0.00002
        inv = 10.0  # nocional = 25 USDC = MAX_POSITION_NOTIONAL_USDC -> ratio 1.0
        self.am.record_mid(0.0, 2.5)
        self.am.record_mid(20.0, 2.5 + 9.0 * self.tick)
        snap = self._snap_quieta(mid=mid, volatility=sigma, inventory=inv)
        bid, ask = self._dist(snap, now_sec=25.0, inv=inv, sigma=sigma)
        # Piso ampliado se mantiene pese al skew.
        self.assertAlmostEqual(bid + ask, self.floor_momentum, places=12)
        # Inventario long -> bid más lejos del mid que el ask.
        self.assertGreater(bid, ask)
        sigma_ef = sigma * math.sqrt(12.0) * math.sqrt(5.0)
        skew = ((inv * mid) / alpha_model.config.MAX_POSITION_NOTIONAL_USDC
                * alpha_model.INVENTORY_SKEW_MULTIPLIER * sigma_ef)
        self.assertAlmostEqual(bid - ask, 2.0 * skew, places=12)

    # (g) comportamiento previo sin historial / desactivado
    def test_sin_historial_comportamiento_previo(self):
        """Sin historial alimentado -> el filtro no altera nada (8 ticks)."""
        snap = self._snap_quieta()
        bid, ask = self._dist(snap, now_sec=100.0)
        self.assertAlmostEqual(bid + ask, self.floor, places=12)
        self.assertFalse(self.am.last_momentum_active)

    def test_record_mid_ignora_muestras_invalidas(self):
        """record_mid descarta ts/mid inválidos (no contaminan el filtro)."""
        for ts, mid in [(0.0, 2.5), (None, 2.5), (10.0, None), (20.0, -1.0),
                        (30.0, 0.0), (float("nan"), 2.5)]:
            self.am.record_mid(ts, mid)
        snap = self._snap_quieta()
        bid, ask = self._dist(snap, now_sec=35.0)
        self.assertAlmostEqual(bid + ask, self.floor, places=12)

    def test_desactivado_por_config(self):
        """MOMENTUM_ENABLED=False -> gran movimiento NO amplía el piso."""
        enabled = alpha_model.config.MOMENTUM_ENABLED
        try:
            alpha_model.config.MOMENTUM_ENABLED = False
            self.am.record_mid(0.0, 2.5)
            self.am.record_mid(20.0, 2.5 + 50.0 * self.tick)
            snap = self._snap_quieta()
            bid, ask = self._dist(snap, now_sec=25.0)
            self.assertAlmostEqual(bid + ask, self.floor, places=12)
            self.assertFalse(self.am.last_momentum_active)
        finally:
            alpha_model.config.MOMENTUM_ENABLED = enabled


if __name__ == "__main__":
    unittest.main()
