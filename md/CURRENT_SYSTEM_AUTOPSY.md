# Autopsia del Sistema Actual — alpha-driven (XRPUSDC MM)

> Fuente: `md/Investigación profunda — Diseño de un sistema de trading completo y potencialmente rentable.md` §18, §19, §20.
> Objetivo: cuantificar dónde pierde (o no gana) el sistema actual usando **solo evidencia local**, sin asumir que el enfoque MM es correcto.

## 0. Alcance y calidad de datos

| Fuente | Contenido | Uso |
|---|---|---|
| `logs/fills/fills_20260809..21.jsonl` | 409 fills reales (side, order_price, fill_price, fill_qty, ts, status) | trade / execution / adverse selection |
| `logs/orders/orders_20260809..21.jsonl` | eventos placed/filled/canceled por orden | ejecución |
| `logs/decisions/agent_decisions.jsonl` | 11.736 decisiones (regime, inventory, mid, spread, imbalance, alpha, reason, expected_pnl, risk_score) | estrategia / inventario |
| `reports/binance_pnl.json` | **NO FIABLE** — todos los `per_day` en 0.0 (fetch vacío) | excluido; se usa evidencia de fills |

**Caveat de markout:** se usa `mid_price` de `agent_decisions.jsonl` (no trade print real) como proxy de `P_future`. Subestima ruido intradía pero es válido para dirección de adverse selection.

## 1. Nivel Trade (§18)

- **409 fills**: 205 BUY / 204 SELL (balanceado).
- Tamaño por fill: `BASE_ORDER_SIZE_XRP = 5.0` (constante, sin sizing por EV).
- No hay `expected_pnl` realizado en fills; el PnL real solo se conoce vía posición cerrada → atribución a nivel fill es indirecta.

## 2. Nivel Inventario (§18) — el mayor driver de riesgo

De 11.736 registros de inventario:

| Métrica | Valor |
|---|---|
| min / max | **−16.80 / +20.00 XRP** |
| media | +0.796 |
| mediana | 0.00 |
| desv. típica | 4.822 |
| % tiempo no nulo | 52.9% |
| registros con abs(inv) >= 5 | 4.125 |

- El inventario oscila entre **−16.8 y +20 XRP** con pstdev 4.8. A precio ~1.0–1.4 USDC esto implica exposición nominal de hasta ~$20–27, por encima del cap actual (`MAX_POSITION_NOTIONAL_USDC = 15`). Históricamente el cap no se respetó consistentemente.
- **Inventario 53% del tiempo distinto de 0**: el bot está casi siempre direccionalmente expuesto, no neutral. Con pstdev 4.8 y ticks de $0.0002, un movimiento adverso de 1–2 ticks ya borra el spread ganado.

## 3. Nivel Ejecución (§18)

- 100% de fills vía `MM-*` (GTX maker) en los registros muestreados → el lado maker funciona (commit `0a7e0dd`/`de0707f` validado).
- Sin campo maker/taker explícito en fills; la promo 0-fee hace que comisión ≈ 0, por lo que **los fees NO explican la pérdida** (consisten con NET negativo observado previo por direccionalidad/inventario).

## 4. Nivel Estrategia + Adverse Selection (§4.2) — hallazgo central

Markout `M(h) = mid(t+h) − fill_price`, separado por lado. "Adverse" = precio se mueve **contra** la posición tomada (BUY: M<0; SELL: M>0).

| Lado | M(+10s) median | M(+60s) median | **% adverse +10s** | **% adverse +60s** |
|---|---|---|---|---|
| BUY | +0.0017 | +0.0019 | 34.1% | 34.1% |
| SELL | +0.0024 | +0.0025 | **68.1%** | **68.6%** |

**Asimetría de adverse selection:** el lado **SELL pierde contra selección adversa el 68% de las veces** (vendemos y el precio sube), mientras el BUY solo el 34%. Esto es señal de que el bot es "picked off" preferentemente en sus fills vendedores, o de que opera en un régimen con sesgo alcista donde vender al bid pierde sistemáticamente. En un MM simétrico esperaríamos ~50% en ambos lados.

## 5. Atribución de rechazos (gate de estrategia)

- **2.993 decisiones rechazadas** por `expected_net_pnl_non_positive`. Es el filtro central del orquestador.
- Implicación: el sistema **solo opera cuando su modelo interno predice PnL positivo**, y aun así acumula pérdida direccional → **el estimador `expected_pnl` es optimista / no captura adverse selection ni riesgo de inventario**. El gate no está protegiendo capital; está filtrando ruido mientras deja pasar el riesgo direccional.

## 6. Inventario de componentes actuales (§19)

| Componente | Propósito declarado | Hipótesis | Rationale económico | Evidencia | Efecto PnL observado | Decisión |
|---|---|---|---|---|---|---|
| `BASE_ORDER_SIZE_XRP=5` | tamaño fijo de quote | spread capture lineal | captura spread por volumen | fills balanceados | no compensa adverse selection | **REVISAR** |
| `MAX_POSITION_NOTIONAL_USDC=15` | cap de inventario | limitar riesgo direccional | bound de pérdida | inventario alcanzó ±16.8/20 | excedido en runs previos | **REVISAR** |
| skew por `imbalance` | sesgar lado según order book | adverse selection mitigation | seguir flujo | imbalance en decisions; adverse SELL 68% | no corrige asimetría | **ABLATE** |
| `alpha` | señal direccional | predicción de retorno | edge direccional | `alpha=-0.01` default casi nulo | sin edge medible | **ABLATE/RESEARCH** |
| `expected_net_pnl` gate | filtrar trades no rentables | PnL positivo predicto | evitar trades perdedores | 2.993 rechazos; aun pierde | gate ineficaz vs adverse selection | **REDESIGN** |
| `regime` (low_vol) | seleccionar parámetros | distintos regímenes | adaptación | casi siempre low_vol | sin adaptación real | **RESEARCH** |
| flatten maker (§10) | cerrar al detener | salida sin costo | 0-fee | validado maker | OK (no es la causa) | **KEEP** |

## 7. Diagnóstico — ¿dónde está la fuga?

1. **Riesgo de inventario no controlado** (§12): pstdev 4.8 XRP, 53% no nulo, picos ±20. El bot no es neutral; es una apuesta direccional no gestionada.
2. **Adverse selection asimétrica en SELL (68%)**: el modelo de pricing/colocación vendedor es sistemáticamente "picked off".
3. **Estimador `expected_pnl` optimista**: filtra el riesgo direccional, no lo modela.
4. **Falta de sizing por EV** (§11): tamaño fijo ignora conveniencia marginal.
5. **Ausencia de validación OOS** (§14): componentes añadidos sin expectativa matemática demostrada.

**Conclusión de autopsia:** la pérdida NO es por fees (promo 0-fee, comisión ≈ 0) ni por bugs de ejecución maker. Es por **exposición direccional de inventario + adverse selection asimétrica en SELL + estimador de PnL que no captura ninguno de los dos**. El sistema actual no tiene un "edge" validado; es MM pasivo con riesgo direccional no cubierto.

→ Dirige a `TRADING_SYSTEM_RESEARCH.md` (¿existe alpha?) y `COMPONENT_ABLATION_PLAN.md`.
