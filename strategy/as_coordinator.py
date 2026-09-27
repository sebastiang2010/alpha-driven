# as_coordinator.py — Offline coordinator for A-S strategy integration (Step 3).
# Owns the calendar and event reader. Uses ExecutionReconstructor as black box.
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Any

from strategy.execution_reconstruction import (
    ExecutionReconstructor,
    ExecutionSnapshot,
    FinalResult,
    ReconstructionConfig,
)
from strategy.calendar import (
    check_book_coverage,
    cycle_grid,
    merged_steps,
    validate_interval_ms,
)
from strategy.alpha_model import AlphaModel
from strategy.market_state import (
    ALPHA_WINDOW_SEC,
    VOLATILITY_WINDOW_SEC,
    MarketState,
)

try:
    from strategy import config as _strategy_config
except ImportError:  # pragma: no cover - mismo fallback que market_state
    import config as _strategy_config


def _warmup_span_sec():
    """Cobertura mínima de warm-up: la ventana más larga entre señales
    (MarketState) y filtro de momentum del AlphaModel (config)."""
    return max(
        float(VOLATILITY_WINDOW_SEC),
        float(ALPHA_WINDOW_SEC),
        float(getattr(_strategy_config, "MOMENTUM_WINDOW_SECONDS", 30.0)),
    )


# ── Coordinator configuration ─────────────────────────────────────────

@dataclass
class ASCoordinatorConfig:
    """Configuración del ASCoordinator."""
    decision_interval_ms: int = 5000
    warmup_intervals: int = 3
    max_gap_ratio: float = 2.0


# ── Main coordinator ──────────────────────────────────────────────────

