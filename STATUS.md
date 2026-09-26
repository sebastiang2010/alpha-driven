# STATUS — Alpha-Driven (XRPUSDC MM)
**Actualizado**: 2026-09-26 (Point 6 markouts completado; reconciliación post-falso-verde)

## Estado actual
- **HEAD real**: `18420e4` en `master` (local, ahead de `origin/master@405f38f`; el `6010803` citado antes no existe — ver lección anti-hash-fantasma)
- **Suite canónica** (pytest `tests/` + `strategy/tests/`, `-p no:cacheprovider`): **491 passed / 9 failed** (2026-09-26)
  - Los 9 fallos son **pre-existentes** (verificado con stash: fallan sin mis cambios; vienen del trabajo sucio ajeno en `alpha_model`/`market_state`/`config`/`walk_forward`):
    - `test_run_monte_carlo_real_data_fails_protocol`, `test_adverse_filter_threshold_defined`,
      3× `TestPisoDeSpread`/`TestFiltroMomentum` (`alpha_model`), `test_inventory_penalty_limits`,
      3× volatilidad (`market_state`)
  - **Point 6 + ajustes del veredicto**: `test_execution_reconstruction_markout_backfill.py` **7/7 OK** + `test_execution_reconstruction.py` (incl. `TestReduceOnlyLimits` 8/8) + `test_as_coordinator.py` → **69/69 OK** en el área tocada
- Nivel 0 / dry-run sigue operativo; presupuestos de riesgo: **pendientes de confirmación humana**
- WS L2 piloto: capture en curso, hueco 3328 s → proceso WS quedó BLOQUEADO SIN SALIDA (silencio total)

## Point 6 — markouts vencidos y ventana consolidada (2026-09-26, hecho)
- `strategy/execution_reconstruction.py`: `MARKOUT_WINDOW_MS=5000`, `_evaluate_pending_markouts()` (al llegar cada book evalúa fills con `firing_time<=ts` contra el PRIMER book `>= firing`; log `markout_5s`), `_markout_value()` con signo BUY +1 / SELL −1.
- Bug de unidades corregido en `_process_submit` (§0.6): el check nocional comparaba `price_ticks×qty` contra USDC 25.0 (rechazaba todo); ahora `lots×qty_step×mid_ticks×tick_size`.
- Tests reparados: `test_equivalence_position_cap` (qty 60→10 para no pisar el check nocional que `Replay` no tiene), `TestPositionLimitCandidate` (eliminado `apply_commands` duplicado + trade qty que supera la cola FIFO + removida línea muerta que subindexaba el dataclass).
- Sin regresiones: mismos 9 fallos pre-existentes con y sin mis cambios (verificado por stash).

## Ajustes del veredicto (2026-09-26, hecho — 66/66 área, 488/9 suite)
1. **Reservas por lado**: la candidata solo ensancha el extremo que puede empeorar (SELL→low, BUY→high); vender en largo / comprar en corto al límite se acepta (`TestReduceOnlyLimits`: 5 tests, largo/corto/aumento-bloqueado).
2. **Nocional configurable y direccional**: usa `ReconstructionConfig.max_notional` (default 25.0) sobre `max(|low|,|high|)` direccionales; la reducción no se bloquea (test inv +20 vende 5 OK; `max_notional=5.0` rechaza 10 lots que con 25.0 pasa).
3. **Tolerancia y motivos de markout** (criterio `Replay.markouts(tolerance_ms=500)`): `MARKOUT_TOLERANCE_MS=500`, `ReconstructionFill.markout_reason` (`pending|ok|late_book|end_of_data`); `finish()` cierra pendientes como `end_of_data`. Filtro de equivalencia ampliado a eventos `markout_*` (incremental-only, igual que `fill`).
- Tests nuevos: `late_book` (book 900ms tarde → sin valor), `end_of_data` (finish sin cobertura), `ok` en firing exacto.

## Bypass reduce-only ante exceso (2026-09-26, hecho — 69/69 área, 491/9 suite)
- Si el inventario existente ya supera el límite (p. ej. precio $1.00→$1.30: 20 XRP = $26 > $25) y la candidata es estrictamente reductora (lado opuesto, `candidata + pendientes_mismo_lado <= |inv|`, sin inversión posible), se acepta con evento `submit_reduce_only` aunque los extremos sigan fuera de límite. Aplica a ambos controles (lots y nocional).
- Tests: largo +20→SELL 5 aceptada / BUY 1 rechazada por nocional; corto −20→BUY 5 aceptada / SELL 1 rechazada; `test_reducer_cannot_flip_position` delimita el bypass (vender 6 con +5 va por vía normal).

## Disciplina de evidencia (lección registrada)
- El task previo reportó "63/63" en un worktree mutado (archivos fantasma, `.venv` externo). Rerun canónico encontró 137 deseleccionados y 9 fallos que mi comando original no veía.
- Lección documentada en `.operator/specs/leccion_duplicate_basename_pytest.md` (incluye anti-patrón "lector perezoso": números de suite solo válidos con WC estable, contrastar conteo selected vs baseline).
- **Regla nueva**: NUNCA aprobar una rama por una corrida verde sin que coincida exactamente el número de tests seleccionados (`collected X items / Y deselected`) con el esperado.

## Pendientes P1+
- (NUEVO) Investigar `test_fill_rate_deterministic_baseline_zero_fees` — riesgo "se degradó a estas simulaciones" del revisor.
- (NUEVO) Aislar teardown en `TestF38ReplayRejectsAgedBadge` (disk-full simulado persiste entre tests).
- Investigar bloqueo WS piloto (post-reconnect code=None); fix ping/pong user-level o watchdog (§13).
- Rellenar placeholders T19.4 (decision-snapshot schema faltante).
- Backtest kernel (§6/§16) + absorber grid + lob-feature-engineer.

## Proceso
- Commits chicos y frecuentes (§0.3). Push a main solo con autorización humana.
- Actualizar este archivo cada 15–20 min.
- **Workspace**: detectado proceso externo mutando WC durante la sesión (archivos aparecen/desaparecen, rama cambia sin push propio). Propuesta al operador: congelar sesiones concurrentes antes de cerrar tareas.
