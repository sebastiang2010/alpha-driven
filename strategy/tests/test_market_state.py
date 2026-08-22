# test_market_state.py
"""
Tests para market_state.MarketState._compute_volatility.

Verifica que la volatilidad realizada (defecto #8) sea invariante a la
frecuencia del WebSocket y no presente la amplificación espúrea 2-7x que
tenía el rescale por evento sqrt(ref/avg_interval).
"""

import math
import os
import sys
from collections import deque

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)

import pytest  # noqa: E402

from strategy.market_state import MarketState  # noqa: E402


def _state_with_mid_samples(samples):
    """Construye un MarketState con _mid_samples prefijados (sin tocar el WS)."""
    ms = MarketState("xrpusdc")
    setattr(ms, "_mid_samples", deque(samples))
    return ms


def _linear_walk(start, step, n, t0=1000.0, dt=0.5):
    """Camino de precios con retornos log ~step entre muestras separadas dt."""
    samples = []
    price = start
    for i in range(n):
        samples.append((t0 + i * dt, price))
        price = price * math.exp(step)
    return samples


def test_volatility_zero_when_few_samples():
    ms = MarketState("xrpusdc")
    # Menos de 3 muestras -> 0.0
    ms._mid_samples = deque([(1000.0, 1.0), (1001.0, 1.01)])
    assert ms._compute_volatility(2000.0) == 0.0


def test_volatility_sane_bound_fast_vs_slow_ws():
    """La sigma entregada debe ser casi idéntica (invariante) para un WS
    rapidísimo (dt=0.1s) y uno lento (dt=1.0s) con la misma volatilidad total.
    Antes el rescale por evento daba ~3.16x de diferencia espúrea."""
    step_fast = 0.0005  # retorno log constante por evento rápido
    step_slow = step_fast * math.sqrt(10)

    fast = _linear_walk(1.0, step_fast, 100, t0=1000.0, dt=0.1)   # 10 s de ventana
    slow = _linear_walk(1.0, step_slow, 10, t0=1000.0, dt=1.0)    # 10 s de ventana

    sig_fast = _state_with_mid_samples(fast)._compute_volatility(fast[-1][0] + 1.0)
    sig_slow = _state_with_mid_samples(slow)._compute_volatility(slow[-1][0] + 1.0)

    assert sig_fast > 0.0
    assert sig_slow > 0.0
    ratio = max(sig_fast, sig_slow) / min(sig_fast, sig_slow)
    assert ratio < 1.15, f"sigma no invariante al WS: ratio={ratio:.3f}"


def test_volatility_within_reasonable_multiple_of_per_event():
    """Con un WS rápido (dt=0.1s) la sigma de 5s entregada debe ser
    coherente: ~sqrt(5/dt) veces el sigma por evento, acotada y sin
    explosión. Verifica que NO supera un tope sensato (p.ej. 15x el sigma
    por-evento, muy por encima del factor teórico ~7x)."""
    step = 0.0005
    samples = _linear_walk(1.0, step, 200, t0=1000.0, dt=0.1)  # 20 s de ventana
    ms = _state_with_mid_samples(samples)
    sigma_ref = ms._compute_volatility(samples[-1][0] + 1.0)

    returns = [math.log(p2 / p1) for (_, p1), (_, p2) in zip(samples, samples[1:])]
    per_event = math.sqrt(sum(r * r for r in returns) / (len(returns) - 1))

    factor = sigma_ref / per_event
    assert factor < 15.0, f"factor de amplificación excesivo: {factor:.2f}x"
    assert factor > 1.0, "sigma de 5s debe ser mayor que la por-evento"


def test_volatility_floor_prevents_degenerate_burst():
    """Un tramo degenerado (3 muestras muy juntas con un salto enorme) no
    debe producir una sigma absurda; el piso en 'elapsed' la acota."""
    samples = [(1000.0, 1.0), (1000.05, 1.10), (1000.10, 1.11)]  # 0.1 s, retorno +11%
    ms = _state_with_mid_samples(samples)
    sigma = ms._compute_volatility(1000.1)
    assert sigma <= 0.15
    assert sigma > 0.0
