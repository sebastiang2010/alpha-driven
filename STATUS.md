# STATUS.md — Bot de Market Making XRPUSDC (Binance Futures)

> Documento vivo. Se actualiza cada 15–20 min durante el desarrollo (§0.3).
> Última actualización: 2026-08-10 (14:10 local).

---

## Estado general

| Área | Estado | Notas |
|---|---|---|
| Infraestructura API (`API_binance_futuros.py`) | ✅ Existe | Única interfaz con Binance Futures (§1). Sin secretos commitables (gitignored §0.5). |
| WebSockets (`websocket_*.py`) | ✅ Existe | bookTicker, depth, trades; testnet por defecto (`real=False`). |
| `strategy/config.py` | ✅ Completo | Único lugar de constantes de riesgo (§0.4, §1, §3). |
| `strategy/market_state.py` | ✅ Completo | Estado de mercado, libro, régimen, alfa, volatilidad. |
| `strategy/alpha_model.py` | ✅ Completo | Inventario, sigma, skew. |
| `strategy/inventory_manager.py` | ✅ Completo | Inventario, target, límites de notional. |
| `strategy/risk_engine.py` | ✅ Completo | Rechazos por límite, kill switch (§12). |
| `strategy/execution_engine.py` | ✅ Completo | Órdenes maker GTX, maker check §10 corregido, reconcile, dry-run no-op. |
| `strategy/market_maker.py` | ✅ Creado | Orquestador que integra todos los módulos. |
| Tests Risk Engine (§12) | ✅ **13/13 OK** | `python -m unittest strategy.tests.test_risk_engine -v`. Incluye kill switch (§13). |
| Tests Execution Engine (§9-§10) | ✅ **14/14 OK** | `python -m unittest strategy.tests.test_execution_engine -v`. Maker check §10 + fills is_buyer_maker. |
| Tests AlphaModel (§6/§9) | ✅ **18/18 OK** | `python -m unittest strategy.tests.test_alpha_model -v`. Convención de side + NetPnL + alpha acotada + regresión §8 + modelo de costos §18. |
| Kill switch (§13) | ✅ Cubierto | 4 tests: daily loss, price anomaly, ws disconnect, no disparo normal. |
| `logs/` | ✅ Creado | `decisions/`, `fills/`, `market_data/`, `orders/`, `pnl/`. |
| `reports/` | ⚠️ Vacío | Sin reportes generados todavía. |
| Git | ✅ Inicializado | 1 commit (`4bf0ae7`, módulos base + tests). `.gitignore` protege credenciales. |

**Regla §0.1**: testnet primero, mainnet SOLO con autorización humana explícita. **Mainnet OPERADA por primera vez el 2026-08-10** (`REAL=True`, `EXPOSURE_LEVEL=1`, autorización explícita §0.2 + confirmación interactiva `CONFIRMAR`).

---

## Presupuesto de riesgo (§0.4) — PROPUESTA pendiente de confirmación

Estos valores en `config.py` son **propuestas conservadoras**; **NO operar con fondos reales** hasta confirmación humana. Sin confirmación, el bot opera solo en testnet o dry-run.

| Constante | Valor propuesto | Estado |
|---|---|---|
| `MAX_DAILY_LOSS_USDC` | 10.0 | ⏳ Pendiente de confirmación |
| `MAX_POSITION_NOTIONAL_USDC` | 25.0 | ⏳ Pendiente de confirmación |
| `MAX_DRAWDOWN_PCT` | 0.05 | ⏳ Pendiente de confirmación |
| `MAX_LEVERAGE_USED` | 5 | ⏳ Pendiente de confirmación |
| `EXPOSURE_LEVEL` | 0 (dry-run) | ✅ Simulación — subir requiere autorización (§0.2) |
| `MAKER_FEE_RATE` | 0.0002 | ⏳ Pendiente de confirmación (fee maker real VIP0 Binance) |
| `FUNDING_RATE_PER_8H` | 0.0001 | ⏳ Pendiente de confirmación (tasa por intervalo de 8 h) |
| `EXPECTED_HOLD_SEC` | 300.0 | ⏳ Pendiente de confirmación (tenencia esperada por posición) |
| `SLIPPAGE_MAKER_BPS` | 0.0 | ⏳ Pendiente de confirmación (GTX post-only no cruza; >0 solo como colchón) |

