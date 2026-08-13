# STATUS.md — Bot de Market Making XRPUSDC (Binance Futures)

> Documento vivo. Se actualiza cada 15–20 min durante el desarrollo (§0.3).
> Última actualización: 2026-08-13 (corrida mainnet "última para evaluar VPS" + aplanado de posición; ver Log de actualizaciones).

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
| `reports/` | ✅ Activo | `trade_status.py`, `fetch_binance_pnl.py`, `trade_status.txt`, `binance_pnl.json` (PnL real API, fuente de verdad §18). |
| Git | ✅ Inicializado | Commits chicos y frecuentes (§0.3); ver `git log`. `.gitignore` protege credenciales. |

**Regla §0.1**: testnet primero, mainnet SOLO con autorización humana explícita. **Mainnet OPERADA por primera vez el 2026-08-10** (`REAL=True`, `EXPOSURE_LEVEL=1`, autorización explícita §0.2 + confirmación interactiva `CONFIRMAR`).

**Corrida actual (2026-08-11)**: bot PID **27840** relanzado 13:52:09 local con `run_mainnet.py --cycles 100000 --yes` (corrida extendida, sin techo de 500 ciclos). **Es la primera corrida con TODO el hardening FASE B ACTIVO EN VIVO**: timeout REST explícito (§8), sweep de huérfanas (pre-flight + stop), lock single-instance (§12), monitor/heartbeat cada 15 s con stall y divergencia (§11), journal con etapas (§10), log por corrida `logs/run_mainnet_20260811_135209.log`. Código `5e723a6` + FASE B. Pre-flight OK: mark 1.0068, leverage máx real 20x, 3 candados §0.2 satisfechos, sweep 0 huérfanas.

**Reporte de trades (nuevo)**: `reports/trade_status.py` genera `reports/trade_status.txt` (trades, PnL neto, últimos fills/rechazos, estado del bot) y `reports/update_trade_status.sh` lo actualiza **cada 5 min** (loop en background con el fetch de PnL integrado). **PnL REAL según Binance API (fuente de verdad, `reports/fetch_binance_pnl.py` → `binance_pnl.json`, recomputado 11:35 con `--days 30`)**: **84 trades XRPUSDC en los últimos 30 días → Net PnL = −0.013541 USDC** (realized −0.013541, commission 0.0, funding 0.0). Por día: **10/08**: 42 trades −0.022540; **11/08**: 42 trades **+0.009000** (fees 0 por promo). **El PnL estimado por logs locales (+4.9630) está INFLADO**: el log de fills del 10/08 quedó truncado (faltan compras → notional comprado subestimado). Auditoría: 48/48 orderIds de la API coinciden con `server_order_id` de los logs → todos los trades son del bot (el `clientOrderId` viene `None` en python-binance, el filtro `MM-` no funciona). El humano tenía razón en dudar de +4.96. Rechazos = maker check §10 (sanos, sin errores Binance). **Conclusión 30 días**: la promo 0 fees hace al bot marginalmente rentable; sin ella (fees 0.0002), los round trips con spread 8 ticks pierden contra los costos — el mes se recupera apretando spread o subiendo tamaño.

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
- [x] **Decisión humana — bug minNotional (§0.2, CRÍTICO, bloqueaba operación)**: el bot NO colocó ninguna orden el 2026-08-10 (bid 2,4 XRP ≈ $2,44 < minNotional $5; posición 4,9 atascada; 5 de 6 kill switches por ese path). **RESUELTO (2026-08-10/11) con opción 1 (aprobada por humano §0.2 — humano confirmó minNotional = $5)**: `BASE_ORDER_SIZE_XRP` **4.9 → 5.0** (5.0 XRP × ~$1,02 ≈ **$5,10 > $5** con margen 2%). El humano pidió explícitamente NO modificar el algoritmo de cálculo en esta iteración (el floor de notional / reduce_only propuestos por el subagente en el commit `30b3cd2` fueron REVERTIDOS y quedan para revisión futura — el cálculo de tamaños tiende a confundirse, revisarlo con calma). **Suite 67/67 OK**.
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

