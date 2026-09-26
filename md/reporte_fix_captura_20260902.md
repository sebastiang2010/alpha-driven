# Reporte Fix Captura L2 — T1-T4 (2026-09-02)

**Scope aislado:** `strategy/capture_l2.py`, `scripts/capture_l2_testnet.py` (copia sincrónica), `tests/test_capture_l2_fix_T1T4.py` + validación smoke Mainnet. **No tocado:** `strategy/market_maker.py`, `risk_engine.py`, `execution_engine.py`/`fill_simulator.py`, `config.py` defaults, quoting, `MIN_SPREAD_TICKS`, `MAX_POSITION_NOTIONAL`, `FILL_SIMULATOR_EXPERIMENTAL`, `H/OFF`.

**Dataset origen:** `data/l2_24h_campaign/20260902_105959` parcial 51m FAILED 101 `snapshot_sync_errors`, WinError 10060, 8 `ws_disconnects` / 1 `ws_reconnects`, `pu_gaps` 0 `crossed` 0. Causa C diagnosticada en `md/diagnostico_captura_101_sync_20260902.md`.

---

## 1. Diff/resumen exacto del fix

**Archivos tocados (git diff --cached --stat):**
```
 scripts/capture_l2_testnet.py     | 1091 +++++++++++++++++++++++++++++++++++++
 strategy/capture_l2.py            | 1091 +++++++++++++++++++++++++++++++++++++
 tests/test_capture_l2_fix_T1T4.py |  314 +++++++++++
 3 files changed, 2496 insertions(+)
```
> Nota: `strategy/capture_l2.py` y `scripts/capture_l2_testnet.py` eran untracked (F8) → diff muestra inserción completa; el **cambio neto** respecto a versión previa es acotado a 7 requisitos.

### Cambios en `strategy/capture_l2.py` (idéntico en `scripts/capture_l2_testnet.py`)

**a) Constantes — Req 5 + 4:**
```diff
 MAX_RESEED_ATTEMPTS = 5
 RESEED_INTERVAL_SEC = 2.0
+RESEED_INTERVAL_SYNCING_SEC = 15.0
+MAX_TOTAL_FAILURES = 200
 WS_MAX_RECONNECT_ATTEMPTS = 50
 ...
-WS_PING_INTERVAL = 10
-WS_PING_TIMEOUT = 5
+WS_PING_INTERVAL = 20
+WS_PING_TIMEOUT = 10
```
*Por qué:* `SYNCING` 5s →15s evita spam REST cuando `pending==[]` (sin evento `U<=L+1<=u` imposible sincronizar). `MAX_TOTAL_FAILURES` 50→200 evita matar captura 24h por 4 min de downtime (50*5s). Ping 10→20 reduce falsos `WSAETIMEDOUT` en Windows/VPN (observado en smoke).

**b) Contador separado — Req 7:**
```diff
-        self.snapshot_sync_errors = 0
+        self.snapshot_sync_errors = 0
+        self.snapshot_fetch_errors = 0
```
y en `stats()`:
```diff
+            "snapshot_fetch_errors": self.snapshot_fetch_errors,
```
*Por qué:* Diferenciar fallo red REST vs mismatch lógico (propuesta diagnóstica).

**c) `CaptureClient.__init__` — Req 2 gate:**
```diff
         self._lock = threading.Lock()
         self._last_reseed_ts = 0.0
+        self._last_reseed_ws_reconnects = 0
```
*Por qué:* Gate anti-spam `pending==[] && ws_reconnects==last`.

**d) `_fetch_snapshot()` — Req 4 retry/backoff:**
```diff
-    def _fetch_snapshot(self) -> Dict[str, Any]:
-        import urllib.request
-        url = snapshot_url(self.symbol, self.levels, self.real)
-        with urllib.request.urlopen(url, timeout=10) as resp:
-            return json.loads(resp.read().decode())
+    def _fetch_snapshot(self) -> Dict[str, Any]:
+        import urllib.request
+        url = snapshot_url(self.symbol, self.levels, self.real)
+        last_exc = None
+        for attempt in range(3):
+            try:
+                with urllib.request.urlopen(url, timeout=10) as resp:
+                    return json.loads(resp.read().decode())
+            except Exception as e:
+                last_exc = e
+                if attempt < 2:
+                    backoff = 1.0 * (2 ** attempt) + random.uniform(-0.1,0.1)*...
+                    time.sleep(backoff)
+                    continue
+                raise
```

