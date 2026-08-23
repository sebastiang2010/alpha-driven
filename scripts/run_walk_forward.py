#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_walk_forward.py — Walk‑forward validation harness.

Este script soporta dos modos:

1. ``return`` (por defecto, demo original): recorre una serie de precios y
   calcula la media de retornos log en cada ventana walk‑forward. No depende
   de la infraestructura de Binance.

2. ``strategy``: validación OOS de la estrategia B+C usando la **lógica real**
   del market‑maker en modo dry‑run (``MarketMaker._compute_quotes``). Para
   cada ventana calibra la volatilidad en el train y corre la estrategia (y un
   baseline naive) en el OOS, calculando NetPnL_OOS, max_drawdown_OOS,
   desviación estándar de inventario, adverse‑selection y reducción vs baseline.

NO emite veredicto final ni hace Monte‑Carlo (lo decide el humano / protocolo).
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import statistics
import sys
from typing import List, Dict, Any

__all__ = [
    "log_returns",
    "mean",
    "compute_walk_forward_metrics",
    "generate_geometric_series",
    "generate_synthetic_series",
    "run_strategy_walk_forward",
    "write_walk_forward_report",
]


def log_returns(prices: List[float]) -> List[float]:
    """Calcula los retornos logarítmicos entre precios consecutivos.

    Args:
        prices: Lista de precios (debe contener al menos dos valores).

    Returns:
        Lista de retornos log(p_t+1 / p_t).
    """
    if len(prices) < 2:
        return []
    return [math.log(p2 / p1) for p1, p2 in zip(prices, prices[1:])]


def mean(values: List[float]) -> float:
    """Media aritmética de una lista no vacía.

    Se asume que ``values`` contiene al menos un elemento; de lo contrario
    levanta ``ValueError``.
    """
    if not values:
        raise ValueError("cannot compute mean of empty list")
    return sum(values) / len(values)


def compute_walk_forward_metrics(
    prices: List[float],
    train_size: int,
    forward_size: int,
) -> List[Dict[str, Any]]:
    """Ejecuta una serie de validaciones walk‑forward.

    La función recorre la serie de precios usando ventanas de entrenamiento de
    ``train_size`` pasos seguidos de una ventana de validación de ``forward_size``
    pasos.  Cada iteración avanza ``forward_size`` pasos (camino clásico de
    "walk‑forward").

    Para cada ventana se calcula la media de los retornos logarítmicos en la
    porción de validación y se devuelve un diccionario con la información de la
    ventana y la métrica.

    Args:
        prices: Serie completa de precios (orden cronológico).
        train_size: Número de observaciones usadas para "entrenar".
        forward_size: Número de observaciones usadas para validar.

    Returns:
        Lista de diccionarios con claves ``train_start``, ``train_end``,
        ``val_start``, ``val_end`` y ``mean_return``.
    """
    if train_size <= 0 or forward_size <= 0:
        raise ValueError("train_size y forward_size deben ser positivos")

    n = len(prices)
    results: List[Dict[str, Any]] = []
    start = 0
    # mientras haya suficiente datos para una ventana completa
    while start + train_size + forward_size <= n:
        train_start = start
        train_end = start + train_size  # excluido
        val_start = train_end
        val_end = train_end + forward_size  # excluido

        # En este ejemplo el "entrenamiento" no produce modelo; solo se mantiene
        # la ventana para que la métrica sea reproducible.
        val_prices = prices[val_start:val_end]
        returns = log_returns(val_prices)
        if returns:
            m = mean(returns)
        else:
            m = 0.0
        results.append(
            {
                "train_start": train_start,
                "train_end": train_end - 1,
                "val_start": val_start,
                "val_end": val_end - 1,
                "mean_return": m,
            }
        )
        # Avanzar al siguiente bloque walk‑forward
        start += forward_size
    return results


def generate_geometric_series(start: float, r: float, n: int) -> List[float]:
    """Genera una serie geométrica de precios.

    Cada paso multiplica el precio por ``1 + r``.
    """
    if n <= 0:
        return []
    series = [start]
    for _ in range(1, n):
        series.append(series[-1] * (1 + r))
    return series


