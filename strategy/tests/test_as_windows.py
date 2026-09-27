# test_as_windows.py — Veredicto F1.3 (V1–V4): cobertura de ventanas,
# vencimiento explícito, consultas no destructivas, un libro por grupo,
# volatilidad sobre ventana real de 60s. Motor + MarketState + AlphaModel
# reales (nada mockeado).

import math
import statistics
import unittest

from strategy import config as strategy_config
from strategy.alpha_model import AlphaModel
from strategy.as_coordinator import ASCoordinator
from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    ReconstructionConfig,
)
from strategy.market_state import (
    MIN_MID_SAMPLES,
    MarketState,
    SignalWindows,
    usdc_to_ticks,
)


# Ventanas offline explícitas (W4): las de strategy.config — momentum 30s,
# NO el local de 15s de MarketState (ese queda para la ruta prod legacy).
WINDOWS = SignalWindows(trade_flow_window_sec=60.0,
                        volatility_window_sec=60.0,
                        momentum_window_sec=30.0)


def _ms(windows=None):
    return MarketState(symbol="xrpusdc", real=False,
                       signal_windows=windows or WINDOWS)


def _snap(ms, sec):
    return ms.get_snapshot(sec)


def _depth(ts_ms, bid, ask, update_id, pu, bid_qty=10, ask_qty=100):
    return {"ts_ms": ts_ms, "bids": [[bid, bid_qty]],
            "asks": [[ask, ask_qty]], "update_id": update_id, "pu": pu}


def _trade(ts_ms, trade_id, price, qty, buyer_maker):
    return {"ts_ms": ts_ms, "trade_id": trade_id, "price_ticks": price,
            "qty_lots": qty, "is_buyer_maker": buyer_maker}


def _engine():
    return ExecutionReconstructor(ReconstructionConfig(
        max_gap_ms=60000, max_book_age_ms=60000))


def _coord(depth, trades, engine):
    return ASCoordinator(config=ReconstructionConfig(), engine=engine,
                         depth_csv=depth, trades_csv=trades,
                         decision_interval_ms=5000, warmup_intervals=3)


def _dense(t_start=1000, t_end=61000, step=5000, bid=10000, ask=10001):
    rows = []
    uid = 1
    prev = 0
    ts = t_start
    while ts <= t_end:
        rows.append(_depth(ts, bid, ask, uid, prev))
        prev = uid
        uid += 1
        ts += step
    return rows, uid


class TestWindowCoverageGate(unittest.TestCase):
    """V1: sin cobertura de ventanas no hay submits, pero el historial
    AlphaModel ya se alimenta durante el warm-up."""

    def test_no_submits_before_coverage_but_history_fed(self):
        depth = [_depth(1000, 10000, 10001, 1, 0),
                 _depth(2000, 10000, 10001, 2, 1),
                 _depth(3000, 10000, 10001, 3, 2)]
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord.run()
        self.assertFalse(coord._warmup_complete)
        self.assertEqual(list(eng.orders.values()), [])
        # ...pero el warm-up best-effort ya alimentó el filtro §14 —
        # W2 (veredicto): los 3 libros cuentan como muestra (span 2s < 60s,
        # por eso no hay cobertura), pero AlphaModel solo se registra en
        # la grilla (t0=1000, D=5000): únicamente el ciclo 1000.
        hist = list(coord._alpha_model._mid_history)
        self.assertEqual(len(hist), 1)
        self.assertAlmostEqual(hist[-1][1], 10000.5 * 0.0001, places=12)

    def test_submits_after_60s_with_fed_history(self):
        depth, nxt = _dense()
        depth += [_depth(66000, 10000, 10001, nxt, nxt - 1),
                  _depth(71000, 10000, 10001, nxt + 1, nxt)]
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord.run()
        self.assertTrue(coord._warmup_complete)
        self.assertGreater(len(eng.orders), 0)
        hist = list(coord._alpha_model._mid_history)
        self.assertGreater(len(hist), 0)
        # Alimentación continua (warm-up + decisiones) con la ventana
        # propia §14 de 30s del filtro: el último es del cierre (71s) y
        # el historial cubre ~30s hacia atrás, nunca vacío al decidir.
        self.assertAlmostEqual(hist[-1][0], 71.0, places=9)
        self.assertGreater(hist[-1][0] - hist[0][0], 0.0)
        self.assertLessEqual(hist[-1][0] - hist[0][0], 30.0 + 1e-9)