**e) `_reseed_now()` — Req 2+3:**
```diff
-    def _reseed_now(self) -> None:
-        self._last_reseed_ts = time.time()
-        try:
-            snap = self._fetch_snapshot()
-        except Exception as e:
-            sys.stderr.write(f"[capture] snapshot fetch failed: {e}\n")
-            return
+    def _reseed_now(self) -> None:
+        # Gate anti-spam: SYNCING/RESYNC/RECONNECTING con pending==[] y ws sin cambio → skip
+        with self._lock:
+            pending_len = len(self.sync.pending); ws_recon = self.sync.ws_reconnects
+            phase = self.sync.phase; has_snap = self.sync.snap is not None
+        if phase in ("SYNCING", "RESYNC", "RECONNECTING"):
+            if pending_len==0 and ws_recon==self._last_reseed_ws_reconnects:
+                self._last_reseed_ts = time.time(); return
+        elif phase=="BUFFERING" and has_snap:
+            if pending_len==0 and ws_recon==self._last_reseed_ws_reconnects:
+                self._last_reseed_ts = time.time(); return
+        self._last_reseed_ts = time.time()
+        self._last_reseed_ws_reconnects = ws_recon
+        try:
+            snap = self._fetch_snapshot()
+        except Exception as e:
+            sys.stderr.write(f"[capture] snapshot fetch failed: {e}\n")
+            with self._lock:
+                self.sync.snapshot_fetch_errors += 1
+            return
```
*Por qué:* Evita reseed inútil cuando `pending==[]` y no hubo nuevos eventos WS (protocolo `U<=L+1<=u` requiere `pending` no vacío). Diferencia red (incrementa `snapshot_fetch_errors`, NO `reseed_attempts`) de mismatch lógico (deja `set_snapshot` avanzar `reseed_attempts`).

**f) `run()` monitor — Req 1+5:**
```diff
-                        if self._total_failures > 50:
+                        if self._total_failures > MAX_TOTAL_FAILURES:
                             self._exit_reason = "max_failures"; break
-                        self.sync.snapshot_sync_errors = old.snapshot_sync_errors + 1
+                        self.sync.snapshot_sync_errors = old.snapshot_sync_errors
+                        self.sync.snapshot_fetch_errors = old.snapshot_fetch_errors
+                        self._last_reseed_ws_reconnects = old.ws_reconnects
```
*Por qué:* Corrige doble conteo (Req1): `set_snapshot` ya hace `snapshot_sync_errors++` al pasar a `FAILED`; el clon no debe `+1`. Preserva `snapshot_fetch_errors` separado.

```diff
-                    if phase in ("RESYNC", "RECONNECTING") and (now - last) >= RESEED_INTERVAL_SEC:
+                    if phase == "BUFFERING" and (now - last) >= RESEED_INTERVAL_SEC:
+                        self._reseed_now()
+                    elif phase in ("RESYNC", "RECONNECTING") and (now - last) >= RESEED_INTERVAL_SEC:
                         self._reseed_now()
-                    elif phase == "SYNCING" and (now - last) >= 5.0:
+                    elif phase == "SYNCING" and (now - last) >= RESEED_INTERVAL_SYNCING_SEC:
                         self._reseed_now()
```
*Por qué:* `SYNCING` 5→15s (Req5) + manejo `BUFFERING` inicial si gate bloqueó (evita deadlock).

**Protocolo Binance intacto (Req6):** `U <= lastUpdateId +1 <= u` y `pu == previous_u` sin cambios (ver `DepthMergeBuffer.apply`, `DepthCaptureState.on_depth_event`, `_try_sync_pending`).

---

## 2. Resultados T1–T4 + suite capture

