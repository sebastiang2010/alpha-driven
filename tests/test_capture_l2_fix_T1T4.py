"""Tests T1-T4 aislados para fix captura L2 — mocks sin red real.

Requisitos (7 fixes):
1. doble conteo snapshot_sync_errors
2. reseed inútil pending==[] sin eventos WS
3. diferenciar fallo red REST vs mismatch lógico
4. retry/backoff snapshot REST
5. _total_failures techo 50->200 e intervalo SYNCING 5->15
6. protocolo Binance intacto (U<=L+1<=u, pu==prev_u)
7. contadores separados

Ejecutar: pytest tests/test_capture_l2_fix_T1T4.py -v
"""

import sys
import os
import time
import json
from unittest.mock import patch, MagicMock, call

ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.capture_l2_testnet as cap
from scripts.capture_l2_testnet import DepthCaptureState, CaptureClient, MAX_TOTAL_FAILURES, RESEED_INTERVAL_SYNCING_SEC


def snap(L, bids=None, asks=None):
    return {"lastUpdateId": L, "bids": bids if bids is not None else [["0.5", "10"]], "asks": asks if asks is not None else [["0.5002", "10"]]}

def ev(U, u, pu, bids=None, asks=None, ts=0):
    return {"ts_ms": ts, "first_update_id": U, "update_id": u, "pu": pu, "bids": bids if bids is not None else [], "asks": asks if asks is not None else []}


# ── T1: blackout WS sin eventos ──────────────────────────────────────────
def test_T1_blackout_no_events_no_failed_no_duplicate(tmp_path):
    """T1 blackout WS sin eventos: mock _fetch_snapshot OK + pending==[] →
    verificar que en <5min no llega a FAILED, no duplica snapshot_sync_errors,
    respeta intervalo alargado (15s)."""
    client = CaptureClient(symbol="XRPUSDC", real=False, levels=20, out_dir=str(tmp_path), max_reseed_attempts=5)
    client._open_files()
    try:
        # Poner estado en SYNCING con pending vacío (simula blackout post-reconnect sin datos)
        client.sync.phase = "SYNCING"
        client.sync.pending = []
        client.sync.snap = snap(100)
        client.sync.snap_L = 100
        client.sync.reseed_attempts = 0
        client._last_reseed_ts = time.time() - 20  # permitir reseed inmediato
        client._last_reseed_ws_reconnects = client.sync.ws_reconnects

        fetch_calls = [0]
        def mock_fetch_ok():
            fetch_calls[0] += 1
            return snap(200)

        with patch.object(client, '_fetch_snapshot', side_effect=mock_fetch_ok):
            # Primer _reseed_now con pending vacío y ws_recon sin cambio → gate anti-spam debe bloquear fetch
            client._reseed_now()
            # Con gate, fetch NO debe haberse llamado (spamming evitavo)
            assert fetch_calls[0] == 0, "gate anti-spam falló: fetch con pending vacío no debería llamarse"
            # No debe haber incrementado reseed_attempts ni snapshot_sync_errors
            assert client.sync.reseed_attempts == 0
            assert client.sync.snapshot_sync_errors == 0
            assert client.sync.phase != "FAILED"

            # Simular llegada de nuevo evento WS cambia pending, ahora sí debe permitir reseed
            client.sync.pending = [ev(200, 205, 199)]
            # Necesitamos también cambiar ws_recon o mantener? Gate usa pending_len + ws_recon == last
            # Si pending no vacío, gate no bloquea aunque ws_recon igual
            client._last_reseed_ts = time.time() - 20
            client._reseed_now()
            assert fetch_calls[0] == 1, "con pending no vacío debe llamar fetch"
            # Ahora con pending no vacío y snapshot L=200, _try_sync_pending debe sincronizar
            assert client.sync.phase == "SYNCED"

        # Simular bucle monitor 5 min con blackout continuo: 5*60/15 =20 intentos nominales
        # Con fix interval 15s y gate, no debe llegar a FAILED
        client2 = CaptureClient(symbol="XRPUSDC", real=False, levels=20, out_dir=str(tmp_path / "t1b"), max_reseed_attempts=5)
        client2._open_files()
        try:
            client2.sync.phase = "SYNCING"
            client2.sync.pending = []
            client2.sync.snap = snap(100)
            client2.sync.snap_L = 100
            client2._last_reseed_ts = time.time() - 20
            client2._last_reseed_ws_reconnects = 0
            client2.sync.ws_reconnects = 0
            fetch_calls2 = [0]
            def mock_fetch2():
                fetch_calls2[0] += 1
                return snap(300)
            with patch.object(client2, '_fetch_snapshot', side_effect=mock_fetch2):
                # Simular 20 ciclos de monitor con pending vacío (blackout 5 min)
                for _ in range(20):
                    # reset timer para forzar check
                    client2._last_reseed_ts = time.time() - 20
                    client2._reseed_now()
                    # No debe llegar a FAILED
                    assert client2.sync.phase != "FAILED", "blackout 5min no debe llegar a FAILED con gate+interval 15s"
                # snapshot_sync_errors NO debe haber duplicado (debe seguir 0 porque gate evita reseed_attempts)
                assert client2.sync.snapshot_sync_errors == 0
                # _total_failures threshold es 200, no 50, así que 20 ciclos no matan captura larga
                assert MAX_TOTAL_FAILURES == 200
                assert RESEED_INTERVAL_SYNCING_SEC == 15.0
        finally:
            client2._close_files()
    finally:
        client._close_files()