class TestExplicitExpiry(unittest.TestCase):
    """V2: con tiempo explícito las observaciones vencen (MarketState
    directo, sin coordinador)."""

    def test_stale_observations_expire(self):
        ms = _ms()
        ms.update_bookticker(1.0, 10.0, 1.0001, 100.0, 1000)
        ms.update_depth([[1.0, 10.0]], [[1.0001, 100.0]], 1, 1000)
        ms.update_trade(1.0001, 4.0, False, 2000)
        ms.update_trade(1.0, 6.0, True, 3000)
        fresh = _snap(ms, 3.0)
        self.assertAlmostEqual(float(fresh["buy_volume_60s"] or 0.0), 4.0,
                               places=12)
        self.assertAlmostEqual(float(fresh["sell_volume_60s"] or 0.0), 6.0,
                               places=12)
        # A los 100s todo flujo/momentum/volatilidad expiró (mid persiste:
        # es nivel, no ventana)
        old = _snap(ms, 100.0)
        self.assertEqual(float(old["buy_volume_60s"] or 0.0), 0.0)
        self.assertEqual(float(old["sell_volume_60s"] or 0.0), 0.0)
        self.assertEqual(float(old["momentum"] or 0.0), 0.0)
        self.assertEqual(float(old["volatility"] or 0.0), 0.0)
        self.assertGreater(float(old["mid"] or 0.0), 0.0)


class TestWarmupRealSamplesOnly(unittest.TestCase):
    """W2: el warm-up solo cuenta libros reales en la grilla; los trades
    no aportan muestras y AlphaModel se alimenta 1 vez por ciclo."""

    def test_trades_without_new_books_do_not_complete_warmup(self):
        depth = [_depth(1000, 10000, 10001, 1, 0)]
        trades = [_trade(ts, f"t{i}", 10001, 2, False)
                  for i, ts in enumerate(range(6000, 71000, 5000))]
        eng = _engine()
        coord = _coord(depth, trades, eng)
        coord.run()
        self.assertFalse(coord._warmup_complete)
        self.assertEqual(list(eng.orders.values()), [])
        # 12 trades en grilla, 1 solo libro real: una sola muestra
        hist = list(coord._alpha_model._mid_history)
        self.assertEqual(len(hist), 1)

    def test_record_mid_exact_grid_frequency(self):
        # Libros cada 1s 1000..11000 (11 grupos); solo los de grilla
        # (1000, 6000, 11000 con t0=1000, D=5000) alimentan AlphaModel:
        # exactamente 3 records (ventana §14 de 30s > span 10s: sin poda).
        depth = [_depth(ts, 10000, 10001, uid + 1, uid)
                 for uid, ts in enumerate(range(1000, 11001, 1000))]
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord.run()
        self.assertFalse(coord._warmup_complete)
        hist = list(coord._alpha_model._mid_history)
        self.assertEqual(len(hist), 3)
        self.assertEqual([t for t, _ in hist], [1.0, 6.0, 11.0])


class TestRegressiveQueriesRejected(unittest.TestCase):
    """W3: consultar antes del estado consumido levanta; el pasado se
    prueba por reproducción cronológica."""

    def _full_state(self):
        ms = _ms()
        for i in range(36):
            ts = 1000 + i * 2000
            m = 1.0 + (0.001 if i % 2 == 0 else 0.0)
            ms.update_bookticker(m - 0.00005, 10.0, m + 0.00005, 100.0, ts)
        return ms

    def test_query_before_consumed_state_raises(self):
        ms = self._full_state()  # consumido hasta 71s
        with self.assertRaises(ValueError):
            ms.get_snapshot(12.0)

    def test_past_via_chronological_reproduction(self):
        # Mismo prefijo (t=1..11s) en dos estados frescos: valores exactos
        # y deterministas — así se prueba el pasado, no consultando atrás.
        def build():
            ms = _ms()
            for i in range(6):
                ts = 1000 + i * 2000
                m = 1.0 + (0.001 if i % 2 == 0 else 0.0)
                ms.update_bookticker(m - 0.00005, 10.0,
                                     m + 0.00005, 100.0, ts)
            return ms
        a, b = build(), build()
        sa, sb = _snap(a, 11.0), _snap(b, 11.0)
        self.assertAlmostEqual(float(sa["mid"] or 0.0),
                               1.0 + (0.001 if 5 % 2 == 0 else 0.0),
                               places=12)
        self.assertGreater(float(sa["volatility"] or 0.0), 0.0)
        self.assertEqual(sa["volatility"], sb["volatility"])
        self.assertEqual(sa["momentum"], sb["momentum"])