### T1–T4 aislados (mocks, sin red)
```
pytest tests/test_capture_l2_fix_T1T4.py -v
test_T1_blackout_no_events_no_failed_no_duplicate PASSED
test_T2_doble_conteo_snapshot_sync_errors PASSED
test_T3_rest_timeout_retry PASSED
test_T4_ws_reconnect_backoff PASSED
4 passed
```
*Detalles:*
- **T1:** `SYNCING` + `pending==[]` + mock fetch OK → gate bloquea 20 ciclos (5 min), `snapshot_sync_errors` 0, no `FAILED`, intervalo 15s respetado. Luego `pending=[ev solapante]` → fetch OK y `SYNCED`.
- **T2:** Fuerza `FAILED` (MAX_RESEED 5 agotado) → `snapshot_sync_errors==1`; clonado `CaptureClient` 2× sin `+1` duplicado → `snapshot_sync_errors` permanece 1, no 3; segundo `FAILED` real → 2, no 4.
- **T3:** `urlopen` timeout 2× luego OK → 3 intentos (`_fetch_snapshot` retry), `snapshot_fetch_errors` 1 en fallo total, `reseed_attempts` no avanza en fallo red.
- **T4:** 3× `WinError 10060` → `ws_disconnects` 3, `ws_reconnects` solo en `on_ws_reconnect` (3), `MAX_TOTAL_FAILURES` 200 y `WS_MAX_RECONNECT` 50 no abortan.

### Suite capture existente
```
pytest tests/test_capture_l2.py -v
26 passed (A-J + pure parse + merge + disconnect/reconnect + reseed_interval_throttle + max_reconnect + aggTrade independence + continuity + timestamp_gap + integration_mock + file_not_truncated + counters_monotonic + metadata_logged)
```

### Execution reconstruction (no debe romperse)
```
pytest tests/test_execution_reconstruction.py -q
23 passed
```

---

## 3. Resultado smoke Mainnet (solo lectura WS público)

**Comando:** `python scripts/capture_l2_24h_campaign.py --duration-minutes 1.2 --levels 5 --out data/l2_smoke_70s` (real Mainnet, `wss://fstream.binance.com/ws`, `depth@100ms` 5lvl + `trade`).

**Duración:** 72.0 s (1.2m) — también validado `data/l2_smoke_20260902` 118.5s con 798 depth/589 trades.

**Heartbeat nominal (70s):**
```
[31s] phase=SYNCED depth=199 trades=69 gaps=0 reseeds=0 reconnects=1
[61s] phase=SYNCED depth=410 trades=195 gaps=0 reseeds=0 reconnects=1
```

**Validación `data/l2_smoke_70s/validation.json`:**
```json
{"n_depth": 485, "n_trades": 227, "pu_gaps_csv": 0, "pu_gap_details": [], "crossed_manual": 0, "spread_ticks_min": 0.99, "trades_invalid_en_csv": 0, "max_timeline_gap_ms": 1043}
```
**Stats `XRPUSDC_capture_stats.json` (extracto):**
```json
{"phase": "RECONNECTING", "snapshot_sync_errors": 0, "snapshot_fetch_errors": 0, "sequence_gaps": 0, "valid_depth_events": 485, "aggtrade_events": 227, "ws_reconnects": 1, "ws_disconnects": 1, "exit_reason": "completed_duration"}
```
> `phase` final `RECONNECTING` es **cierre ordenado** (on_close al terminar deadline → RECONNECTING), no fallo durante captura. Heartbeat demuestra `SYNCED` estable 61s. `pu_gaps_csv 0`, `crossed_manual 0`, `trades_invalid_csv 0`, `crossed_book_events 0`, `snapshot_sync_errors 0`, `snapshot_fetch_errors 0`.

**Primer smoke (`data/l2_smoke_20260902`, 118.5s):** `n_depth 798`, `n_trades 589`, `pu_gaps 0`, `crossed 0`, `spread_ticks_mean 1.14`, `max_gap 1043ms`, también `SYNCED` en heartbeat 91s.

