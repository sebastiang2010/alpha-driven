# MAINNET_RESULTS.md

## Resumen de la corrida controlada en Mainnet

- **Fecha y hora de inicio:** 2026-08-22 20:17:28 (UTC)
- **Configuración temporal:** `REAL=True`, `EXPOSURE_LEVEL=1` (exposición mínima con órdenes reales).
- **Duración:** 5 ciclos (~36 s).
- **Leverage:** 20x (según `config.MAX_LEVERAGE_USED`).

## Métricas clave (extraídas del log `run_mainnet_20260822_201728.log`)

| Métrica | Valor |
|---|---|
| **Decisiones nuevas** | 4 |
| **Órdenes nuevas** | 4 |
| **Fills nuevos** | 0 |
| **Kill‑switch activado** | 0 |
| **Mid price (min / max)** | 1.46165 / 1.46215 |
| **Bid size máximo** | 5.0000 (objetivo > 0) |
| **Ask size máximo** | 5.0000 (objetivo > 0) |
| **Reason == ok** | 4 / 4 |
| **Estados de órdenes** | `['canceled', 'placed']` |
| **Gate** | OK – tamaños > 0, reason==ok, sin kill‑switch, sin errores, sin simulación |

## Observaciones

- Todas las decisiones fueron `ok` y los tamaños de bid/ask alcanzaron el objetivo de 5 XRP.
- No se registraron fills ni pérdidas; las órdenes fueron canceladas al final del ciclo.
- El `kill_switch` no se activó y los límites de riesgo (`MAX_DAILY_LOSS_USDC`, `LOSS_GUARD_USDC`) no fueron violados.
- El filtro anti‑adverse (`ADVERSE_FILTER_THRESHOLD`) y la penalización de inventario (`INVENTORY_GAMMA`, `MAX_SKU_SKEW`) se ejecutaron sin generar errores.
- El log muestra que la conexión WS se estableció correctamente y se cerró al final de la corrida.

## Próximos pasos (según `PROFITABILITY_VALIDATION_PROTOCOL.md`)

1. **Documentar resultados completos** en este archivo (`MAINNET_RESULTS.md`).
2. **Ejecutar validación walk‑forward / Monte‑Carlo** siguiendo los criterios de `PROFITABILITY_VALIDATION_PROTOCOL.md`:
   - Split temporal estricto y re‑entrenamiento en ventana móvil.
   - 5 ventanas walk‑forward, requerimiento `E[PnL]_OOS > 0` en al menos 3.
   - Monte‑Carlo 1000 paths, requerimiento `P(NetPnL_OOS < 0) < 0.30`.
   - Ablación de componentes según `COMPONENT_ABLATION_PLAN.md`.
3. **Ajustar hiper‑parámetros** (`INVENTORY_GAMMA`, `ADVERSE_FILTER_THRESHOLD`, etc.) basándose en los resultados OOS.
4. **Actualizar documentación** (`md/PROFITABILITY_VALIDATION_PROTOCOL.md`, `md/IMPLEMENTATION.md`) con los hallazgos.
5. **Revertir configuración a testnet** (ya realizado) y preparar una nueva corrida de dry‑run con los parámetros ajustados.

---
*Este documento se generó automáticamente después de la corrida controlada en Mainnet.*
