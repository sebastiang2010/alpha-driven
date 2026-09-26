# calendar.py — Calendario offline compartido (F1.2).
# Una sola implementación para ambas rutas (ASCoordinator y OfflineCoordinator):
# grilla de ciclos t0+k*D, pasos fusionados (ciclos ∪ eventos) y validaciones.

from __future__ import annotations

from typing import Dict, List, Sequence, Any


def validate_interval_ms(value: Any, name: str = "decision_interval_ms") -> int:
    """Valida el intervalo de calendario: entero (no bool) positivo.

    Rechaza cero, negativos, fraccionarios y no-enteros antes de construir
    cualquier grilla (un intervalo inválido colgaría o rompería el contrato
    de milisegundos enteros).
    """
    if type(value) is not int or value <= 0:
        raise ValueError(
            f"{name} debe ser entero positivo en ms, recibido {value!r}")
    return value


def cycle_grid(t0_ms: int, interval_ms: int, last_ts_ms: int) -> List[int]:
    """Ciclos programados t0+k*D con k>=0 dentro de cobertura (<= last_ts_ms)."""
    validate_interval_ms(interval_ms)
    grid: List[int] = []
    k = 0
    while True:
        ts = t0_ms + k * interval_ms
        if ts > last_ts_ms:
            break
        grid.append(ts)
        k += 1
    return grid


def merged_steps(event_timestamps: Sequence[int], t0_ms: int,
                 interval_ms: int, last_ts_ms: int) -> List[int]:
    """Pasos fusionados: timestamps con eventos ∪ ciclos programados.

    Ascendente, sin duplicados. Los ciclos sin eventos avanzan igual
    (drenan timers y pueden decidir); ningún evento se salta.
    """
    return sorted(set(event_timestamps) | set(
        cycle_grid(t0_ms, interval_ms, last_ts_ms)))


def check_book_coverage(book: Any, ts_ms: int, max_book_age_ms: int) -> None:
    """Valida cobertura de libro antes de decidir, haya comandos o no.

    Un ciclo (vacío o no) nunca decide sobre un libro obsoleto o ausente.
    """
    if not book:
        raise ValueError(f"Sin libro para decidir en ts={ts_ms}")
    book_ts = int(book.get("ts_ms", ts_ms))
    age = ts_ms - book_ts
    if age > max_book_age_ms:
        raise ValueError(
            f"Libro obsoleto en ts={ts_ms}: edad {age}ms > {max_book_age_ms}ms")