### 2026-08-13 — Corrida "última para evaluar VPS" + aplanado la posición (autorizado por humano)
- **Fix freeze aplicado y COMITEADO** (`e4a5c0d`, autorizado por humano): en `risk_engine.check_order` se añadió `is_reducing = (side=="ask" and inventory>0) or (side=="bid" and inventory<0)`; las verificaciones #2 (inventory) y #9 (exposure) se **saltean** si `is_reducing`, y la #9 usa `gross_exposure = abs(projected_notional) + abs(notional)` (notional proyectado en vez de solo la posición actual). Tests actualizados: renombrado `test_rejects_when_exposure_exceeded`→`test_rejects_when_exposure_exceeded_non_reducing` (BUY desde plano, notional 25, precio 5.0 → gross 50>25 rechaza) + nuevo `test_allows_reducing_order_to_unwind` (SELL cuando long, inventory=49, current_pos_notional=24.5, notional 2.0 → permitido). **Suite 77/77 OK.**
- **Corrida mainnet 4** (`run_mainnet.py --cycles 200 --yes`, PID 2170, ~09:22→09:41, exit 0): 199/199 decisiones `ok`, 72 órdenes, 7 fills, kill_switch=0, 1111.5 s, mid 1.00325/1.00585, bid/ask_size max 5.0, GATE OK. **Confirma que el fix de desarmar funciona**: fills muestran SELL por encima de BUY (BUY 1.0043→SELL 1.0057; BUY 1.0037→SELL 1.0051). 2 round-trips completos, **+0.014 USDC, 100% ganadores**. 7 rechazos `exposure_exceeded` (BUYs cerca del tope). La cuenta quedó **long 15.0 XRP** (la corrida redujo de un arrastre previo a 15 vía ventas maker).
- **Verificación de posición real** (lectura read-only, autorizada): `get_open_position_for_symbol` → **long 15.0 XRP, entry 1.0051, mark 1.0039, notional $15.06, uPnL −0.018, SIN órdenes abiertas**. **Corrige el cálculo fill-derivado erróneo de 35.1 XRP**: la reconciliación real es 15 (bajo el tope `MAX_POSITION_NOTIONAL_USDC=25`). **El tope de posición SÍ se respeta**; el bot redujo el inventario vía ventas maker durante la corrida.
- **Aplanado de la posición** (autorizado por humano, opción "Taker IOC reduceOnly"): las órdenes **maker GTX `reduceOnly` fueron rechazadas por Binance con `-5022`** (el libro de 1 tick + latencia del cliente hacían que la orden cruzara al colocarse). IOC LIMIT por encima del ask expiró sin fill (`EXPIRED`, libro fino). Finalmente **MARKET `reduceOnly` SELL 15.0 XRP FILLED @ avg 1.00540** (orderId 8025135291) → **cuenta plana (0 XRP)**. Nota: rompe la regla §0 maker-first (taker); fue decisión explícita del humano para cerrar ya. El endpoint de posición dio `None` transitorio pero el fill es definitivo.
- **Evaluación VPS (conclusión)**: el unwind funciona y los round-trips son positivos (muestra pequeña: 2 RTs +0.014), y la posición real respetó el tope. **PERO sigue NO listo para VPS** por: (1) fill rate extremadamente bajo (7 fills / ~18 min) → rentabilidad no probada a escala; (2) muestra de RTs ganadores aún pequeña. Además pasar a VPS = producción mainnet requiere autorización humana explícita §0.1/§0.2. Pendiente: más corridas con mayor fill rate antes de declararlo listo; y sincronizar `vps_upload/strategy/` (que aún tiene `MIN_SPREAD_TICKS=8` y NO el fix Opción 1) antes de cualquier deploy.