class TestConfigurableWindows(unittest.TestCase):
    """W4: la ruta offline usa ventanas explícitas (momentum 30s de
    config, no el local de 15s)."""

    def _varied_state(self, windows=None):
        # Variación t=1..55s (alternada), plano t=57..71s: la variación
        # vive ENTRE 15 y 60s de antigüedad al consultar en 71s.
        ms = _ms(windows)
        for i in range(36):
            ts = 1000 + i * 2000
            m = (1.0 + (0.001 if i % 2 == 0 else 0.0)) if i < 28 else 1.0
            ms.update_bookticker(m - 0.00005, 10.0, m + 0.00005, 100.0, ts)
        return ms

    def test_momentum_window_from_config(self):
        ms = self._varied_state()
        m30 = _snap(ms, 71.0)
        self.assertAlmostEqual(float(m30["momentum"] or 0.0), -0.001,
                               places=12)
        m15 = _snap(self._varied_state(SignalWindows(60.0, 60.0, 15.0)),
                      71.0)
        self.assertEqual(float(m15["momentum"] or 0.0), 0.0)

    def test_volatility_window_sees_old_variation(self):
        ms = self._varied_state()
        v60 = _snap(ms, 71.0)
        self.assertGreater(float(v60["volatility"] or 0.0), 0.0)
        v15 = _snap(self._varied_state(SignalWindows(60.0, 15.0, 30.0)),
                      71.0)
        self.assertEqual(float(v15["volatility"] or 0.0), 0.0)

    def test_coordinator_windows_come_from_config(self):
        coord = _coord([], [], _engine())
        self.assertEqual(coord._signal_windows.momentum_window_sec,
                         float(strategy_config.MOMENTUM_WINDOW_SECONDS))
        self.assertNotEqual(coord._signal_windows.momentum_window_sec, 15.0)
        self.assertEqual(coord._warmup_span,
                         max(coord._signal_windows.trade_flow_window_sec,
                             coord._signal_windows.volatility_window_sec,
                             coord._signal_windows.momentum_window_sec))


class TestNonDestructiveQueries(unittest.TestCase):
    """V3: snapshots repetidos no se alteran entre sí ni podan la deque."""

    def test_repeated_snapshots_stable_and_deque_intact(self):
        ms = _ms()
        for i in range(36):
            ts = 1000 + i * 2000  # 1s..71s cada 2s
            m = 1.0 + (0.001 if i % 2 else 0.0)
            ms.update_bookticker(m - 0.00005, 10.0, m + 0.00005, 100.0, ts)
        n0 = len(ms._mid_samples)
        s1 = _snap(ms, 71.0)
        n1 = len(ms._mid_samples)
        s2 = _snap(ms, 71.0)
        self.assertEqual(n0, n1)
        self.assertEqual(s1["volatility"], s2["volatility"])
        self.assertEqual(s1["momentum"], s2["momentum"])
        # 36 muestras intactas: ninguna poda entre consultas
        self.assertEqual(n1, 36)


class TestOneSamplePerGroup(unittest.TestCase):
    """V4: varios libros del mismo ts se validan todos pero incorporan uno."""

    def test_same_ts_books_yield_single_mid_sample(self):
        eng = _engine()
        coord = _coord([], [], eng)
        n0 = len(coord._market_state._mid_samples)
        coord._feed_market_state(1000, [
            {"ts_ms": 1000, "kind": "book", "bids": [[10000, 10]],
             "asks": [[10001, 100]], "update_id": 1, "pu": 0},
            {"ts_ms": 1000, "kind": "book", "bids": [[10002, 10]],
             "asks": [[10003, 100]], "update_id": 2, "pu": 1},
            {"ts_ms": 1000, "kind": "book", "bids": [],
             "asks": [[10003, 100]], "update_id": 3, "pu": 2},
        ])
        samples = list(coord._market_state._mid_samples)
        self.assertEqual(len(samples), n0 + 1)
        # El último VÁLIDO del grupo (el vacío se salta): mid 10002.5 ticks
        self.assertAlmostEqual(samples[-1][1], 10002.5 * 0.0001, places=12)
        self.assertAlmostEqual(float(coord._market_state.best_bid or 0.0),
                               10002 * 0.0001, places=12)


