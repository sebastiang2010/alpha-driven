# WALK_FORWARD_RESULTS.md

## Resultados de la validación walk‑forward OOS (estrategia B+C, dry‑run)

- Ventanas walk‑forward: **5**
- tick_size usado: `0.0001`
- base_size (XRP, nivel de exposición actual): `5.0`
- fuente de datos: **REAL (Binance 1m closes)**

> **NOTA**: La serie de precios es **real** (cierres 1m de XRPUSDC desde Binance, no testnet). Valida la lógica de la estrategia B+C sobre un camino de precios real, **pero** usa solo el precio de cierre como proxy de mid: no incluye micro‑estructura completa de order book ni flujo real de volumen. **NO** es un veredicto final de rentabilidad (falta Monte‑Carlo y validación en mainnet).

### Métricas por ventana

| Ventana | NetPnL_OOS | MaxDD_OOS | Std_Inv | Adverse | Fills | Decisions | Reduction_vs_Baseline |
|---|---|---|---|---|---|---|---|
| 1 | 0.152964 | 0.026312 | 2.7013 | 4 | 19 | 20 | -2.6420 |
| 2 | 0.012676 | 0.038748 | 2.4863 | 4 | 18 | 20 | -1.9390 |
| 3 | 0.041243 | 0.042940 | 2.4341 | 3 | 19 | 20 | +0.2764 |
| 4 | -0.226080 | 0.242506 | 2.4611 | 8 | 19 | 20 | +0.8607 |
| 5 | 0.294337 | 0.024820 | 2.8039 | 2 | 17 | 20 | -0.9888 |

### Agregados (suma / promedio)

- **NetPnL_OOS total estrategia**: `0.275141`
- **NetPnL_OOS total baseline**: `0.112000`
- **Reducción promedio vs baseline**: `-0.8865`
- **Fills totales (estrategia)**: `92`
- **Adverse‑selection total (estrategia)**: `21`

### Criterios de aceptación (PROFITABILITY_VALIDATION_PROTOCOL.md)

Estos criterios se listan como referencia; **no** se evalúan ni se emite veredicto en este script (lo decide el humano / protocolo):

1. `NetPnL_OOS > 0` en la mayoría de las ventanas (rentabilidad neta).
2. `MaxDD_OOS` dentro del presupuesto de riesgo (`MAX_DRAWDOWN_PCT`).
3. `Std_Inv` acotada (el skew de inventario controla el riesgo de inventario).
4. Reducción de `adverse_fills` respecto al baseline (el filtro Familia C funciona).
5. `Reduction_vs_Baseline > 0` (la estrategia B+C supera al baseline naive).

### Qué falta

- **Micro‑estructura completa**: se usa la serie de **cierres 1m reales** de XRPUSDC (Binance, no testnet) como proxy de mid. Falta el libro de órdenes real (profundidad, spread dinámico) y el flujo de volumen (`buy/sell_volume_60s`) para una validación de micro‑estructura concluyente. El testnet no es representativo (nota del usuario).
- **Monte‑Carlo**: 1000 simulaciones con la distribución de retornos para obtener intervalos de confianza (NO ejecutado aquí, a propósito).
- **Umbrales formales**: definir y codificar los umbrales numéricos de aceptación del protocolo.
- **Validación en mainnet**: requiere autorización humana explícita (§0.2) y presupuestos de riesgo confirmados.
- **Datos de flujo reales**: el adverse‑selection usa flujo derivado de la dirección de precios; idealmente usar `buy/sell_volume_60s` reales del exchange.

*Este documento se generó automáticamente por `scripts/run_walk_forward.py --mode strategy` (sin veredicto final).*