---

## Verificaciones ejecutadas

- `python -m unittest strategy.tests.test_risk_engine -v` → **13/13 OK** (2026-08-09).
  Rechazos por: notional, tamaño, daily loss, drawdown, exposición, open orders,
  unrealized loss, volatilidad. Kill switch: daily loss, price anomaly, ws disconnect,
  no disparo normal.
- `python -m unittest strategy.tests.test_execution_engine -v` → **14/14 OK** (2026-08-09).
  Maker check §10 (BUY < best_ask, SELL > best_bid, sin libro → rechazo) y
  `process_fills_from_trades` con semántica correcta de `is_buyer_maker`.
- `MarketMaker(dry_run=True)` instancia offline sin errores (API no inicializa `client` en el import).
- **CORRIDA INTEGRADA DRY-RUN** `python run_dry_run.py --cycles 20` (2026-08-09 19:19 local, ~107 s):
  3 WS testnet conectados (`bookTicker`, `depth5@100ms`, `trade`), 19 decisiones nuevas,
  8 órdenes simuladas, 0 fills, 0 kill switch, `ws_connected=True`, sin excepciones,
  shutdown limpio con `cancel_all_orders()`. Filtros reales de símbolo cargados (§3):
  `price_precision=4`, `quantity_precision=1`, `tick_size=0.0001`, `step_size=0.1`,
  `min_qty=0.1`, `min_notional=5.0`.
  - **Sizes > 0 confirmado**: `bid_size`/`ask_size` = 20.0 XRP (bug `sizes=0.0` superado).
  - **Quality gate OK** (post-fix del resumen): 6/19 decisiones `reason=ok`; 13/19
    `expected_net_pnl_non_positive:ask` (comportamiento por diseño §18: no cotizar lado
    con NetPnL esperado ≤ 0, NO es falla). Eventos de órdenes solo `placed`/`canceled`
    (esperados). Alpha pinned en −0.01 (clamp) por trade flow vendedor en ventana de 60 s
    con mid plano; a monitorear, no confirmado como bug.
  - Corrección del resumen: `_summary` ahora filtra orders/fills/kill_switch por `ts`
    epoch float (no ISO) y acepta `canceled` como evento esperado (el `GATE FAIL: canceled`
    previo era falso positivo del propio resumen).
- **CORRIDA POST-FIX `python run_dry_run.py --cycles 5` (2026-08-09 19:41 local, ~31 s)**:
  tras corregir el doble conteo de alpha (§8) y recalibrar la escala de la señal,
  **GATE OK en vivo**: 4/4 decisiones `reason=ok` (antes 0/4), `bid_size`/`ask_size`=20.0,
  0 fills, 0 kill switch, exit code 0. Quotes sanos: con alpha=−0.0005, bid≈mid−0.0063 y
  ask≈mid+0.0052 (spread 0.0005 + sigma efectiva) — ya NO hay ask a mid+2%.
- **CORRIDA POST-FIX §18 `python run_dry_run.py --cycles 5` (2026-08-09 19:48 local, ~31 s)**:
  **GATE OK en vivo**: 4/4 decisiones `reason=ok`, `bid_size`/`ask_size`=20.0, 0 fills,
  0 kill switch, exit code 0. Confirma el efecto del modelo de costos §18: las quotes
  con edge real dentro del spread ya NO son rechazadas por `expected_net_pnl_non_positive`
  (antes 13/19 decisiones rechazadas con costos fijos 0.0004 > spread p50≈0.0003).
- `python -m unittest discover -s strategy/tests -v` → **54/54 OK** (2026-08-09, post-fix modelo de costos §18).

## Hallazgos de auditoría (2026-08-09)