class TestSixtySecondVolWindow(unittest.TestCase):
    """La ventana de vol es 60s reales: 70s de datos con 60s finales
    planos dan sigma 0 (el pasado variable queda fuera). El contrapeso
    con variación ENTRE 15 y 60s vive en TestConfigurableWindows (W4);
    la consulta al pasado (12s) está prohibida (W3) y se prueba por
    reproducción cronológica en TestRegressiveQueriesRejected."""

    def test_vol_uses_only_60s_window(self):
        ms = _ms()
        # t=1..9s alternados, luego plano 1.0 hasta t=71s (cada 2s)
        mids = [1.0, 1.001, 1.0, 1.001, 1.0] + [1.0] * 31
        for i, m in enumerate(mids):
            ts = 1000 + i * 2000
            ms.update_bookticker(m - 0.00005, 10.0, m + 0.00005, 100.0, ts)
        snap = _snap(ms, 71.0)
        # Ventana (11s, 71s]: todo plano -> solo retornos 0 -> sigma 0
        self.assertEqual(snap["volatility"], 0.0)
        # ...y la deque conserva las 36 muestras (sin poda destructiva)
        self.assertEqual(len(ms._mid_samples), 36)


def _misaligned(t_start=1000, n=56, step=1100, bid=10000, ask=10001):
    """Libros cada `step` ms (casi ninguno cae en la grilla t0+k*5000)."""
    rows = []
    uid = 1
    prev = 0
    for k in range(n):
        ts = t_start + k * step
        rows.append(_depth(ts, bid, ask, uid, prev))
        prev = uid
        uid += 1
    return rows


class TestMisalignedBooksWarmup(unittest.TestCase):
    """Veredicto W2: con libros desalineados de la grilla el warm-up
    igual se completa (cuenta todo libro real), AlphaModel se alimenta
    1 vez por ciclo de grilla con libro fresco, y no hay submits
    prematuros (el warm-up nunca aplica comandos)."""

    def test_misaligned_books_complete_warmup_on_grid_records(self):
        depth = _misaligned()  # 1000..61500 cada 1100ms: 56 libros, span 60.5s
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord._warmup_phase()
        self.assertTrue(coord._warmup_complete)
        # Sin submits prematuros: el warm-up jamás aplica comandos
        self.assertEqual(
            [j for j in eng.journal if j.get("event") == "submit"], [])
        # Registros AlphaModel: todos en la grilla, uno por ciclo como máximo
        hist = list(coord._alpha_model._mid_history)
        self.assertGreaterEqual(len(hist), 3)
        self.assertLess(len(hist), len(depth))
        for ts_sec, _mid in hist:
            self.assertEqual((round(ts_sec * 1000) - 1000) % 5000, 0)
        # ...y estrictamente crecientes en el tiempo (un registro por ciclo)
        for (t0, _), (t1, _) in zip(hist, hist[1:]):
            self.assertGreater(t1, t0)

    def test_full_run_no_submits_before_warmup_end(self):
        depth = _misaligned(n=66)  # hasta 72500: cola para decidir tras el warm-up
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord.run()
        self.assertTrue(coord._warmup_complete)
        warmup_end = coord._warmup_last_ts
        submits = [j for j in eng.journal if j.get("event") == "submit"]
        self.assertGreater(len(submits), 0)
        for s in submits:
            self.assertGreater(s["ts_ms"], warmup_end)


def _merged(uid_rows):
    """Reasigna uid/pu secuenciales a filas ya ordenadas por ts."""
    out = []
    uid, prev = 1, 0
    for r in uid_rows:
        row = dict(r)
        row["update_id"], row["pu"] = uid, prev
        prev, uid = uid, uid + 1
        out.append(row)
    return out