def generate_synthetic_series(
    n: int,
    seed: int = 42,
    start: float = 1.47,
    drift: float = 0.0,
    vol: float = 0.0008,
) -> List[float]:
    """Genera una serie de precios sintética (random walk log‑normal sembrado).

    Se usa SOLO como placeholder demostrativo porque no hay datos de
    micro‑estructura OOS reales disponibles (el testnet no es realista, ver
    nota del usuario). NO es un veredicto de rentabilidad.
    """
    rng = random.Random(seed)
    prices = [float(start)]
    for _ in range(1, n):
        ret = drift + vol * rng.gauss(0.0, 1.0)
        prices.append(prices[-1] * math.exp(ret))
    return prices


def _sigma_from_train(train: List[float]) -> float:
    """Volatilidad (std de retornos log) de la ventana de entrenamiento."""
    rets = log_returns(train)
    if len(rets) < 2:
        return 0.0
    return statistics.pstdev(rets)


def _build_snapshot(mid, sigma, inventory, prev_mid, tick_size):
    """Construye el snapshot que consume ``MarketMaker._compute_quotes``."""
    if mid > prev_mid:
        buy_vol, sell_vol = 1.0, 0.5
    elif mid < prev_mid:
        buy_vol, sell_vol = 0.5, 1.0
    else:
        buy_vol, sell_vol = 0.75, 0.75
    spread = max(tick_size * 2.0, sigma * mid)
    return {
        "mid": float(mid),
        "best_bid": float(mid) - spread / 2.0,
        "best_ask": float(mid) + spread / 2.0,
        "spread": float(spread),
        "volatility": float(sigma),
        "inventory": float(inventory),
        "imbalance": 0.0,
        "microprice": float(mid),
        "tick_size": float(tick_size),
        "buy_volume_60s": float(buy_vol),
        "sell_volume_60s": float(sell_vol),
        "ts": 0.0,
    }


def _simulate_oos(prices, sigma, tick_size, mode="strategy", mm=None, base_size=5.0):
    """Simula un paseo OOS con la lógica de estrategia (o baseline naive).

    Modelo de fill (dry‑run simplificado): en cada paso se cotiza; si el
    siguiente precio se mueve al alza, se asume un print en el ask (fill SELL);
    si baja, print en el bid (fill BUY). Esto usa el camino de precios real y
    la cotización de la estrategia.

    Retorna métricas: net_pnl_oos, max_drawdown_oos, inv_std, adverse_fills,
    total_fills, decisions, equity_curve, inv_history.
    """
    inventory = 0.0
    cash = 0.0
    equity_curve: List[float] = []
    inv_history: List[float] = []
    fills: List[Dict[str, Any]] = []
    adverse_count = 0
    decisions = 0
    half_spread = tick_size * 20.0  # piso base para baseline naive
    prev_mid = prices[0]
    n = len(prices)
    for t in range(n):
        mid = prices[t]
        if mode == "strategy" and mm is not None:
            snap = _build_snapshot(mid, sigma, inventory, prev_mid, tick_size)
            quotes = mm._compute_quotes(snap)
            bid = quotes["bid_price"]
            ask = quotes["ask_price"]
            bid_qty = quotes["bid_size"]
            ask_qty = quotes["ask_size"]
        else:
            # Baseline naive: cotiza a mid ± half_spread, tamaño fijo.
            bid = mid - half_spread
            ask = mid + half_spread
            bid_qty = base_size
            ask_qty = base_size
        decisions += 1
        fill_side = None
        fill_price = 0.0
        fill_qty = 0.0
        if t + 1 < n:
            nxt = prices[t + 1]
            if nxt > mid and ask_qty > 0:
                fill_side = "SELL"
                fill_price = ask
                fill_qty = ask_qty
            elif nxt < mid and bid_qty > 0:
                fill_side = "BUY"
                fill_price = bid
                fill_qty = bid_qty
        if fill_side:
            fill_qty = round(float(fill_qty), 1)
            if fill_qty > 0:
                if fill_side == "BUY":
                    inventory += fill_qty
                    cash -= fill_qty * fill_price
                else:
                    inventory -= fill_qty
                    cash += fill_qty * fill_price
                fills.append(
                    {"t": t, "side": fill_side, "price": fill_price,
                     "qty": fill_qty, "inv": inventory}
                )
                # Adverse selection: el precio se mueve contra la posición en
                # los próximos 3 pasos (más allá de 1 tick).
                look = 3
                if t + look < n:
                    fut = prices[t + 1 : t + 1 + look]
                    if fill_side == "BUY" and min(fut) < fill_price - tick_size:
                        adverse_count += 1
                    elif fill_side == "SELL" and max(fut) > fill_price + tick_size:
                        adverse_count += 1
        equity = cash + inventory * mid
        equity_curve.append(equity)
        inv_history.append(inventory)
        prev_mid = mid

    net_pnl = equity_curve[-1] if equity_curve else 0.0
    peak = equity_curve[0] if equity_curve else 0.0
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    inv_std = statistics.pstdev(inv_history) if len(inv_history) > 1 else 0.0
    return {
        "net_pnl_oos": net_pnl,
        "max_drawdown_oos": max_dd,
        "inv_std": inv_std,
        "adverse_fills": adverse_count,
        "total_fills": len(fills),
        "decisions": decisions,
        "equity_curve": equity_curve,
        "inv_history": inv_history,
    }


