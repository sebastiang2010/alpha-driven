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


# ── Coordinator configuration ─────────────────────────────────────────

@dataclass
class ASCoordinatorConfig:
    """Configuración del ASCoordinator."""
    decision_interval_ms: int = 1000
    warmup_intervals: int = 3
    max_gap_ratio: float = 2.0


# ── Main coordinator ──────────────────────────────────────────────────

class ASCoordinator:
    """
    Offline coordinator that owns the calendar and event reader.
    The ExecutionReconstructor is a black box.

    Responsibilities:
    - Owns the deterministic calendar: t0 + k*D (D = decision_interval_ms)
    - Interleaves A-S cycles with market groups without skipping trades or books
    - Warm-up: requires warmup_intervals mids with positive intervals (mid prices strictly increase)
    - No decisions after EOF; gaps invalidate the run without resetting inventory
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
        decision_interval_ms: int = 1000,
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

    # ── A-S decision placeholder ────────────────────────────────────────

    def _as_decision(self, snapshot: ExecutionSnapshot) -> List[Dict[str, Any]]:
        """Placeholder A-S decision: generate submit/cancel commands based on snapshot.

        Decision logic (per spec):
        - inventory == 0: submit 1 BUY and 1 SELL at best bid/ask ± 1 tick
        - inventory > 0: only submit SELL (reduce long)
        - inventory < 0: only submit BUY (reduce short)

        Uses state().best_bid, state().best_ask, state().inventory_lots.
        Respects _side_occupied: if a side was cancelled, it stays occupied
        until the next scheduled cycle and is not re-submitted immediately.
        """
        commands: List[Dict[str, Any]] = []
        inventory = snapshot.inventory_lots

        # Determine best bid/ask: prefer coordinator's tracking, fall back to engine's book
        best_bid = self._best_bid if self._best_bid > 0 else self._extract_best_bid(snapshot)
        best_ask = self._best_ask if self._best_ask > 0 else self._extract_best_ask(snapshot)

        tick_size = (
            getattr(self.config, "tick_size", 0.0001)
            if self.config
            else 0.0001
        )

        # Helper: check if side is occupied (from previous cancel)
        def is_occupied(side: str) -> bool:
            return bool(self._side_occupied.get(side, False))

        # Build decisions
        if inventory == 0:
            # Submit 1 BUY and 1 SELL at best bid/ask ± 1 tick
            if best_bid > 0 and not is_occupied("BUY"):
                buy_price = int(round(best_bid - tick_size))
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
            if best_ask > 0 and not is_occupied("SELL"):
                sell_price = int(round(best_ask + tick_size))
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
        elif inventory > 0:
            # Only submit SELL (reduce long) — respect occupied status
            if best_ask > 0 and not is_occupied("SELL"):
                sell_price = int(round(best_ask + tick_size))
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
        elif inventory < 0:
            # Only submit BUY (reduce short) — respect occupied status
            if best_bid > 0 and not is_occupied("BUY"):
                buy_price = int(round(best_bid - tick_size))
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

    def _extract_best_bid(self, snapshot: ExecutionSnapshot) -> float:
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
        """Check gap condition.

        If ts_siguiente_decisión - ts_ultimo_evento > 2 * decision_interval_ms,
        invalidates the simulation (raises ValueError). Does NOT reset inventory.
        """
        if self._last_processed_ts > 0:
            gap = ts_ms - self._last_processed_ts
            if gap > 2 * self.decision_interval_ms:
                raise ValueError(
                    f"Gap invalidates simulation: {gap}ms > 2*{self.decision_interval_ms}ms "
                    f"between {self._last_processed_ts} and {ts_ms}"
                )

    # ── Main run loop ───────────────────────────────────────────────────

    def run(self) -> FinalResult:
        """Execute complete simulation and return FinalResult from the engine.

        Flow:
        1. Warm-up phase: advance advance_to() without apply_commands until
           warmup_intervals mids with positive intervals are collected.
        2. Main loop: for each scheduled decision timestamp (every decision_interval_ms):
            - advance_to(ts, market_events_at_ts) — consume all market events at ts
            - If events at that timestamp: process ALL (trades, books) before deciding
            - Get state() — authoritative snapshot with inventory from the engine
            - A-S decision (placeholder) — generate submit/cancel commands
            - apply_commands(ts, commands) — inject decisions
        3. Gap validation after each step.
        4. EOF: finish(observed_end_ms=último_ts_evento) — closes the engine.
        """
        if not self._market_events:
            raise ValueError("No market events loaded")

        # ─── Step 1: Warm-up phase ───
        self._warmup_phase()

        # ─── Step 2: Main loop over remaining event timestamps ───
        self._cycle_counter = 0
        self._finished = False
        self._last_processed_ts = getattr(self, "_warmup_last_ts", -1)
        self._side_occupied = {}

        last_event_ts = self._market_events[-1]["ts_ms"]
        start_ts = self._last_processed_ts if self._last_processed_ts > 0 else -1

        # Distinct event timestamps strictly after warm-up consumption.
        # Iterate EVERY event timestamp (not only decision-grid points) so no
        # trade/book is ever skipped; decisions only fire on grid timestamps.
        pending_ts = sorted({
            e["ts_ms"] for e in self._market_events if e["ts_ms"] > start_ts
        })

        for ts_ms in pending_ts:
            self._current_ts = ts_ms

            # Collect all market events at this timestamp, flattened
            market_events_for_advance: List[Dict[str, Any]] = [
                {"ts_ms": ts_ms, "kind": e["kind"], **e["data"]}
                for e in self._market_events
                if e["ts_ms"] == ts_ms and e["kind"] in ("book", "trade")
            ]

            # advance_to with all events at this timestamp
            self.engine.advance_to(ts_ms, market_events_for_advance)

            # Update best bid/ask from the engine's internal book
            if getattr(self.engine, "book", None) and self.engine.book:
                bids = self.engine.book.get("bids", [])
                asks = self.engine.book.get("asks", [])
                if bids:
                    self._best_bid = float(bids[0][0])
                if asks:
                    self._best_ask = float(asks[0][0])

            # Gap validation BEFORE updating _last_processed_ts
            # (raises without resetting inventory)
            self._check_gap(ts_ms)
            self._last_processed_ts = ts_ms

            # Get authoritative snapshot (engine owns inventory/cash)
            snapshot = self.engine.state()

            # A-S decision only if this is a decision timestamp and warmup is complete
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