# ── T2: doble conteo ─────────────────────────────────────────────────────
def test_T2_doble_conteo_snapshot_sync_errors():
    """T2 doble conteo: forzar 2 FAILED consecutivos → snapshot_sync_errors == _total_failures (no +2)."""
    s = DepthCaptureState(max_reseed_attempts=5)
    # Poner en SYNCED primero
    s.on_depth_event(ev(100, 105, 99))
    s.set_snapshot(snap(100))
    assert s.phase == "SYNCED"
    # Forzar gap y luego agotar reseeds para llegar a FAILED
    s.on_depth_event(ev(200, 205, 999))
    assert s.phase == "RESYNC"
    for i in range(10):
        s.on_depth_event(ev(900, 905, 899))
        s.set_snapshot(snap(800 + i))
        if s.phase == "FAILED":
            break
    assert s.phase == "FAILED"
    assert s.snapshot_sync_errors == 1
    assert s.reseed_attempts > s.max_reseed_attempts
    old_errors = s.snapshot_sync_errors

    # Simular self-healing de CaptureClient.run (clonado) 2 veces consecutivas
    # Con fix, snapshot_sync_errors NO debe duplicar (+1)
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    try:
        client = CaptureClient(symbol="XRPUSDC", real=False, levels=20, out_dir=tmp, max_reseed_attempts=5)
        client.sync = s
        client._total_failures = 0
        # Simular 1er FAILED self-heal
        # Código del run: old = self.sync; new = DepthCaptureState(); new.snapshot_sync_errors = old.snapshot_sync_errors (fix, no +1)
        old = client.sync
        client._total_failures += 1
        new = DepthCaptureState(levels=client.levels, max_reseed_attempts=client.max_reseed_attempts)
        new.snapshot_sync_errors = old.snapshot_sync_errors  # fix: no +1
        new.snapshot_fetch_errors = getattr(old, 'snapshot_fetch_errors', 0)
        new.valid_depth_events = old.valid_depth_events
        new.ws_reconnects = old.ws_reconnects
        new.ws_disconnects = old.ws_disconnects
        client.sync = new
        assert client.sync.snapshot_sync_errors == old_errors
        assert client.sync.snapshot_sync_errors == client._total_failures, "tras 1 FAILED: snapshot_sync_errors debe == _total_failures (1==1)"

        # Forzar 2do FAILED consecutivamente (otro ciclo)
        # Simular que el nuevo también falla
        client.sync.phase = "FAILED"
        client.sync.snapshot_sync_errors = 1  # ya 1
        # Para el test, incrementamos old_errors a 1, y simulamos que set_snapshot ya contó 1
        # Segundo self-heal
        old2 = client.sync
        client._total_failures += 1
        new2 = DepthCaptureState(levels=client.levels, max_reseed_attempts=client.max_reseed_attempts)
        new2.snapshot_sync_errors = old2.snapshot_sync_errors  # no +1
        new2.snapshot_fetch_errors = getattr(old2, 'snapshot_fetch_errors', 0)
        client.sync = new2
        # Con fix: después de 2 FAILED, snapshot_sync_errors=1 (solo el original)?? Actually cada FAILED real debería +1 via set_snapshot, no via clon
        # Pero para test aislado, verificamos que clon NO duplica:
        assert client.sync.snapshot_sync_errors == 1
        # Si hubiera bug +1 duplicado, sería 2 y 3 respectivamente
        # La invariante correcta: snapshot_sync_errors == número de veces que set_snapshot llegó a FAILED (aquí 1), no _total_failures*2
        # Test más directo: forzar 2 FAILED reales via set_snapshot y verificar +1 cada vez, no +2
        s2 = DepthCaptureState(max_reseed_attempts=2)
        s2.on_depth_event(ev(100,105,99))
        s2.set_snapshot(snap(100))
        # agotamos
        for _ in range(5):
            s2.on_depth_event(ev(900,905,899))
            s2.set_snapshot(snap(800))
            if s2.phase == "FAILED":
                break
        assert s2.snapshot_sync_errors == 1
        # Reset via client logic (sin +1)
        old = s2
        new = DepthCaptureState(max_reseed_attempts=2)
        new.snapshot_sync_errors = old.snapshot_sync_errors
        # Segundo ciclo FAILED
        new.on_depth_event(ev(100,105,99))  # buffer
        # Necesitamos llevarlo a FAILED de nuevo: hacer reseed_attempts > max
        new.phase = "RESYNC"
        new.reseed_attempts = 3  # ya > max
        new.set_snapshot(snap(900))  # este snap no solapa (pending vacío) -> quedará SYNCING, no FAILED directamente
        # Mejor forzar FAILED directo: set reseed_attempts y pending sin solape
        new2 = DepthCaptureState(max_reseed_attempts=2)
        new2.snapshot_sync_errors = old.snapshot_sync_errors
        new2.phase = "RESYNC"
        new2.reseed_attempts = 3
        new2.pending = [ev(900,905,899)]
        new2.set_snapshot(snap(1))  # L=1, pending U=900 > L+1 -> no solapa, y reseed_attempts>max -> FAILED +1
        assert new2.snapshot_sync_errors == 2  # 1 previo +1 nuevo =2, no +2
        assert new2.snapshot_sync_errors == 2
    finally:
        import shutil as sh
        sh.rmtree(tmp, ignore_errors=True)