class TestGridCycleMidDatesDecision(unittest.TestCase):
    """R1: al cerrar un ciclo de grilla se registra el mid del snapshot de
    ESE instante; los libros entre grillas alimentan MarketState pero no
    AlphaModel. Corrida con libros densos fuera de grilla == corrida sin
    intermedios (mismo mid constante): historiales idénticos y en grilla."""

    def _run_hist(self, depth):
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord.run()
        self.assertTrue(coord._warmup_complete)
        return list(coord._alpha_model._mid_history)

    def test_offgrid_books_do_not_change_grid_records(self):
        grid = [r for r in _dense()[0]]  # 1000..61000 cada 5s, mid cte
        off = [_depth(ts, 10000, 10001, 0, 0)
               for ts in range(1000, 61001, 1000)
               if (ts - 1000) % 5000 != 0]
        hist_a = self._run_hist(_merged(sorted(grid, key=lambda r: r["ts_ms"])))
        both = sorted(grid + off, key=lambda r: r["ts_ms"])
        hist_b = self._run_hist(_merged(both))
        self.assertEqual(hist_a, hist_b)
        self.assertEqual(len(hist_a), 13)
        for ts_sec, _ in hist_a:
            self.assertEqual((round(ts_sec * 1000) - 1000) % 5000, 0)


class TestMicropricePriceBasis(unittest.TestCase):
    """R2: el precio base A-S es microprice si hay libro, si no mid; la
    conversión vive en usdc_to_ticks (sin aritmética manual). Spread 3
    ticks + asks pesados: variantes separadas por ~2 ticks."""

    def test_emitted_quote_uses_microprice(self):
        tick = 0.0001
        book = {"ts_ms": 1000, "kind": "book", "bids": [[10000, 10]],
                "asks": [[10003, 100]], "update_id": 1, "pu": 0}
        eng = _engine()
        eng.advance_to(1000, [book])
        coord = _coord(
            [{"ts_ms": 1000, "bids": [[10000, 10]], "asks": [[10003, 100]]}],
            [], eng)
        coord._feed_market_state(1000, [book])
        snap = eng.state()
        cmds = coord._as_decision(snap)
        buys = [c for c in cmds if c["side"] == "BUY"]
        self.assertEqual(len(buys), 1)

        ms_snap = coord._market_state.get_snapshot(1.0)
        mp = float(ms_snap["microprice"])
        mid = float(ms_snap["mid"])
        self.assertGreater(abs(mp - mid), 0)  # el caso distingue

        def variant(px):
            am = AlphaModel()
            d = {"mid": px, "spread": float(ms_snap["spread"]),
                 "momentum": 0.0,
                 "imbalance": float(ms_snap["imbalance"]),
                 "microprice": mp, "buy_volume_60s": 0.0,
                 "sell_volume_60s": 0.0, "tick_size": tick}
            a = am.compute_alpha(d)
            am.record_mid(1.0, px)
            r = am.reservation_price(d, a, 0.0, 0.0)
            bd, _ = am.quote_distances(d, a, 0.0, 0.0, now_sec=1.0)
            return a, usdc_to_ticks(r - bd, tick)

        _a_mp, bid_mp = variant(mp)
        _a_mid, bid_mid = variant(mid)
        self.assertNotEqual(bid_mp, bid_mid)
        self.assertEqual(buys[0]["price_ticks"], bid_mp)


class TestConsumedClockBoundary(unittest.TestCase):
    """R3: el reloj de consumo es explícito — consultar en el instante
    consumido vale; un ms antes levanta. El coordinador lo trackea."""

    def _fed_state(self, upto_ms=61000):
        ms = _ms()
        for ts in range(1000, upto_ms + 1, 5000):
            ms.update_bookticker(0.99995, 10.0, 1.00005, 100.0, ts)
        return ms

    def test_query_at_consumed_instant_ok_just_before_raises(self):
        ms = self._fed_state()
        self.assertEqual(ms._last_feed_ts_ms, 61000)
        _snap(ms, 61.0)  # frontera: vale
        with self.assertRaises(ValueError):
            _snap(ms, 60.999)

    def test_coordinator_tracks_last_feed(self):
        eng = _engine()
        coord = _coord(
            [{"ts_ms": 1000, "bids": [[10000, 10]], "asks": [[10001, 100]]}],
            [], eng)
        self.assertIsNone(getattr(coord, "_last_feed_ts", None))
        book = {"ts_ms": 1000, "kind": "book", "bids": [[10000, 10]],
                "asks": [[10001, 100]], "update_id": 1, "pu": 0}
        coord._feed_market_state(1000, [book])
        self.assertEqual(coord._last_feed_ts, 1000)
        with self.assertRaises(ValueError):
            coord._market_state.get_snapshot(0.5)
        trade = {"ts_ms": 2000, "kind": "trade", "trade_id": "t",
                 "price_ticks": 10000, "qty_lots": 2, "is_buyer_maker": True}
        coord._feed_market_state(2000, [trade])
        self.assertEqual(coord._last_feed_ts, 2000)
        with self.assertRaises(ValueError):
            coord._market_state.get_snapshot(1.5)