**Conclusión smoke:** Camino nominal `U<=L+1<=u` + `pu==prev_u` verificado en vivo, sin contaminación CSV.

---

## 4. Confirmación defaults producción intactos

```
grep -n MIN_SPREAD_TICKS strategy/config.py
180:MIN_SPREAD_TICKS: int = 8

grep -n MAX_POSITION_NOTIONAL_USDC strategy/config.py
41:MAX_POSITION_NOTIONAL_USDC: float = 25.0

grep -n FILL_SIMULATOR_EXPERIMENTAL strategy/config.py
290:FILL_SIMULATOR_EXPERIMENTAL: bool = False

grep -n FILL_SIMULATOR_H_MS strategy/config.py
293:FILL_SIMULATOR_H_MS: int = 3000
294:FILL_SIMULATOR_OFF_TICKS: int = 0

grep -n MAX_DAILY_LOSS_USDC strategy/config.py
38:MAX_DAILY_LOSS_USDC: float = 10.0  # presupuesto conservador
```

Todos intactos: `MIN_SPREAD 8`, `MAX 25`, `FILL false`, `H 3000 OFF 0`, `FILL_SIMULATOR_PARAMS_PATH` sin cambios, `EXPOSURE_LEVEL`/`SIMULATION_QUOTE_MULTIPLIER` no tocados.

---

## 5. Recomendación para nueva campaña 24h

**¿Listo para relanzar 24h?** **SÍ**, con matices:

- **Fix cubre causa C completa:** doble conteo eliminado, anti-spam `pending==[]`, retry REST, `SYNCING` 15s, `MAX_TOTAL_FAILURES` 200 (≈50 min tolerancia vs 4 min antes), ping 20/10 más estable.
- **Tests T1-T4 + 26 capture + 23 execution OK**, smoke vivo `SYNCED` con invariants 0.
- **Riesgo residual:** `WS_MAX_RECONNECT 50` con backoff 60s da ~30 min de tolerancia; `MAX_TOTAL_FAILURES 200` lo desacopla pero sigue habiendo `WS_PING_TIMEOUT 10` que puede dar `ping/pong timed out` esporádico (visto en retry). Con 20/10 mejoró pero no elimina. Para 24h, considerar `WS_MAX_RECONNECT` mayor o `MAX_TOTAL_FAILURES` time-based (3600s sin SYNCED) como segunda capa si se observa otro `10060` prolongado.
- **Próximo paso recomendado:** Lanzar campaña 24h con `--levels 5 --duration-hours 24` bajo `scripts/capture_l2_24h_campaign.py` (ya valida defaults). Monitorear `data/l2_24h_campaign/<ts>/validation.json` cada hora; si `snapshot_sync_errors` >0 pero `pu_gaps` 0 y `phase` se recupera a `SYNCED`, es comportamiento esperado del nuevo self-heal.
- **No relanzar hoy sin observar al menos 1 captura de 1–2h estable** (smoke 70s es prometedor pero 24h expone ventana VPN/firewall nocturna). Si se quiere máxima seguridad, hacer piloto 1h antes de 24h.

---

## 6. Ruta reporte técnico

- **Diagnóstico original:** `md/diagnostico_captura_101_sync_20260902.md` (101 errores, causa C, propuesta mínima)
- **Este reporte (fix):** `md/reporte_fix_captura_20260902.md`
- **Tests:** `tests/test_capture_l2_fix_T1T4.py` (T1–T4), `tests/test_capture_l2.py` (26), `tests/test_execution_reconstruction.py` (23)
- **Smokes:** `data/l2_smoke_20260902/` (798/589), `data/l2_smoke_70s/` (485/227), `data/l2_smoke_final3/` (575/352)
- **No commit realizado** (diff acotado staged `git diff --cached --stat` arriba). Para deshacer stage: `git reset HEAD strategy/capture_l2.py scripts/capture_l2_testnet.py tests/test_capture_l2_fix_T1T4.py`

---

*Generado 2026-09-02 — Fix aislado sin tocar producción, protocolo Binance intacto, contadores separados, smoke Mainnet solo lectura.*
