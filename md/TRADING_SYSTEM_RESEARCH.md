# Trading System Research — ¿Existe un edge explotable para XRPUSDC?

> Fuente: `md/Investigación profunda...` §3–§5, §10–§17, §27. Construye sobre `CURRENT_SYSTEM_AUTOPSY.md`.
> Idioma: español. Hipótesis falsables, cada una con fuente de evidencia y test OOS.

## 1. Pregunta central

¿Tiene XRPUSDC/USDC (Futures, promo 0-fee maker) una ventaja económica explotable **después de** fees, slippage, adverse selection y riesgo de inventario? El sistema actual NO la tiene (autopsia): pierde por direccionalidad + adverse selection asimétrica en SELL, con un estimador de PnL optimista.

Descomponer el PnL esperado de un MM:

```
E[PnL] = E[spread_capture] - E[adverse_selection] - E[inventory_risk] - E[fees] - E[slippage]
```

Con promo 0-fee: `E[fees] ≈ 0`. Por tanto el edge debe venir de `spread_capture` superando `adverse_selection + inventory_risk`. La autopsia muestra que hoy `adverse_selection (SELL 68%) + inventory_risk (pstdev 4.8)` dominan.

## 2. Hipótesis de alpha candidatas (falsables)

| ID | Hipótesis | Mecanismo económico | Estado evidencia local | Test OOS propuesto |
|---|---|---|---|---|
| H1 | Mean-reversion de corto plazo del mid en XRPUSDC | ruido de order flow revierte en segundos | no medido (solo markout post-fill) | regresión de retorno t+1s..t+60s sobre signo de flujo |
| H2 | Inventory-driven adverse selection | fills grandes correlacionados con movimiento adverso | **CONFIRMADO parcial**: SELL 68% adverse | markout condicional a tamaño de fill |
| H3 | Regime-dependence del spread realizado | vol/liquidez dictan conveniencia de MM | regime casi siempre low_vol (sin variación) | cluster por volatilidad realizada |
| H4 | Skew por imbalance reduce adverse selection | seguir flujo mejora fill price | imbalance presente; SELL adverse sigue 68% | A/B skew on vs off (ver ablation) |
| H5 | Asimetría de adverse selection es aprovechable | vender menos / comprar más según sesgo | hallazgo central autopsia | estrategia direccional asimétrica |

## 3. Cost model (§16)

- `fee_maker ≈ 0` (promo). `fee_taker` solo si se cruza → debe evitarse siempre (post-only GTX).
- `slippage` = diferencia fill vs mid deseado; en maker ≈ 0 por construcción.
- `adverse_selection` = movimiento del precio tras el fill en contra de la posición (medido: SELL 68%).
- `inventory_risk` = varianza del PnL de inventario = 0.5 * inventory^2 * sigma^2 * dt (modelo Avellaneda-Stoikov). Con inv pstdev 4.8 y sigma diario de XRP (~3-5%), el drift direccional domina.

## 4. EV por componente (estimación de autopsia)

| Componente | Magnitud (aprox) | Signo |
|---|---|---|
| spread_capture por fill (5 XRP * ~0.002 spread) | ~0.01 USDC/fill | + |
| adverse_selection SELL (68% * ~0.0024) | negativo sistemático | − |
| inventory_risk (pstdev 4.8, 53% tiempo) | domina en tendencia | − |
| fees | 0 (promo) | 0 |

**Conclusión intermedia:** con tamaño fijo y sin control de inventario, `E[PnL] < 0`. El edge de spread existe pero es anulado por adverse selection + riesgo de inventario.

## 5. Regímenes (§7)

La data muestra `regime` casi siempre `low_vol` → el sistema no está discriminando regímenes reales. Para que un edge sea robusto debe funcionar fuera de un solo régimen. **Requerir** validación por cuartiles de volatilidad realizada y de spread.

## 6. Literatura dirigida (2 familias finalistas)

1. **Avellaneda-Stoikov (2008)** — MM óptimo con control de inventario vía penalización exponencial. Directamente ataca el riesgo de inventario (hallazgo #1 de autopsia).
2. **Adverse selection / "picked-off" en crypto MM** — estudios de microstructure (e.g., Easley-O'Hara VPIN, Foucault-Moinas) sugieren que el lado pasivo sufre selección adversa en flujo informado. La asimetría SELL 68% es consistente con flujo informado en alzas.

> Pendiente: validación externa vía websearch/context7 de estas dos familias antes de comprometer arquitectura.

## 7. Veredicto preliminar (§29, §33)

- El sistema actual: **REJECT** como está (sin edge validado, pierde por direccionalidad).
- La familia **MM con control de inventario (Avellaneda-Stoikov) + filtro de adverse selection asimétrico** es la candidata a **RESEARCH_MORE / REDESIGN**: tiene rationale y aborda directamente las fugas de la autopsia.
- No hay evidencia de un edge direccional fuerte (alpha ~0). El camino es MM inteligente, no predicción de retorno.

→ Dirige a `STRATEGY_COMPARISON.md` y `TRADING_SYSTEM_ARCHITECTURE.md`.
