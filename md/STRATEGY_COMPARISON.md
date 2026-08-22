# Strategy Comparison — familias de estrategia para XRPUSDC

> Fuente: `md/Investigación profunda...` §6, §28. Compara familias por edge potencial, riesgo, y fit con evidencia de `CURRENT_SYSTEM_AUTOPSY.md`.

## 1. Familia A — Market Making pasivo simétrico (actual)

- Edge: captura de spread bid-ask.
- Riesgo: adverse selection + inventario direccional (autopsia: SELL 68% adverse, inv pstdev 4.8).
- Veredicto: **REJECT** — pierde sistemáticamente tras adverse selection.

## 2. Familia B — MM con control de inventario (Avellaneda-Stoikov)

- Edge: igual que A, pero penaliza acumulación de inventario → reduce riesgo direccional.
- Mecanismo: quote skewing exponencial `delta = inventory * gamma`; cancela/ajusta según nivel de inventario.
- Riesgo: aún vulnerable a adverse selection si el skewing no anticipa flujo informado.
- Fit con autopsia: **DIRECTO** — ataca hallazgo #1 (riesgo de inventario no controlado). Candidata principal.

## 3. Familia C — MM asimétrico anti-adverse-selection

- Edge: ajusta tamaño/spread por lado según señal de adverse selection (la autopsia muestra SELL 68% adverse → reducir quotes vendedores en régimen alcista).
- Mecanismo: filtro de flujo informado (VPIN / order-flow imbalance) que desactiva el lado perdedor.
- Riesgo: reducción de fill rate; puede infrautilizar la promo 0-fee.
- Fit con autopsia: **DIRECTO** — ataca hallazgo #2 (asimetría SELL).

## 4. Familia D — Predictiva direccional (alpha de retorno)

- Edge: predicción de retorno t+1.
- Evidencia: `alpha=-0.01` default casi nulo; sin señal medible en decisions.
- Veredicto: **REJECT por falta de evidencia** — requiere research previo no disponible.

## 5. Familia E — Statistical Arbitrage / pairs

- Edge: desviación temporal vs par relacionado (ej. XRP/USDT vs XRP/USDC).
- Riesgo: requiere correlación estable y ejecución en 2 venues.
- Veredicto: **RESEARCH_MORE** — fuera de alcance del bot actual, pero teóricamente viable en XRP (múltiples pares).

## 6. Matriz comparativa

| Familia | Edge potencial | Riesgo | Fit autopsia | Complejidad | Veredicto |
|---|---|---|---|---|---|
| A pasivo simétrico | bajo | alto | actual (pierde) | baja | REJECT |
| B MM + inv control | medio | medio | alto (#1) | media | **REDESIGN (base)** |
| C anti-adverse asim | medio-alto | medio | alto (#2) | media | **REDESIGN (capa)** |
| D direccional | desconocido | alto | nulo | alta | REJECT (falta evid) |
| E stat-arb | medio | medio | n/a | alta | RESEARCH_MORE |

## 7. Recomendación

Arquitectura objetivo = **B + C**: MM con control de inventario (B) como núcleo, con capa anti-adverse-selection asimétrica (C) derivada del hallazgo SELL 68%. Eliminar dependencia de alpha direccional (D) hasta tener evidencia. Ver `TRADING_SYSTEM_ARCHITECTURE.md`.
