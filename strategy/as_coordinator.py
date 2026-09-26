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
from strategy.alpha_model import AlphaModel


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
    - Warm-up: requires warmup_intervals mids with positive intervals (mid prices strictly increase)
    - No decisions after EOF; event gaps invalidate the run without resetting inventory
    - Event group closes completely before delivering; never filters future to policy
    - Delayed replacement: if a decision cancels a side, that side stays "occupied"
      until the next scheduled cycle (no immediate re-submit).
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
        self.decision_interval_ms = decision_interval_ms
        self.warmup_intervals = warmup_intervals
        self._cfg = ASCoordinatorConfig(
            decision_interval_ms=decision_interval_ms,
            warmup_intervals=warmup_intervals,
        )

        # Load and unify events from CSVs into a sorted list (plain dicts,
        # format expected by ExecutionReconstructor.advance_to).
        # Each event dict has keys: ts_ms (int), kind (str: "book"|"trade"), data (dict).
        self._market_events = self._load_market_events(depth_csv, trades_csv)

        # Derive t0 from the first event timestamp
        self._t0: int = self._market_events[0]["ts_ms"] if self._market_events else 0
        self._last_processed_ts: int = -1
        self._current_ts: int = self._t0
        self._cycle_counter: int = 0
        self._finished: bool = False

        # Warm-up state
        self._warmup_complete: bool = False
        self._warmup_mids: List[float] = []
        self._best_bid: float = 0.0
        self._best_ask: float = 0.0

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

    # ── Warm-up phase ───────────────────────────────────────────────────

    def _warmup_phase(self) -> None:
        """Warm-up: advance advance_to() without apply_commands until having
        warmup_intervals mids sampled at timestamps with positive intervals
        (strictly increasing ts).

        After warmup, _warmup_complete=True and the main loop may emit decisions.
        The phase is idempotent: a second call returns immediately (the engine
        already consumed those events and rejects duplicate advance_to).

        If the data is exhausted before collecting warmup_intervals mids, the
        warm-up ends best-effort WITHOUT raising: _warmup_complete stays False
        and the main loop still processes the remaining events (no decisions).
        """
        if getattr(self, "_warmup_done", False):
            return  # already executed — engine state must not be re-advanced

        self._warmup_mids = []
        self._warmup_ts: List[int] = []
        self._warmup_complete = False
        self._warmup_done = True
        self._best_bid = 0.0
        self._best_ask = 0.0
        self._side_occupied = {}

        i = 0
        n = len(self._market_events)

        # Process events timestamp by timestamp until we have enough mids
        while i < n and len(self._warmup_mids) < self.warmup_intervals:
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

            # Update best bid/ask from the engine's internal book
            if getattr(self.engine, "book", None) and self.engine.book:
                bids = self.engine.book.get("bids", [])
                asks = self.engine.book.get("asks", [])
                if bids:
                    self._best_bid = float(bids[0][0])
                if asks:
                    self._best_ask = float(asks[0][0])

            # Calculate mid and record it
            if self._best_bid > 0 and self._best_ask > 0:
                mid = (self._best_bid + self._best_ask) / 2.0
                self._warmup_mids.append(mid)
                self._warmup_ts.append(ts_ms)

        # Verify warm-up condition: need warmup_intervals mids with positive
        # intervals (strictly increasing timestamps between consecutive mids)
        if len(self._warmup_mids) < self.warmup_intervals:
            # Data exhausted before completing warm-up: best-effort, no raise.
            return

        # Check that we have at least (warmup_intervals - 1) positive intervals
        positive_intervals = 0
        for j in range(1, len(self._warmup_ts)):
            if self._warmup_ts[j] > self._warmup_ts[j - 1]:
                positive_intervals += 1

        if positive_intervals >= self.warmup_intervals - 1:
            self._warmup_complete = True
        else:
            raise ValueError(
                f"Warm-up requires {self.warmup_intervals} mids with positive intervals, "
                f"but found only {positive_intervals} positive intervals "
                f"out of {len(self._warmup_mids)} sampled mids."
            )

    # ── A-S decision using AlphaModel ─────────────────────────────────
    def _as_decision(self, snapshot: ExecutionSnapshot) -> List[Dict[str, Any]]:
        """Generate submit/cancel commands using real Avellaneda-Stoikov quotes.

        Decision logic (per A-S spec):
        - Compute reservation price r_t = mid + alpha - gamma * inv * sigma^2 * T
        - Compute quote distances (bid_dist, ask_dist) with inventory skew
        - Optimal bid = r_t - bid_dist, optimal ask = r_t + ask_dist
        - Inventory side: if inventory > 0 reduce long (SELL only), < 0 reduce short (BUY only), = 0 both sides

        Uses snapshot.inventory_lots, self.engine.book for mid/volatility,
        and AlphaModel for the A-S calculations.
        """
        commands: List[Dict[str, Any]] = []
        inventory_lots = snapshot.inventory_lots

        # Extract mid from engine's book if available, else fall back to bests
        engine = getattr(self, "engine", None)
        best_bid: float = 0.0
        best_ask: float = 0.0
        if engine and getattr(engine, "book", None) and engine.book:
            bids = engine.book.get("bids", [])
            asks = engine.book.get("asks", [])
            if bids and asks:
                best_bid = float(bids[0][0])
                best_ask = float(asks[0][0])
            mid = (best_bid + best_ask) / 2.0
        else:
            mid = 0.0

        # Inventory in XRP (not lots): lots * qty_step (default 1.0)
        qty_step = getattr(self.config, "qty_step", 1.0) if self.config else 1.0
        inventory_xrp = float(inventory_lots) * float(qty_step)

        # Alpha model instance and calculations
        am = AlphaModel()
        alpha = 0.0
        # Build snapshot dict for AlphaModel; use mid > 0 guard for safe compute
        snap_dict: Dict[str, object] = {
            "mid": mid if mid > 0 else None,
            "momentum": 0.0,
            "imbalance": 0.0,
            "microprice": 0.0,
            "buy_volume_60s": 0.0,
            "sell_volume_60s": 0.0,
            "tick_size": getattr(self.config, "tick_size", 0.0001) if self.config else 0.0001,
            "volatility": float(getattr(self.engine, "volatility", 0.0)) if engine and hasattr(engine, "volatility") else 0.0,
        }
        if mid > 0:
            try:
                alpha = am.compute_alpha(snap_dict)
            except Exception:
                alpha = 0.0

        # Reservation price and quote distances via A-S model
        # V2: sigma already normalized to ref_s from market_state; alpha_model
        # handles the √T scaling internally (never double √T per §0.6).
        sigma = max(
            float(getattr(self.engine, "volatility", 0.0)) if engine and hasattr(engine, "volatility") else 0.0,
            1e-12,
        )
        r = am.reservation_price(snap_dict if mid > 0 else {}, alpha, inventory_xrp, sigma)
        bid_dist, ask_dist = am.quote_distances(snap_dict if mid > 0 else {}, alpha, inventory_xrp, sigma)

        # Optimal A-S prices: bid = r - bid_dist, ask = r + ask_dist
        as_bid_price = r - bid_dist
        as_ask_price = r + ask_dist

        # Helper: check if side is occupied (from previous cancel)
        def is_occupied(side: str) -> bool:
            return bool(self._side_occupied.get(side, False))

        tick_size = (
            getattr(self.config, "tick_size", 0.0001)
            if self.config
            else 0.0001
        )

        # Build decisions using A-S prices
        if inventory_lots == 0:
            # Submit both sides at A-S optimal prices
            if best_bid > 0 and not is_occupied("BUY") and as_bid_price > 0:
                buy_price = int(round(as_bid_price))
                commands.append(
                    {
                        "ts_ms": self._current_ts,
                        "kind": "submit",
                        "order_id": f"buy_{self._cycle_counter}",
                        "side": "BUY",
                        "price_ticks": buy_price,
                        "qty_lots": 1,
                    }
                )
            if best_ask > 0 and not is_occupied("SELL") and as_ask_price > 0:
                sell_price = int(round(as_ask_price))
                commands.append(
                    {
                        "ts_ms": self._current_ts,
                        "kind": "submit",
                        "order_id": f"sell_{self._cycle_counter}",
                        "side": "SELL",
                        "price_ticks": sell_price,
                        "qty_lots": 1,
                    }
                )
        elif inventory_lots > 0:
            # Only submit SELL (reduce long) at A-S optimal price
            if best_ask > 0 and not is_occupied("SELL") and as_ask_price > 0:
                sell_price = int(round(as_ask_price))
                commands.append(
                    {
                        "ts_ms": self._current_ts,
                        "kind": "submit",
                        "order_id": f"sell_{self._cycle_counter}",
                        "side": "SELL",
                        "price_ticks": sell_price,
                        "qty_lots": 1,
                    }
                )
        elif inventory_lots < 0:
            # Only submit BUY (reduce short) at A-S optimal price
            if best_bid > 0 and not is_occupied("BUY") and as_bid_price > 0:
                buy_price = int(round(as_bid_price))
                commands.append(
                    {
                        "ts_ms": self._current_ts,
                        "kind": "submit",
                        "order_id": f"buy_{self._cycle_counter}",
                        "side": "BUY",
                        "price_ticks": buy_price,
                        "qty_lots": 1,
                    }
                )

        return commands
        """Extract best bid from snapshot (fallback)."""
        # The snapshot does not directly expose best_bid/best_ask;
        # try the engine's internal book.
        engine = getattr(self, "engine", None)
        if engine and getattr(engine, "book", None) and engine.book:
            bids = engine.book.get("bids", [])
            if bids:
                return float(bids[0][0])
        return 0.0

    def _extract_best_ask(self, snapshot: ExecutionSnapshot) -> float:
        """Extract best ask from snapshot (fallback)."""
        engine = getattr(self, "engine", None)
        if engine and getattr(engine, "book", None) and engine.book:
            asks = engine.book.get("asks", [])
            if asks:
                return float(asks[0][0])
        return 0.0

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
        """Scheduled cycle timestamps t0 + k*D within data coverage (<= last event)."""
        grid: List[int] = []
        if not self._market_events:
            return grid
        t0 = self._t0
        k = 0
        while True:
            ts = t0 + k * self.decision_interval_ms
            if ts > last_event_ts:
                break
            grid.append(ts)
            k += 1
        return grid

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
        # timestamps are all consumed (never skip trades/books). Both sets
        # strictly after warm-up consumption, ascending, no duplicates.
        event_ts = sorted({
            e["ts_ms"] for e in self._market_events if e["ts_ms"] > start_ts
        })
        cycle_ts = [ts for ts in self._cycle_grid(last_event_ts) if ts > start_ts]
        steps = sorted(set(event_ts) | set(cycle_ts))
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

            # A-S decision only on grid timestamps once warm-up is complete
            if self._is_decision_timestamp(ts_ms) and self._warmup_complete:
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