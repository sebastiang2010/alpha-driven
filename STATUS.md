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
| `strategy/execution_engine.py` | ✅ Completo | Órdenes maker GTX, maker check, reconcile, dry-run no-op. |
| `strategy/market_maker.py` | ✅ Creado | Orquestador que integra todos los módulos. |
| Tests Risk Engine (§12) | ✅ **13/13 OK** | `python -m unittest strategy.tests.test_risk_engine -v`. Incluye kill switch (§13). |
| Kill switch (§13) | ✅ Cubierto | 4 tests: daily loss, price anomaly, ws disconnect, no disparo normal. |
| `logs/` | ✅ Creado | `decisions/`, `fills/`, `market_data/`, `orders/`, `pnl/`. |
| `reports/` | ⚠️ Vacío | Sin reportes generados todavía. |
| Git | ✅ Inicializado | Sin commits aún; primer commit pendiente. `.gitignore` protege credenciales. |

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
- `MarketMaker(dry_run=True)` instancia offline sin errores (API no inicializa `client` en el import).

## Pendiente

- [ ] Primera corrida integrada dry-run contra stream de testnet (WS públicos, sin credenciales).
- [ ] Confirmación humana del presupuesto de riesgo (§0.4).
- [ ] Autorización para subir de nivel de exposición (§0.2).
- [ ] Generación de reportes en `reports/` (§22).
- [ ] Primer commit de git (§0.3, §24).

## Log de actualizaciones

- **2026-08-09**: creado este archivo. Módulos `strategy/` completos, `market_maker.py` orquestador creado, tests Risk Engine 13/13 OK, kill switch cubierto.