class TestReadinessFollowsConfiguredSpan(unittest.TestCase):
    """R4: la prontitud usa el mismo _warmup_span (nada de 60s fijos);
    el umbral de muestras y la ventana de trades están expuestos."""

    def test_short_windows_complete_early(self):
        depth, _ = _dense(t_start=1000, t_end=11000, step=5000)
        eng = _engine()
        coord = _coord(depth, [], eng)
        coord._signal_windows = SignalWindows(10.0, 10.0, 10.0)
        coord._warmup_span = 10.0
        coord.run()
        self.assertTrue(coord._warmup_complete)  # span 10s basta

    def test_min_samples_constant(self):
        self.assertEqual(MIN_MID_SAMPLES, 3)
        ms = _ms()
        ms.update_bookticker(0.99995, 10.0, 1.00005, 100.0, 1000)
        ms.update_bookticker(1.00095, 10.0, 1.00105, 100.0, 2000)
        self.assertEqual(_snap(ms, 2.0)["volatility"], 0.0)  # 2 < 3
        ms.update_bookticker(0.99995, 10.0, 1.00005, 100.0, 3000)
        self.assertGreater(_snap(ms, 3.0)["volatility"], 0.0)  # 3 varían

    def test_trade_window_configurable(self):
        def fed(windows):
            ms = _ms(windows)
            ms.update_bookticker(0.99995, 10.0, 1.00005, 100.0, 1000)
            ms.update_trade(1.0, 5.0, False, 1000)
            return ms
        wide = _snap(fed(WINDOWS), 61.0)
        narrow = _snap(fed(SignalWindows(30.0, 60.0, 30.0)), 61.0)
        self.assertEqual(wide["buy_volume_60s"], 5.0)    # corte en 1.0
        self.assertEqual(narrow["buy_volume_60s"], 0.0)   # corte en 31.0


class TestCoordinatorEngineLimitInvariant(unittest.TestCase):
    """R5: con límites por defecto del motor, las órdenes del coordinador
    nunca son rechazadas por límites (cap/nocional). Corrida completa con
    motor default (gaps 2000/1000): libros cada 1s + cola."""

    def test_no_limit_rejections_with_default_limits(self):
        rows = []
        uid, prev = 1, 0
        # Libros cada 1s (gaps engine default 2000 OK); la cola llega a
        # 66000 (grilla) para que el main loop decida una vez que el
        # warm-up termina en 61000.
        for ts in list(range(1000, 61001, 1000)) + list(range(62000, 66001, 1000)):
            rows.append(_depth(ts, 10000, 10001, uid, prev))
            prev, uid = uid, uid + 1
        eng = ExecutionReconstructor(ReconstructionConfig())  # defaults
        coord = ASCoordinator(
            config=ReconstructionConfig(), engine=eng,
            depth_csv=[{"ts_ms": r["ts_ms"], "bids": r["bids"],
                        "asks": r["asks"]} for r in rows],
            trades_csv=[], decision_interval_ms=5000, warmup_intervals=3)
        coord.run()
        self.assertTrue(coord._warmup_complete)
        events = [j.get("event") for j in eng.journal]
        self.assertGreater(len([e for e in events if e == "submit"]), 0)
        self.assertNotIn("rejected_position_cap", events)
        self.assertNotIn("rejected_position_notional", events)

    def test_all_emitted_orders_avoid_limit_rejections_trending(self):
        """Q3 (refuerzo): mercado plano + alcista con A-S real y motor
        default. Se emiten MUCHAS órdenes de ambos lados (las quotes
        persiguen al mercado; las viejas caen por depth, no por límites).
        Invariante por orden: ninguna termina en rechazo por límites, y el
        conteo global de rechazos por límites es 0."""
        rows = []
        for t in list(range(1000, 61001, 1000)) + list(range(62000, 66001, 1000)):
            rows.append({"ts_ms": t, "bids": [[10000, 10]],
                         "asks": [[10001, 100]]})
        k = 0
        for t in range(67000, 101001, 1000):
            k += 1
            rows.append({"ts_ms": t, "bids": [[10000 + 2 * k, 10]],
                         "asks": [[10001 + 2 * k, 100]]})
        uid, prev = 1, 0
        for r in rows:
            r["update_id"], r["pu"] = uid, prev
            prev, uid = uid, uid + 1
        eng = ExecutionReconstructor(ReconstructionConfig())  # defaults
        coord = ASCoordinator(
            config=ReconstructionConfig(), engine=eng,
            depth_csv=[{"ts_ms": r["ts_ms"], "bids": r["bids"],
                        "asks": r["asks"]} for r in rows],
            trades_csv=[], decision_interval_ms=5000, warmup_intervals=3)
        coord.run()
        self.assertTrue(coord._warmup_complete)
        orders = list(eng.orders.values())
        # Muchas órdenes, ambos lados (las viejas caen por depth al
        # perseguir al mercado — eso NO es rechazo por límites).
        self.assertGreaterEqual(len(orders), 10)
        self.assertGreater(len([o for o in orders if o["side"] == "BUY"]), 0)
        self.assertGreater(len([o for o in orders if o["side"] == "SELL"]), 0)
        for o in orders:
            self.assertNotIn(o["status"], ("rejected_position_cap",
                                           "rejected_position_notional"),
                             o["order_id"])
        events = [j.get("event") for j in eng.journal]
        self.assertEqual(events.count("rejected_position_cap"), 0)
        self.assertEqual(events.count("rejected_position_notional"), 0)