### 2026-08-12 15:22 — Relanzamiento mainnet con fix "no da pérdida" (autorizado por humano)
- **Estado previo**: bot CAÍDO (lock obsoleto, PID 1952 muerto). Cuenta real mainnet sin posición XRPUSDC (plana); el STATUS previo "LONG 5 XRP" no correspondía a esta cuenta.
- **Fix deployado**: `LOSS_GUARD_USDC = 0.02` + nuevo motivo `REASON_KS_REALIZED_LOSS_GUARD` en `risk_engine.py` (§0.2: frena y aplana si `daily_pnl = realized_pnl − fees` < −2 céntimos). El skew de inventario (`INVENTORY_SKEW_MULTIPLIER` / `GAMMA_INVENTORY_RISK` en `alpha_model.py`) **ya estaba en 1.0** — no requirió cambio (corrige nota anterior).
- **Baseline PnL** (desde aquí se cuenta): `reports/pnl_baseline_20260812_152211.json` → USDC wallet 738.13, disponible **101.74** (compartido con bot BTCUSDC: margin 570 / UPnL −66.34), XRPUSDC plano, mark 1.00911.
- **Arranque OK**: Pre-flight 15:22:57, WS conectados, primer fill SELL @1.0093 x5 a 15:23:39. PID 95124 · log `logs/run_mainnet_20260812_152253.log`.
- **Pendiente**: vigilar fills y que el loss guard no salte prematuramente; revisar en ~15 min.
- **2026-08-11 (11:40 local)**: **PnL del último mes recomputado** (`fetch_binance_pnl.py --days 30`, fuente de verdad API §18): **84 trades → Net −0.013541 USDC** (10/08 −0.022540; 11/08 **+0.009000** y subiendo — el bot sigue llenando). El bot sigue operando normal post-fix (PID 27636, 0 rechazos minNotional, posición rotando: vendió 5 @ 1.0047 a las 11:28:38 tras comprar @ 1.0052). Lectura a largo plazo: con la promo 0 fees el bot ya es marginalmente positivo el día 11/08; el mes aún está −0.0135 por el día 1 (arranque con fees reales y round trips malos). Commit de reports + STATUS.md.
- **2026-08-11 (11:29 local)**: **fix del spam de rechazos minNotional APLICADO EN VIVO** (commit `5e723a6`, autorizado por humano). El lado agravante del skew §11 (2.5 XRP ≈ $2.52 < minNotional $5) se reintentaba cada ciclo → ~5 rechazos/s en mainnet quemando rate-limit. Fix: `_compute_quotes` no cotiza un lado cuyo notional quede bajo minNotional (`below_min_notional:{side}`), con cálculo **dinámico al precio vigente** (minNotional está en USDC → si XRP sube, el mismo tamaño cumple el piso y el lado vuelve a cotizar solo, sin intervención). 3 tests nuevos (agravante bloqueado; XRP $2.2 → vuelve a cotizar; flat precio bajo → sin spam). **Suite 70/70 OK**. Bot relanzado 11:24 (PID 26816, `--cycles 500 --yes`) tras morir la corrida previa (PID 20732): pre-flight OK, leverage 20x intacto, **0 rechazos minNotional post-fix** (antes ~5/s). El long +5.0 se cerró (SELL 5 @ 1.0053, fill real) y el bot re-compró 5 @ 1.0052 (operando normal, ambos lados cotizan). **PnL real 11:28: 84 trades → Net −0.016540 USDC** (10/08 −0.022540; 11/08 **+0.006000** en 42 trades, fees 0 por promo). El relauncher v3+lock cerró su ventana de 2h a las 11:19: la corrida actual ya NO tiene relauncher (la próxima muerte del proceso deja el bot caído hasta relanzar manualmente).
- **2026-08-11 (09:54 local)**: **PnL REAL integrado al reporte — el estimado por logs estaba mal**. Nuevo `reports/fetch_binance_pnl.py` (usa `get_my_trades` + `get_income_history` de la API, fuente de verdad §18) → `reports/binance_pnl.json` con `trades_raw` para auditoría. Resultado: **Net PnL real = −0.026540 USDC** (49 trades; 10/08 −0.0225, 11/08 −0.0040; commission/funding 0.0 según API — posible fee-0 del par, reportar tal cual). El **+4.9590 estimado era artefacto del log de fills truncado del 10/08** (faltan compras). Auditoría de coincidencia: 48/48 orderIds API ↔ `server_order_id` de logs → 0 trades ajenos; `clientOrderId` es `None` en `futures_account_trades` (la librería no lo mapea) → el filtro `MM-` es inútil, usar `orderId`/`server_order_id`. `trade_status.py` ampliado (secciones PNL BINANCE / PNL ESTIMADO / ULTIMOS TRADES BINANCE); `update_trade_status.sh` ahora corre fetch+status cada 5 min (loop relanzado, PIDs viejos 102528/61688 matados). Bot sigue operando normal (trades 09:31–09:49 local). Pendiente commit.
- **2026-08-11**: **fix bug minNotional — SOLO constante (decisión humana §0.2: "usa 5.0 XRP")**: `BASE_ORDER_SIZE_XRP` 4.9→5.0 (5.0 XRP × ~$1,02 ≈ $5,10 ≥ minNotional $5). El intento del subagente (commit `30b3cd2`: floor de notional + reduce_only + exención minNotional en engine, suite 76/76) fue **revertido a pedido del humano**: no tocar el algoritmo de cálculo ahora, revisarlo en una iteración dedicada. **Suite 67/67 OK.** Pendiente: verificación en vivo (re-lanzar mainnet — el relauncher relanza al matar el proceso).
- **2026-08-09**: creado este archivo. Módulos `strategy/` completos, `market_maker.py` orquestador creado, tests Risk Engine 13/13 OK, kill switch cubierto.
- **2026-08-09**: corregidos maker check §10 y semántica de fills `is_buyer_maker`. Nuevo `test_execution_engine.py` (14/14 OK). Auditados bugs pendientes (Nivel 0/dry-run, init_client, cancel purga local, kill switch reduce/close, NetPnL, sigma).
- **2026-08-09**: implementada la decisión Nivel 0 con simulación (`SIMULATION_QUOTE_MULTIPLIER` + `effective_exposure_multiplier()`), corregido bug de convención de side en NetPnL (bot nunca cotizaba), nuevo `test_alpha_model.py`. **Suite 37/37 OK.** Commit `1e5432f`.
- **2026-08-09**: conectado `init_client(real=False)` en `run()` (testnet, §0.1) con método `ExecutionEngine.init_client()` que degrada offline. Filtros reales del símbolo cargados en testnet (§3). Commit `db4a115`.
- **2026-08-09**: **corrida integrada dry-run exitosa** (`run_dry_run.py`, 20 ciclos, ~107 s). 3 WS testnet, 19 decisiones, 8 órdenes simuladas, sizes=20.0, 0 kill switch, gate OK. Creado `run_dry_run.py` (watchdog 125 s, resumen con quality gate, exit codes 0/1/2); corregido el resumen (filtro por `ts` float + eventos `canceled` aceptados). `STATUS.md` actualizado. Pendiente commit.
- **2026-08-09**: **corregido doble conteo de alpha (§8)** — el gate en vivo lo detectó (ask a mid+2%, 0/4 decisiones `ok`). `ALPHA_QUOTE_FACTOR` 1.0 → 0.0; recalibrada la señal a escala de spread (`ALPHA_MAX` 0.01 → 0.0005, `IMBALANCE_WEIGHT`/`TRADE_FLOW_WEIGHT` → 0.003, conservador "pendiente de confirmación" §0.4). 2 tests de regresión nuevos. **GATE OK en vivo post-fix (4/4)**, suite 50/50 OK.
- **2026-08-09**: **corregido modelo de costos §18** — funding era costo fijo por fill (0.0001) pero Binance lo cobra cada 8 h sobre notional; slippage 0.0001 por fill sobrestimaba un maker GTX que nunca cruza. Ahora: `FUNDING_RATE_PER_8H` × `EXPECTED_HOLD_SEC`/`FUNDING_INTERVAL_SEC` (≈0.00000104/trade) + `SLIPPAGE_MAKER_BPS=0.0` + fee único de config. 4 tests nuevos (`TestModeloDeCostos`): quote con edge de 3 ticks dentro del spread ahora operable donde antes NetPnL<0. Suite **54/54 OK**.
- **2026-08-09**: **verificación en vivo del fix §18** — `run_dry_run.py --cycles 5`: GATE OK 4/4 `reason=ok` (antes 13/19 rechazadas por `expected_net_pnl_non_positive`). Commit `f2e4081`.
- **2026-08-10**: **PRIMERA CORRIDA MAINNET OPERADA** (exit 0, 123.8 s, 20 ciclos). Fix previo `345c7a7`: `reconcile_position` distinguía posición vacía (dict `positionAmt=0`) de error API (`None`) — el pre-flight abortaba con cuenta limpia (exit 3) en el primer intento. Lanzamiento: `echo CONFIRMAR | python run_mainnet.py --cycles 20` → pre-flight OK (posición 0, filtros reales: min_notional 5.0, step 0.1, tick 0.0001; leverage 20x; PERCENT_PRICE ±5%; mark 1.0273), **8 órdenes reales colocadas** (bid/ask size **4.9000** = mínima exacta), 0 fills, kill_switch 0, GATE OK (sin simuladas). 8/19 decisiones reason==ok; resto `expected_net_pnl_non_positive:bid` (filtro §18 correcto: spread ~0.0001 no cubre fees en el lado bid). Pendientes menores: DeprecationWarning `utcnow()` (`run_mainnet.py:284`), UnicodeEncodeError cp1252 al loguear `→` (cosmético), error transitorio `get_symbol` antes de `init_client` (no fatal). Corrida extendida 500 ciclos lanzada en monitoreo.
- **2026-08-10 (14:10 local)**: **CORRIDA MAINNET 500 ciclos — PRIMER FILL REAL y ROUND TRIP 1**. El bot compró 4.9 XRP @ 1.0279 (server `8012000560`, fill real maker) y vendió 4.9 @ 1.026 (server `8012132404`) → **NetPnL round trip 1 = −0.0113 USDC** (adverse selection: el mercado cayó 19 ticks entre entrada y salida; fees maker 2×0.0002≈0.002; NO es bug de ejecución). Un 3er fill real (SELL 4.9 @ 1.0255, server `8012191197`) abrió **posición corta −4.9 XRP** (cotización neutral que se llenó; comportamiento normal de MM, bot recomprando vía bid reduce; PnL no realizado +0.0073; liq ~1.0725/+4.6%, pérdida máx ~$0.23 ≤ 2.3% del presupuesto diario). **Fixes aplicados tras el diagnóstico**: `632f9c3` clamp del lado reduce a `max_order_size` (desatasca: con inventory=4.9 el ask 7.35 era rechazado, ahora 4.9); `1a2b11e` registro de fills reales en `refresh_open_orders` idempotente por `client_order_id` (§15/§0.6); `611efb2` watchdog derivado `max(125 s, cycles·6+30)` + pre-flight tolera posición pre-existente dentro de límites (round trip, §0.2); **`be637e5` piso de spread `MIN_SPREAD_TICKS=8`** (aprobado por humano; breakeven fees maker = 4.1 ticks → 8 ticks = 1.95× fees) enforced en `alpha_model.quote_distances()` tras el skew, ensanche simétrico. **Suite 57/57 OK** (54 + 3 `TestPisoDeSpread`). **Doc generada**: `doc/estrategia_market_making.pdf` (9 págs, LaTeX, commit `92af3b3`). **Cierre de la corrida**: a las 11:17 el proceso terminó por watchdog (3030 s según diseño) con **inventory 0.0 — el corto se cerró solo** (recompra 4.9 @ 1.0225, RT short +0.0147). Día completo: **7 round trips reales**, Gross ≈ +0.0059 USDC, fees ≈ −0.0141 → **Net día ≈ −0.008 USDC (breakeven; el spread de 1 tick no cubría fees, problema que el piso resuelve)**. **Relanzamiento 11:28 (PID 267, autorizado por humano: “relanza automáticamente”)**: `echo CONFIRMAR | nohup python run_mainnet.py --cycles 500` con `MIN_SPREAD_TICKS=8` activo — pre-flight OK, posición flat, WebSocket conectado. Primeras decisiones verificadas: spread cotizado 13 ticks ≥ 8 ✓, skew bajista operando (ask 1.5 ticks del mid con alpha −0.0005), inventory 0.0. **Filtro de momentum §14 implementado y ACTIVO en mainnet**: commit `6ed9d8b` (67/67 tests OK) agrega `MOMENTUM_*` a config.py (window 30 s, max 8 ticks, spread ×2 → piso efectivo 16 ticks, cooldown 60 s) alimentado por ring buffer del mid en alpha_model. Aprobado por humano (13:37) → reinicio forzado de la corrida: proceso viejo cerró con GATE FAIL exit 1 (32 órdenes simuladas — investigar), relauncher relanzó; 1er intento murió por kill switch ws_disconnected (13:48:38); **PID 112016 activo desde 13:48:56 con el filtro cargado** (evidencia: campo `momentum_active` en decisiones). Relauncher 2h renovado (PID 1261, ventana hasta ~15:37). Pendientes: investigar GATE FAIL de 32 simuladas; `get_order_status` (aprobación §0.2); verificar 2-3 RTs bajo piso+momentum antes de declarar éxito.
- **2026-08-10**: **implementado el filtro de momentum (§14) — control anti-adverse-selection de 2ª línea**. Evidencia: RT1 −0.0113 USDC (el mercado cayó 19 ticks mientras el bid estaba expuesto con piso de 8 ticks). Mecanismo: si |Δmid| ≥ `MOMENTUM_MAX_TICKS` (8 ticks) dentro de `MOMENTUM_WINDOW_SECONDS` (30 s), el piso efectivo pasa de 8 a 16 ticks (`MOMENTUM_SPREAD_MULTIPLIER=2.0`), simétrico, con `MOMENTUM_COOLDOWN_SECONDS=60`; `AlphaModel.record_mid()` alimentado desde el run loop (`market_maker.py`), `momentum_active` expuesto en journal. Config: bloque `MOMENTUM_*` en `config.py` marcado "PROPUESTA pendiente de confirmacion §0.4". **Suite 67/67 OK** (57 + 10 `TestFiltroMomentum`), smoke de integración 8→16 ticks OK. Parámetros (ventana/máx ticks/cooldown/multiplicador) pendientes de confirmación humana (§0.4).
- **2026-08-10 (21:25 local)**: **GATE FAIL resuelto + fix de WebSockets + limpieza de relaunchers duplicados**. (1) **GATE FAIL** (diagnosticado 14:48 local): los tests escribían `SIMULATED (dry-run)` al journal real de decisions (`strategy/tests/test_execution_engine.py` y `test_market_maker.py`, 5 clases) y el gate `_summary()` (run_mainnet.py L301-312) matcheaba "simul" → abortaba la corrida real. Fix `a01374c`: aislamiento de journal en las 5 clases; suite 67/67 OK. (2) **Kill switches por `ws_disconnected`** (6 kills, el 5º a las 21:13): 3 websockets se colgaban (stale > 15 s) y el mercado seguía fluyendo. Fix `43a6150` (WS_STALE_SEC 15→60 en `market_maker.py`; `ping_interval=10`, `sleep(5)→sleep(1)` en reconexión) + **`fa6df21` (correctivo)**: el intento `43a6150` con `ping_interval=10, ping_timeout=10` VIOLABA el requisito de websocket-client (`ping_interval > ping_timeout`) → loop infinito de errores "Ensure ping_interval > ping_timeout" (1155 errores en 6 min, bot degradado sin decisiones). Correctivo: `ping_timeout=10→5` en los 3 websockets (+ comentario). Lección §0.6: **websocket-client exige `ping_interval > ping_timeout`**. (3) **Reinicio del bot**: PID 9624 → 69980 (código roto 21:15:12) → 102064 (21:21:43, código OK). Verificado: 3 WS conectados 21:21:48, decisión fresca `00:24:34Z` (21:24:34 local) `reason=ok`, **0 errores WS post-21:21:42**. (4) **Relaunchers duplicados**: 5+ procesos relauncher acumulados (kills con `wmic /format:list` inefectivos + duplicación MSYS) — limpiados vía PowerShell `Get-CimInstance`; quedó **1 loop** (lanzado 21:21:42 con `nohup bash` desde bash; `Start-Process` y `setsid` NO usar en Git Bash). (5) **Lockfile agregado** a `logs/relauncher_2h.sh` (previene duplicados futuros). **Pendiente crítico**: el bot NO pudo colocar NINGUNA orden en todo el día — bid 2,4 XRP ≈ $2,44 < minNotional $5; la posición LONG 4,9 (del round trip de la mañana) quedó atascada y reduce/close también falla → 5 de 6 kills por ese path. **Requiere decisión humana** (ver Pendiente).

