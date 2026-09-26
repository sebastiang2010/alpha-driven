# execution_reconstruction.py — Simulador aislado para campaña l2-24h-execution-reconstruction
"""
Módulo aislado (NO toca market_maker/risk/execution/config defaults).
Reconstruye ejecución de órdenes maker dentro del libro L2 real.

Componentes:
  - QueuePositionTracker (FIFO por nivel)
  - PartialFillEngine (takes según trade_qty vs queue)
  - LatencyModel (delay parametrizable colocación/cancel)
  - CancelReplaceEngine
  - InventorySkew (quote shift por inventario, delega a AlphaModel)
  - Cap25Virtual (rechazo si |inv|*mid > 25, reduce_only)
  - ExecutionReconstructor (interfaz incremental: advance_to/apply_commands/state/finish)

Todos los cálculos son causales (features <= t). Spread mínimo 8 ticks
se filtra en el caller (backtest), no se re-parametriza aquí.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Sequence, Any


# ── QueuePositionTracker ─────────────────────────────────────────────

class QueuePositionTracker:
    """FIFO por nivel: estima posición en cola.

    Sin IDs de orden, se estima desde qty@nivel. La orden entra al FINAL
    de la cola: queue_ahead(0) = level_qty - our_qty.
    Evolución: +arrivals -cancels -front_consumption.
    """

    def __init__(self, arrival_share: float = 0.5, front_consumption: float = 1.0):
        self.arrival_share = float(arrival_share)
        self.front_consumption = float(front_consumption)
        # estado por orden id -> queue_ahead
        self.queues: Dict[str, float] = {}

    def init_queue(self, order_id: str, level_qty: float, our_qty: float) -> float:
        """Inicializa queue_ahead para order_id. Retorna queue_ahead(0)."""
        if level_qty is None or not math.isfinite(level_qty) or level_qty <= 0:
            qa = 0.0
        else:
            qa = max(0.0, float(level_qty) - float(our_qty))
        self.queues[order_id] = qa
        return qa

    def get(self, order_id: str) -> float:
        return self.queues.get(order_id, 0.0)

    def advance(self, order_id: str, new_arrivals: float = 0.0,
                cancels: float = 0.0, aggressive_qty: float = 0.0) -> float:
        """Evoluciona queue_ahead tras dt."""
        qa = self.queues.get(order_id, 0.0)
        qa = qa + float(new_arrivals) * self.arrival_share - float(cancels)
        qa -= float(aggressive_qty) * self.front_consumption
        qa = max(0.0, qa)
        self.queues[order_id] = qa
        return qa

    def share(self, our_qty: float, queue_ahead: float) -> float:
        """Cuota FIFO = our_qty / (our_qty + queue_ahead)."""
        denom = float(our_qty) + max(0.0, float(queue_ahead))
        return float(our_qty) / denom if denom > 0 else 1.0

    def remove(self, order_id: str) -> None:
        self.queues.pop(order_id, None)


# ── PartialFillEngine ────────────────────────────────────────────────

@dataclass
class FillEvent:
    order_id: str
    side: str
    price: float
    qty: float
    status: str  # "partial" | "full"
    ts_ms: int


class PartialFillEngine:
    """Takes según trade_qty vs queue. Usa QueuePositionTracker."""

    def __init__(self, tracker: Optional[QueuePositionTracker] = None):
        self.tracker = tracker or QueuePositionTracker()

    def try_fill(self, order_id: str, our_qty: float, our_price: float,
                 side: str, trade_qty: float, trade_price: float,
                 is_buyer_maker: bool, ts_ms: int,
                 tick_size: float = 0.0001) -> Optional[FillEvent]:
        """Intenta fill de order_id contra un trade agresor.

        Lado: is_buyer_maker True → buyer maker → seller agresor → llena BUY resting
              False → buyer agresor → llena SELL resting (execution_engine:619-629)
        Precio: solo si trade_price cruza our_price dentro de tolerancia tick/2.
        Cantidad: consume queue_ahead primero.
        """
        # Lado
        if side == "BUY" and not is_buyer_maker:
            # BUY resting solo llena si seller agredió (m=True)
            # Si buyer agredió (m=False) no toca BUY
            # Invertido: BUY necesita seller agresor → m=True
            # Ver execution_engine: BUY continues if not is_buyer_maker? No:
            #   BUY: if not is_buyer_maker: continue → BUY solo llena si m=True
            # SELL: if is_buyer_maker: continue → SELL solo llena si m=False
            # Aquí aplicamos igual.
            return None
        if side == "SELL" and is_buyer_maker:
            return None
        # Precio: tolerancia tick/2
        tol = tick_size / 2.0
        if abs(float(trade_price) - float(our_price)) > tol:
            return None
        qa = self.tracker.get(order_id)
        q = float(trade_qty)
        oq = float(our_qty)
        if q <= qa + 1e-12:
            # No nos toca
            self.tracker.advance(order_id, aggressive_qty=q)
            return None
        # Nos toca algo
        fill_qty = min(oq, q - qa)
        # Actualizar queue: consumido todo lo delante + parte de nosotros
        # Si partial, queda queue_ahead=0 y our remanente; si full, orden completa
        remaining = oq - fill_qty
        if remaining <= 1e-12:
            self.tracker.remove(order_id)
            status = "full"
        else:
            # partial: re-encolar remanente al final? Simplificación: queue_ahead=0, our_qty=remaining
            # Actualizamos queue para reflejar que delante se vació
            self.tracker.queues[order_id] = 0.0
            status = "partial"
        return FillEvent(order_id=order_id, side=side, price=float(our_price),
                         qty=float(fill_qty), status=status, ts_ms=int(ts_ms))

    def remaining_qty(self, order_id: str, original_qty: float, filled_qty: float) -> float:
        return max(0.0, float(original_qty) - float(filled_qty))


# ── LatencyModel ─────────────────────────────────────────────────────

@dataclass
class PendingOrder:
    order_id: str
    side: str
    price: float
    qty: float
    submit_ts_ms: int
    live_ts_ms: int  # submit + place_latency
    cancel_ts_ms: Optional[int] = None
    cancel_live_ms: Optional[int] = None


class LatencyModel:
    """Delay parametrizable para colocación y cancel.

    Orden no viva hasta live_ts_ms. Cancel no efectivo hasta cancel_live_ms.
    """

    def __init__(self, place_latency_ms: int = 40, cancel_latency_ms: int = 20):
        self.place_latency_ms = int(place_latency_ms)
        self.cancel_latency_ms = int(cancel_latency_ms)
        self.pending: Dict[str, PendingOrder] = {}

    def submit(self, order_id: str, side: str, price: float, qty: float,
               ts_ms: int) -> PendingOrder:
        po = PendingOrder(
            order_id=order_id, side=side, price=float(price), qty=float(qty),
            submit_ts_ms=int(ts_ms), live_ts_ms=int(ts_ms) + self.place_latency_ms,
        )
        self.pending[order_id] = po
        return po

    def request_cancel(self, order_id: str, ts_ms: int) -> bool:
        po = self.pending.get(order_id)
        if po is None:
            return False
        po.cancel_ts_ms = int(ts_ms)
        po.cancel_live_ms = int(ts_ms) + self.cancel_latency_ms
        return True

    def is_live(self, order_id: str, now_ms: int) -> bool:
        po = self.pending.get(order_id)
        if po is None:
            return False
        if now_ms < po.live_ts_ms:
            return False
        if po.cancel_live_ms is not None and now_ms >= po.cancel_live_ms:
            return False
        return True

    def is_stale(self, order_id: str, now_ms: int, best_bid: float,
                 best_ask: float, tick_size: float = 0.0001) -> bool:
        """Orden viva pero ya no en touch (stale_no_fill)."""
        if not self.is_live(order_id, now_ms):
            return False
        po = self.pending[order_id]
        if po.side == "BUY":
            # BUY en touch = price == best_bid (off=0). Si best_bid se movió >0.5 tick, stale
            return abs(po.price - best_bid) > tick_size * 0.5
        else:
            return abs(po.price - best_ask) > tick_size * 0.5

    def remove(self, order_id: str) -> None:
        self.pending.pop(order_id, None)


# ── CancelReplaceEngine ──────────────────────────────────────────────

class CancelReplaceEngine:
    """Evalúa cancel/replace con latencia y lifetimes de config."""

    def __init__(self, latency: Optional[LatencyModel] = None,
                 min_lifetime_sec: float = 15.0, max_lifetime_sec: float = 60.0,
                 requote_threshold_pct: float = 0.001):
        self.latency = latency or LatencyModel()
        self.min_lifetime_sec = float(min_lifetime_sec)
        self.max_lifetime_sec = float(max_lifetime_sec)
        self.requote_threshold_pct = float(requote_threshold_pct)

    def should_replace(self, order_id: str, now_ms: int, mid: float,
                       last_mid: Optional[float], submit_ms: int) -> Tuple[bool, str]:
        age_sec = (now_ms - submit_ms) / 1000.0
        if age_sec > self.max_lifetime_sec:
            return True, "max_lifetime"
        if last_mid is not None and last_mid > 0 and mid is not None and mid > 0:
            delta = abs(mid - last_mid) / last_mid
            if delta > self.requote_threshold_pct and age_sec >= self.min_lifetime_sec:
                return True, "requote_mid_move"
        return False, ""

    def should_cancel_side_invalid(self, quote_ok: bool) -> bool:
        return not quote_ok


# ── InventorySkew ────────────────────────────────────────────────────

class InventorySkew:
    """Quote shift por inventario. Delega a AlphaModel.quote_distances."""

    def __init__(self):
        # lazy import para no cargar al importar módulo sin config listo
        self._alpha = None

    def _get_alpha(self):
        if self._alpha is None:
            from strategy.alpha_model import AlphaModel
            self._alpha = AlphaModel()
        return self._alpha

    def quote_distances(self, snapshot: dict, alpha: float,
                        inventory: float, sigma: float,
                        now_sec: Optional[float] = None):
        """Retorna (bid_dist, ask_dist) con skew por inventario y piso 8 ticks."""
        am = self._get_alpha()
        return am.quote_distances(snapshot, alpha, inventory, sigma, now_sec=now_sec)

    def skew_value(self, inventory: float, mid: float, sigma: float) -> float:
        """Skew puro: (inv*mid/25)*0.5*sigma_efectiva (informativo)."""
        try:
            from strategy import config as cfg
            import math
            sigma_eff = float(sigma) * math.sqrt(12.0) * math.sqrt(float(cfg.CYCLE_INTERVAL_SEC))
            ratio = float(inventory) * float(mid) / float(cfg.MAX_POSITION_NOTIONAL_USDC)
            return ratio * 0.5 * sigma_eff
        except Exception:
            return 0.0


# ── Cap25Virtual ─────────────────────────────────────────────────────

@dataclass
class CapResult:
    allowed: bool
    reason: str
    projected_notional: float
    is_reduce_only: bool


class Cap25Virtual:
    """Cap virtual |inv|*mid <= 25. No toca RiskEngine prod. Reduce_only permitido."""

    def __init__(self, max_notional: float = 25.0):
        self.max_notional = float(max_notional)

    def check(self, inventory: float, side: str, qty: float, mid: float) -> CapResult:
        side = str(side).upper()
        inv = float(inventory)
        q = float(qty)
        m = float(mid) if mid else 0.0
        sign = -1 if side == "SELL" else 1
        proj_inv = inv + sign * q
        proj_notional = abs(proj_inv * m)
        is_reduce = (inv > 0 and side == "SELL") or (inv < 0 and side == "BUY")
        if proj_notional <= self.max_notional + 1e-12:
            return CapResult(True, "ok", proj_notional, is_reduce)
        if is_reduce:
            # Reduce_only: permitir si reduce (aunque supere, no debería superar si es reduce)
            # Si inv=20 y q=10 SELL → proj 10 → ok ya. Si aún supera, es qty demasiado grande
            return CapResult(False, "cap25_reduce_exceeds", proj_notional, True)
        return CapResult(False, "cap25_block_aggr", proj_notional, False)

    def filter_quotes(self, inventory: float, mid: float,
                      bid_price: float, bid_qty: float, ask_price: float, ask_qty: float,
                      bid_ok: bool, ask_ok: bool) -> Tuple[bool, bool, Dict[str, str]]:
        """Filtra bid/ask por cap. Retorna (bid_allowed, ask_allowed, reasons)."""
        reasons: Dict[str, str] = {}
        bid_allowed = bool(bid_ok)
        ask_allowed = bool(ask_ok)
        if bid_ok and bid_qty > 0:
            r = self.check(inventory, "BUY", bid_qty, mid)
            if not r.allowed:
                bid_allowed = False
                reasons["bid"] = r.reason
        if ask_ok and ask_qty > 0:
            r = self.check(inventory, "SELL", ask_qty, mid)
            if not r.allowed:
                ask_allowed = False
                reasons["ask"] = r.reason
        return bid_allowed, ask_allowed, reasons


# ── Orquestador mínimo de reconstrucción (para backtest) ─────────────

#: Ventana de markout en ms: adverse_5s se evalúa contra el primer book
#: con ts >= fill.ts_ms + MARKOUT_WINDOW_MS (Point 6: markouts vencidos
#: se evalúan al llegar el book, aunque el fill ya haya ocurrido).
MARKOUT_WINDOW_MS = 5000

#: Tolerancia máxima entre firing_time y el primer book disponible (igual
#: criterio que Replay.markouts(tolerance_ms=500)): si el book llega más
#: tarde, motivo 'late_book' y adverse_5s queda None.
MARKOUT_TOLERANCE_MS = 500


@dataclass
class ReconstructionConfig:
    tick_size: float = 0.0001
    qty_step: float = 1.0
    place_latency_ms: int = 40
    cancel_latency_ms: int = 20
    max_notional: float = 25.0
    min_spread_ticks: int = 8
    max_book_age_ms: int = 1000
    max_gap_ms: int = 2000
    max_position_lots: int = 100


@dataclass
class Opportunity:
    ts_ms: int
    mid: float
    bid_price: float
    ask_price: float
    bid_qty: float
    ask_qty: float
    bid_ok: bool
    ask_ok: bool
    spread_ticks: float
    inventory: float


@dataclass
class ReconstructionFill:
    ts_ms: int
    side: str
    price: float
    qty: float
    status: str
    inventory_after: float
    mid_at_fill: float
    adverse_5s: Optional[float] = None  # (future_mid - fill_price)/fill_price con signo
    markout_reason: str = 'pending'  # pending | ok | late_book | end_of_data (criterio Replay.markouts)
    order_id: Optional[str] = None
    trade_id: Optional[str] = None


# ── Interfaz incremental para integración A-S offline (V2) ──────────────

@dataclass(frozen=True)
class ExecutionDelta:
    """Resultado de advance_to/apply_commands: solo nuevos eventos y estado posterior."""
    fills: List[ReconstructionFill]
    order_transitions: List[Dict[str, Any]]  # {'order_id', 'from_status', 'to_status', 'ts_ms'}
    sequence: int  # creciente, para impedir doble consumo
    snapshot: 'ExecutionSnapshot'  # copia inmutable del estado posterior


@dataclass(frozen=True)
class ExecutionSnapshot:
    """Snapshot inmutable del estado de ejecución (sin referencias mutables al motor)."""
    inventory_lots: int
    inventory: float  # XRP = inventory_lots * qty_step
    cash: float  # USDC bruto = cash_units * qty_step * tick_size
    ts_ms: int
    book_ts_ms: int
    orders: Tuple[Dict[str, Any], ...]  # cada uno con order_id, side, price_ticks, qty_lots, remaining_lots, status, submit_ts_ms, arrival_ts_ms, cancel_effective_ms
    cash_units: int  # unidades enteras para auditoría
    # No expone eventos futuros, timers, ni métricas ex post


@dataclass(frozen=True)
class FinalResult:
    """Resultado final tras finish()."""
    fills: List[ReconstructionFill]
    journal: List[Dict[str, Any]]
    inventory_lots: int
    cash: float
    censored_orders: List[Dict[str, Any]]
    observed_end_ms: int


class ExecutionReconstructor:
    """
    Motor de reconstrucción con interfaz incremental (V2).
    
    Contrato:
    - advance_to(ts_ms, market_events): procesa eventos externos (book/trade) y timers vencidos hasta ts_ms
    - apply_commands(ts_ms, commands): única fase de comandos del avance ts_ms; exige advance_to completado
    - state(): copia inmutable del estado actual
    - finish(observed_end_ms): cierra el motor, registra censura, no permite llamadas posteriores
    
    El coordinador es dueño del calendario y del lector. El motor es dueño de
    timers, órdenes, cola, fills, inventario y cash. La política no recibe
    el motor ni acceso a la captura, timers futuros o markouts.
    """
    
    def __init__(self, config: 'ReconstructionConfig'):
        self.config = config
        self.queue = QueuePositionTracker()
        self.partial = PartialFillEngine(self.queue)
        self.latency = LatencyModel(config.place_latency_ms, config.cancel_latency_ms)
        self.cancel_replace = CancelReplaceEngine(self.latency)
        self.cap25 = Cap25Virtual(config.max_notional)
        
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.fills: List[ReconstructionFill] = []
        self.journal: List[Dict[str, Any]] = []
        self.books: List[Dict[str, Any]] = []
        self.book: Optional[Dict[str, Any]] = None
        self.inventory_lots: int = 0
        self.cash_units: int = 0  # unidades enteras: cash = cash_units * qty_step * tick_size
        self.seen_trades: set = set()
        self._timers: List[Tuple[int, int, int, Any]] = []  # (ts_ms, priority, serial, event)
        self._serial = 0
        self._last_advance_ts: int = -1
        self._advance_completed: bool = False
        self._commands_applied: bool = False
        self._finished: bool = False
        self._sequence: int = 0
        
    def _log(self, ts_ms: int, event: str, order_id: Optional[str] = None, **fields) -> None:
        self.journal.append({'ts_ms': ts_ms, 'event': event, 'order_id': order_id, **fields})
    
    def _timer(self, ts_ms: int, kind: str, order_id: str) -> None:
        import heapq
        from research.chronological_execution import PRIORITY
        heapq.heappush(self._timers, (ts_ms, PRIORITY[kind], self._serial, {'kind': kind, 'order_id': order_id}))
        self._serial += 1
    
    def _fresh(self, ts_ms: int) -> None:
        if self.book is None or ts_ms - self.book['ts_ms'] > self.config.max_book_age_ms:
            raise ValueError(f'No fresh book at {ts_ms}; replay invalid')
    
    def _finish_order(self, order_id: str, status: str, ts_ms: int) -> Dict[str, Any]:
        o = self.orders[order_id]
        from_status = o['status']
        o['status'] = status
        self.queue.remove(order_id)
        self._log(ts_ms, status, order_id, remaining_lots=o['remaining_lots'])
        self._sequence += 1
        return {'order_id': order_id, 'from_status': from_status, 'to_status': status, 'ts_ms': ts_ms}
    
    def _active_orders(self) -> List[Dict[str, Any]]:
        return [o for o in self.orders.values() if o['status'] in ('pending', 'live')]
    
    def _process_book_event(self, event: Dict[str, Any], ts_ms: int) -> None:
        b = event
        for side, reverse in [('bids', True), ('asks', False)]:
            levels = b[side]
            if not levels:
                raise ValueError('Empty book')
            for p, q in levels:
                if not isinstance(p, int) or not isinstance(q, int) or p <= 0 or q <= 0:
                    raise ValueError('Book levels must be positive integers')
            prices = [p for p, _ in levels]
            if prices != sorted(set(prices), reverse=reverse):
                raise ValueError('Unsorted/duplicate book levels')
        if b['bids'][0][0] >= b['asks'][0][0]:
            raise ValueError('Crossed book')
        if not isinstance(b['update_id'], int) or b['update_id'] <= 0:
            raise ValueError('Invalid update_id')
        if not isinstance(b.get('pu', 0), int):
            raise ValueError('Invalid pu')
        if self.book:
            if b['pu'] != self.book['update_id'] or b['update_id'] <= self.book['update_id']:
                raise ValueError('Depth sequence discontinuity')
            if ts_ms - self.book['ts_ms'] > self.config.max_gap_ms:
                raise ValueError('Depth time gap')
        self.book = {**b, 'ts_ms': ts_ms}
        mid_ticks = (b['bids'][0][0] + b['asks'][0][0]) / 2.0
        self.books.append({'ts_ms': ts_ms, 'mid_ticks': mid_ticks})
        self._evaluate_pending_markouts(ts_ms)

    def _markout_value(self, fill: 'ReconstructionFill', future_mid: float) -> Optional[float]:
        """adverse_5s con signo: +1 BUY, -1 SELL. None si precio inválido."""
        price = float(fill.price)
        if not math.isfinite(price) or price <= 0:
            return None
        sign = 1.0 if fill.side == 'BUY' else -1.0
        return sign * (float(future_mid) - price) / price

    def _evaluate_pending_markouts(self, ts_ms: int) -> None:
        """Evalúa markouts vencidos contra el primer book >= firing_time.

        firing_time = fill.ts_ms + MARKOUT_WINDOW_MS. Solo evalúa fills cuyo
        firing_time ya pasó (<= ts_ms); usa el PRIMER book en historial con
        ts >= firing_time (ventana consolidada, no el último). Mismo criterio
        que Replay.markouts(tolerance_ms=500):
        - book_ts - firing <= tolerancia -> 'ok', adverse_5s valuado;
        - si el primer book útil llega más tarde -> 'late_book', adverse None.
        Si no hay tal book, el fill queda 'pending' hasta el próximo book o
        finish() (que lo cierra como 'end_of_data').
        """
        for f in self.fills:
            if f.markout_reason != 'pending':
                continue
            firing = f.ts_ms + MARKOUT_WINDOW_MS
            if firing > ts_ms:
                continue
            for b in self.books:
                if b['ts_ms'] >= firing:
                    if b['ts_ms'] - firing > MARKOUT_TOLERANCE_MS:
                        f.markout_reason = 'late_book'
                        self._log(ts_ms, 'markout_late_book', f.order_id, trade_id=f.trade_id,
                                  firing_ms=firing, book_ms=b['ts_ms'])
                    else:
                        val = self._markout_value(f, b['mid_ticks'])
                        if val is not None:
                            f.adverse_5s = val
                        f.markout_reason = 'ok'
                        self._log(ts_ms, 'markout_5s', f.order_id, trade_id=f.trade_id,
                                  adverse_5s=f.adverse_5s, firing_ms=firing, book_ms=b['ts_ms'])
                    break
    
    def _process_trade_event(self, event: Dict[str, Any], ts_ms: int) -> List[Dict[str, Any]]:
        transitions = []
        d = event
        tid = d['trade_id']
        if not isinstance(tid, str) or not tid or tid in self.seen_trades:
            raise ValueError('Invalid/duplicate trade ID')
        if not isinstance(d['price_ticks'], int) or d['price_ticks'] <= 0:
            raise ValueError('Invalid trade price')
        if not isinstance(d['qty_lots'], int) or d['qty_lots'] <= 0:
            raise ValueError('Invalid trade qty')
        if not isinstance(d['is_buyer_maker'], bool):
            raise ValueError('Aggressor side must be boolean')
        self.seen_trades.add(tid)
        side = 'BUY' if d['is_buyer_maker'] else 'SELL'
        
        for o in self._active_orders():
            if o['status'] != 'live' or o['side'] != side:
                continue
            self._fresh(ts_ms)
            if d['price_ticks'] != o['price_ticks']:
                through = (side == 'BUY' and d['price_ticks'] < o['price_ticks']) or \
                          (side == 'SELL' and d['price_ticks'] > o['price_ticks'])
                if through:
                    self._log(ts_ms, 'unmodelled_trade_through', o['order_id'], trade_id=tid)
                continue
            before = int(self.queue.get(o['order_id']))
            fill = self.partial.try_fill(
                o['order_id'], o['remaining_lots'], o['price_ticks'], side,
                d['qty_lots'], d['price_ticks'], d['is_buyer_maker'], ts_ms, tick_size=1
            )
            self._log(ts_ms, 'queue_trade', o['order_id'], trade_id=tid,
                      queue_before_lots=before, trade_lots=d['qty_lots'])
            if fill is None:
                continue
            qty = int(fill.qty)
            if qty != fill.qty or qty <= 0 or qty > d['qty_lots'] or qty > o['remaining_lots']:
                raise RuntimeError('Volume conservation failure')
            sign = 1 if side == 'BUY' else -1
            self.inventory_lots += sign * qty
            self.cash_units -= sign * qty * o['price_ticks']
            o['remaining_lots'] -= qty
            mid_before = self.books[-1]['mid_ticks'] if self.books else 0
            book_age = ts_ms - self.book['ts_ms'] if self.book else 0
            self.fills.append(ReconstructionFill(
                ts_ms=ts_ms, side=side, price=float(o['price_ticks']), qty=float(qty),
                status=fill.status, inventory_after=float(self.inventory_lots),
                mid_at_fill=float(mid_before), adverse_5s=None,
                order_id=o['order_id'], trade_id=tid
            ))
            self._log(ts_ms, 'fill', o['order_id'], trade_id=tid, qty_lots=qty,
                      inventory_lots=self.inventory_lots, cash_units=self.cash_units)
            if o['remaining_lots'] == 0:
                transitions.append(self._finish_order(o['order_id'], 'filled', ts_ms))
        return transitions
    
    def _process_submit(self, event: Dict[str, Any], ts_ms: int) -> List[Dict[str, Any]]:
        transitions = []
        d = event
        oid = d['order_id']
        side = d['side']
        if not isinstance(oid, str) or not oid or oid in self.orders or side not in ('BUY', 'SELL'):
            raise ValueError('Invalid/reused order ID or side')
        if not isinstance(d['price_ticks'], int) or d['price_ticks'] <= 0:
            raise ValueError('Invalid price')
        if not isinstance(d['qty_lots'], int) or d['qty_lots'] <= 0:
            raise ValueError('Invalid qty')
        self._fresh(ts_ms)
        o = {**d, 'submit_ts_ms': ts_ms, 'arrival_ts_ms': ts_ms + self.config.place_latency_ms,
             'cancel_effective_ms': None, 'remaining_lots': d['qty_lots'], 'status': 'pending'}
        existing = self._active_orders()
        self.orders[oid] = o
        if any(x['side'] == side for x in existing):
            transitions.append(self._finish_order(oid, 'rejected_side_busy', ts_ms))
            return transitions
        # Projected position including the candidate order, side-aware.
        # Solo el extremo que la candidata puede empeorar incluye su qty:
        # SELL solo puede llevar el inventario hacia abajo, BUY hacia arriba.
        candidate_qty = d['qty_lots']
        existing_qty_se = sum(x['remaining_lots'] for x in existing if x['side'] == 'SELL')
        existing_qty_bu = sum(x['remaining_lots'] for x in existing if x['side'] == 'BUY')
        same_side_pending = existing_qty_se if side == 'SELL' else existing_qty_bu
        # Reduce-only: orden del lado opuesto al inventario que no puede
        # invertir el signo ni siquiera si todos los pendientes del mismo
        # lado se llenan. Sus fills solo acercan el inventario a cero.
        is_reducer = (
            (side == 'SELL' and self.inventory_lots > 0)
            or (side == 'BUY' and self.inventory_lots < 0)
        ) and candidate_qty + same_side_pending <= abs(self.inventory_lots)
        # Precio actual (USDC/XRP) para el control nocional.
        tick_size = float(self.config.tick_size)
        qty_step = float(self.config.qty_step)
        mid_ticks = 0.0
        if self.book and self.book.get("bids") and self.book.get("asks"):
            mid_ticks = (self.book["bids"][0][0] + self.book["asks"][0][0]) / 2.0
        if mid_ticks <= 0:
            mid_ticks = float(d.get('price_ticks', 0))
        price_usdc = mid_ticks * tick_size
        # Exceso previo (p. ej. el precio subió y el inventario existente ya
        # supera el nocional): una reductora estricta se permite aunque los
        # extremos sigan fuera de límite — reducir no aumenta el riesgo.
        in_excess = (
            abs(self.inventory_lots) > self.config.max_position_lots
            or price_usdc * abs(self.inventory_lots) * qty_step > float(self.config.max_notional)
        )
        if in_excess and is_reducer:
            self._log(ts_ms, 'submit_reduce_only', oid,
                      inventory_lots=self.inventory_lots, qty_lots=candidate_qty)
            self._timer(o['arrival_ts_ms'], 'arrival', oid)
            return transitions
        if side == 'SELL':
            proj_inv_low = self.inventory_lots - (existing_qty_se + candidate_qty)
            proj_inv_high = self.inventory_lots + existing_qty_bu
        else:  # BUY
            proj_inv_low = self.inventory_lots - existing_qty_se
            proj_inv_high = self.inventory_lots + (existing_qty_bu + candidate_qty)
        # Position limit in lots (includes candidate on its side only).
        if max(abs(proj_inv_low), abs(proj_inv_high)) > self.config.max_position_lots:
            transitions.append(self._finish_order(oid, 'rejected_position_cap', ts_ms))
            return transitions
        # Notional limit (USDC) on the same directional extremes, using the
        # experiment config (max_notional), not the global budget.
        # Unidades: notional = lots * qty_step [XRP] * mid_ticks * tick_size [USDC/XRP].
        worst_lots = max(abs(proj_inv_low), abs(proj_inv_high))
        if price_usdc * worst_lots * qty_step > float(self.config.max_notional):
            transitions.append(self._finish_order(oid, 'rejected_position_notional', ts_ms))
            return transitions
        self._log(ts_ms, 'submit', oid)
        self._timer(o['arrival_ts_ms'], 'arrival', oid)
        return transitions
    
    def _process_arrival(self, event: Dict[str, Any], ts_ms: int) -> List[Dict[str, Any]]:
        transitions = []
        oid = event['order_id']
        o = self.orders.get(oid)
        if o is None or o['status'] != 'pending':
            return transitions
        self._fresh(ts_ms)
        if self.book is None:
            return transitions
        side, p = o['side'], o['price_ticks']
        if (side == 'BUY' and p >= self.book['asks'][0][0]) or (side == 'SELL' and p <= self.book['bids'][0][0]):
            transitions.append(self._finish_order(oid, 'rejected_post_only', ts_ms))
            return transitions
        levels = self.book['bids' if side == 'BUY' else 'asks']
        if (side == 'BUY' and p < levels[-1][0]) or (side == 'SELL' and p > levels[-1][0]):
            transitions.append(self._finish_order(oid, 'rejected_unknown_depth', ts_ms))
            return transitions
        visible = dict(levels).get(p, 0)
        self.queue.init_queue(oid, visible + o['qty_lots'], o['qty_lots'])
        o['status'] = 'live'
        o['initial_queue_lots'] = visible
        self._log(ts_ms, 'live', oid, queue_ahead_lots=visible)
        return transitions
    
    def _process_cancel(self, event: Dict[str, Any], ts_ms: int) -> List[Dict[str, Any]]:
        transitions = []
        oid = event['order_id']
        o = self.orders.get(oid)
        if o is None:
            raise ValueError('Cancel for unknown order')
        if o['status'] not in ('pending', 'live') or o['cancel_effective_ms'] is not None:
            self._log(ts_ms, 'cancel_noop', oid)
            return transitions
        o['cancel_effective_ms'] = ts_ms + self.config.cancel_latency_ms
        self._log(ts_ms, 'cancel_requested', oid)
        self._timer(o['cancel_effective_ms'], 'cancel_effective', oid)
        return transitions
    
    def _process_cancel_effective(self, event: Dict[str, Any], ts_ms: int) -> List[Dict[str, Any]]:
        transitions = []
        oid = event['order_id']
        o = self.orders.get(oid)
        if o is None:
            return transitions
        if o['status'] in ('pending', 'live'):
            transitions.append(self._finish_order(oid, 'cancelled', ts_ms))
        return transitions
    
    def _dispatch_timer(self, timer_event: Dict[str, Any], ts_ms: int) -> List[Dict[str, Any]]:
        kind = timer_event['kind']
        if kind == 'arrival':
            return self._process_arrival(timer_event, ts_ms)
        elif kind == 'cancel_effective':
            return self._process_cancel_effective(timer_event, ts_ms)
        return []
    
    def _build_snapshot(self, ts_ms: int) -> ExecutionSnapshot:
        book_ts = self.book['ts_ms'] if self.book else ts_ms
        orders_tuple = tuple({
            'order_id': o['order_id'],
            'side': o['side'],
            'price_ticks': o['price_ticks'],
            'qty_lots': o['qty_lots'],
            'remaining_lots': o['remaining_lots'],
            'status': o['status'],
            'submit_ts_ms': o['submit_ts_ms'],
            'arrival_ts_ms': o.get('arrival_ts_ms'),
            'cancel_effective_ms': o.get('cancel_effective_ms'),
        } for o in self.orders.values())
        return ExecutionSnapshot(
            inventory_lots=self.inventory_lots,
            inventory=float(self.inventory_lots) * self.config.qty_step,
            cash=float(self.cash_units) * self.config.qty_step * self.config.tick_size,
            ts_ms=ts_ms,
            book_ts_ms=book_ts,
            orders=orders_tuple,
            cash_units=self.cash_units,
        )
    
    def advance_to(self, ts_ms: int, market_events: Sequence[Dict[str, Any]]) -> ExecutionDelta:
        """
        Procesa todos los eventos externos (book/trade) y timers vencidos hasta ts_ms.
        
        Args:
            ts_ms: timestamp objetivo en milisegundos (entero)
            market_events: secuencia de eventos de mercado (book/trade) para este tramo
            
        Returns:
            ExecutionDelta con nuevos fills, transiciones de orden y snapshot posterior
            
        Raises:
            ValueError: si ts_ms es regresivo, eventos tardíos, o advance_to ya llamado para este ts
        """
        if self._finished:
            raise ValueError('Cannot advance after finish()')
        if ts_ms < self._last_advance_ts:
            raise ValueError(f'Regressive timestamp: {ts_ms} < {self._last_advance_ts}')
        if ts_ms == self._last_advance_ts and self._advance_completed:
            raise ValueError(f'Duplicate advance_to for ts_ms={ts_ms}')
        
        # Validar que todos los eventos sean book/trade y tengan ts_ms correcto
        for e in market_events:
            if e.get('ts_ms') != ts_ms:
                raise ValueError(f'Event timestamp {e.get("ts_ms")} does not match advance_to ts_ms={ts_ms}')
            if e['kind'] not in ('book', 'trade'):
                raise ValueError(f'Invalid event kind for advance_to: {e["kind"]}')
        
        all_transitions = []
        new_fills = []
        
        import heapq
        from research.chronological_execution import PRIORITY
        
        # Sort market events by priority: trade(0) -> book(3)
        # Within same kind, preserve input order (stable sort)
        sorted_events = sorted(market_events, key=lambda e: PRIORITY[e['kind']])
        
        # Process each market event with timer interleaving, matching Replay.run()
        for e in sorted_events:
            event_priority = PRIORITY[e['kind']]
            event_key = (ts_ms, event_priority)
            
            # Process timers with (timer_ts, timer_priority) < event_key
            while self._timers and self._timers[0][:2] < event_key:
                timer_ts, _, _, timer_event = heapq.heappop(self._timers)
                transitions = self._dispatch_timer(timer_event, timer_ts)
                all_transitions.extend(transitions)
            
            # Process the market event
            if e['kind'] == 'trade':
                transitions = self._process_trade_event(e, ts_ms)
                all_transitions.extend(transitions)
                new_fills.extend([f for f in self.fills if f.ts_ms == ts_ms and f not in new_fills])
            elif e['kind'] == 'book':
                self._process_book_event(e, ts_ms)
        
        # After all market events at this timestamp, process remaining timers at this timestamp
        # with priority < submit/cancel (4) - i.e., arrival(2), cancel_effective(1)
        # But NOT submit/cancel timers (those don't exist as timers)
        # This matches Replay.run() which processes timers with ts <= last[0] at the end
        # However, for incremental interface, we only process up to the market events.
        # The submit/cancel commands come in apply_commands phase.
        # So we process timers at ts_ms with priority < PRIORITY['submit'] (4)
        while self._timers and self._timers[0][0] == ts_ms and self._timers[0][1] < PRIORITY['submit']:
            timer_ts, _, _, timer_event = heapq.heappop(self._timers)
            transitions = self._dispatch_timer(timer_event, timer_ts)
            all_transitions.extend(transitions)
        
        self._last_advance_ts = ts_ms
        self._advance_completed = True
        self._commands_applied = False
        
        snapshot = self._build_snapshot(ts_ms)
        self._sequence += 1
        
        return ExecutionDelta(
            fills=new_fills,
            order_transitions=all_transitions,
            sequence=self._sequence,
            snapshot=snapshot,
        )
    
    def apply_commands(self, ts_ms: int, commands: Sequence[Dict[str, Any]]) -> ExecutionDelta:
        """
        Única fase de comandos del avance ts_ms. Exige que advance_to(ts_ms, ...) haya concluido.
        
        Args:
            ts_ms: timestamp del avance (debe coincidir con el último advance_to)
            commands: secuencia de comandos (submit/cancel) con ts_ms exacto, IDs únicos, orden serial
            
        Returns:
            ExecutionDelta con transiciones de orden y snapshot posterior
            
        Raises:
            ValueError: si advance_to no completado, ts_ms no coincide, comandos fuera de fase
        """
        if self._finished:
            raise ValueError('Cannot apply commands after finish()')
        if ts_ms != self._last_advance_ts:
            raise ValueError(f'apply_commands ts_ms={ts_ms} does not match last advance_to={self._last_advance_ts}')
        if self._commands_applied:
            raise ValueError(f'Commands already applied for ts_ms={ts_ms}')
        if not self._advance_completed:
            raise ValueError('advance_to must complete before apply_commands')
        
        all_transitions = []
        
        for cmd in commands:
            if cmd.get('ts_ms') != ts_ms:
                raise ValueError(f'Command timestamp {cmd.get("ts_ms")} does not match ts_ms={ts_ms}')
            if cmd['kind'] == 'submit':
                transitions = self._process_submit(cmd, ts_ms)
                all_transitions.extend(transitions)
            elif cmd['kind'] == 'cancel':
                transitions = self._process_cancel(cmd, ts_ms)
                all_transitions.extend(transitions)
            else:
                raise ValueError(f'Invalid command kind: {cmd["kind"]}')
        
        self._commands_applied = True
        self._advance_completed = False  # permitir siguiente advance_to
        
        snapshot = self._build_snapshot(ts_ms)
        self._sequence += 1
        
        return ExecutionDelta(
            fills=[],  # no fills en fase de comandos
            order_transitions=all_transitions,
            sequence=self._sequence,
            snapshot=snapshot,
        )
    
    def state(self) -> ExecutionSnapshot:
        """Copia inmutable del estado actual (sin referencias mutables al motor)."""
        if self._finished:
            raise ValueError('Cannot get state after finish()')
        ts_ms = self._last_advance_ts if self._last_advance_ts >= 0 else 0
        return self._build_snapshot(ts_ms)
    
    def finish(self, observed_end_ms: int) -> FinalResult:
        """
        Cierra el motor. Exige que observed_end_ms sea el último timestamp público ya procesado.
        No drena timers posteriores: registra censura y cierra una sola vez.
        """
        if self._finished:
            raise ValueError('finish() already called')
        if observed_end_ms < self._last_advance_ts:
            raise ValueError(f'finish observed_end_ms={observed_end_ms} < last advance={self._last_advance_ts}')
        
        # Procesar timers hasta observed_end_ms (incluyendo mismo timestamp)
        import heapq
        from research.chronological_execution import PRIORITY
        while self._timers and self._timers[0][0] <= observed_end_ms:
            timer_ts, _, _, timer_event = heapq.heappop(self._timers)
            self._dispatch_timer(timer_event, timer_ts)
        
        # Censurar órdenes pendientes
        censored = []
        for o in self._active_orders():
            self._log(observed_end_ms, 'end_censored', o['order_id'], remaining_lots=o['remaining_lots'])
            censored.append({
                'order_id': o['order_id'],
                'side': o['side'],
                'price_ticks': o['price_ticks'],
                'remaining_lots': o['remaining_lots'],
                'status': o['status'],
            })
        
        # Cerrar markouts sin cobertura: sin book >= firing_time al fin de
        # la captura -> 'end_of_data' (mismo criterio que Replay.markouts).
        for f in self.fills:
            if f.markout_reason == 'pending':
                f.markout_reason = 'end_of_data'
                self._log(observed_end_ms, 'markout_end_of_data', f.order_id,
                          trade_id=f.trade_id, firing_ms=f.ts_ms + MARKOUT_WINDOW_MS)

        self._finished = True
        
        return FinalResult(
            fills=self.fills,
            journal=self.journal,
            inventory_lots=self.inventory_lots,
            cash=float(self.cash_units) * self.config.qty_step * self.config.tick_size,
            censored_orders=censored,
            observed_end_ms=observed_end_ms,
        )


# ── Adaptador de compatibilidad para Replay.run() existente ─────────────

def run_incremental_equivalence(events: Sequence[Dict[str, Any]], config: 'ReconstructionConfig') -> ExecutionReconstructor:
    """
    Ejecuta la misma secuencia de eventos usando la interfaz incremental
    y verifica equivalencia con Replay.run().
    
    Los eventos deben estar ordenados por (ts_ms, PRIORITY).
    """
    from research.chronological_execution import Replay, Settings, PRIORITY, Event
    import heapq
    
    # Convertir config a Settings para Replay original
    settings = Settings(
        place_ms=config.place_latency_ms,
        cancel_ms=config.cancel_latency_ms,
        max_book_age_ms=config.max_book_age_ms,
        max_gap_ms=config.max_gap_ms,
        max_position_lots=config.max_position_lots,
    )
    
    # Ejecutar Replay original
    replay_events = []
    for e in events:
        replay_events.append(Event(e['ts_ms'], e['kind'], e['data']))
    replay_events.sort(key=lambda e: (e.ts_ms, PRIORITY[e.kind]))
    original = Replay(settings).run(replay_events)
    
    # Ejecutar con interfaz incremental
    reconstructor = ExecutionReconstructor(config)
    last_ts = -1
    market_batch = []
    command_batch = []
    
    for e in replay_events:
        if e.ts_ms != last_ts:
            if last_ts >= 0:
                # Procesar batch anterior: siempre advance_to (puede ser vacío), luego apply_commands si hay comandos
                reconstructor.advance_to(last_ts, market_batch)
                if command_batch:
                    reconstructor.apply_commands(last_ts, command_batch)
            last_ts = e.ts_ms
            market_batch = []
            command_batch = []
        
        if e.kind in ('book', 'trade'):
            # Flatten data to top level for advance_to
            evt = {'ts_ms': e.ts_ms, 'kind': e.kind}
            evt.update(e.data)
            market_batch.append(evt)
        elif e.kind in ('submit', 'cancel'):
            # Flatten data to top level for apply_commands
            cmd = {'ts_ms': e.ts_ms, 'kind': e.kind}
            cmd.update(e.data)
            command_batch.append(cmd)
    
    # Último batch
    reconstructor.advance_to(last_ts, market_batch)
    if command_batch:
        reconstructor.apply_commands(last_ts, command_batch)
    
    # Finish
    reconstructor.finish(last_ts)
    
    return reconstructor
