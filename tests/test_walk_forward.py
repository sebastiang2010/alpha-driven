"""Tests for the walk‑forward validation utilities defined in
`scripts/run_walk_forward.py`.

The tests focus on the pure‑Python helper functions, avoiding any external
dependencies.
"""

import math

from scripts.run_walk_forward import (
    generate_geometric_series,
    compute_walk_forward_metrics,
    log_returns,
    mean,
)


def test_log_returns_and_mean_constant_growth():
    # Serie geométrica con crecimiento constante del 1% por paso
    start = 100.0
    r = 0.01  # 1% incremento
    n = 20
    prices = generate_geometric_series(start, r, n)
    # Los retornos logarítmicos deben ser idénticos a log(1+r)
    expected = math.log(1 + r)
    returns = log_returns(prices)
    assert all(abs(ret - expected) < 1e-12 for ret in returns)
    # La media debe coincidir con el valor esperado
    assert abs(mean(returns) - expected) < 1e-12


def test_compute_walk_forward_metrics_basic():
    # Serie con crecimiento constante del 2%
    start = 1.0
    r = 0.02
    total = 30
    prices = generate_geometric_series(start, r, total)
    train_sz = 10
    forward_sz = 5
    metrics = compute_walk_forward_metrics(prices, train_sz, forward_sz)
    # Debe haber floor((total - train_sz) / forward_sz) = 4 windows
    assert len(metrics) == 4
    expected_ret = math.log(1 + r)
    for m in metrics:
        # La media del retorno en la ventana de validación debe ser la esperada
        assert abs(m["mean_return"] - expected_ret) < 1e-12
        # Los índices deben estar dentro de los límites de la serie
        assert 0 <= m["train_start"] < m["train_end"] < total
        assert m["val_start"] == m["train_end"] + 1
        assert m["val_end"] == m["val_start"] + forward_sz - 1


def test_compute_walk_forward_metrics_insufficient_data():
    prices = [1.0, 1.01, 1.02]  # Muy corta
    result = compute_walk_forward_metrics(prices, train_size=5, forward_size=2)
    # No hay suficiente datos para una ventana completa
    assert result == []