---

## FASE B (2026-08-11 12:0x) — Aprendizajes de la corrida 1 y hardening §8/§11/§12

Cambios aplicados y validados (74/74 tests OK + smoke tests):

- **Timeout explícito en REST (§8)**: `API_TIMEOUT_SECONDS=10` en `API_binance_futuros.py`, propagado a cada request vía `requests_params` (python-binance 1.0.36 ya tenía timeout=10 implícito en `REQUESTS_PARAMS_DEFAULT`; ahora es explícito y documentado). Ante timeout → `BinanceAPIException` → `_call_with_retry` → degrade; nunca se auto-coloca orden nueva por timeout.
- **Sweep de órdenes huérfanas (§0.6)**: `ExecutionEngine.sweep_orphan_orders()` cancela makers `MM-` abiertas en el exchange que quedaron fuera del tracking local (clase de bug de la corrida 1: orden huérfana + -2011). Corre en pre-flight de `run_mainnet.py` y en `MarketMaker.stop()`. NO toca órdenes de otros bots (sin prefijo `MM-`). 4 tests nuevos.
- **Lock single-instance (§12)**: `logs/run_mainnet.lock` con O_EXCL+PID en `run_mainnet.py`; aborta con **exit code 4** si otra instancia corre (reemplaza locks con PID muerto). Previene 2 procesos compartiendo XRPUSDC.
- **Monitor ligero (§11)**: hilo con heartbeat cada 15 s (`MONITOR_INTERVAL_SEC`); stall si el journal no avanza con WS vivo (`STALL_WARN_SEC=60` warn, `STALL_STOP_SEC=180` stop conservador que cierra órdenes); divergencia decisión/ejecución (`DIVERGENCE_MIN_DECISIONS=8` ok-decisions en 60 s sin eventos de órdenes → warn). No reemplaza el techo duro del watchdog §13.
- **Journal con etapas (§10)**: `agent_decisions.jsonl` ahora incluye `cycle` siempre y `stage_ts` (cycle_start/snapshot_ready/quotes_done/orders_done) en ciclos 1-3 y cada 100 ciclos → permite medir duraciones reales y detectar colgamientos entre etapas.
- **Log por corrida (§13)**: `logs/run_mainnet_YYYYmmdd_HHMMSS.log` en vez de un archivo compartido.
- **Nota**: la corrida mainnet activa (PID 27636, código `5e723a6`) sigue con el código viejo; los cambios aplican al próximo reinicio vía relauncher.