Corregidos en esta sesión:
- **Maker check §10** (`execution_engine._maker_check_ok`): exigía `price <= best_bid` (BUY) / `price >= best_ask` (SELL), rechazando cotizaciones válidas dentro del spread. Ahora: BUY es maker si `price < best_ask`, SELL si `price > best_bid`.
- **Fills** (`execution_engine.process_fills_from_trades`): condiciones de `is_buyer_maker` invertidas; un BUY resting nunca se marcaba filled. Ahora usa la semántica de Binance (documentada en `market_state.update_trade`).
- **Decisión Nivel 0 con simulación IMPLEMENTADA** (§0.2): `SIMULATION_QUOTE_MULTIPLIER` en `config.py` (antes solo documentada en AGENTS.md, nunca existió en el código). Nuevo helper `config.effective_exposure_multiplier()` usado por `risk_engine` y `market_maker._compute_quotes` (fuente única, §0.4). Con esto el Nivel 0 ya no queda bloqueado en tamaño 0.
- **Bug de convención de side (§0.6)**: `expected_net_pnl_estimate` solo reconocía `"bid"/"ask"` pero `_compute_quotes` le pasaba `"BUY"/"SELL"` → devolvía 0.0 siempre → `quote_*_ok=False` → el bot NUNCA cotizaba (ni en dry-run simulado). Ahora acepta ambas convenciones. Cubierto con `test_alpha_model.py` (10 tests).
- **Doble conteo de alpha (bug §8, encontrado por el gate en vivo 2026-08-09)**: `reservation_price` pone `r = mid + alpha` (§8) PERO `quote_distances` SUMABA además `ALPHA_QUOTE_FACTOR × |alpha|` (=1.0) a la distancia base → con alpha=±0.01 el lado alejado quedaba a ±2% del mid y el cercano sin borde (NetPnL≤0). En la corrida 22:26 UTC: mid=1.04215, ask=1.0622, reason=`expected_net_pnl_non_positive:bid` → el gate rechazó todo (0/4 ok) y solo se colocó una SELL que luego se canceló. Corregido: `ALPHA_QUOTE_FACTOR = 0.0` (§8: el alpha ya desplaza la reserva). Regresión cubierta con 2 tests nuevos (§0.6).
- **Saturación de alpha (hallazgo 2026-08-09)**: `ALPHA_MAX=0.01` (±1%) es ~100× el spread real de XRPUSDC (p50≈0.0003); con `IMBALANCE_WEIGHT=0.20`, un imbalance típico (0.077) ya da 0.0154 > 0.01 → alpha pinneado en ±1% SIEMPRE (señal degenerada). Recalibrado conservador (§0.4, "pendiente de confirmación"): `IMBALANCE_WEIGHT=0.003`, `TRADE_FLOW_WEIGHT=0.003`, `ALPHA_MAX=0.0005` (~5 ticks, ~2× spread p50). Mantener bajo monitoreo y recalibrar con más datos.
- **Test de Risk Engine actualizado**: Nivel 0 con simulación ya NO tiene `max_order_size == 0.0`; ahora es `BASE_ORDER_SIZE_XRP * SIMULATION_QUOTE_MULTIPLIER` (= 20.0 XRP). Nuevo test verifica que una orden dentro del tamaño simulado es admitida.
- **Modelo de costos §18 (hallazgo auditoría 2026-08-09, implementado)**: `expected_net_pnl_estimate` aplicaba funding (0.0001) y slippage (0.0001) como costos fijos **por fill**, pero el funding de Binance se cobra **cada 8 h sobre el notional de la posición** (no por trade), y una orden maker GTX post-only **nunca cruza el spread** (slippage 0). Con `fee+funding+slippage = 0.0004` fijos y spread p50≈0.0003, el edge mínimo exigido (≈0.00042) superaba el spread típico → ninguna quote maker podía ser NetPnL-positiva (gate siempre rechazaba, salvo reduce-only). Corregido en `config.py` + `alpha_model.expected_net_pnl_estimate`:
  - `FUNDING_RATE` → `FUNDING_RATE_PER_8H` (0.0001) + `FUNDING_INTERVAL_SEC` (28,800 s) + `EXPECTED_HOLD_SEC` (300 s): funding por trade = `tasa * (hold / intervalo)` ≈ 0.00000104 (antes 0.0001 fijo — sobreestimaba ~1000×).
  - `SLIPPAGE_RATE` → `SLIPPAGE_MAKER_BPS = 0.0` (maker post-only no paga cruce).
  - Nuevos tests `TestModeloDeCostos` (4): hold=0 → funding 0; proporcionalidad al hold (verificada numéricamente); default slippage maker = 0; **quote con edge real dentro del spread (3 ticks, mid=1.0) ahora es operable** (NetPnL>0) donde el modelo viejo daba NetPnL<0.
  - Efecto esperado en vivo: el gate `expected_net_pnl_non_positive` deja de rechazar sistemáticamente; **CONFIRMADO (2026-08-09 19:48)**: corrida post-fix GATE OK 4/4 `reason=ok` (antes 13/19 rechazadas).
  - Parámetros marcados ⏳ "pendiente de confirmación" (§0.4).