def run_strategy_walk_forward(
    prices: List[float],
    n_windows: int = 5,
    train_len: int = 40,
    oos_len: int = 20,
) -> Dict[str, Any]:
    """Valida OOS la estrategia B+C usando lógica real en dry‑run.

    Para cada ventana: calibra sigma en el train, corre la estrategia (y un
    baseline naive) en el OOS, y calcula métricas. NO hace Monte‑Carlo ni
    emite veredicto final (lo decide el humano / protocolo).
    """
    import sys as _sys
    import pathlib as _pl
    _root = _pl.Path(__file__).resolve().parent.parent
    if str(_root) not in _sys.path:
        _sys.path.insert(0, str(_root))
    from strategy.market_maker import MarketMaker
    import strategy.config as _cfg

    tick_size = float(getattr(_cfg, "TICK_SIZE_XRPUSDC", 0.0001))
    base_size = float(getattr(_cfg, "BASE_ORDER_SIZE_XRP", 5.0)) * float(
        _cfg.effective_exposure_multiplier()
    )
    mm = MarketMaker(dry_run=True)

    n = len(prices)
    windows: List[Dict[str, Any]] = []
    if n_windows > 1:
        step = max((n - train_len - oos_len) // (n_windows - 1), 1)
    else:
        step = oos_len
    for i in range(n_windows):
        start = i * step
        if start + train_len + oos_len > n:
            break
        train = prices[start : start + train_len]
        oos = prices[start + train_len : start + train_len + oos_len]
        sigma = _sigma_from_train(train)
        strat = _simulate_oos(
            oos, sigma, tick_size, mode="strategy", mm=mm, base_size=base_size
        )
        base = _simulate_oos(
            oos, sigma, tick_size, mode="naive", base_size=base_size
        )
        denom = abs(base["net_pnl_oos"]) + 1e-9
        reduction = (base["net_pnl_oos"] - strat["net_pnl_oos"]) / denom
        windows.append(
            {
                "window": i + 1,
                "train_start": start,
                "train_end": start + train_len - 1,
                "oos_start": start + train_len,
                "oos_end": start + train_len + oos_len - 1,
                "sigma_train": sigma,
                "strategy": strat,
                "baseline": base,
                "reduction_vs_baseline": reduction,
            }
        )
    return {
        "n_windows": len(windows),
        "windows": windows,
        "tick_size": tick_size,
        "base_size": base_size,
    }


def write_walk_forward_report(
    results: Dict[str, Any], path: pathlib.Path, data_source: str = "synthetic"
) -> None:
    """Escribe el informe walk‑forward a ``path`` con secciones
    Resultados / Criterios / Qué falta.

    ``data_source`` es ``"synthetic"`` o ``"real"`` y ajusta la nota aclaratoria.
    """
    is_real = data_source == "real"
    lines: List[str] = []
    lines.append("# WALK_FORWARD_RESULTS.md")
    lines.append("")
    lines.append("## Resultados de la validación walk‑forward OOS (estrategia B+C, dry‑run)")
    lines.append("")
    lines.append(f"- Ventanas walk‑forward: **{results['n_windows']}**")
    lines.append(f"- tick_size usado: `{results['tick_size']}`")
    lines.append(
        f"- base_size (XRP, nivel de exposición actual): `{results['base_size']}`"
    )
    lines.append(f"- fuente de datos: **{'REAL (Binance 1m closes)' if is_real else 'sintética sembrada (placeholder)'}**")
    lines.append("")
    if is_real:
        lines.append(
            "> **NOTA**: La serie de precios es **real** (cierres 1m de XRPUSDC "
            "desde Binance, no testnet). Valida la lógica de la estrategia B+C "
            "sobre un camino de precios real, **pero** usa solo el precio de "
            "cierre como proxy de mid: no incluye micro‑estructura completa de "
            "order book ni flujo real de volumen. **NO** es un veredicto final "
            "de rentabilidad (falta Monte‑Carlo y validación en mainnet)."
        )
    else:
        lines.append(
            "> **NOTA**: La serie de precios usada es **sintética sembrada** "
            "(placeholder). No hay datos de micro‑estructura OOS reales disponibles "
            "y el testnet no es realista (nota del usuario). Esto valida la "
            "*estructura* del motor y el cálculo de métricas, **NO** es un veredicto "
            "de rentabilidad."
        )
    lines.append("")
    lines.append("### Métricas por ventana")
    lines.append("")
    lines.append(
        "| Ventana | NetPnL_OOS | MaxDD_OOS | Std_Inv | "
        "Adverse | Fills | Decisions | Reduction_vs_Baseline |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for w in results["windows"]:
        s = w["strategy"]
        lines.append(
            f"| {w['window']} "
            f"| {s['net_pnl_oos']:.6f} "
            f"| {s['max_drawdown_oos']:.6f} "
            f"| {s['inv_std']:.4f} "
            f"| {s['adverse_fills']} "
            f"| {s['total_fills']} "
            f"| {s['decisions']} "
            f"| {w['reduction_vs_baseline']:+.4f} |"
        )

    # Agregados
    tot_strat_pnl = sum(w["strategy"]["net_pnl_oos"] for w in results["windows"])
    tot_base_pnl = sum(w["baseline"]["net_pnl_oos"] for w in results["windows"])
    tot_adverse = sum(w["strategy"]["adverse_fills"] for w in results["windows"])
    tot_fills = sum(w["strategy"]["total_fills"] for w in results["windows"])
    avg_red = sum(w["reduction_vs_baseline"] for w in results["windows"]) / max(
        len(results["windows"]), 1
    )
    lines.append("")
    lines.append("### Agregados (suma / promedio)")
    lines.append("")
    lines.append(f"- **NetPnL_OOS total estrategia**: `{tot_strat_pnl:.6f}`")
    lines.append(f"- **NetPnL_OOS total baseline**: `{tot_base_pnl:.6f}`")
    lines.append(f"- **Reducción promedio vs baseline**: `{avg_red:+.4f}`")
    lines.append(f"- **Fills totales (estrategia)**: `{tot_fills}`")
    lines.append(f"- **Adverse‑selection total (estrategia)**: `{tot_adverse}`")
    lines.append("")

    # Criterios (no evaluados automáticamente)
    lines.append("### Criterios de aceptación (PROFITABILITY_VALIDATION_PROTOCOL.md)")
    lines.append("")
    lines.append(
        "Estos criterios se listan como referencia; **no** se evalúan ni se "
        "emite veredicto en este script (lo decide el humano / protocolo):"
    )
    lines.append("")
    lines.append(
        "1. `NetPnL_OOS > 0` en la mayoría de las ventanas (rentabilidad neta)."
    )
    lines.append(
        "2. `MaxDD_OOS` dentro del presupuesto de riesgo (`MAX_DRAWDOWN_PCT`)."
    )
    lines.append(
        "3. `Std_Inv` acotada (el skew de inventario controla el riesgo de "
        "inventario)."
    )
    lines.append(
        "4. Reducción de `adverse_fills` respecto al baseline (el filtro "
        "Familia C funciona)."
    )
    lines.append(
        "5. `Reduction_vs_Baseline > 0` (la estrategia B+C supera al baseline "
        "naive)."
    )
    lines.append("")

    # Qué falta
    lines.append("### Qué falta")
    lines.append("")
    lines.append(
        "- **Micro‑estructura completa**: se usa la serie de **cierres 1m reales** "
        "de XRPUSDC (Binance, no testnet) como proxy de mid. Falta el libro de "
        "órdenes real (profundidad, spread dinámico) y el flujo de volumen "
        "(`buy/sell_volume_60s`) para una validación de micro‑estructura "
        "concluyente. El testnet no es representativo (nota del usuario)."
    )
    lines.append(
        "- **Monte‑Carlo**: 1000 simulaciones con la distribución de retornos "
        "para obtener intervalos de confianza (NO ejecutado aquí, a propósito)."
    )
    lines.append(
        "- **Umbrales formales**: definir y codificar los umbrales numéricos de "
        "aceptación del protocolo."
    )
    lines.append(
        "- **Validación en mainnet**: requiere autorización humana explícita "
        "(§0.2) y presupuestos de riesgo confirmados."
    )
    lines.append(
        "- **Datos de flujo reales**: el adverse‑selection usa flujo derivado "
        "de la dirección de precios; idealmente usar `buy/sell_volume_60s` "
        "reales del exchange."
    )
    lines.append("")
    lines.append(
        "*Este documento se generó automáticamente por "
        "`scripts/run_walk_forward.py --mode strategy` (sin veredicto final).*"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _load_prices_from_csv(path: pathlib.Path) -> List[float]:
    """Lee una columna de precios desde un CSV simple ``timestamp,price``.
    Ignora la primera columna y devuelve la lista de precios como ``float``.
    """
    prices: List[float] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    prices.append(float(parts[1]))
                except ValueError:
                    continue
    except OSError as e:
        print(f"[run_walk_forward] No se pudo leer {path}: {e}", file=sys.stderr)
        sys.exit(1)
    return prices


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk‑forward validation harness")
    parser.add_argument(
        "price_file",
        type=pathlib.Path,
        nargs="?",
        default=None,
        help="CSV con timestamp,price (modo 'return' o 'strategy' con datos reales)",
    )
    parser.add_argument(
        "--mode",
        choices=["return", "strategy"],
        default="return",
        help="Modo de validación",
    )
    parser.add_argument("--train-size", type=int, default=200,
                        help="Tamaño de la ventana de entrenamiento (modo return)")
    parser.add_argument("--forward-size", type=int, default=50,
                        help="Tamaño de la ventana de validación (modo return)")
    parser.add_argument("--n-windows", type=int, default=5,
                        help="Cantidad de ventanas walk‑forward (modo strategy)")
    parser.add_argument("--train-len", type=int, default=40,
                        help="Largo de train por ventana (modo strategy)")
    parser.add_argument("--oos-len", type=int, default=20,
                        help="Largo de OOS por ventana (modo strategy)")
    parser.add_argument("--synthetic-n", type=int, default=300,
                        help="Largo de la serie sintética si no hay price_file")
    parser.add_argument("--seed", type=int, default=42,
                        help="Semilla de la serie sintética")
    parser.add_argument("--report", type=pathlib.Path, default=None,
                        help="Ruta del informe markdown a escribir (modo strategy)")
    args = parser.parse_args(argv)

    if args.mode == "strategy":
        if args.price_file is not None:
            prices = _load_prices_from_csv(args.price_file)
        else:
            prices = generate_synthetic_series(args.synthetic_n, seed=args.seed)
        if not prices:
            print("[run_walk_forward] No se cargaron precios", file=sys.stderr)
            return 1
        results = run_strategy_walk_forward(
            prices, args.n_windows, args.train_len, args.oos_len
        )
        if args.report is not None:
            src = "real" if args.price_file is not None else "synthetic"
            write_walk_forward_report(results, args.report, data_source=src)
            print(f"[run_walk_forward] Reporte escrito en {args.report} (fuente={src})")
        else:
            print(json.dumps(results, indent=2, default=str))
        return 0

    # Modo 'return' (demo original, retrocompatible)
    if args.price_file is None:
        print("[run_walk_forward] En modo 'return' se requiere price_file",
              file=sys.stderr)
        return 1
    prices = _load_prices_from_csv(args.price_file)
    if not prices:
        print("[run_walk_forward] No se cargaron precios", file=sys.stderr)
        return 1
    metrics = compute_walk_forward_metrics(prices, args.train_size, args.forward_size)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
