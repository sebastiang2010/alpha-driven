# STATUS — Alpha-Driven (XRPUSDC MM)
**Actualizado**: 2026-09-26 (Point 6 markouts completado; reconciliación post-falso-verde)

## Estado actual
- **HEAD real**: `18420e4` en `master` (local, ahead de `origin/master@405f38f`; el `6010803` citado antes no existe — ver lección anti-hash-fantasma)
- **Suite canónica** (pytest `tests/` + `strategy/tests/`, `-p no:cacheprovider`): **515 passed / 9 failed** (2026-09-26)
  - Los 9 fallos son **pre-existentes** (verificado con stash: fallan sin mis cambios; vienen del trabajo sucio ajeno en `alpha_model`/`market_state`/`config`/`walk_forward`):
    - `test_run_monte_carlo_real_data_fails_protocol`, `test_adverse_filter_threshold_defined`,
      3× `TestPisoDeSpread`/`TestFiltroMomentum` (`alpha_model`), `test_inventory_penalty_limits`,
      3× volatilidad (`market_state`)
  - **Point 6 + ajustes + F1.1 + F1.2**: `test_execution_reconstruction_markout_backfill.py` **7/7 OK** + `test_execution_reconstruction.py` (incl. `TestReduceOnlyLimits` 9/9 y `TestCycleTimerDrain` 2/2) + `test_as_coordinator.py` + `test_offline_coordinator.py` (13/13) + `test_as_calendar.py` (F1.2, 8/8) → **93/93 OK** en el área tocada
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
- Tests: largo +20→SELL 5 aceptada / BUY 1 rechazada por nocional; corto −20→BUY 5 aceptada / SELL 1 rechazada; `test_reducer_cannot_flip_position` delimita el bypass (vender 6 con +5 va por vía normal); `test_flip_excluded_from_bypass_under_excess` (largo y corto): orden que invertiría el signo bajo exceso → rechazada por vía normal y sin `submit_reduce_only`.

## F1.1 — coordinador e interfaz unificados (2026-09-26, hecho — 76/76 área, 498/9 suite)
- `strategy/offline_coordinator.py`: frontera única `flatten_event`/`flatten_command` (MarketEvent/Command → dict plano, el único protocolo que `ExecutionReconstructor` acepta). Antes se entregaban dataclasses al motor (`e.get` → AttributeError).
- `advance_to_next_timestamp` conserva TODOS los eventos del ts en orden de carga (varios libros mismo ts incluidos; el motor aplica sort estable por prioridad). `_get_book_for_ts` queda solo para warm-up; eliminado `_get_trades_for_ts` en desuso; `apply_policy_commands` usa `flatten_command`.
- Tests sintéticos nuevos `strategy/tests/test_offline_coordinator.py` (9/9): protocolo flatten, varios libros mismo ts en orden, submit→fill→finish end-to-end, `run_full` con policy_fn, factory desde filas crudas, rechazo de claves reservadas (`ts_ms`/`kind` en data → `ValueError` antes de tocar el motor).
- Sin regresiones: mismos 9 fallos pre-existentes. Sin simulaciones de mercado, sin push (F1.2+ pendientes de revisión).

## F1.2 — calendario 5s, ciclos sin eventos, drenado de timers (2026-09-26, hecho + 4 ajustes — 91/91 área, 513/9 suite)
- `strategy/as_coordinator.py`: default `decision_interval_ms` 1000→5000 (`__init__` y `ASCoordinatorConfig`); `run()` recorre el calendario fusionado (ciclos `t0+k*D` dentro de cobertura ∪ timestamps de eventos, ascendente, sin duplicados): los ciclos sin eventos avanzan con lista vacía y deciden si hay warm-up; ningún trade/libro se salta. `_check_gap` ahora solo entre EVENTOS consecutivos (`_last_event_ts`; los pasos de ciclo no cuentan); `_cycle_grid()` genera la grilla.
- `strategy/execution_reconstruction.py` (`advance_to`): drena timers con `timer_ts < ts_ms` antes de los eventos (equivale al interleave cuando hay eventos; habilita avances vacíos). Cada timer se despacha en su propio ts; prioridades y `finish()` intactos.
- Tests: `TestCycleTimerDrain` (2, motor: avance vacío dispara arrival@1040; cronología preservada con eventos posteriores) + `strategy/tests/test_as_calendar.py` (5: default 5s, pasos fusionados exactos `[1000,2000,3000,6000,7000,11000,12000]` con `observed_end=12000`, decisión en ciclo vacío sin pendientes, trade off-grid consumido, hueco entre eventos invalida sin reset).
- `finish()` sigue cerrando en el último ts con eventos (límite de fin de datos). Sin simulaciones de mercado, sin push. Tests existentes con `decision_interval_ms=1000` explícito: intactos.
- **4 ajustes del veredicto**: (1) `strategy/calendar.py` nuevo — `validate_interval_ms` / `cycle_grid` / `merged_steps` / `check_book_coverage`, una sola implementación para ambas rutas; `OfflineCoordinator` camina pasos fusionados (`_steps`) y valida su intervalo en el ctor; (2) `ASCoordinator._t0` = primer LIBRO (trades previos se procesan igual); (3) intervalo no entero-positivo (0, negativo, fraccionario, str, bool, None) → `ValueError` antes de la grilla, en ambas rutas; (4) cobertura de libro validada antes de la política en cada ciclo (vacío o no, haya comandos o no) — libro ausente/obsoleto invalida.
- Tests: trade@500 previo al primer libro (t0=1000, trade consumido); intervalos inválidos ×6 en ambas rutas; ciclo vacío con libro obsoleto y política muda (invalida antes de llamarla, `calls==[]`); no-reset con inventario 5 (detectaría un reset; el 0 no); segunda ruta camina ciclo vacío 1500 y cierra en 2000.
- **Fin de datos (ajuste)**: `OfflineCoordinator.load_events` fija cobertura SOLO con eventos públicos; comando posterior al último evento → `ValueError` explícito en carga (motor intacto, sin pasos/decisiones/drenado/cierre extendido); sin eventos de mercado → `ValueError`. Tests: comando@2500 con datos hasta 2000 rechazado; comando en el fin (2000) admitido y pasos acotados.

## Veredicto global — fases 1 y 2 ABIERTAS (2026-09-26, pendiente confirmación humana)
- Diseñador: aprobado lo hecho hasta `test_flip_excluded_from_bypass_under_excess`; markouts aprobados. Bloqueos en integración. **No ejecutar sin confirmación del usuario; no comparaciones ni push.**
- Fase 1: (1) ~~unificar `OfflineCoordinator`~~ **HECHO (F1.1)**; (2) ~~calendario 1s→5s, ciclos sin eventos + drenar timers~~ **HECHO (F1.2)**; (3) conectar A-S real: estado causal, conversión ticks↔USDC, tiempo simulado explícito, warmup >3 muestras (`as_coordinator.py:270`, `alpha_model.py:329`); (4) flujo cancel/replace + registro de objetivos por ciclo (`as_coordinator.py:324`).
- Fase 2: (5) medición integrada en `FinalResult`: equity neta, costes, valoración final, agregados de fills en cancelación, tiempo sin cotizar, exposición por lado (`execution_reconstruction.py:940`).
- Orden propuesto + pruebas sintéticas de integración sin mockear la decisión A-S.

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
