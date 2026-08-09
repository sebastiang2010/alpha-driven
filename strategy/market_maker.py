# market_maker.py
"""Market Maker — orquestador principal del bot de Binance Futures (XRPUSDC).

Integra TODOS los módulos de la estrategia:
    - market_state      -> estado continuo del mercado via WebSocket (§7)
    - alpha_model       -> señal de corto plazo, reserva y distancias (§6, §8, §11)
    - inventory_manager -> inventario y PnL realizado (§11)
    - risk_engine       -> validación de órdenes y kill switch (§12, §13)
    - execution_engine  -> única interfaz con Binance Futures (§1, §9, §10)

Nivel 0 (§0.1, §21): por defecto DRY-RUN. Nunca se envía una orden real sin
autorización humana explícita. Si EXPOSURE_LEVEL == 0 y se intenta crear un
MarketMaker en modo real, se fuerza dry_run=True con un warning.

Este módulo es el UNICO punto que decide qué se cotiza (§8) y cuándo (§10),
registra decisiones en el journal (§16) y métricas de mercado (§15), y
detecta adverse selection (§14). NUNCA loguea credenciales (§0.5).
"""

from bisect import bisect_left
from collections import deque
from datetime import datetime
import json
import logging
import pathlib
import threading
import time

from . import config
from .alpha_model import AlphaModel
from .execution_engine import ExecutionEngine
from .inventory_manager import InventoryManager
from .market_state import MarketState
from .risk_engine import RiskEngine, MAX_ERROR_COUNT, MAX_VOLATILITY

# ---------------------------------------------------------------------------
# Constantes propias del orquestador (propuestas, §0.4)
# ---------------------------------------------------------------------------

# Fee maker de referencia para estimar NetPnL esperado (§9/§18).
MAKER_FEE_RATE: float = 0.0002

# Frescura del snapshot de WS: por encima de esto se considera desconectado (§13).
WS_STALE_SEC: float = 15.0

# Horizonte de la medida de adverse selection (§14).
ADVERSE_SELECTION_HORIZON_SEC: float = 5.0

# Umbrales de régimen de volatilidad para el journal (§16).
LOW_VOL_THRESHOLD: float = 0.002  # propuesta pendiente de confirmación (§0.4)
HIGH_VOL_THRESHOLD: float = float(MAX_VOLATILITY)  # tope alto = tope del Risk Engine (§13)

# Tamaño máximo de las deques de adverse selection / historial de precios.
_ADVERSE_MAXLEN: int = 256
_PRICE_HISTORY_MAXLEN: int = 1024

logger = logging.getLogger("MarketMaker")