---

## Relanzamiento mainnet 2026-08-11 13:52 + observación §20 (14:22 local)

- **2026-08-11 (13:52–14:07 local)**: **relanzamiento mainnet PID 27840** (autorizado: corrida previa PID 27636 venció por watchdog). Pre-flight OK (mark 1.0068, leverage real 20x, lock único OK al arranque, sweep 0 huérfanas, 3 candados §0.2). **Observación completa de 15 min — TODOS los criterios §20 OK**:
  - **1 solo proceso** + `logs/run_mainnet.lock` con PID (exit 4 si duplicado). Sin relauncher activo compitiendo (relauncher_2h cerró ventana 11:19, no re-lanzado).
  - **Market data**: ws=True continuo, stall_age ≤ 6 s en todo momento (umbral warn 60 s).
  - **Decision loop**: 0 → 141 decisiones en 15 min, `ok` 29, avanzando sin pausas.
  - **Execution**: 39 eventos de órdenes, **7 fills reales** (source=refresh, idempotente por client_order_id, véase `logs/fills/fills_20260811.jsonl`).
  - **Reconciliation**: sync 30 s purga órdenes llenadas y registra el fill (sin órdenes fantasma §0.6 — la cancel de una orden ya llenada devuelve `-2011` y NO se purga localmente hasta verificar, por diseño).
  - **`-2011` acotados**: 16 totales, todos WARNINGs auto-resueltos (4–5 reintentos ≤ 30 s hasta el siguiente sync; luego cesan). **0 cadena infinita** (§14), 0 `-2011` en los últimos 2 min de la ventana.
  - **Errores**: 1 único ERROR (transitorio `get_symbol` antes de `init_client` en pre-flight, auto-curado tras init — mismo patrón conocido). Kill switch: **0 disparos** en la corrida (kill_switch.jsonl solo tiene entradas de ayer).
  - **Posición reconciliada en vivo**: arrancó 0.0, 3 fills (SELL 5 @ 1.0092, BUY 5 @ 1.0083, BUY 5 @ 1.0085) → **long 5.0 XRP ≈ $5.05** (<< $25 límite, dentro de presupuesto §0.4), bot recomprando el lado SELL a 1.0087 GTX para cerrar el round trip. Capturando spread maker real con promo 0 fees.
