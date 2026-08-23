# WALK_FORWARD_RESULTS.md

## Resultados de la validación walk‑forward OOS (estrategia B+C, dry‑run)

- Ventanas walk‑forward: **5**
- tick_size usado: `0.0001`
- base_size (XRP, nivel de exposición actual): `5.0`

> **NOTA**: La serie de precios usada es **sintética sembrada** (placeholder). No hay datos de micro‑estructura OOS reales disponibles y el testnet no es realista (nota del usuario). Esto valida la *estructura* del motor y el cálculo de métricas, **NO** es un veredicto de rentabilidad.

### Métricas por ventana

| Ventana | NetPnL_OOS | MaxDD_OOS | Std_Inv | Adverse | Fills | Decisions | Reduction_vs_Baseline |
|---|---|---|---|---|---|---|---|
| 1 | -0.089964 | 0.097197 | 2.5975 | 4 | 19 | 20 | +1.7418 |
| 2 | 0.198582 | 0.008300 | 2.5937 | 3 | 19 | 20 | -0.2487 |
| 3 | 0.319315 | 0.020854 | 2.1522 | 2 | 19 | 20 | -7.5526 |
| 4 | 0.154327 | 0.014781 | 2.5558 | 2 | 19 | 20 | +0.0776 |
| 5 | -0.176315 | 0.193479 | 2.3424 | 4 | 19 | 20 | +2.9051 |

### Agregados (suma / promedio)

- **NetPnL_OOS total estrategia**: `0.405945`
- **NetPnL_OOS total baseline**: `0.577492`
- **Reducción promedio vs baseline**: `-0.6154`
- **Fills totales (estrategia)**: `95`
- **Adverse‑selection total (estrategia)**: `15`

### Criterios de aceptación (PROFITABILITY_VALIDATION_PROTOCOL.md)

Estos criterios se listan como referencia; **no** se evalúan ni se emite veredicto en este script (lo decide el humano / protocolo):

1. `NetPnL_OOS > 0` en la mayoría de las ventanas (rentabilidad neta).
2. `MaxDD_OOS` dentro del presupuesto de riesgo (`MAX_DRAWDOWN_PCT`).
3. `Std_Inv` acotada (el skew de inventario controla el riesgo de inventario).
4. Reducción de `adverse_fills` respecto al baseline (el filtro Familia C funciona).
5. `Reduction_vs_Baseline > 0` (la estrategia B+C supera al baseline naive).

### Qué falta

- **Datos reales OOS**: no hay micro‑estructura real disponible; el testnet no es representativo (nota del usuario). Se requiere serie histórica real de XRPUSDC (varios días) para validación concluyente.
- **Monte‑Carlo**: 1000 simulaciones con la distribución de retornos para obtener intervalos de confianza (NO ejecutado aquí, a propósito).
- **Umbrales formales**: definir y codificar los umbrales numéricos de aceptación del protocolo.
- **Validación en mainnet**: requiere autorización humana explícita (§0.2) y presupuestos de riesgo confirmados.
- **Datos de flujo reales**: el adverse‑selection usa flujo derivado de la dirección de precios; idealmente usar `buy/sell_volume_60s` reales del exchange.

*Este documento se generó automáticamente por `scripts/run_walk_forward.py --mode strategy` (sin veredicto final).*
