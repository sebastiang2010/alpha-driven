"""Offline execution reconstruction. No network, strategy config or account imports.

Integer ticks/lots, one outstanding order per side. Reuses the existing FIFO
partial-fill primitive. Queue starts behind all displayed volume (the shadow
order is NOT part of public depth). Later snapshots never decrement queue:
only exact-price aggressive trades do. This deliberately omits cancellation
advancement and trade-through fills; it is an assumption, not an exchange model.

Equal timestamps: trades -> effective cancels -> arrivals -> books -> commands.
Thus a trade can beat a cancel at the same ms, but cannot fill a new arrival.
Books/commands within the same category keep input order. Gaps abort the run;
there is no invented flatten or automatic inventory reset across missing data.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, asdict
import heapq
from itertools import count
from typing import Iterable

from strategy.execution_reconstruction import PartialFillEngine, QueuePositionTracker

MAX_UNITS = 10**12  # Exact integer arithmetic inside the float-based primitive.
PRIORITY = {'trade': 0, 'cancel_effective': 1, 'arrival': 2, 'book': 3, 'submit': 4, 'cancel': 4}


def integer(value, *, positive=False, limit=MAX_UNITS):
    if type(value) is not int or not (0 < value <= limit if positive else 0 <= value <= limit):
        raise ValueError('Expected bounded nonnegative integer units')


@dataclass(frozen=True)
class Settings:
    place_ms: int
    cancel_ms: int
    max_book_age_ms: int
    max_gap_ms: int
    max_position_lots: int

    def __post_init__(self):
        for value in asdict(self).values():
            integer(value, positive=True)


@dataclass(frozen=True)
class Event:
    ts_ms: int
    kind: str
    data: dict


class CoverageError(ValueError):
    pass


class Replay:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue = QueuePositionTracker()
        self.partial = PartialFillEngine(self.queue)
        self.orders = {}
        self.fills = []
        self.journal = []
        self.books = []
        self.book = None
        self.inventory = 0
        self.cash = 0  # quote currency / (tick_size * qty_step); gross only
        self.seen_trades = set()
        self._timers = []
        self._serial = count()
        self._used = False
        self._complete = False

    def _log(self, ts, event, oid=None, **fields):
        self.journal.append(dict(ts_ms=ts, event=event, order_id=oid, **fields))

    def _timer(self, ts, kind, oid):
        heapq.heappush(self._timers, (ts, PRIORITY[kind], next(self._serial), Event(ts, kind, {'order_id': oid})))

    def _fresh(self, ts):
        if self.book is None or ts - self.book['ts_ms'] > self.settings.max_book_age_ms:
            raise CoverageError(f'No fresh book at {ts}; replay invalid, not a cancellation')

    def _finish(self, o, status, ts):
        o['status'] = status
        self.queue.remove(o['order_id'])
        self._log(ts, status, o['order_id'], remaining_lots=o['remaining_lots'])

    def _active(self):
        return [o for o in self.orders.values() if o['status'] in ('pending', 'live')]

    def _book_event(self, e):
        b = e.data
        for side, reverse in [('bids', True), ('asks', False)]:
            levels = b[side]
            if not levels:
                raise ValueError('Empty book')
            for p, q in levels:
                integer(p, positive=True)
                integer(q, positive=True)
            prices = [p for p, _ in levels]
            if prices != sorted(set(prices), reverse=reverse):
                raise ValueError('Unsorted/duplicate book levels')
        if b['bids'][0][0] >= b['asks'][0][0]:
            raise ValueError('Crossed book')
        integer(b['update_id'], positive=True, limit=2**63-1)
        integer(b['pu'], limit=2**63-1)
        if self.book:
            if b['pu'] != self.book['update_id'] or b['update_id'] <= self.book['update_id']:
                raise CoverageError('Depth sequence discontinuity')
            if e.ts_ms - self.book['ts_ms'] > self.settings.max_gap_ms:
                raise CoverageError('Depth time gap')
        self.book = {**b, 'ts_ms': e.ts_ms}
        self.books.append({'ts_ms': e.ts_ms, 'mid_ticks': (b['bids'][0][0]+b['asks'][0][0])/2})

    def _submit(self, e):
        d, ts = e.data, e.ts_ms
        oid, side = d['order_id'], d['side']
        if not isinstance(oid, str) or not oid or oid in self.orders or side not in ('BUY', 'SELL'):
            raise ValueError('Invalid/reused order ID or side')
        integer(d['price_ticks'], positive=True)
        integer(d['qty_lots'], positive=True)
        self._fresh(ts)
        o = {**d, 'submit_ts_ms': ts, 'arrival_ts_ms': ts+self.settings.place_ms,
             'cancel_effective_ms': None, 'remaining_lots': d['qty_lots'], 'status': 'pending'}
        existing = self._active()
        self.orders[oid] = o
        if any(x['side'] == side for x in existing):
            self._finish(o, 'rejected_side_busy', ts)
            return
        # Both one-sided fill extremes reserve pending+live quantities.
        low = self.inventory - sum(x['remaining_lots'] for x in self._active() if x['side'] == 'SELL')
        high = self.inventory + sum(x['remaining_lots'] for x in self._active() if x['side'] == 'BUY')
        if max(abs(low), abs(high)) > self.settings.max_position_lots:
            self._finish(o, 'rejected_position_cap', ts)
            return
        self._log(ts, 'submit', oid)
        self._timer(o['arrival_ts_ms'], 'arrival', oid)

    def _arrive(self, e):
        o = self.orders[e.data['order_id']]
        if o['status'] != 'pending':
            return
        self._fresh(e.ts_ms)
        side, p = o['side'], o['price_ticks']
        if (side == 'BUY' and p >= self.book['asks'][0][0]) or (side == 'SELL' and p <= self.book['bids'][0][0]):
            self._finish(o, 'rejected_post_only', e.ts_ms)
            return
        levels = self.book['bids' if side == 'BUY' else 'asks']
        if (side == 'BUY' and p < levels[-1][0]) or (side == 'SELL' and p > levels[-1][0]):
            self._finish(o, 'rejected_unknown_depth', e.ts_ms)
            return
        visible = dict(levels).get(p, 0)
        # Legacy init subtracts our_qty. Add it back ONLY in this adapter:
        # public depth excludes our hypothetical order; initial queue = visible.
        self.queue.init_queue(o['order_id'], visible + o['qty_lots'], o['qty_lots'])
        o['status'] = 'live'
        o['initial_queue_lots'] = visible
        self._log(e.ts_ms, 'live', o['order_id'], queue_ahead_lots=visible)

    def _cancel(self, e):
        o = self.orders.get(e.data['order_id'])
        if o is None:
            raise ValueError('Cancel for unknown order')
        if o['status'] not in ('pending', 'live') or o['cancel_effective_ms'] is not None:
            self._log(e.ts_ms, 'cancel_noop', o['order_id'])
            return
        o['cancel_effective_ms'] = e.ts_ms + self.settings.cancel_ms
        self._log(e.ts_ms, 'cancel_requested', o['order_id'])
        self._timer(o['cancel_effective_ms'], 'cancel_effective', o['order_id'])

    def _trade(self, e):
        d = e.data
        tid = d['trade_id']
        if not isinstance(tid, str) or not tid or tid in self.seen_trades:
            raise ValueError('Invalid/duplicate trade ID')
        integer(d['price_ticks'], positive=True)
        integer(d['qty_lots'], positive=True)
        if type(d['is_buyer_maker']) is not bool:
            raise ValueError('Aggressor side must be an observed boolean')
        self.seen_trades.add(tid)
        side = 'BUY' if d['is_buyer_maker'] else 'SELL'
        for o in self._active():
            if o['status'] != 'live' or o['side'] != side:
                continue
            self._fresh(e.ts_ms)
            if d['price_ticks'] != o['price_ticks']:
                through = (side == 'BUY' and d['price_ticks'] < o['price_ticks']) or (side == 'SELL' and d['price_ticks'] > o['price_ticks'])
                if through:
                    self._log(e.ts_ms, 'unmodelled_trade_through', o['order_id'], trade_id=tid)
                continue
            before = int(self.queue.get(o['order_id']))
            fill = self.partial.try_fill(o['order_id'], o['remaining_lots'], o['price_ticks'], side,
                                         d['qty_lots'], d['price_ticks'], d['is_buyer_maker'], e.ts_ms, tick_size=1)
            self._log(e.ts_ms, 'queue_trade', o['order_id'], trade_id=tid,
                      queue_before_lots=before, trade_lots=d['qty_lots'])
            if fill is None:
                continue
            qty = int(fill.qty)
            if qty != fill.qty or qty <= 0 or qty > d['qty_lots'] or qty > o['remaining_lots']:
                raise RuntimeError('Volume conservation failure')
            sign = 1 if side == 'BUY' else -1
            self.inventory += sign*qty
            self.cash -= sign*qty*o['price_ticks']
            o['remaining_lots'] -= qty
            self.fills.append(dict(order_id=o['order_id'], trade_id=tid, ts_ms=e.ts_ms,
                                   side=side, price_ticks=o['price_ticks'], qty_lots=qty,
                                   mid_before_ticks=self.books[-1]['mid_ticks'],
                                   book_age_ms=e.ts_ms-self.book['ts_ms'],
                                   inventory_lots=self.inventory, kind='reconstructed_exact_price'))
            if not o['remaining_lots']:
                self._finish(o, 'filled', e.ts_ms)

    def _dispatch(self, e):
        if e.kind == 'book': self._book_event(e)
        elif e.kind == 'trade': self._trade(e)
        elif e.kind == 'submit': self._submit(e)
        elif e.kind == 'cancel': self._cancel(e)
        elif e.kind == 'arrival': self._arrive(e)
        elif e.kind == 'cancel_effective':
            o = self.orders[e.data['order_id']]
            if o['status'] in ('pending', 'live'):
                self._finish(o, 'cancelled', e.ts_ms)

    def run(self, events: Iterable[Event]):
        """Events must be sorted by (timestamp, PRIORITY); one run per instance."""
        if self._used:
            raise ValueError('Replay instance already used')
        self._used = True
        last = (-1, -1)
        for e in events:
            if type(e.ts_ms) is not int or e.ts_ms < 0 or e.kind not in ('book', 'trade', 'submit', 'cancel'):
                raise ValueError('Invalid input event')
            key = (e.ts_ms, PRIORITY[e.kind])
            if key < last:
                raise ValueError('Out-of-order input')
            while self._timers and self._timers[0][:2] < key:
                self._dispatch(heapq.heappop(self._timers)[3])
            self._dispatch(e)
            last = key
        # Finish same-ms timers after the last external event, but never advance
        # beyond the observed end. A cancel tying the last trade still takes effect.
        while self._timers and self._timers[0][0] <= last[0]:
            self._dispatch(heapq.heappop(self._timers)[3])
        # No time advancement beyond the recorded interval. Pending timers and
        # orders are censored, NOT magically filled or cancelled at EOF.
        for o in self._active():
            self._log(last[0], 'end_censored', o['order_id'], remaining_lots=o['remaining_lots'])
        self._complete = True
        return self

    def markouts(self, horizons_ms=(1000, 5000, 30000, 60000, 300000), tolerance_ms=500):
        if not self._complete:
            raise ValueError('Markouts require a completed, valid replay')
        integer(tolerance_ms)
        for h in horizons_ms:
            integer(h, positive=True)
        times = [b['ts_ms'] for b in self.books]
        rows = []
        for f in self.fills:
            sign = 1 if f['side'] == 'BUY' else -1
            spread = sign*(f['mid_before_ticks']-f['price_ticks'])/f['price_ticks']*10000
            for h in horizons_ms:
                target = f['ts_ms']+h
                ix = bisect_left(times, target)
                reason = 'end_of_data' if ix == len(times) else ('late_book' if times[ix]-target > tolerance_ms else 'ok')
                future = self.books[ix]['mid_ticks'] if reason == 'ok' else None
                rows.append(dict(order_id=f['order_id'], trade_id=f['trade_id'], fill_ts_ms=f['ts_ms'],
                                 horizon_ms=h, reason=reason, future_ts_ms=times[ix] if reason == 'ok' else None,
                                 spread_bps=spread,
                                 markout_bps=None if future is None else sign*(future-f['price_ticks'])/f['price_ticks']*10000,
                                 adverse_move_bps=None if future is None else -sign*(future-f['mid_before_ticks'])/f['price_ticks']*10000))
        return rows