- **2026-08-11 (14:22 local)**: **fix cosmético `UnicodeEncodeError` cp1252** — el único carácter no-cp1252 en strings de log era `→` (U+2192) en `run_mainnet.py:356` (`Pre-flight: requiredMarginPercent...`); reemplazado por `->` (los acentos/§/— son cp1252-compatibles, verificados con escaneo de los 8 módulos). El error solo rompía el handler de consola, no el archivo de log. **Aplica al próximo reinicio** (el proceso vivo 27840 ya cargó el código previo).
- **2026-08-12 19:05 local**: **fix del gate `expected_net_pnl` (§10) — el bot ahora cotiza two-sided en testnet**. Diagnosis: `expected_net_pnl_estimate` medía el edge contra el `mid` crudo, ignorando el sesgo de alpha; con `alpha≈0` el lado opuesto a la señal (el SELL sin señal alcista) daba NetPnL<0 → el gate §10 lo rechazaba siempre → 0 fills / 0 `reason=ok`. **Fix**: medir contra el valor justo `reservation_price = mid + alpha − penalty` (no contra mid); el skew de alpha y de inventario se cancelan y ambos lados quedan con spread capturado > 0 cuando el piso se cumple. `strategy/alpha_model.py:expected_net_pnl_estimate`. Test de la lógica de signo actualizado (`test_quote_dentro_del_spread...` ahora valida branch de bloqueo con quote sin edge, sin asumir alpha=0). **Suite 76/76 OK**. **Validación testnet (dry-run, REAL=False, sin mainnet)**: 24 órdenes colocadas / 16 fills (vs 0 antes), `reason==ok` 17/35, sin kill switch ni errores. Rechazos residuales `below_min_notional:bid` = el engine descarta bids cuyo notional < $5 de Binance cuando el skew los empuja bajo $1.00 (comportamiento correcto del risk engine, no bug). **Nota**: testnet solo valida mecánica de órdenes (caveat humano: no refleja estructura de mercado real → no se infiere edge/PnL de esta corrida). El fix modifica un gate de riesgo: requiere revisión humana antes de cualquier uso con capital real.
- **Lección §0.6 (observada en vivo)**: el patrón "cancel de orden ya llenada → `-2011` → reintentar" está acotado por el sync de 30 s y termina solo; NO es cadena infinita. Opcional futuro: ante `-2011` en cancel, forzar refresh inmediato para purgar antes del siguiente ciclo (reduce ruido y rate-limit).