**Suite completa: 37/37 OK** (`python -m unittest discover -s strategy/tests`).

Pendientes de decisión (NO corregidos aún — ver Pendiente):
- ~~**Nivel 0 y dry-run**: con `EXPOSURE_MULTIPLIERS[0] = 0.0`, `base_size = 0` → `quote_bid_ok/ask_ok = False` → el dry-run no coloca (ni simula) ninguna orden. Bloquea la primera corrida integrada (§0.4 — decidir multiplicador de simulación o subir nivel con autorización).~~ **RESUELTO 2026-08-09**: `SIMULATION_QUOTE_MULTIPLIER` + `effective_exposure_multiplier()` (ver historial).
- ~~**`init_client` nunca se llama**: en modo real (aun testnet) `api.client` es `None` y todo REST falla. `run()` debería llamar `api.init_client(real=config.REAL)` (testnet permitido por §0.1).~~ **RESUELTO 2026-08-09** (commit `db4a115`): `init_client()` conectado en `run()`, degrada offline; en mainnet `REAL=True` lo inicializa con credenciales reales.
- **`cancel_order_by_id` purga local aunque el cancel falle en la API** → orden fantasma del lado del exchange (§0.6).
- **Kill switch**: `reduce_or_close`/`disable_new_entries` declarados pero `market_maker` solo ejecuta `cancel_all` (§13).
- **NetPnL** omite funding y slippage en `expected_net_pnl_estimate` (§18); `MAKER_FEE_RATE` duplicada en 2 módulos (§0.4).
- **Escala de sigma inconsistente** (§0.6): sigma por evento (ms) vs referencia de 5 s en alpha_model.
- **`_manage_orders`**: `has_bid/has_ask` calculados antes del loop de expire → reposición con 1 ciclo de latencia.
- **Doble skew de tamaño**: `SKEW_SIZE` + `order_side_aggression` compuestos en `_compute_quotes`.
- `.gitignore`: faltan `ruvector.db`, `graphify-out/`, `.opencode/`.

## Pendiente

