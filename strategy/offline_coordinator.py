# offline_coordinator.py — Coordinador offline para integración A-S (V2)
"""
Coordinador dueño del calendario y del lector. El motor (ExecutionReconstructor)
es dueño de timers, órdenes, cola, fills, inventario y cash.

Responsabilidades:
- Posee el calendario determinista: t0 + k*D (D = CYCLE_INTERVAL_SEC * 1000)
- Intercala ciclos A-S con grupos de mercado sin saltarse trades ni libros
- Warm-up: W = max(ventanas de mercado, momentum); exige 3 mids con intervalos positivos
- No decisiones tras EOF; gaps invalidan la corrida sin resetear inventario
- Lector cierra grupo timestamp completo antes de entregarlo; nunca filtra futuro a política
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Any, Iterator
from collections import deque

from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    ReconstructionConfig,
    ExecutionDelta,
    ExecutionSnapshot,
    FinalResult,
)
from strategy import config


@dataclass(frozen=True)
class MarketEvent:
    """Evento de mercado normalizado para el coordinador."""
    ts_ms: int
    kind: str  # 'book' | 'trade'
    data: Dict[str, Any]


@dataclass(frozen=True)
class Command:
    """Comando de la política para el motor."""
    ts_ms: int
    kind: str  # 'submit' | 'cancel'
    data: Dict[str, Any]


@dataclass
class CoordinatorConfig:
    """Configuración del coordinador offline."""
    cycle_interval_ms: int = int(config.CYCLE_INTERVAL_SEC * 1000)
    max_book_age_ms: int = 1000
    max_gap_ms: int = 2000
    max_position_lots: int = 100
    place_latency_ms: int = 40
    cancel_latency_ms: int = 20
    tick_size: float = config.TICK_SIZE_XRPUSDC
    qty_step: float = 1.0
    max_notional: float = config.MAX_POSITION_NOTIONAL_USDC
    min_spread_ticks: int = config.MIN_SPREAD_TICKS
    # Ventanas para warm-up
    volatility_window_sec: float = config.VOLATILITY_WINDOW_SEC
    alpha_window_sec: float = config.ALPHA_WINDOW_SEC
    momentum_window_sec: float = config.MOMENTUM_WINDOW_SECONDS


@dataclass
class WarmupState:
    """Estado del calentamiento."""
    mid_samples: deque = field(default_factory=lambda: deque(maxlen=10000))
    min_mids_required: int = 3
    warmup_complete: bool = False
    warmup_end_ts: Optional[int] = None


class OfflineCoordinator:
    """
    Coordinador offline para reconstrucción de ejecución con política A-S.
    
    Flujo:
    1. Lee eventos de mercado (book/trade) ordenados por timestamp
    2. Agrupa por timestamp y cierra cada grupo completamente
    3. Intercala ciclos A-S en t0 + k*D
    4. Para cada timestamp: advance_to -> (ciclo A-S si toca) -> apply_commands
    5. Warm-up antes de emitir submits
    6. finish() al final
    """
    
    def __init__(self, coordinator_config: CoordinatorConfig):
        self.cfg = coordinator_config
        self.reconstructor_config = ReconstructionConfig(
            tick_size=coordinator_config.tick_size,
            qty_step=coordinator_config.qty_step,
            place_latency_ms=coordinator_config.place_latency_ms,
            cancel_latency_ms=coordinator_config.cancel_latency_ms,
            max_notional=coordinator_config.max_notional,
            min_spread_ticks=coordinator_config.min_spread_ticks,
            max_book_age_ms=coordinator_config.max_book_age_ms,
            max_gap_ms=coordinator_config.max_gap_ms,
            max_position_lots=coordinator_config.max_position_lots,
        )
        self.engine = ExecutionReconstructor(self.reconstructor_config)
        self.warmup = WarmupState()
        self._t0: Optional[int] = None
        self._last_processed_ts: int = -1
        self._cycle_counter: int = 0
        self._finished: bool = False
        self._market_events_by_ts: Dict[int, List[MarketEvent]] = {}
        self._commands_by_ts: Dict[int, List[Command]] = {}
        self._all_timestamps: List[int] = []
        self._current_idx: int = 0
        
    def load_events(self, market_events: Sequence[MarketEvent], 
                    commands: Sequence[Command] = ()) -> None:
        """Carga y valida eventos de mercado y comandos."""
        # Agrupar eventos de mercado por timestamp
        for e in market_events:
            if e.kind not in ('book', 'trade'):
                raise ValueError(f'Invalid market event kind: {e.kind}')
            if e.ts_ms < 0:
                raise ValueError(f'Negative timestamp: {e.ts_ms}')
            self._market_events_by_ts.setdefault(e.ts_ms, []).append(e)
        
        # Agrupar comandos por timestamp
        for c in commands:
            if c.kind not in ('submit', 'cancel'):
                raise ValueError(f'Invalid command kind: {c.kind}')
            if c.ts_ms < 0:
                raise ValueError(f'Negative command timestamp: {c.ts_ms}')
            self._commands_by_ts.setdefault(c.ts_ms, []).append(c)
        
        # Combinar y ordenar todos los timestamps únicos
        all_ts = set(self._market_events_by_ts.keys()) | set(self._commands_by_ts.keys())
        self._all_timestamps = sorted(all_ts)
        
        if not self._all_timestamps:
            raise ValueError('No events to process')
        
        # Validar que no hay gaps en los timestamps de mercado (solo para libros)
        book_ts = sorted([ts for ts, evs in self._market_events_by_ts.items() 
                         if any(e.kind == 'book' for e in evs)])
        for i in range(1, len(book_ts)):
            gap = book_ts[i] - book_ts[i-1]
            if gap > self.cfg.max_gap_ms:
                raise ValueError(f'Depth time gap: {gap}ms > {self.cfg.max_gap_ms}ms '
                               f'between {book_ts[i-1]} and {book_ts[i]}')
        
        # Establecer t0 como el primer libro válido
        if book_ts:
            self._t0 = book_ts[0]
        else:
            self._t0 = self._all_timestamps[0]
    
    def _is_cycle_timestamp(self, ts_ms: int) -> bool:
        """Verifica si ts_ms corresponde a un ciclo programado t0 + k*D."""
        if self._t0 is None:
            return False
        if ts_ms < self._t0:
            return False
        diff = ts_ms - self._t0
        return diff % self.cfg.cycle_interval_ms == 0
    
    def _next_cycle_ts(self, after_ts: int) -> Optional[int]:
        """Próximo timestamp de ciclo después de after_ts."""
        if self._t0 is None:
            return None
        t0 = self._t0
        if after_ts < t0:
            return t0
        k = (after_ts - t0) // self.cfg.cycle_interval_ms + 1
        next_ts = t0 + k * self.cfg.cycle_interval_ms
        return next_ts
    
    def _update_warmup(self, ts_ms: int, book_data: Optional[Dict[str, Any]]) -> None:
        """Actualiza estado de warm-up con nuevo libro."""
        if self.warmup.warmup_complete:
            return
        
        if book_data and 'bids' in book_data and 'asks' in book_data:
            bids = book_data['bids']
            asks = book_data['asks']
            if bids and asks:
                mid_ticks = (bids[0][0] + asks[0][0]) / 2.0
                mid_price = mid_ticks * self.cfg.tick_size
                ts_sec = ts_ms / 1000.0
                self.warmup.mid_samples.append((ts_sec, mid_price))
        
        # Verificar si tenemos suficientes mids con intervalos positivos
        if len(self.warmup.mid_samples) >= self.warmup.min_mids_required:
            intervals = []
            for i in range(1, len(self.warmup.mid_samples)):
                dt = self.warmup.mid_samples[i][0] - self.warmup.mid_samples[i-1][0]
                if dt > 0:
                    intervals.append(dt)
            if len(intervals) >= self.warmup.min_mids_required - 1:
                # Calcular W = max(ventanas requeridas)
                w_sec = max(self.cfg.volatility_window_sec, 
                           self.cfg.alpha_window_sec,
                           self.cfg.momentum_window_sec)
                w_ms = int(w_sec * 1000)
                t0 = self._t0
                if t0 is not None and ts_ms >= t0 + w_ms:
                    self.warmup.warmup_complete = True
                    self.warmup.warmup_end_ts = ts_ms
    
    def _get_book_for_ts(self, ts_ms: int) -> Optional[Dict[str, Any]]:
        """Obtiene el último libro válido para un timestamp."""
        events = self._market_events_by_ts.get(ts_ms, [])
        books = [e.data for e in events if e.kind == 'book']
        return books[-1] if books else None
    
    def _get_trades_for_ts(self, ts_ms: int) -> List[Dict[str, Any]]:
        """Obtiene todos los trades para un timestamp."""
        events = self._market_events_by_ts.get(ts_ms, [])
        return [e.data for e in events if e.kind == 'trade']
    
    def _get_commands_for_ts(self, ts_ms: int) -> List[Command]:
        """Obtiene comandos para un timestamp."""
        return self._commands_by_ts.get(ts_ms, [])
    
    def advance_to_next_timestamp(self) -> Tuple[ExecutionDelta, Optional[ExecutionDelta]]:
        """
        Avanza al siguiente timestamp en el calendario.
        
        Returns:
            (market_delta, cycle_delta) donde cycle_delta es None si no hay ciclo en este ts.
            El cycle_delta contiene el resultado de apply_commands si la política emitió comandos.
        """
        if self._finished:
            raise ValueError('Coordinator already finished')
        if self._current_idx >= len(self._all_timestamps):
            raise ValueError('No more timestamps to process')
        
        ts_ms = self._all_timestamps[self._current_idx]
        
        # Validar timestamp no regresivo
        if ts_ms < self._last_processed_ts:
            raise ValueError(f'Regressive timestamp: {ts_ms} < {self._last_processed_ts}')
        
        # Preparar eventos de mercado para este timestamp
        market_events = []
        book_data = self._get_book_for_ts(ts_ms)
        trades = self._get_trades_for_ts(ts_ms)
        
        if book_data:
            market_events.append(MarketEvent(ts_ms, 'book', book_data))
        for trade in trades:
            market_events.append(MarketEvent(ts_ms, 'trade', trade))
        
        # Actualizar warm-up
        self._update_warmup(ts_ms, book_data)
        
        # advance_to con eventos de mercado
        market_delta = self.engine.advance_to(ts_ms, market_events)
        
        # Verificar si hay ciclo A-S en este timestamp
        cycle_delta = None
        if self._is_cycle_timestamp(ts_ms) and self.warmup.warmup_complete:
            # El ciclo A-S ocurre aquí (fase 4).
            # La política debería haber generado comandos para este ts.
            # apply_commands se llama aparte cuando la política entrega comandos.
            pass
        
        self._last_processed_ts = ts_ms
        self._current_idx += 1
        
        return market_delta, cycle_delta
    
    def apply_policy_commands(self, ts_ms: int, commands: Sequence[Command]) -> ExecutionDelta:
        """
        Aplica comandos de la política para un timestamp.
        Debe llamarse después de advance_to_next_timestamp para el mismo ts.
        """
        if self._finished:
            raise ValueError('Coordinator already finished')
        if ts_ms != self._last_processed_ts:
            raise ValueError(f'Commands ts_ms={ts_ms} does not match last advance={self._last_processed_ts}')
        
        # Validar que todos los comandos tengan el timestamp correcto
        for cmd in commands:
            if cmd.ts_ms != ts_ms:
                raise ValueError(f'Command timestamp {cmd.ts_ms} != {ts_ms}')
        
        # Convertir Command a dict para el motor
        cmd_dicts = [
            {'ts_ms': cmd.ts_ms, 'kind': cmd.kind, **cmd.data}
            for cmd in commands
        ]
        
        return self.engine.apply_commands(ts_ms, cmd_dicts)
    
    def get_state(self) -> ExecutionSnapshot:
        """Obtiene snapshot inmutable del estado actual."""
        return self.engine.state()
    
    def finish(self) -> FinalResult:
        """Finaliza la reconstrucción."""
        if self._finished:
            raise ValueError('Already finished')
        
        observed_end_ms = self._last_processed_ts
        if observed_end_ms < 0:
            observed_end_ms = self._all_timestamps[-1] if self._all_timestamps else 0
        
        self._finished = True
        return self.engine.finish(observed_end_ms)
    
    def run_full(self, policy_fn) -> FinalResult:
        """
        Ejecución completa con una función de política.
        
        policy_fn(ts_ms: int, snapshot: ExecutionSnapshot) -> List[Command]
        """
        while self._current_idx < len(self._all_timestamps):
            market_delta, _ = self.advance_to_next_timestamp()
            ts_ms = self._last_processed_ts
            
            # Obtener snapshot para la política
            snapshot = self.get_state()
            
            # Llamar a la política solo si warm-up completo y es timestamp de ciclo
            commands = []
            if self.warmup.warmup_complete and self._is_cycle_timestamp(ts_ms):
                commands = policy_fn(ts_ms, snapshot)
            
            if commands:
                self.apply_policy_commands(ts_ms, commands)
        
        return self.finish()


def create_coordinator_from_capture(depth_rows: Sequence[Dict[str, Any]],
                                     trade_rows: Sequence[Dict[str, Any]],
                                     command_rows: Sequence[Dict[str, Any]] = (),
                                     coordinator_config: Optional[CoordinatorConfig] = None) -> OfflineCoordinator:
    """
    Factory para crear coordinador desde filas de captura (CSV/JSONL).
    
    depth_rows: dicts con ts_ms, update_id, pu, bids, asks
    trade_rows: dicts con ts_ms, trade_id, price_ticks, qty_lots, is_buyer_maker
    command_rows: dicts con ts_ms, kind, order_id, side, price_ticks, qty_lots
    """
    cfg = coordinator_config or CoordinatorConfig()
    coord = OfflineCoordinator(cfg)
    
    market_events = []
    for row in depth_rows:
        market_events.append(MarketEvent(
            ts_ms=int(row['ts_ms']),
            kind='book',
            data={
                'bids': row['bids'],
                'asks': row['asks'],
                'update_id': int(row['update_id']),
                'pu': int(row.get('pu', 0)),
            }
        ))
    
    for row in trade_rows:
        market_events.append(MarketEvent(
            ts_ms=int(row['ts_ms']),
            kind='trade',
            data={
                'trade_id': str(row['trade_id']),
                'price_ticks': int(row['price_ticks']),
                'qty_lots': int(row['qty_lots']),
                'is_buyer_maker': bool(row['is_buyer_maker']),
            }
        ))
    
    commands = []
    for row in command_rows:
        commands.append(Command(
            ts_ms=int(row['ts_ms']),
            kind=row['kind'],
            data={k: v for k, v in row.items() if k not in ('ts_ms', 'kind')}
        ))
    
    coord.load_events(market_events, commands)
    return coord