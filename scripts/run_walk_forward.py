#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_walk_forward.py — Simple walk‑forward validation harness.

Este script no depende de la infraestructura de Binance; sirve como
demostración de la lógica de validación descrita en
`md/PROFITABILITY_VALIDATION_PROTOCOL.md` y `md/TRADING_SYSTEM_ARCHITECTURE.md`.

Se basa en una serie de precios "mid" y, para cada ventana de entrenamiento,
entrena (aquí simplemente calcula la media de los retornos) y valida en la
ventana siguiente.  Los resultados se imprimen en formato JSON para que puedan
consumirse por otras herramientas.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from typing import List, Dict, Any

__all__ = [
    "log_returns",
    "mean",
    "compute_walk_forward_metrics",
    "generate_geometric_series",
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
    parser.add_argument("price_file", type=pathlib.Path, help="CSV con timestamp,price")
    parser.add_argument("--train-size", type=int, default=200, help="Tamaño de la ventana de entrenamiento")
    parser.add_argument("--forward-size", type=int, default=50, help="Tamaño de la ventana de validación")
    args = parser.parse_args(argv)

    prices = _load_prices_from_csv(args.price_file)
    if not prices:
        print("[run_walk_forward] No se cargaron precios", file=sys.stderr)
        return 1

    metrics = compute_walk_forward_metrics(prices, args.train_size, args.forward_size)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
