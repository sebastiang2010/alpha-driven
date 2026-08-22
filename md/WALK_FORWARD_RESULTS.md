# WALK_FORWARD_RESULTS.md

## Resultados de la validación walk‑forward (demo)

Se ejecutó el script `scripts/run_walk_forward.py` sobre la serie de precios extraída de los logs de mercado (`data/price_series.csv`).

Parámetros usados:
- `train_size = 4`
- `forward_size = 2`

### Salida JSON
```json
[
  {
    "train_start": 0,
    "train_end": 3,
    "val_start": 4,
    "val_end": 5,
    "mean_return": 6.841349116721905e-05
  },
  {
    "train_start": 2,
    "train_end": 5,
    "val_start": 6,
    "val_end": 7,
    "mean_return": 0.0
  }
]
```

### Interpretación (demo)
- En la primera ventana de validación, el retorno medio logarítmico es positivo pero muy pequeño (`~6.8e-05`).
- En la segunda ventana, no hay variación de precios (los dos últimos valores son idénticos), por lo que el retorno medio es `0.0`.

> **Nota:** Este script es una demostración simplificada. Para cumplir con el `PROFITABILITY_VALIDATION_PROTOCOL.md` se requerirá:
> 1. Uso de datos históricos completos (varios días) en lugar de una muestra de 8 ticks.
> 2. Cálculo de métricas de rentabilidad (`NetPnL_OOS`), drawdown, reducción de adverse‑selection y desviación estándar del inventario.
> 3. Implementación de Monte‑Carlo (1000 simulaciones) y umbrales de aceptación.
> 4. Integración con el modelo de market‑making (alpha model, inventory penalty, filtro anti‑adverse) para generar decisiones reales en modo dry‑run.

Los pasos siguientes serán:
1. Extender `run_walk_forward.py` para cargar los logs JSONL completos y calcular las métricas requeridas.
2. Implementar la simulación Monte‑Carlo basada en la distribución de retornos.
3. Generar un informe final (`md/WALK_FORWARD_SUMMARY.md`) con el veredicto según los criterios del protocolo.

---
*Este documento se generó automáticamente después de la ejecución de la demo de walk‑forward.*