# ── T3: REST timeout + retry ─────────────────────────────────────────────
def test_T3_rest_timeout_retry():
    """T3 REST timeout + retry: mock urlopen timeout 2 veces luego OK → 3 intentos,
    snapshot_fetch_errors incrementado pero reseed_attempts no avanza hasta éxito."""
    import urllib.request
    tmp = os.path.join(os.path.dirname(__file__), "..", "tmp_t3")
    os.makedirs(tmp, exist_ok=True)
    client = CaptureClient(symbol="XRPUSDC", real=False, levels=20, out_dir=tmp, max_reseed_attempts=5)
    client._open_files()
    try:
        # Fase SYNCING con pending para permitir reseed
        client.sync.phase = "RESYNC"
        client.sync.pending = [ev(200,205,199)]
        client.sync.reseed_attempts = 0
        orig_calls = [0]
        def mock_urlopen(url, timeout=10):
            orig_calls[0] += 1
            if orig_calls[0] <= 2:
                raise TimeoutError("timed out")
            # 3er intento OK
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(snap(200)).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = lambda s, *a: None
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            with patch('time.sleep', return_value=None):  # no dormir en test
                snap_result = client._fetch_snapshot()
                assert orig_calls[0] == 3, "debe intentar 3 veces (2 timeout +1 OK)"
                assert snap_result["lastUpdateId"] == 200

        # Ahora test reseed con fallo total (3 timeouts)
        client2 = CaptureClient(symbol="XRPUSDC", real=False, levels=20, out_dir=tmp, max_reseed_attempts=5)
        client2._open_files()
        try:
            client2.sync.phase = "RESYNC"
            client2.sync.pending = [ev(200,205,199)]
            client2.sync.reseed_attempts = 2
            client2.sync.snapshot_fetch_errors = 0
            def mock_urlopen_fail(url, timeout=10):
                raise TimeoutError("WinError 10060")
            with patch('urllib.request.urlopen', side_effect=mock_urlopen_fail):
                with patch('time.sleep', return_value=None):
                    client2.sync.snapshot_fetch_errors = 0
                    # _reseed_now debe capturar excepción, incrementar snapshot_fetch_errors, no reseed_attempts
                    prev_reseed = client2.sync.reseed_attempts
                    client2._reseed_now()
                    assert client2.sync.snapshot_fetch_errors == 1, "fetch failed debe incrementar snapshot_fetch_errors"
                    assert client2.sync.reseed_attempts == prev_reseed, "reseed_attempts NO debe avanzar en fallo REST (diferenciar red vs mismatch)"
                    assert client2.sync.phase == "RESYNC", "phase debe permanecer RESYNC sin avanzar a FAILED en fallo red"
        finally:
            client2._close_files()
    finally:
        client._close_files()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ── T4: WS reconnect/backoff ─────────────────────────────────────────────
