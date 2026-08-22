# Profitability Validation Protocol — criterios y veredicto

> Fuente: `md/Investigación profunda...` §14, §22, §25, §33. Define qué cuenta como "rentable" y cómo llegar al veredicto final.

## 1. Definición de rentabilidad (§22)

Un sistema es rentable si, en validación **out-of-sample (OOS)**:

```
NetPnL_OOS > 0  Y  NetPnL_OOS > coste_de_oportunidad  Y  max_drawdown < límite_de_riesgo
```

donde `NetPnL = GrossPnL − fees − funding − slippage − adverse_selection − inventory_risk`.

## 2. Protocolo de validación (§14, §25)

1. **Split temporal estricto**: entrenar en días 10–13/08, validar OOS en 20–21/08 (no tocar datos OOS durante tuning).
2. **Walk-forward** (§14): reentrenar en ventana móvil, validar en siguiente; repetir.
3. **Monte Carlo** (§13): 1000 paths para distribución de drawdown y `P(NetPnL<0)`.
4. **Ablation** (§20): cada componente aislado (ver `COMPONENT_ABLATION_PLAN.md`).
5. **Umbrales de aceptación** (justificados, no curve-fit):
   - `E[PnL]_OOS > 0` en al menos 3 de 5 ventanas walk-forward.
   - `P(NetPnL_OOS < 0) < 0.30` (Monte Carlo).
   - `max_drawdown_OOS < 2% del capital`.
   - Adverse selection SELL reducida de 68% a < 55%.
   - pstdev de inventario reducida de 4.8 a < 2.0.

## 3. Veredicto (§33)

| Veredicto | Condición | Acción |
|---|---|---|
| **PROCEED** | cumple todos los umbrales OOS | deploy en testnet ampliado |
| **REDESIGN** | edge presente pero componentes inestables | iterar arquitectura (B+C) |
| **RESEARCH_MORE** | señales prometedoras no concluyentes | más data / otra familia (E stat-arb) |
| **REJECT** | no hay edge tras validación rigurosa | detener, no operar capital real |

## 4. Estado actual vs protocolo

- Sistema actual: **REJECT** (autopsia: NET<0, SELL adverse 68%, inv pstdev 4.8).
- Arquitectura propuesta B+C: **RESEARCH_MORE / REDESIGN** pendiente de ablation + walk-forward.
- **Ningún** componente debe pasar a mainnet real hasta cumplir §3.

## 5. Anti-fallas comunes (§15)

- No optimizar en todo el histórico.
- No declarar "ganador" por una operación positiva aislada (§19).
- No usar `expected_pnl` interno como evidencia de rentabilidad (autopsia: optimista).
- Validar con datos OOS reales, no replay sesgado.
