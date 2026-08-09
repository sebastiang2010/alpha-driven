# STATUS.md — Bot de Market Making XRPUSDC (Binance Futures)

> Documento vivo. Se actualiza cada 15–20 min durante el desarrollo (§0.3).
> Última actualización: 2026-08-09.

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
| Kill switch (§13) | ✅ Cubierto | 4 tests: daily loss, price anomaly, ws disconnect, no disparo normal. |
| `logs/` | ✅ Creado | `decisions/`, `fills/`, `market_data/`, `orders/`, `pnl/`. |
| `reports/` | ⚠️ Vacío | Sin reportes generados todavía. |
| Git | ✅ Inicializado | 1 commit (`4bf0ae7`, módulos base + tests). `.gitignore` protege credenciales. |

**Regla §0.1**: testnet primero, mainnet SOLO con autorización humana explícita. El bot está en **dry-run / testnet** (`EXPOSURE_LEVEL=0`, `REAL=False`).

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

## Hallazgos de auditoría (2026-08-09)

Corregidos en esta sesión:
- **Maker check §10** (`execution_engine._maker_check_ok`): exigía `price <= best_bid` (BUY) / `price >= best_ask` (SELL), rechazando cotizaciones válidas dentro del spread. Ahora: BUY es maker si `price < best_ask`, SELL si `price > best_bid`.
- **Fills** (`execution_engine.process_fills_from_trades`): condiciones de `is_buyer_maker` invertidas; un BUY resting nunca se marcaba filled. Ahora usa la semántica de Binance (documentada en `market_state.update_trade`).

Pendientes de decisión (NO corregidos aún — ver Pendiente):
- **Nivel 0 y dry-run**: con `EXPOSURE_MULTIPLIERS[0] = 0.0`, `base_size = 0` → `quote_bid_ok/ask_ok = False` → el dry-run no coloca (ni simula) ninguna orden. Bloquea la primera corrida integrada (§0.4 — decidir multiplicador de simulación o subir nivel con autorización).
- **`init_client` nunca se llama**: en modo real (aun testnet) `api.client` es `None` y todo REST falla. `run()` debería llamar `api.init_client(real=config.REAL)` (testnet permitido por §0.1).
- **`cancel_order_by_id` purga local aunque el cancel falle en la API** → orden fantasma del lado del exchange (§0.6).
- **Kill switch**: `reduce_or_close`/`disable_new_entries` declarados pero `market_maker` solo ejecuta `cancel_all` (§13).
- **NetPnL** omite funding y slippage en `expected_net_pnl_estimate` (§18); `MAKER_FEE_RATE` duplicada en 2 módulos (§0.4).
- **Escala de sigma inconsistente** (§0.6): sigma por evento (ms) vs referencia de 5 s en alpha_model.
- **`_manage_orders`**: `has_bid/has_ask` calculados antes del loop de expire → reposición con 1 ciclo de latencia.
- **Doble skew de tamaño**: `SKEW_SIZE` + `order_side_aggression` compuestos en `_compute_quotes`.
- `.gitignore`: faltan `ruvector.db`, `graphify-out/`, `.opencode/`.

## Pendiente

- [ ] Corrida integrada dry-run — bloqueada por el multiplicador de Nivel 0 (ver Hallazgos).
- [ ] Decidir multiplicador de simulación para dry-run o autorizar subida de nivel (§0.2).
- [ ] Confirmación humana del presupuesto de riesgo (§0.4).
- [ ] Autorización para subir de nivel de exposición (§0.2).
- [ ] Wire `init_client(real=config.REAL)` en `run()` (testnet).
- [ ] Endurecer `cancel_order_by_id` (no purgar si la API falla).
- [ ] Implementar reduce/close en kill switch (§13).
- [ ] NetPnL con funding + slippage; unificar fee en config (§18).
- [ ] Reescalar sigma a intervalo fijo (§0.6).
- [ ] Generación de reportes en `reports/` (§22).
- [ ] Commit de los fixes de esta sesión (§0.3, §24).

## Log de actualizaciones

- **2026-08-09**: creado este archivo. Módulos `strategy/` completos, `market_maker.py` orquestador creado, tests Risk Engine 13/13 OK, kill switch cubierto.
- **2026-08-09**: corregidos maker check §10 y semántica de fills `is_buyer_maker`. Nuevo `test_execution_engine.py` (14/14 OK). Auditados bugs pendientes (Nivel 0/dry-run, init_client, cancel purga local, kill switch reduce/close, NetPnL, sigma).