def _ensure_logger() -> None:
    """Logger básico del orquestador (no duplica handlers si el host ya configuró)."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


_ensure_logger()


class MarketMaker:
    """Orquestador del market making (§8, §10, §13, §14, §15, §16)."""

    def __init__(self, dry_run: bool = True):
        # Seguridad §0.1/§21: Nivel 0 (simulación) jamás manda órdenes reales.
        if not dry_run and int(config.EXPOSURE_LEVEL) == 0:
            logger.warning(
                "MarketMaker: EXPOSURE_LEVEL==0 (simulación). "
                "Forzando dry_run=True. No se enviarán órdenes reales (§0.1/§21)."
            )
            dry_run = True
        self.dry_run = dry_run

        self.alpha = AlphaModel()
        self.inventory = InventoryManager()
        self.risk = RiskEngine()
        self.exec = ExecutionEngine(config.SYMBOL, real=config.REAL, dry_run=dry_run)
        self.state = MarketState(config.SYMBOL, config.REAL)

        # Journal de decisiones (§16). NUNCA contiene credenciales (§0.5).
        self.journal_path: pathlib.Path = config.LOG_DECISIONS / "agent_decisions.jsonl"
        # Timestamps de colocación por orden (medición de lifetime, §10).
        self.quote_age: dict = {}

        # Equity de referencia (propuesta) y PnL diario.
        self.equity_start: float = 1000.0
        self.equity_peak: float = 1000.0
        self.equity_current: float = 1000.0
        self.daily_pnl: float = 0.0

        # Estado de conexión WS (se actualiza en el run loop, §13).
        self.ws_connected: bool = False
        self._stop = threading.Event()
        self._journal_lock = threading.Lock()

        # Contexto de ciclo para el risk engine / re-cotización.
        self._last_snapshot: dict | None = None
        self._last_mid: float | None = None

        # Adverse selection (§14): fills registrados y historial de precios.
        self._adverse_fills: deque = deque(maxlen=_ADVERSE_MAXLEN)
        self._price_history: deque = deque(maxlen=_PRICE_HISTORY_MAXLEN)

        logger.info(
            "MarketMaker: inicializado (symbol=%s, real=%s, dry_run=%s, exposure_level=%s)",
            config.SYMBOL, config.REAL, self.dry_run, config.EXPOSURE_LEVEL,
        )

    # ── Helpers de logging ───────────────────────────────────────────────
    def _log_event_jsonl(self, filepath, event) -> None:
        """Append JSONL con lock. Nunca escribe credenciales (§0.5)."""
        with self._journal_lock:
            try:
                path = pathlib.Path(filepath)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, default=str) + "\n")
            except Exception as e:
                logger.warning("MarketMaker: no se pudo escribir %s: %s", filepath, e)

    # ── 2. Detección de fills (§14) ──────────────────────────────────────
    def _on_fill_detection(self, snapshot) -> None:
        """Detecta fills en dry-run: orden cuyo precio coincide con el mid.

        - bid se considera llenada si mid <= order_price (dentro de ±tick).
        - ask se considera llenada si mid >= order_price (dentro de ±tick).

        En modo real la detección se apoya en refresh_open_orders()
        (frecuencia controlada, §0.6) y en process_fills_from_trades desde el
        ciclo; aquí no hay trades disponibles en el snapshot.
        """
        if not self.exec.dry_run:
            return
        mid = snapshot.get("mid")
        if mid is None or mid <= 0:
            return

        tick = (self.exec.symbol_info or {}).get("tick_size") or 1e-9
        tol = max(float(tick), 1e-9)

        for oid, info in self.exec.get_orders().items():
            side = str(info.get("side")).upper()
            price = float(info.get("price") or 0.0)
            qty = float(info.get("qty") or 0.0)
            hit = False
            if side == "BUY" and mid <= price + tol:
                hit = True
            elif side == "SELL" and mid >= price - tol:
                hit = True
            if not hit:
                continue

            if not self.exec.simulate_fill(oid):
                continue
            self.quote_age.pop(oid, None)

            fee = MAKER_FEE_RATE * price * qty  # fee maker estimada (§9/§18)
            self.inventory.record_fill(side, qty, price, fee)
            self.daily_pnl = self.inventory.realized_pnl - self.inventory.total_fees
            self.risk.update_daily_pnl(self.daily_pnl)
            self._track_adverse_selection(
                {"ts": time.time(), "fill_price": price, "side": side, "qty": qty}
            )  # §14
            logger.info(
                "MarketMaker: fill simulado %s %s @ %.8g x %.6g (fee=%.6g)",
                side, oid, price, qty, fee,
            )

    # ── 3. Decisión central de cotización (§8, §9, §10, §11) ─────────────
    def _compute_quotes(self, snapshot) -> dict:
        """Calcula reserva, distancias, tamaños y NetPnL esperado por lado."""
        mid = snapshot.get("mid")
        if mid is None or mid <= 0:
            return {
                "reservation_price": 0.0, "alpha": 0.0,
                "bid_price": 0.0, "ask_price": 0.0,
                "bid_size": 0.0, "ask_size": 0.0,
                "bid_dist": 0.0, "ask_dist": 0.0,
                "expected_pnl_bid": 0.0, "expected_pnl_ask": 0.0,
                "quote_bid_ok": False, "quote_ask_ok": False,
                "reasons": ["no_mid"],
            }

        alpha = self.alpha.compute_alpha(snapshot)
        inventory = float(snapshot.get("inventory") or self.inventory.inventory)
        sigma = float(snapshot.get("volatility") or 0.0)

        r = self.alpha.reservation_price(snapshot, alpha, inventory, sigma)  # §8
        bid_dist, ask_dist = self.alpha.quote_distances(
            snapshot, alpha, inventory, sigma
        )  # §11

        # Tamaño efectivo por nivel de exposición (§21). Nivel 0 (dry-run)
        # simula órdenes con SIMULATION_QUOTE_MULTIPLIER (decisión aprobada
        # §0.2); los niveles >=1 usan EXPOSURE_MULTIPLIERS sin modificar.
        base_size = (
            config.BASE_ORDER_SIZE_XRP * config.effective_exposure_multiplier()
        )
        bid_size, ask_size = self.alpha.choose_order_sizes(
            snapshot, inventory, base_size
        )
        # Agresión por inventario (§11): el lado que reduce recibe más tamaño.
        bid_size *= self.inventory.order_side_aggression("BUY")
        ask_size *= self.inventory.order_side_aggression("SELL")

        bid_price = r - bid_dist
        ask_price = r + ask_dist

        # NetPnL esperado (§9/§18): NO cotizar si <= 0 (regla §10), salvo que
        # ese lado reduzca inventario (reduce_only, §11).
        expected_pnl_bid = self.alpha.expected_net_pnl_estimate(
            snapshot, "BUY", bid_price, bid_size
        )
        expected_pnl_ask = self.alpha.expected_net_pnl_estimate(
            snapshot, "SELL", ask_price, ask_size
        )

        reduce_bid = inventory < 0.0  # comprar reduce inventario short
        reduce_ask = inventory > 0.0  # vender reduce inventario long

        quote_bid_ok = expected_pnl_bid > 0.0 or (reduce_bid and bid_size > 0.0)
        quote_ask_ok = expected_pnl_ask > 0.0 or (reduce_ask and ask_size > 0.0)

        reasons = []
        if not quote_bid_ok:
            reasons.append("expected_net_pnl_non_positive:bid")
        if not quote_ask_ok:
            reasons.append("expected_net_pnl_non_positive:ask")

        return {
            "reservation_price": r,
            "alpha": alpha,
            "bid_price": bid_price,
            "ask_price": ask_price,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "bid_dist": bid_dist,
            "ask_dist": ask_dist,
            "expected_pnl_bid": expected_pnl_bid,
            "expected_pnl_ask": expected_pnl_ask,
            "quote_bid_ok": quote_bid_ok,
            "quote_ask_ok": quote_ask_ok,
            "reasons": reasons,
        }

    # ── 4. Reconciliación (§0.6, §2) ─────────────────────────────────────
    def _reconcile(self) -> None:
        """Sincroniza órdenes y posición con el exchange (frecuencia controlada)."""
        try:
            self.exec.refresh_open_orders()  # frecuencia interna §0.6
        except Exception as e:
            logger.warning("MarketMaker: refresh_open_orders falló: %s", e)

        try:
            position = self.exec.reconcile_position()
        except Exception as e:
            logger.warning("MarketMaker: reconcile_position falló: %s", e)
            position = None

        # En dry-run la API no está disponible y reconcile devuelve None; el
        # inventario se mantiene por los fills simulados (no se aplana).
        if position is not None:
            self.inventory.update_position(position)
            self.risk.update_unrealized(self.inventory.unrealized_pnl)

        self.risk.update_equity(self.equity_current)
        self.risk.update_daily_pnl(self.daily_pnl)

    # ── 5. Journal del agente (§16) ──────────────────────────────────────
    def _journal(self, snapshot, quotes, risk_score) -> None:
        """Escribe una línea en agent_decisions.jsonl (§16). Sin credenciales."""
        sigma = float(snapshot.get("volatility") or 0.0)
        event = {
            "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
            "regime": self._regime(sigma),
            "inventory": float(snapshot.get("inventory") or 0.0),
            "mid_price": float(snapshot.get("mid") or 0.0),
            "spread": float(snapshot.get("spread") or 0.0),
            "imbalance": float(snapshot.get("imbalance") or 0.0),
            "alpha": float(quotes.get("alpha") or 0.0),
            "bid_price": float(quotes.get("bid_price") or 0.0),
            "ask_price": float(quotes.get("ask_price") or 0.0),
            "bid_size": float(quotes.get("bid_size") or 0.0),
            "ask_size": float(quotes.get("ask_size") or 0.0),
            "reason": ";".join(quotes.get("reasons") or []) or "ok",
            "expected_pnl": round(
                float(quotes.get("expected_pnl_bid") or 0.0)
                + float(quotes.get("expected_pnl_ask") or 0.0),
                8,
            ),
            "risk_score": float(risk_score),
        }
        self._log_event_jsonl(self.journal_path, event)  # §16

    # ── 6. Métricas de mercado (§15) ─────────────────────────────────────
    def _log_market_data(self, snapshot) -> None:
        """Append JSONL de métricas por ciclo (§15)."""
        today = datetime.now().strftime("%Y%m%d")
        path = config.LOG_MARKET_DATA / f"market_data_{today}.jsonl"
        event = {
            "ts": float(snapshot.get("ts") or 0.0),
            "mid": float(snapshot.get("mid") or 0.0),
            "bid": float(snapshot.get("best_bid") or 0.0),
            "ask": float(snapshot.get("best_ask") or 0.0),
            "spread": float(snapshot.get("spread") or 0.0),
            "inventory": float(snapshot.get("inventory") or 0.0),
            "position": float(snapshot.get("inventory") or 0.0),
            "volatility": float(snapshot.get("volatility") or 0.0),
            "imbalance": float(snapshot.get("imbalance") or 0.0),
            "microprice": float(snapshot.get("microprice") or 0.0),
        }
        self._log_event_jsonl(path, event)  # §15

    # ── 7. Ciclo de vida de órdenes (§10) ────────────────────────────────
    def _manage_orders(self, snapshot, quotes) -> None:
        """Gestiona órdenes: expire/replace, re-cotización por mid, place/cancel."""
        now = time.time()
        mid = snapshot.get("mid")
        orders = self.exec.get_orders()

        has_bid = any(o["side"] == "BUY" for o in orders.values())
        has_ask = any(o["side"] == "SELL" for o in orders.values())

        # Re-cotizar si el mid se movió más que el umbral (§8).
        requote = False
        if mid and self._last_mid and self._last_mid > 0:
            delta = abs(mid - self._last_mid) / self._last_mid
            requote = delta > config.QUOTE_UPDATE_THRESHOLD_PCT

        for oid, info in orders.items():
            side = info["side"]
            created = self.quote_age.get(oid, info.get("created_ts", 0.0))
            age = now - created if created else 0.0

            side_ok = quotes["quote_bid_ok"] if side == "BUY" else quotes["quote_ask_ok"]
            new_price = quotes["bid_price"] if side == "BUY" else quotes["ask_price"]
            new_qty = quotes["bid_size"] if side == "BUY" else quotes["ask_size"]

            # La cotización de este lado ya no es válida -> cancelar.
            if not side_ok:
                self._cancel_order(oid)
                continue

            # Vida máxima excedida -> reemplazar con la cotización vigente (§10).
            if age > config.MAX_ORDER_LIFETIME_SEC:
                self._replace_order(oid, side, new_qty, new_price, mid)
                continue

            # Mid movido + respeto del lifetime mínimo -> re-cotizar (§8/§10).
            if requote and age >= config.MIN_ORDER_LIFETIME_SEC:
                self._replace_order(oid, side, new_qty, new_price, mid)

        # Colocar lados faltantes (solo si el lado cotiza y hay tamaño).
        if quotes["quote_bid_ok"] and not has_bid and quotes["bid_size"] > 0:
            self._place_order("BUY", quotes["bid_size"], quotes["bid_price"], mid)
        if quotes["quote_ask_ok"] and not has_ask and quotes["ask_size"] > 0:
            self._place_order("SELL", quotes["ask_size"], quotes["ask_price"], mid)

        self._last_mid = mid

    def _place_order(self, side, qty, price, mid):
        """Valida contra el Risk Engine (§12) y coloca la orden maker (§10)."""
        qty = float(qty)
        price = float(price)
        if qty <= 0 or price <= 0:
            return None
        notional = price * qty
        open_count = self.exec.get_open_order_count()
        risk_side = "ask" if side == "SELL" else "bid"
        allowed, reasons = self.risk.check_order(
            config.SYMBOL, risk_side, qty, price, notional,
            open_count, self._risk_state_snapshot(mid),
        )
        if not allowed:
            logger.info(
                "MarketMaker: %s rechazada por Risk Engine (%s)", side, ";".join(reasons)
            )
            return None
        oid, ok, reason = self.exec.place_maker_order(side, qty, price)
        if ok and oid:
            self.quote_age[oid] = time.time()
            logger.debug(
                "MarketMaker: %s %s colocada @ %.8g x %.6g", side, oid, price, qty
            )
        else:
            logger.warning("MarketMaker: falló colocación %s: %s", side, reason)
        return oid

    def _cancel_order(self, oid) -> None:
        if self.exec.cancel_order_by_id(oid):
            self.quote_age.pop(oid, None)

    def _replace_order(self, oid, side, new_qty, new_price, mid):
        """Cancela y recoloca (nueva validación de riesgo al colocar)."""
        if new_qty <= 0 or new_price <= 0:
            self._cancel_order(oid)
            return None
        self._cancel_order(oid)
        return self._place_order(side, new_qty, new_price, mid)

    # ── 8. Kill switch (§13) ─────────────────────────────────────────────
    def _kill_switch_check(self, snapshot) -> bool:
        """Evalúa el kill switch y, si se dispara, cancela todo y detiene el loop."""
        state = self._risk_state_snapshot(snapshot.get("mid"))
        error_count = int(self.risk.get_state().get("error_count", 0))
        triggered, reasons = self.risk.check_kill_switch(
            state, ws_connected=self.ws_connected, error_count=error_count
        )
        if triggered:
            logger.critical(
                "MarketMaker: KILL SWITCH disparado (%s)", ";".join(reasons)
            )
            actions = self.risk.actions_on_trigger(reasons)
            if actions.get("cancel_all"):
                n = self.exec.cancel_all_orders()
                logger.info("MarketMaker: canceladas %d órdenes por kill switch", n)
            self._log_event_jsonl(config.LOG_PNL / "kill_switch.jsonl", {
                "ts": time.time(),
                "reasons": reasons,
                "mid": float(snapshot.get("mid") or 0.0),
                "ws_connected": self.ws_connected,
            })
            self._stop.set()
        return triggered

    # ── 9. Loop principal (§8, §13, §15, §16) ────────────────────────────
    def run(self, max_cycles: int | None = None) -> None:
        """Arranca WS, inicializa el exchange y ejecuta el ciclo de cotización."""
        # §0.1/§21: en Nivel 0 (dry-run) el cliente se inicializa SOLO en
        # testnet (real=config.REAL=False por defecto). Si config.REAL=True
        # (mainnet), NUNCA se inicializa aquí: requiere autorización humana
        # explícita (§0.1) y no existe sin ella.
        if config.REAL:
            logger.warning(
                "MarketMaker: config.REAL=True detectado. NO se inicializa el "
                "cliente mainnet sin autorización humana (§0.1). Continúa en "
                "dry-run/testnet según EXPOSURE_LEVEL."
            )
        else:
            self.exec.init_client(real=False)  # testnet (seguro §0.1/§0.5)

        self.state.start_ws()
        self.exec.init_symbol_info()
        self.exec.set_leverage(config.MAX_LEVERAGE_USED)  # dry-run: no-op
        logger.info(
            "MarketMaker: run iniciado (dry_run=%s, real=%s, exposure_level=%s)",
            self.exec.dry_run, self.exec.real, config.EXPOSURE_LEVEL,
        )

        cycles = 0
        try:
            while not self._stop.is_set():
                if max_cycles is not None and cycles >= max_cycles:
                    logger.info("MarketMaker: max_cycles=%d alcanzado. Deteniendo.", max_cycles)
                    break
                cycles += 1
                try:
                    snapshot = self.state.get_snapshot()
                    mid = snapshot.get("mid")
                    ts = snapshot.get("ts")
                    if mid is None or mid <= 0 or not ts:
                        self.ws_connected = False
                        time.sleep(config.CYCLE_INTERVAL_SEC)
                        continue

                    # Conexión WS viva si los datos tienen < WS_STALE_SEC (§13).
                    ts_sec = float(ts) / 1000.0
                    self.ws_connected = (time.time() - ts_sec) < WS_STALE_SEC
                    self._last_snapshot = snapshot
                    self._price_history.append((time.time(), float(mid)))  # §14

                    self._on_fill_detection(snapshot)
                    self._reconcile()

                    # Rellenar el snapshot con el estado de inventario/PnL.
                    snapshot["inventory"] = self.inventory.inventory
                    snapshot["unrealized_pnl"] = self.inventory.unrealized_pnl
                    snapshot["realized_pnl"] = self.inventory.realized_pnl
                    snapshot["fill_count"] = len(self.exec.fills)

                    quotes = self._compute_quotes(snapshot)
                    self._manage_orders(snapshot, quotes)

                    # Actualizar equity/PnL para el Risk Engine (§13).
                    self.daily_pnl = self.inventory.realized_pnl - self.inventory.total_fees
                    equity = self.equity_start + self.daily_pnl
                    self.equity_current = equity
                    self.equity_peak = max(self.equity_peak, equity)
                    self.risk.update_price(float(mid))
                    self.risk.update_equity(equity)
                    self.risk.update_unrealized(self.inventory.unrealized_pnl)
                    self.risk.update_daily_pnl(self.daily_pnl)

                    risk_score = self._compute_risk_score()
                    self._journal(snapshot, quotes, risk_score)  # §16
                    self._log_market_data(snapshot)              # §15
                    self.risk.reset_error_count()

                    if self._kill_switch_check(snapshot):  # §13
                        break
                except Exception as e:
                    # Nunca matar el proceso; el contador alimenta el kill switch.
                    logger.exception("MarketMaker: error en ciclo %d: %s", cycles, e)
                    self.risk.record_error()
                time.sleep(config.CYCLE_INTERVAL_SEC)
        finally:
            self.stop()

    # ── 10. Detención (idempotente) ──────────────────────────────────────
    def stop(self) -> None:
        """Cierra WS y cancela órdenes. Idempotente."""
        if self._stop.is_set():
            return
        logger.info("MarketMaker: deteniendo...")
        try:
            self.state.close_ws()
        except Exception as e:
            logger.warning("MarketMaker: error cerrando WS: %s", e)
        try:
            n = self.exec.cancel_all_orders()
            if n:
                logger.info("MarketMaker: canceladas %d órdenes al detener", n)
        except Exception as e:
            logger.warning("MarketMaker: error cancelando órdenes: %s", e)
        self._stop.set()
        logger.info("MarketMaker: detenido.")

    # ── Adverse selection (§14) ──────────────────────────────────────────
    def _track_adverse_selection(self, fill) -> None:
        """Registra el precio de un fill con timestamp para medir §14."""
        self._adverse_fills.append({
            "ts": float(fill.get("ts", time.time())),
            "price": float(fill.get("fill_price", fill.get("price", 0.0))),
            "side": str(fill.get("side", "UNKNOWN")),
            "qty": float(fill.get("qty", 0.0)),
        })

    def adverse_selection_stats(self) -> dict:
        """Estadística simple de adverse selection (§14).

        Compara el precio de cada fill con el mid futuro a
        ADVERSE_SELECTION_HORIZON_SEC segundos (busca el precio más cercano
        al target en el historial). Para un BUY, adverse selection = -Δp:
        compramos y el precio bajó. Para un SELL, adverse = +Δp.
        """
        now = time.time()
        cutoff = now - ADVERSE_SELECTION_HORIZON_SEC - 1.0
        while self._adverse_fills and self._adverse_fills[0]["ts"] < cutoff:
            self._adverse_fills.popleft()

        prices = sorted(self._price_history, key=lambda x: x[0])
        times = [p[0] for p in prices]
        impacts = []
        measured = 0
        for f in self._adverse_fills:
            target = f["ts"] + ADVERSE_SELECTION_HORIZON_SEC
            idx = bisect_left(times, target)
            if idx >= len(prices):
                continue
            future_price = prices[idx][1]
            if future_price <= 0 or f["price"] <= 0:
                continue
            rel = (future_price - f["price"]) / future_price
            impacts.append(-rel if f["side"] == "BUY" else rel)
            measured += 1
        return {
            "measured": measured,
            "pending": len(self._adverse_fills) - measured,
            "avg_adverse_impact_pct": (sum(impacts) / len(impacts)) if impacts else 0.0,
            "last_price": prices[-1][1] if prices else 0.0,
        }

    # ── Helpers de estado para el Risk Engine / journal ──────────────────
    @staticmethod
    def _regime(sigma: float) -> str:
        """Régimen de volatilidad para el journal (§16): low_vol/high_vol/normal."""
        if sigma < LOW_VOL_THRESHOLD:
            return "low_vol"
        if sigma > HIGH_VOL_THRESHOLD:
            return "high_vol"
        return "normal"

    def _risk_state_snapshot(self, mid) -> dict:
        """Estado consolidado para risk.check_order / check_kill_switch (§12/§13)."""
        inventory = self.inventory.inventory
        volatility = float((self._last_snapshot or {}).get("volatility") or 0.0)
        drawdown = 0.0
        if self.equity_peak > 0:
            drawdown = max(0.0, (self.equity_peak - self.equity_current) / self.equity_peak)
        return {
            "mid": float(mid or 0.0),
            "inventory": inventory,
            "current_position_notional": abs(inventory * float(mid or 0.0)),
            "daily_pnl": self.daily_pnl,
            "equity": self.equity_current,
            "peak_equity": self.equity_peak,
            "unrealized_pnl": self.inventory.unrealized_pnl,
            "drawdown": drawdown,
            "volatility": volatility,
            "error_count": int(self.risk.get_state().get("error_count", 0)),
        }

    def _compute_risk_score(self) -> float:
        """Score 0..1 para el journal (§16): uso de límites de pérdida,
        drawdown y errores consecutivos."""
        daily_loss = max(0.0, -self.daily_pnl)
        daily_usage = (
            min(1.0, daily_loss / self.risk.max_daily_loss)
            if self.risk.max_daily_loss
            else 0.0
        )
        drawdown = 0.0
        if self.equity_peak > 0:
            drawdown = max(0.0, (self.equity_peak - self.equity_current) / self.equity_peak)
        dd_usage = (
            min(1.0, drawdown / self.risk.max_drawdown_pct)
            if self.risk.max_drawdown_pct
            else 0.0
        )
        err_usage = min(
            1.0, int(self.risk.get_state().get("error_count", 0)) / float(MAX_ERROR_COUNT)
        )
        return round(0.4 * daily_usage + 0.4 * dd_usage + 0.2 * err_usage, 4)