class ASCoordinator:
    """
    Offline coordinator that owns the calendar and event reader.
    The ExecutionReconstructor is a black box.

    Responsibilities:
    - Owns the deterministic calendar: t0 + k*D (D = decision_interval_ms, default 5s)
    - Walks the merged calendar (scheduled cycles ∪ event timestamps): cycles run
      even without market events (empty advance drains expired timers first),
      and no trade/book is ever skipped
    - Warm-up (V1): requires warmup_intervals mids AND window coverage
      (span >= longest signal/momentum window); group mids also feed the
      shared AlphaModel history, so §14 never starts empty
    - No decisions after EOF; event gaps invalidate the run without resetting inventory
    - Event group closes completely before delivering; never filters future to policy
    - Delayed replacement: if a decision cancels a side, that side stays "occupied"
      until the next scheduled cycle (no immediate re-submit).
    - Market signals (F1.3): owns a REAL MarketState (no WS started) fed with
      the same causal events the engine consumes (books/trades from t0, prices
      converted ticks→USDC, qty lots→XRP). The A-S policy reads
      MarketState.get_snapshot(now_sec) with the EXPLICIT cycle time — windows
      expire against the simulated clock (including empty cycles) — so
      imbalance/microprice/momentum/volatility use the exact production
      formulas (no parallel estimators) with non-destructive queries (V3).
      Same-timestamp books: all validated, only the last incorporated (V4).
    """

    def __init__(
        self,
        config: Any,
        engine: ExecutionReconstructor,
        depth_csv: Sequence[Dict[str, Any]],
        trades_csv: Sequence[Dict[str, Any]],
        decision_interval_ms: int = 5000,
        warmup_intervals: int = 3,
    ):
        self.config = config
        self.engine = engine
        self.decision_interval_ms = validate_interval_ms(decision_interval_ms)
        self.warmup_intervals = warmup_intervals
        self._cfg = ASCoordinatorConfig(
            decision_interval_ms=decision_interval_ms,
            warmup_intervals=warmup_intervals,
        )

        # Derive t0 from the FIRST BOOK (F1.2): a leading trade must not shift
        # the decision grid. Earlier trades are still loaded and processed.
        # Each event dict has keys: ts_ms (int), kind (str: "book"|"trade"), data (dict).
        self._market_events = self._load_market_events(depth_csv, trades_csv)
        books = [e["ts_ms"] for e in self._market_events if e["kind"] == "book"]
        if books:
            self._t0: int = books[0]
        elif self._market_events:
            self._t0 = self._market_events[0]["ts_ms"]
        else:
            self._t0 = 0
        self._last_processed_ts: int = -1
        self._current_ts: int = self._t0
        self._cycle_counter: int = 0
        self._finished: bool = False

        # Warm-up state
        self._warmup_complete: bool = False
        self._warmup_mids: List[float] = []
        self._best_bid: float = 0.0
        self._best_ask: float = 0.0

        # F1.3: real signal stack, driven by the SIMULATED clock. MarketState
        # is fed exclusively from the causal event stream (never wall-clock,
        # WS never started); one shared AlphaModel accumulates record_mid
        # once per decision cycle (contract §14).
        symbol = (
            getattr(config, "symbol", None)
            or getattr(getattr(engine, "config", None), "symbol", None)
            or "xrpusdc"
        )
        self._market_state = MarketState(str(symbol))
        self._alpha_model = AlphaModel()

        # Delayed replacement: track which sides are "occupied" after a cancel
        self._side_occupied: Dict[str, bool] = {}

    # ── CSV loading ────────────────────────────────────────────────────

    def _load_market_events(
        self, depth_csv: Sequence[Dict[str, Any]], trades_csv: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Load and normalize market events from CSV row data.

        depth_rows: dicts with at least {ts_ms, bids, asks}
        trade_rows: dicts with at least {ts_ms, trade_id, price_ticks, qty_lots, is_buyer_maker}

        Returns plain dicts with keys: ts_ms, kind, data
        The data format matches what ExecutionReconstructor.advance_to expects:
        - book data: {'bids': [...], 'asks': [...], 'best_bid': float, 'best_ask': float}
        - trade data: {'trade_id': str, 'price_ticks': int, 'qty_lots': int, 'is_buyer_maker': bool}
        """
        raw_events: List[Dict[str, Any]] = []

        # Depth events (books)
        # ExecutionReconstructor requires update_id (int > 0, strictly increasing)
        # and pu == previous update_id for depth-sequence continuity. If the row
        # does not provide them (synthetic tests, minimal captures), synthesize a
        # monotone sequence: update_id = seq+1, pu = seq.
        seq = 0
        for row in depth_csv:
            ts_ms = int(row.get("ts_ms", 0))
            bids = row.get("bids", [])
            asks = row.get("asks", [])
            if not bids or not asks:
                continue  # skip invalid depth rows
            try:
                best_bid = float(bids[0][0])
                best_ask = float(asks[0][0])
            except (IndexError, ValueError):
                continue
            update_id = int(row.get("update_id") or 0)
            pu = int(row.get("pu") or 0)
            if update_id <= 0:
                update_id = seq + 1
                pu = seq
            seq = update_id
            # Store full book data for ExecutionReconstructor, plus best_bid/ask for coordinator
            book_data = {
                "bids": bids,
                "asks": asks,
                "update_id": update_id,
                "pu": pu,
                "best_bid": best_bid,
                "best_ask": best_ask,
            }
            raw_events.append({
                "ts_ms": ts_ms,
                "kind": "book",
                "data": book_data,
            })

        # Trade events
        for row in trades_csv:
            ts_ms = int(row.get("ts_ms", 0))
            trade_data = {
                "trade_id": str(row.get("trade_id", "")),
                "price_ticks": int(row.get("price_ticks", 0)),
                "qty_lots": int(row.get("qty_lots", 0)),
                "is_buyer_maker": bool(row.get("is_buyer_maker", False)),
            }
            raw_events.append({
                "ts_ms": ts_ms,
                "kind": "trade",
                "data": trade_data,
            })

        # Sort by timestamp (stable sort preserves input order for same ts)
        raw_events.sort(key=lambda e: e["ts_ms"])
        return raw_events

    # ── Causal signal feed (F1.3) ────────────────────────────────────

    def _feed_market_state(
        self, ts_ms: int, events: Sequence[Dict[str, Any]]
    ) -> None:
        """Feed the owned MarketState with already-consumed causal events.

        Must be called AFTER a successful engine.advance_to(ts, events): the
        same flattened event dicts (book/trade with top-level keys). Unit
        conversion with the ENGINE config (ticks→USDC via tick_size,
        lots→XRP via qty_step — never unit ticks). Both bookticker (best
        bid/ask → mid samples for volatility/momentum) and full depth
        (levels → imbalance) are fed per book event.

        V4 (varios libros del mismo ts): TODOS los libros del grupo se
        validan (parseo; el inválido se salta), pero solo el ÚLTIMO válido
        se incorpora: una muestra de mid y un depth por grupo temporal.
        Los trades son eventos distintos y se alimentan todos, en orden.
        """
        eng_cfg = getattr(self.engine, "config", None)
        tick_size = float(getattr(eng_cfg, "tick_size", 0.0001) or 0.0001)
        qty_step = float(getattr(eng_cfg, "qty_step", 1.0) or 1.0)
        last_book = None
        for e in events:
            kind = e.get("kind")
            if kind == "book":
                bids = e.get("bids", []) or []
                asks = e.get("asks", []) or []
                if not bids or not asks:
                    continue
                try:
                    bb = float(bids[0][0]) * tick_size
                    ba = float(asks[0][0]) * tick_size
                    bq = float(bids[0][1]) * qty_step
                    aq = float(asks[0][1]) * qty_step
                    bids_usdc = [
                        [float(p) * tick_size, float(q) * qty_step]
                        for p, q in bids
                    ]
                    asks_usdc = [
                        [float(p) * tick_size, float(q) * qty_step]
                        for p, q in asks
                    ]
                except (IndexError, TypeError, ValueError):
                    continue
                # Validado: solo el último del grupo se incorpora (V4).
                last_book = (bb, bq, ba, aq, bids_usdc, asks_usdc)
            elif kind == "trade":
                try:
                    price_usdc = float(e.get("price_ticks", 0)) * tick_size
                    qty_xrp = float(e.get("qty_lots", 0)) * qty_step
                except (TypeError, ValueError):
                    continue
                self._market_state.update_trade(
                    price_usdc, qty_xrp,
                    bool(e.get("is_buyer_maker", False)), ts_ms,
                )
        if last_book is not None:
            bb, bq, ba, aq, bids_usdc, asks_usdc = last_book
            self._market_state.update_bookticker(bb, bq, ba, aq, ts_ms)
            self._market_state.update_depth(
                bids_usdc, asks_usdc, len(bids_usdc), ts_ms
            )

    # ── Warm-up phase ───────────────────────────────────────────────────

    def _warmup_ready(self) -> bool:
        """Cobertura de ventanas: conteo mínimo Y span temporal.

        V1: warmup_intervals mids ya no bastan — se exige además que el span
        (último menos primer mid, en segundos) cubra la ventana más larga
        entre señales y filtro de momentum. Sin cobertura no hay submits.
        """
        if len(self._warmup_mids) < self.warmup_intervals:
            return False
        span = self._warmup_ts[-1] - self._warmup_ts[0]
        return span >= _warmup_span_sec()

    def _warmup_phase(self) -> None:
        """Warm-up: advance advance_to() without apply_commands until having
        BOTH warmup_intervals mids AND window coverage (span >= longest
        signal/momentum window). One mid sample per timestamp group (V4);
        each group mid also feeds the shared AlphaModel history via
        record_mid (V1: el filtro §14 llega con historial a la primera
        decisión — nunca empieza vacío tras el warm-up).

        After warmup, _warmup_complete=True and the main loop may emit decisions.
        The phase is idempotent: a second call returns immediately (the engine
        already consumed those events and rejects duplicate advance_to).

        If the data is exhausted before coverage, the warm-up ends
        best-effort WITHOUT raising: _warmup_complete stays False and the
        main loop still processes the remaining events (no decisions).
        """
        if getattr(self, "_warmup_done", False):
            return  # already executed — engine state must not be re-advanced

        self._warmup_mids = []
        self._warmup_ts: List[float] = []
        self._warmup_complete = False
        self._warmup_done = True
        self._best_bid = 0.0
        self._best_ask = 0.0
        self._side_occupied = {}

        i = 0
        n = len(self._market_events)

        # Process events timestamp by timestamp until window coverage
        while i < n and not self._warmup_ready():
            ts_ms = self._market_events[i]["ts_ms"]

            # Collect all events at this timestamp
            events_at_ts = []
            while i < n and self._market_events[i]["ts_ms"] == ts_ms:
                events_at_ts.append(self._market_events[i])
                i += 1

            # Extract market events (book + trade), flattened to top-level keys
            # (ts_ms, kind, ...) as expected by ExecutionReconstructor.advance_to
            market_events_for_advance = [
                {"ts_ms": ts_ms, "kind": e["kind"], **e["data"]}
                for e in events_at_ts
                if e["kind"] in ("book", "trade")
            ]

            # Advance to this timestamp (no apply_commands during warmup)
            self.engine.advance_to(ts_ms, market_events_for_advance)
            self._warmup_last_ts = ts_ms
            # Same causal events feed the signal stack (one sample per
            # timestamp group — V4 inside _feed_market_state)
            self._feed_market_state(ts_ms, market_events_for_advance)

            # Update best bid/ask from the engine's internal book
            if getattr(self.engine, "book", None) and self.engine.book:
                bids = self.engine.book.get("bids", [])
                asks = self.engine.book.get("asks", [])
                if bids:
                    self._best_bid = float(bids[0][0])
                if asks:
                    self._best_ask = float(asks[0][0])

            # Mid consistente con la ruta de decisión: el del snapshot con
            # tiempo explícito (V2). Una muestra por grupo (V4).
            ts_sec = float(ts_ms) / 1000.0
            mid = self._market_state.get_snapshot(ts_sec).get("mid") or 0.0
            if mid > 0:
                self._warmup_mids.append(float(mid))
                self._warmup_ts.append(ts_sec)
                # Historial AlphaModel alimentado DURANTE el warm-up (V1)
                self._alpha_model.record_mid(ts_sec, float(mid))

        # Warm-up completo solo con conteo Y cobertura de ventanas
        self._warmup_complete = self._warmup_ready()

    # ── A-S decision with real signals (F1.3) ──────────────────────────
    def _as_decision(self, snapshot: ExecutionSnapshot) -> List[Dict[str, Any]]:
        """Generate submit/cancel commands using real Avellaneda-Stoikov quotes
        driven by real causal signals.

        Pipeline (canonical AlphaModel usage, no parallel estimators):
        - signals = self._market_state.get_snapshot() (USDC mid/spread/
          microprice, dimensionless imbalance/momentum/volatility-sigma_ref,
          XRP trade-flow volumes; windows pruned against the SIMULATED clock)
        - alpha = AlphaModel.compute_alpha(signals)
        - record_mid(now_sec, mid) once per decision cycle (contract §14),
          THEN quote_distances(..., now_sec=simulated) — explicit sim time,
          never wall-clock
        - r = reservation_price(signals, alpha, inventory_XRP, sigma_ref);
          bid = r - bid_dist, ask = r + ask_dist (USDC)
        - USDC→ticks via engine tick_size (never bare round()); maker guard
          against the engine's current bests (ticks); inventory side rule
          (0→both, >0 SELL only, <0 BUY only) with delayed-replacement
          occupancy.
        """
        commands: List[Dict[str, Any]] = []
        inventory_lots = snapshot.inventory_lots
        eng_cfg = getattr(self.engine, "config", None)
        tick_size = float(getattr(eng_cfg, "tick_size", 0.0001) or 0.0001)
        qty_step = float(getattr(eng_cfg, "qty_step", 1.0) or 1.0)
        # Explicit simulated time (seconds); quote_distances must never fall
        # back to time.time() in offline replays.
        now_sec = float(snapshot.ts_ms) / 1000.0

        am = self._alpha_model
        # V2: tiempo explícito del ciclo — las ventanas vencen contra el
        # reloj simulado (en ciclos vacíos las observaciones viejas expiran;
        # pasar now_sec solo a quote_distances no corregiría las señales).
        ms_snap = self._market_state.get_snapshot(now_sec)
        mid = ms_snap.get("mid") or 0.0
        if mid <= 0:
            return []
        sigma_ref = max(float(ms_snap.get("volatility") or 0.0), 0.0)

        snap_dict: Dict[str, object] = {
            "mid": float(mid),
            "spread": float(ms_snap.get("spread") or 0.0),
            "momentum": float(ms_snap.get("momentum") or 0.0),
            "imbalance": float(ms_snap.get("imbalance") or 0.0),
            "microprice": float(ms_snap.get("microprice") or 0.0),
            "buy_volume_60s": float(ms_snap.get("buy_volume_60s") or 0.0),
            "sell_volume_60s": float(ms_snap.get("sell_volume_60s") or 0.0),
            "tick_size": tick_size,
        }
        inventory_xrp = float(inventory_lots) * qty_step
        alpha = am.compute_alpha(snap_dict)
        am.record_mid(now_sec, float(mid))
        r = am.reservation_price(snap_dict, alpha, inventory_xrp, sigma_ref)
        bid_dist, ask_dist = am.quote_distances(
            snap_dict, alpha, inventory_xrp, sigma_ref, now_sec=now_sec
        )
        as_bid_ticks = int(round((r - bid_dist) / tick_size))
        as_ask_ticks = int(round((r + ask_dist) / tick_size))

        # Engine bests in ticks for the maker guard (post-only GTX:
        # a quote that already crosses can never rest — don't emit it).
        best_bid_ticks = 0
        best_ask_ticks = 0
        book = getattr(getattr(self, "engine", None), "book", None)
        if book:
            bids = book.get("bids", []) or []
            asks = book.get("asks", []) or []
            if bids and asks:
                try:
                    best_bid_ticks = int(bids[0][0])
                    best_ask_ticks = int(asks[0][0])
                except (IndexError, TypeError, ValueError):
                    best_bid_ticks = 0
                    best_ask_ticks = 0

        def is_occupied(side: str) -> bool:
            return bool(self._side_occupied.get(side, False))

        def emit(side: str, price_ticks: int) -> None:
            commands.append(
                {
                    "ts_ms": int(snapshot.ts_ms),
                    "kind": "submit",
                    "order_id": f"{'buy' if side == 'BUY' else 'sell'}_{self._cycle_counter}",
                    "side": side,
                    "price_ticks": price_ticks,
                    "qty_lots": 1,
                }
            )

        # Inventory side rule with maker guard per side.
        if inventory_lots == 0:
            if (best_bid_ticks > 0 and best_ask_ticks > 0
                    and not is_occupied("BUY")
                    and as_bid_ticks > 0 and as_bid_ticks < best_ask_ticks):
                emit("BUY", as_bid_ticks)
            if (best_bid_ticks > 0 and best_ask_ticks > 0
                    and not is_occupied("SELL")
                    and as_ask_ticks > 0 and as_ask_ticks > best_bid_ticks):
                emit("SELL", as_ask_ticks)
        elif inventory_lots > 0:
            # Reduce long: SELL only.
            if (best_bid_ticks > 0 and best_ask_ticks > 0
                    and not is_occupied("SELL")
                    and as_ask_ticks > 0 and as_ask_ticks > best_bid_ticks):
                emit("SELL", as_ask_ticks)
        else:
            # Reduce short: BUY only.
            if (best_bid_ticks > 0 and best_ask_ticks > 0
                    and not is_occupied("BUY")
                    and as_bid_ticks > 0 and as_bid_ticks < best_ask_ticks):
                emit("BUY", as_bid_ticks)

        return commands

    # ── Gap validation ──────────────────────────────────────────────────

    def _check_gap(self, ts_ms: int) -> None:
        """Check gap condition between consecutive EVENT timestamps.

        If ts_evento - ts_evento_previo > 2 * decision_interval_ms,
        invalidates the simulation (raises ValueError). Does NOT reset
        inventory. Cycle steps without events don't count (the calendar
        itself advances in fixed D steps by design).
        """
        last_event = getattr(self, "_last_event_ts", -1)
        if last_event > 0:
            gap = ts_ms - last_event
            if gap > 2 * self.decision_interval_ms:
                raise ValueError(
                    f"Gap invalidates simulation: {gap}ms > 2*{self.decision_interval_ms}ms "
                    f"between {last_event} and {ts_ms}"
                )

    def _cycle_grid(self, last_event_ts: int) -> List[int]:
        """Scheduled cycle timestamps t0 + k*D within data coverage (delegates
        to the shared calendar so both routes build the same grid)."""
        return cycle_grid(self._t0, self.decision_interval_ms, last_event_ts)

    # ── Main run loop ───────────────────────────────────────────────────

    def run(self) -> FinalResult:
        """Execute complete simulation and return FinalResult from the engine.

        Flow:
        1. Warm-up phase: advance advance_to() without apply_commands until
           warmup_intervals mids with positive intervals are collected.
        2. Main loop over the MERGED calendar (scheduled cycles t0+k*D within
           data coverage ∪ event timestamps), strictly ascending:
             - advance_to(ts, market_events_at_ts) — empty list on cycles
               without events; expired timers drain before deciding
             - consume ALL market events at ts (trades, books) before deciding
             - event gaps > 2*D invalidate the run (no inventory reset)
             - Get state() — authoritative snapshot with inventory from the engine
             - A-S decision on grid timestamps once warm-up is complete
             - apply_commands(ts, commands) — inject decisions
        3. EOF: finish(observed_end_ms=último_ts_evento) — closes the engine,
           never beyond the recorded data.
        """
        if not self._market_events:
            raise ValueError("No market events loaded")

        # ─── Step 1: Warm-up phase ───
        self._warmup_phase()

        # ─── Step 2: Main loop over merged calendar ───
        self._cycle_counter = 0
        self._finished = False
        self._last_processed_ts = getattr(self, "_warmup_last_ts", -1)
        self._last_event_ts = self._last_processed_ts
        self._side_occupied = {}

        last_event_ts = self._market_events[-1]["ts_ms"]
        start_ts = self._last_processed_ts if self._last_processed_ts > 0 else -1

        # F1.2: scheduled cycles run even without market events; event
        # timestamps are all consumed (never skip trades/books). Shared
        # merged calendar (same implementation as OfflineCoordinator).
        event_ts = sorted({
            e["ts_ms"] for e in self._market_events if e["ts_ms"] > start_ts
        })
        steps = [ts for ts in merged_steps(
            event_ts, self._t0, self.decision_interval_ms, last_event_ts)
            if ts > start_ts]
        event_ts_set = set(event_ts)

        for ts_ms in steps:
            self._current_ts = ts_ms
            has_events = ts_ms in event_ts_set

            # Collect all market events at this timestamp, flattened
            # (possibly empty on cycles without events)
            market_events_for_advance: List[Dict[str, Any]] = [
                {"ts_ms": ts_ms, "kind": e["kind"], **e["data"]}
                for e in self._market_events
                if e["ts_ms"] == ts_ms and e["kind"] in ("book", "trade")
            ]

            # advance_to drains expired timers (F1.2) then consumes events
            self.engine.advance_to(ts_ms, market_events_for_advance)
            # Signal stack sees exactly what the engine consumed (F1.3)
            self._feed_market_state(ts_ms, market_events_for_advance)

            # Update best bid/ask from the engine's internal book
            if getattr(self.engine, "book", None) and self.engine.book:
                bids = self.engine.book.get("bids", [])
                asks = self.engine.book.get("asks", [])
                if bids:
                    self._best_bid = float(bids[0][0])
                if asks:
                    self._best_ask = float(asks[0][0])

            # Gap validation on EVENT timestamps only, BEFORE updating state
            # (raises without resetting inventory)
            if has_events:
                self._check_gap(ts_ms)
                self._last_event_ts = ts_ms
            self._last_processed_ts = ts_ms

            # Get authoritative snapshot (engine owns inventory/cash)
            snapshot = self.engine.state()

            # A-S decision only on grid timestamps once warm-up is complete.
            # Coverage is validated BEFORE calling the policy, whether or not
            # it would emit commands: no cycle decides on a stale/missing book.
            if self._is_decision_timestamp(ts_ms) and self._warmup_complete:
                max_age = self.engine.config.max_book_age_ms
                check_book_coverage(self.engine.book, ts_ms, max_age)
                # Generate decision (respects _side_occupied for delayed replacement)
                commands = self._as_decision(snapshot)

                # Apply commands if any
                if commands:
                    self.engine.apply_commands(ts_ms, commands)

                    # Mark cancelled sides as occupied for delayed replacement
                    for cmd in commands:
                        if cmd["kind"] == "cancel":
                            side = cmd.get("side", "")
                            if side:
                                self._side_occupied[side] = True

                self._cycle_counter += 1

        # ─── Step 3: EOF ───
        # observed_end_ms = last timestamp that had events processed
        observed_end_ms = self._last_processed_ts if self._last_processed_ts > 0 else last_event_ts
        result = self.engine.finish(observed_end_ms)

        return result

    # ── Timestamp check ───────────────────────────────────────────────

    def _is_decision_timestamp(self, ts_ms: int) -> bool:
        """Check if ts_ms corresponds to a scheduled decision timestamp.

        The calendar is t0 + k * decision_interval_ms for k = 0, 1, 2, ...
        """
        if self._t0 is None:
            return False
        diff = ts_ms - self._t0
        return diff % self.decision_interval_ms == 0