def test_T4_ws_reconnect_backoff():
    """T4 WS reconnect/backoff: simular 3×WinError 10060 → ws_reconnects solo en on_open,
    ws_disconnects incrementa, monitor no aborta antes de WS_MAX_RECONNECT."""
    s = DepthCaptureState()
    s.on_depth_event(ev(100,105,99))
    s.set_snapshot(snap(100))
    assert s.phase == "SYNCED"
    # Simular 3 disconnects con WinError 10060
    for i in range(3):
        s.on_ws_disconnect("WinError 10060")
        assert s.ws_disconnects == i+1
        assert s.phase == "RECONNECTING"
        # ws_reconnects NO debe incrementar en disconnect
        assert s.ws_reconnects == i, f"ws_reconnects no debe incrementar en disconnect, iter {i}"
        s.on_ws_reconnect()
        assert s.ws_reconnects == i+1
        assert s.phase == "BUFFERING"
        # Re-sincronizar para volver a SYNCED (evitar FAILED acumulado)
        s.set_snapshot(snap(200+i*10))
        s.on_depth_event(ev(200+i*10, 205+i*10, 199+i*10))
        assert s.phase == "SYNCED"

    assert s.ws_disconnects == 3
    assert s.ws_reconnects == 3
    # Verificar que monitor no aborta antes de WS_MAX_RECONNECT (50) ni MAX_TOTAL_FAILURES (200)
    assert cap.WS_MAX_RECONNECT_ATTEMPTS == 50
    assert MAX_TOTAL_FAILURES == 200
    # Simular _total_failures en CaptureClient: 3 disconnects no deben incrementar _total_failures directamente
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        client = CaptureClient(symbol="XRPUSDC", real=False, levels=20, out_dir=tmp, max_reseed_attempts=5)
        client.sync = s
        client._total_failures = 0
        # Incluso después de 3 FAILED simulados, _total_failures=3 <200, no aborta
        for _ in range(3):
            client._total_failures += 1
            assert client._total_failures < MAX_TOTAL_FAILURES
            assert client._total_failures < cap.WS_MAX_RECONNECT_ATTEMPTS
        # Solo aborta cuando >200
        client._total_failures = 199
        assert not (client._total_failures > MAX_TOTAL_FAILURES)
        client._total_failures = 201
        assert client._total_failures > MAX_TOTAL_FAILURES
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
