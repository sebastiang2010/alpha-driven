# Trading System Architecture — propuesta objetivo (B + C)

> Fuente: `md/Investigación profunda...` §8, §9, §10, §21, §25. Construye sobre `STRATEGY_COMPARISON.md` (MM con control de inventario + capa anti-adverse-selection asimétrica).

## 1. Pipeline (§8)

```
market_data (WS bookticker/depth/trades)
   -> feature_layer (spread, imbalance, vol_realized, VPIN)
   -> alpha_or_intent (NO direccional por ahora; solo control de inventario)
   -> risk_layer (inventory penalty, kill switch, caps)
   -> execution_layer (quotes GTX maker, skew exponencial, cancel stale)
   -> fill_loop -> inventory_state -> feedback a risk_layer
```

Separación estricta **Alpha / Risk / Execution** (§9): ningún módulo decide tamaño y precio a la vez.

## 2. Risk Layer (§9, §12) — núcleo nuevo

- **Inventory penalty (Avellaneda-Stoikov)**: `skew = clamp(inventory * gamma, -max_skew, +max_skew)`. Convierte inventario en desplazamiento de quotes, no en apuesta direccional.
- **Cap duro**: `MAX_POSITION_NOTIONAL_USDC` respetado por ciclo (autopsia: fue excedido a ±20 XRP).
- **Kill switch**: mantener (ya existe, validado 0 triggers).
- **Inventory risk budget**: `0.5 * inventory^2 * sigma^2 * dt` no debe exceder % del capital por ciclo.

## 3. Execution Layer (§10, §11) — sizing por EV

- Quotes `MM-*` GTX maker (validado, 0-fee).
- **Sizing no fijo**: `qty = f(EV_side, inventory_penalty, adverse_signal)`. Reemplaza `BASE_ORDER_SIZE_XRP=5` constante.
- Asimetría anti-adverse (Familia C): si señal indica sesgo alcista (SELL adverse 68%), reducir `qty_sell` y ensanchar spread vendedor.

## 4. Adverse-Selection Filter (capa C)

- Calcular `VPIN` o `order_flow_imbalance` rolling.
- Si adverse selection histórica del lado > umbral (ej. SELL > 60%), suprimir temporalmente ese lado o requerir spread mayor.
- Derivado directo del hallazgo central de autopsia.

## 5. Evaluación (§13, §14, §15)

- **Monte Carlo**: simular PnL con paths de precio sintéticos + inventario para estimar drawdown y distribución de `E[PnL]`.
- **Walk-forward**: entrenar parámetros en ventana, validar en siguiente (§14). Prohibido optimizar en todo el histórico.
- **Anti-overfitting (§15)**: parámetros con justificación económica (gamma, caps), no curve-fit.

## 6. Componentes mínimos viables (MPS, §21)

1. Risk layer con inventory penalty.
2. Execution layer con sizing por EV + GTX maker.
3. Adverse-selection filter asimétrico.
4. Walk-forward validation harness.
5. Kill switch + caps (existentes).

## 7. Qué SE ELIMINA del sistema actual

- `alpha` direccional por defecto (sin evidencia).
- `expected_net_pnl` gate optimista (reemplazar por risk-layer explícito).
- Tamaño fijo `BASE_ORDER_SIZE_XRP`.

→ Ver `COMPONENT_ABLATION_PLAN.md` (cómo aislar el efecto de cada componente) y `PROFITABILITY_VALIDATION_PROTOCOL.md`.