- [x] Confirmación humana del presupuesto de riesgo (§0.4). — **CONFIRMADO (2026-08-09)**: `MAX_DAILY_LOSS_USDC=10.0`, `MAX_POSITION_NOTIONAL_USDC=25.0`, `MAX_DRAWDOWN_PCT=0.05`, `MAX_LEVERAGE_USED=20` (=máximo real: requiredMarginPercent 5.0%→20x, verificado en exchangeInfo), `BASE_ORDER_SIZE_XRP=5.0` (≈$5.17 > minNotional $5). Flag `BUDGETS_CONFIRMED=True` en config; `run_mainnet.py` aborta si es False.
- [x] Autorización para subir de nivel de exposición (§0.2). — **CONFIRMADO (2026-08-09)**: `EXPOSURE_LEVEL=1` (mainnet mínimo). Mainnet queda BLOQUEADO hasta: correr `run_mainnet.py` con confirmación interactiva `CONFIRMAR` (§0.2) y pre-flight OK (posición 0, leverage ≤ 20x real, PERCENT_PRICE, mark price).
- [x] Decidir si `config.REAL=True` se fija al momento de la corrida mainnet (hoy False; el gate de `market_maker.run()` exige `REAL=True and EXPOSURE_LEVEL>=1`). — **HECHO (2026-08-10)**: `REAL=True` aplicado en commit `13c38bf` (autorización §0.2), `BASE_ORDER_SIZE_XRP` ajustado de 5.0 a **4.9** (=mínimo exacto: 5 USDC ÷ precio ÷ stepSize 0.1 → 4.9 XRP ≈ $5.05 ≥ minNotional $5; cumple mientras precio > $1.0204).
- [ ] Confirmación humana de la recalibración de alpha (pesos + `ALPHA_MAX=0.0005`, §0.4).
- [ ] Investigar el modelo de costos (§18): con `MAKER_FEE_RATE+FUNDING_RATE+SLIPPAGE_RATE = 0.0004` y spread p50≈0.0003, el edge mínimo exigido (≈0.00042) es MAYOR que el spread típico → ninguna quote maker puede ser NetPnL-positiva salvo `reduce_only`. Sospecha: `FUNDING_RATE` aplicado como costo por-trade plano, pero el funding real se cobra periódico (cada 8 h) sobre notional, no por fill. Revisar `expected_net_pnl_estimate` y cómo se aplica. — **Resuelto (2026-08-09)**: funding ahora proporcional al hold time (`FUNDING_RATE_PER_8H` × hold/28800) y slippage maker 0 (`SLIPPAGE_MAKER_BPS`). Quotes con edge real dentro del spread son operables. Ver "Modelo de costos §18" en auditoría.
- [ ] Endurecer `cancel_order_by_id` (no purgar local si la API falla) (§0.6).
- [ ] Implementar reduce/close en kill switch (§13).
- [ ] NetPnL con funding + slippage; unificar fee en config (§18). — **Implementado (2026-08-09)**: funding proporcional al hold time, slippage maker 0, fee único `MAKER_FEE_RATE` en config (fuente única §0.4). Confirmar parámetros con humano (§0.4).
- [ ] Reescalar sigma a intervalo fijo (§0.6).
- [ ] Generación de reportes en `reports/` (§22).
- [ ] Investigar alpha pinned en −0.01 (clamp) en dry-run: trade flow vendedor vs bug de convención (§0.6). — **Resuelto (2026-08-09)**: era saturación por `ALPHA_MAX=0.01` con `IMBALANCE_WEIGHT=0.20` (imbalance típico 0.077 → 0.0154 > cap). Recalibrado a `ALPHA_MAX=0.0005` y pesos 0.003.
- [ ] Verificación final en vivo: re-ejecutar `run_dry_run.py` y ver `GATE OK` (opcional, ya validado offline). — **Hecho (2026-08-09 19:41)**: GATE OK 4/4 en vivo tras el fix §8.
- [ ] Commit de los fixes de esta sesión (§0.3, §24).

## Log de actualizaciones