class TestSignalWindowsFrozen(unittest.TestCase):
    """Q1: ventanas por estructura Frozen — 30s/90s configurables,
    min_mid_samples=1, inmutabilidad y sin aliasing."""

    def _ramp(self, windows, n=41, step_ms=2000, upto_varied=11):
        # Variación t=1..21s (i<11 alternado), plano hasta 81s.
        ms = _ms(windows)
        for i in range(n):
            ts = 1000 + i * step_ms
            m = 1.0 + (0.001 if i % 2 == 0 else 0.0) if i < upto_varied else 1.0
            ms.update_bookticker(m - 0.00005, 10.0, m + 0.00005, 100.0, ts)
        return ms

    def test_momentum_30s_vs_90s(self):
        # Consulta a 81s: ventana 30s (corte 51s) no ve variación (0.0);
        # ventana 90s sí la ve (≠0.0).
        s30 = _snap(self._ramp(SignalWindows(60.0, 60.0, 30.0)), 81.0)
        self.assertEqual(float(s30["momentum"] or 0.0), 0.0)
        s90 = _snap(self._ramp(SignalWindows(60.0, 60.0, 90.0)), 81.0)
        self.assertNotEqual(float(s90["momentum"] or 0.0), 0.0)

    def test_min_mid_samples_one_vs_five(self):
        # 4 muestras variadas: min=1 valúa (>0), min=5 exige más (0.0).
        def fed(min_n):
            ms = _ms(SignalWindows(60.0, 60.0, 60.0, min_n))
            for i, m in enumerate([1.0, 1.001, 1.0, 1.001]):
                ts = 1000 + i * 2000
                ms.update_bookticker(m - 0.00005, 10.0,
                                     m + 0.00005, 100.0, ts)
            return ms
        self.assertGreater(float(_snap(fed(1), 7.0)["volatility"] or 0.0),
                           0.0)
        self.assertEqual(float(_snap(fed(5), 7.0)["volatility"] or 0.0),
                         0.0)

    def test_windows_invalid_rejected(self):
        with self.assertRaises(ValueError):
            SignalWindows(0.0, 60.0, 30.0)
        with self.assertRaises(ValueError):
            SignalWindows(60.0, -5.0, 30.0)
        with self.assertRaises(ValueError):
            SignalWindows(60.0, 60.0, 30.0, 0)
        with self.assertRaises(ValueError):
            MarketState(symbol="xrpusdc", real=False,
                        signal_windows={"trade": 1.0})  # type: ignore[arg-type]

    def test_frozen_no_aliasing(self):
        import dataclasses
        w = SignalWindows(60.0, 60.0, 30.0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            w.momentum_window_sec = 15.0  # type: ignore[misc]
        ms = _ms(w)
        self.assertEqual(ms._signal_windows, w)
        self.assertEqual(ms._signal_windows,
                         SignalWindows(60.0, 60.0, 30.0))
        # get_snapshot sin args sigue siendo la ruta legacy (prod intacta)
        legacy = MarketState(symbol="xrpusdc", real=False).get_snapshot()
        self.assertIn("volatility", legacy)


if __name__ == "__main__":
    unittest.main()
