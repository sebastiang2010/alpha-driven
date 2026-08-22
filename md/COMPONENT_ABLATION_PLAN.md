# Component Ablation Plan — aislar el efecto de cada componente

> Fuente: `md/Investigación profunda...` §19, §20, §26. Matriz Baseline ± componente, sobre datos reales (no live).

## 1. Principio (§19)

Cada componente debe tener un **rationale económico** y un **efecto PnL medible y aislable**. Si ablacionar un componente no cambia el PnL esperado, es ruido y se elimina.

## 2. Matriz de ablation (§20)

Usar replay de fills/orders/decisions reales (8 días) simulando cada variante. Métrica: `E[PnL]` neta, adverse selection por lado, pstdev de inventario.

| Variante | Cambio vs Baseline | Hipótesis de mejora | Métrica clave |
|---|---|---|---|
| Baseline | sistema actual | referencia (pierde) | NET < 0 |
| + inventory penalty (B) | skew exponencial por inv | reduce riesgo direccional | pstdev inv ↓, NET ↑ |
| + adverse filter (C) | suprimir lado SELL en sesgo alcista | reduce SELL adverse 68% | SELL adverse % ↓ |
| + sizing por EV | qty variable | mejora conveniencia marginal | fill rate / NET |
| − alpha direccional | quitar alpha | sin efecto esperado | NET ≈ Baseline |
| − expected_pnl gate | quitar gate optimista | sin protección hoy | NET ≈ Baseline (peor drawdown) |

## 3. Regla de decisión (§26)

- Componente se **KEEP** si `NET_variante > NET_baseline` con significancia y rationale económico.
- Componente se **DROP** si `NET_variante <= NET_baseline` o efecto no aislable.
- No se añade ningún componente sin ablation previa (anti-overfitting, §15).

## 4. Orden de ejecución

1. Baseline (replay del actual) — establecer suelo de pérdida.
2. + inventory penalty — objetivo: bajar pstdev de inventario de 4.8.
3. + adverse filter — objetivo: bajar SELL adverse de 68%.
4. Combinar B+C y medir sinergia.
5. Probar − alpha y − gate para confirmar inutilidad.

## 5. Entregable de la ablation

Tabla final: cada componente con `delta_NET`, `delta_adverse_SELL`, `delta_pstdev_inv`, y veredicto KEEP/DROP. Esta tabla es la base del `PROFITABILITY_VALIDATION_PROTOCOL.md`.