- **2026-08-09**: creado este archivo. Módulos `strategy/` completos, `market_maker.py` orquestador creado, tests Risk Engine 13/13 OK, kill switch cubierto.
- **2026-08-09**: corregidos maker check §10 y semántica de fills `is_buyer_maker`. Nuevo `test_execution_engine.py` (14/14 OK). Auditados bugs pendientes (Nivel 0/dry-run, init_client, cancel purga local, kill switch reduce/close, NetPnL, sigma).
- **2026-08-09**: implementada la decisión Nivel 0 con simulación (`SIMULATION_QUOTE_MULTIPLIER` + `effective_exposure_multiplier()`), corregido bug de convención de side en NetPnL (bot nunca cotizaba), nuevo `test_alpha_model.py`. **Suite 37/37 OK.** Commit `1e5432f`.
- **2026-08-09**: conectado `init_client(real=False)` en `run()` (testnet, §0.1) con método `ExecutionEngine.init_client()` que degrada offline. Filtros reales del símbolo cargados en testnet (§3). Commit `db4a115`.
- **2026-08-09**: **corrida integrada dry-run exitosa** (`run_dry_run.py`, 20 ciclos, ~107 s). 3 WS testnet, 19 decisiones, 8 órdenes simuladas, sizes=20.0, 0 kill switch, gate OK. Creado `run_dry_run.py` (watchdog 125 s, resumen con quality gate, exit codes 0/1/2); corregido el resumen (filtro por `ts` float + eventos `canceled` aceptados). `STATUS.md` actualizado. Pendiente commit.
- **2026-08-09**: **corregido doble conteo de alpha (§8)** — el gate en vivo lo detectó (ask a mid+2%, 0/4 decisiones `ok`). `ALPHA_QUOTE_FACTOR` 1.0 → 0.0; recalibrada la señal a escala de spread (`ALPHA_MAX` 0.01 → 0.0005, `IMBALANCE_WEIGHT`/`TRADE_FLOW_WEIGHT` → 0.003, conservador "pendiente de confirmación" §0.4). 2 tests de regresión nuevos. **GATE OK en vivo post-fix (4/4)**, suite 50/50 OK.
- **2026-08-09**: **corregido modelo de costos §18** — funding era costo fijo por fill (0.0001) pero Binance lo cobra cada 8 h sobre notional; slippage 0.0001 por fill sobrestimaba un maker GTX que nunca cruza. Ahora: `FUNDING_RATE_PER_8H` × `EXPECTED_HOLD_SEC`/`FUNDING_INTERVAL_SEC` (≈0.00000104/trade) + `SLIPPAGE_MAKER_BPS=0.0` + fee único de config. 4 tests nuevos (`TestModeloDeCostos`): quote con edge de 3 ticks dentro del spread ahora operable donde antes NetPnL<0. Suite **54/54 OK**.
- **2026-08-09**: **verificación en vivo del fix §18** — `run_dry_run.py --cycles 5`: GATE OK 4/4 `reason=ok` (antes 13/19 rechazadas por `expected_net_pnl_non_positive`). Commit `f2e4081`.
- **2026-08-10**: **PRIMERA CORRIDA MAINNET OPERADA** (exit 0, 123.8 s, 20 ciclos). Fix previo `345c7a7`: `reconcile_position` distinguía posición vacía (dict `positionAmt=0`) de error API (`None`) — el pre-flight abortaba con cuenta limpia (exit 3) en el primer intento. Lanzamiento: `echo CONFIRMAR | python run_mainnet.py --cycles 20` → pre-flight OK (posición 0, filtros reales: min_notional 5.0, step 0.1, tick 0.0001; leverage 20x; PERCENT_PRICE ±5%; mark 1.0273), **8 órdenes reales colocadas** (bid/ask size **4.9000** = mínima exacta), 0 fills, kill_switch 0, GATE OK (sin simuladas). 8/19 decisiones reason==ok; resto `expected_net_pnl_non_positive:bid` (filtro §18 correcto: spread ~0.0001 no cubre fees en el lado bid). Pendientes menores: DeprecationWarning `utcnow()` (`run_mainnet.py:284`), UnicodeEncodeError cp1252 al loguear `→` (cosmético), error transitorio `get_symbol` antes de `init_client` (no fatal). Corrida extendida 500 ciclos lanzada en monitoreo.
- **2026-08-10 (14:10 local)**: **CORRIDA MAINNET 500 ciclos — PRIMER FILL REAL y ROUND TRIP 1**. El bot compró 4.9 XRP @ 1.0279 (server `8012000560`, fill real maker) y vendió 4.9 @ 1.026 (server `8012132404`) → **NetPnL round trip 1 = −0.0113 USDC** (adverse selection: el mercado cayó 19 ticks entre entrada y salida; fees maker 2×0.0002≈0.002; NO es bug de ejecución). Un 3er fill real (SELL 4.9 @ 1.0255, server `8012191197`) abrió **posición corta −4.9 XRP** (cotización neutral que se llenó; comportamiento normal de MM, bot recomprando vía bid reduce; PnL no realizado +0.0073; liq ~1.0725/+4.6%, pérdida máx ~$0.23 ≤ 2.3% del presupuesto diario). **Fixes aplicados tras el diagnóstico**: `632f9c3` clamp del lado reduce a `max_order_size` (desatasca: con inventory=4.9 el ask 7.35 era rechazado, ahora 4.9); `1a2b11e` registro de fills reales en `refresh_open_orders` idempotente por `client_order_id` (§15/§0.6); `611efb2` watchdog derivado `max(125 s, cycles·6+30)` + pre-flight tolera posición pre-existente dentro de límites (round trip, §0.2); **`be637e5` piso de spread `MIN_SPREAD_TICKS=8`** (aprobado por humano; breakeven fees maker = 4.1 ticks → 8 ticks = 1.95× fees) enforced en `alpha_model.quote_distances()` tras el skew, ensanche simétrico. **Suite 57/57 OK** (54 + 3 `TestPisoDeSpread`). **Doc generada**: `doc/estrategia_market_making.pdf` (9 págs, LaTeX, commit `92af3b3`). Pendiente: aplicar piso en el próximo relanzamiento (proceso actual corre con config vieja), control de adverse selection (2ª línea recomendada, NO aprobada aún), `get_order_status` en la API para distinguir fill real de cancel externo (requiere aprobación §0.2).