## 2026-08-12 19:22 — MAINNET live (autorización humana §0.2 "pasa a mainet")
- Run mainnet 1h: `echo CONFIRMAR | python run_mainnet.py --cycles 600` (PID 12036, watchdog ~3630s).
- Pre-flight OK: mark 1.0055, posición 0, leverage 20x = máx real, PERCENT_PRICE ±5%.
- Fix `expected_net_pnl_estimate` (commit 628cb4f) confirmado en mainnet: dos lados cotizan con expected_pnl>0.
- Fills reales two-sided (76 hoy, simulated:False), round-trips ~1.0054-1.0076.
- Risk gate conservador: rechazos below_min_notional / expected_net_pnl_non_positive por diseño (seguro).
- Inventario manejado (corto -5 XRP siendo aplanado). Monitor cada 5 min: monitor_mainnet.log.

## 2026-08-12 20:23 — Fin graceful de la corrida mainnet 1h (PID 12036)
- **Corrida completa**: `max_cycles=600` alcanzado a las 20:19:17 local (3402.1 s ≈ 56.7 min); WS cerrados limpios (bookTicker/depth/trade), lock liberado, **Exit code=0**. Process dead (RUN_GONE) confirmado a 20:26:33.
- **RESUMEN MAINNET**: EXPOSURE_LEVEL=1, dry_run=False, real=True, ws_connected=True. decisions=599, orders=304, fills=12 (este run; 58 reales hoy), kill_switch=0. mid min/max 0.99995/1.00635. bid_size/ask_size max 5.0. reason==ok 307/599; resto below_min_notional:ask/bid (skew-manage). **GATE OK: sizes>0, reason==ok, sin kill switch, sin errores, sin simuladas.**
- **Reconciliación post-run (read-only, mainnet real)**: `futures_position_information(XRPUSDC)` = `[]` (posición plana, 0 XRP); `futures_get_open_orders(XRPUSDC)` = 0 órdenes abiertas. Journal decisión final (cyc 600, ts 2026-08-12T23:19:11): inventory 0.0. **Sin exposición abierta al cierre.**
- **Kill switch**: 0 eventos durante la corrida (kill_switch.jsonl solo tiene 8 entradas históricas pre-run, última `ws_disconnected`).
- **DNS**: 2 `NameResolutionError` transitorios en `fapi.binance.com` `reconcile_position` REST (primer 19:32:49); no afectaron cotización (WS ok; kill switch solo en `ws_disconnected`). Count estable en 2.
- **Edge**: expected_pnl consistentemente +0.003 a +0.008 por lado; epnl acumulado snapshot ~+0.003. Real fills today = 58 (0 simulados en mainnet; 24 simulados pre-mainnet 16:43–17:09).
- **Nota skew**: el bot sostuvo +5.0 XRP de inventario (real maker bid fill) y lo manejó vía `below_min_notional:bid` (no recompró hasta reducir) — skew-manage conservador, dentro de $25 notional.
- **Cambios on-disk sin commit corriendo en mainnet**: `strategy/execution_engine.py`, `strategy/market_maker.py`, `strategy/tests/test_market_maker.py`. Commit `628cb4f` (alpha_model + test + STATUS) ya está local (no push).

---

## 2026-08-12 21:20 — FIX del piso de spread (causa raíz de la pérdida)

**Diagnóstico (PnL real `reports/binance_pnl.json`, fetch 23:43Z)**: NET mainnet = **−0.020046 USDC** (178 trades, 82 round-trips). Win 47.6% (39W/43L) con avg win **+0.00405** ≈ avg loss **−0.00413** (SIMÉTRICOS → no es fee ni slippage, es adverse selection pura). Precio real **~1.01** (PnL/tick = qty 5 × tick 0.0001 = **0.0005 USDC/tick**). Spread 8 ticks = 0.004 USDC/RT = coincide con avg |RT| 0.00409 → el piso captura ~8 ticks pero adverse selection sesga **−0.00024/RT**.

**Por qué el floor de 8 no basta (promo 0 fees)**: con `MAKER_FEE_RATE=0.0` el breakeven por fees es 0; el piso ya NO cubre fees sino adverse selection. Subir a **12 ticks** (+4 ticks = +0.002 USDC/RT) vuelca la media de −0.00024 a **~+0.0018/RT** (claramente positivo). Ensanchar es SEGURO: solo reduce fills / aumenta edge, no puede aumentar pérdidas.

**Cambio**: `strategy/config.py:197` `MIN_SPREAD_TICKS: int = 8 → 12`. Piso efectivo en momentum = 12 × 2 = **24 ticks**. Comentarios desactualizados actualizados en `config.py`, `alpha_model.py`, `analyze_loss_guard.py`, `test_alpha_model.py` (docstrings 8→12 / 16→24; el cálculo usa el valor de config dinámicamente, así que no rompe). Tests dinámicos pasan.

**Suite 76/76 OK** (commit local pendiente).

**Pendiente (acción con fondos reales — requiere autorización §0.2)**: re-corrida mainnet corta (30–60 min) con `monitor_mainnet.py` para medir PnL real con 12 ticks. Criterio de éxito: NET > 0. Si tras el cambio sigue ≤ 0, subir a 16 (ver comentario en config.py).

**Nota deploy**: `vps_upload/strategy/config.py` y `vps_upload/strategy/alpha_model.py` siguen en 8 ticks (espejo de deploy). Si el bot corre desde `vps_upload/`, debe actualizarse también antes de la medición.

## 2026-08-12 22:10 — Medición con 12 ticks: NO alcanza. Causa raíz distinta.

**Run mainnet (PID 46560, --cycles 600) pausado tras ~25 min**: 6 fills reales, 2 RTs limpios (ventana run UTC 00:46+), ambos **negativos** (pnl −0.001 / −0.003, holds 258s/798s). Muestra más amplia del run: 8 RTs, net −0.0255, win 37.5%, avgLoss −0.007 ≫ avgWin +0.003.

**Hallazgo crítico — el piso NO era la palanca correcta**: el precio promedio de un round-trip histórico era ~0.00409 (≈40 ticks) mientras el spread cotizado es 0.0008–0.0012 (8–12 ticks). El bot **NO captura el spread**: compra a 1.00390 y vende a 1.00370 (precio cae tras llevar inventario). La guarda `below_min_notional` del skew hace que un lado se rechace (solo el bid se coloca cuando está short −5), así que el bot no cierra two-sided y lleva exposición direccional ~14 ticks en contra por RT. Ensanchar el piso 8→12 suma +4 ticks pero el adverse selection es ~14 ticks/RT → sigue perdiendo.

**Conclusión**: el driver de pérdida es el **turnover de inventario / ejecución**, no el ancho del piso. Próximo fix (a decidir): mantener ambos lados cotizando (no rechazar el lado min_notional; en su lugar reducir el tamaño del lado corto para cumplir notional, p.ej. 5/5 siempre, o escalar size) para capturar el spread simétricamente; y/o subir a 16 ticks como parche. Requiere validación testnet primero.

**Run detenido** (PID 46560 kill /F; lock removido; bots del VPS intactos). Pendiente decisión de diseño antes de relanzar mainnet.

## 2026-08-12 22:25 — Fix opción 1 aplicado: ambos lados cotizan siempre.

Decisión del usuario: **opción 1** (mantener ambos lados cotizando; no rechazar el lado min-notional).

**Cambio en `strategy/market_maker.py`** (bloque min_notional, ~línea 278): antes RECHAZABA el lado cuyo notional < minNotional (lo volvía one-sided). Ahora **sube el tamaño al mínimo que cumple minNotional** (`ceil(min_notional/price/step)*step`, redondeado a quantity_precision) para mantener AMBOS lados vivos y cerrar two-sided en el spread. Solo rechaza si ni siquiera `max_order_size` alcanza el notional (precio tan bajo que 5 XRP < minNotional).

**Hallazgo de config**: `max_order_size = BASE_ORDER_SIZE_XRP × multiplier = 5.0` XRP; el piso notional a ~$1.0 exige ≥5 XRP. El "4.9" del comentario viejo era un ejemplo obsoleto. Con ambos lados obligados a ≥5 XRP, el size-skew queda neutralizado y el control de inventario recae en el reservation-price skew / alpha (comportamiento MM simétrico, correcto).

**Tests**: `strategy/tests/test_market_maker.py` actualizado (`test_lado_agravante_bajo_notional_se_sube_al_piso`); suite completa **76/76 OK**. `run_mainnet.py` importa `strategy.market_maker` → el fix aplica en vivo (no hace falta tocar `vps_upload/` para la corrida local; ver nota deploy abajo si se despliega en VPS).

**Siguiente**: medición mainnet corta (run acotado) para confirmar que el RT se vuelve positivo al capturar spread two-sided. Nota: `vps_upload/strategy/config.py` y `alpha_model.py` siguen en 8 ticks y sin este fix — actualizar si el deploy corre desde `vps_upload/`.

## 2026-08-12 23:30 — Run 2 (PID 1944): fix validado, termina por kill switch WS.

- Corrida `--cycles 200 --yes`, 22:28→23:25 local (~57 min). `reason==ok: 79/79` (antes ~33/210). **Ambos lados cotizan two-sided confirmado en vivo.**
- Fill rate real: 7 fills en la ventana (escaso para XRPUSDC maker — mercado ilíquido o niveles no tocados). 2 round-trips analizados (FIFO):
  - RT1: BUY 1.0012 → SELL 1.0015 = **+0.0015** (primer RT positivo de toda la medición).
  - RT2: BUY 1.0017 → SELL 1.0015 = **−0.001** (adverse: compró en local-high, vendió en baja).
  - Net: **+0.0005** (~breakeven). Con MAKER_FEE=0 el spread debería acumular +, pero la **adverse selection** sigue comiéndolo en ~50% de los RTs.
- **Fin por kill switch `ws_disconnected`** (§13) tras caída transitoria del WebSocket a las 23:25. Comportamiento de seguridad CORRECTO (evita órdenes huérfanas). Exit code 1, lock liberado, VPS bots intactos.
- **Pendiente / siguientes palancas** (todavía no rentable de forma conclusiva):
  1. **Tuning de adverse selection**: el reservation-price skew / alpha hace que el bot compre en local-highs. Recalibrar `RESERVATION_SKEW` / `alpha` para que el bid baje cuando está long y el ask suba cuando está long (no comprar en la subida). Con 0 fees, capturar el spread requiere no ser adverse-selected.
  2. **Relanzar** corrida acotada para acumular >10 RTs y confirmar signo del net (2 RTs es muestra chica).
  3. **WS resilience** (opcional, post-rentabilidad): reconexión con backoff ya existe en los módulos WS; el kill switch por `ws_disconnected` es deliberado y seguro — no cambiar sin evaluar riesgo de órdenes huérfanas.
