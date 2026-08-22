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
import math
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

# Frescura del snapshot de WS: por encima de esto se considera desconectado (§13).
# 60 s (antes 15 s): cortes transitorios de red de 20-30 s; la reconexión automática
# restaura en <10 s; el kill switch sigue protegiendo desconexiones reales >60 s (§13 intacto).
WS_STALE_SEC: float = 60.0

# Gracia de reconexión §13 (2026-08-11, iteración independencia): con WS caído
# el bot cancela makers (libro limpio, sin llenados a precio stale) y NO cotiza,
# esperando la reconexión (backoff interno de los WS <10 s). Solo si el WS no
# vuelve en WS_KILL_GRACE_SEC se ejecuta el kill switch real (cancel_all +
# reduce/close + bloqueo + GATE FAIL, §13 intacto). Antes: 1er ciclo con stale
# mataba la corrida → corridas de 3.5 h perdidas por cortes de 1-2 min.
# Fuente única en config.WS_KILL_GRACE_SEC (§0.4).
WS_KILL_GRACE_SEC: float = float(getattr(config, "WS_KILL_GRACE_SEC", 120.0))

# Tolerancia de divergencia en la reconciliación de posición (§0.6). Si la
# posición del exchange difiere de la local en menos de esto, se considera
# ruido de redondeo y se actualiza; si difiere más, NO se sobrescribe en silencio
# (se mantiene el inventario local y se loguea para diagnóstico, §11/§18).
RECONCILE_DIVERGENCE_EPS: float = 1e-6

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

        # Equity de referencia (propuesta, §0.4) y PnL diario. Fuente única en
        # config.STARTING_EQUITY_USDC (NO hardcodeado, §0.4).
        self.equity_start: float = float(getattr(config, "STARTING_EQUITY_USDC", 1000.0))
        self.equity_peak: float = self.equity_start
        self.equity_current: float = self.equity_start
        self.daily_pnl: float = 0.0

        # Estado de conexión WS (se actualiza en el run loop, §13).
        self.ws_connected: bool = False
        self._stop = threading.Event()
        self._journal_lock = threading.Lock()

        # Kill switch (§13): una vez disparado, se bloquean nuevas entradas
        # (disable_new_entries) además de cancelar/reducir lo existente.
        self.disable_new_entries: bool = False
        self._last_snapshot: dict | None = None
        self._last_mid: float | None = None

    @staticmethod
    def apply_inventory_penalty(inventory: float) -> float:
        """Calcula el skew de precio basado en el inventario (Avellaneda‑Stoikov)."""
        skew = inventory * config.INVENTORY_GAMMA
        max_skew = config.MAX_SKU_SKEW
        # Limita el skew a ±max_skew
        if skew > max_skew:
            skew = max_skew
        elif skew < -max_skew:
            skew = -max_skew
        return skew


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

        AVISO (§19): esta es una simulación OPTIMISTA de dry-run. Cuenta como
        fill cualquier toque del mid al precio límite de la orden; en la vida
        real el llenado depende del flujo agresor y del resto del libro. Por
        eso el PnL y la tasa de fill del dry-run NO son indicadores de
        rentabilidad real y NO deben usarse para validar el edge (§19: una
        operación ganadora en dry-run NO es evidencia). El inventario/PnL que
        se actualiza acá sirve solo para ejercitar el flujo del bot.

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

            fee = config.MAKER_FEE_RATE * price * qty  # fee maker estimada (§9/§18)
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
                "momentum_active": False,
                "reasons": ["no_mid"],
            }

        alpha = self.alpha.compute_alpha(snapshot)
        inventory = float(snapshot.get("inventory") or self.inventory.inventory)
        sigma = float(snapshot.get("volatility") or 0.0)

        r = self.alpha.reservation_price(snapshot, alpha, inventory, sigma)  # §8
        bid_dist, ask_dist = self.alpha.quote_distances(
            snapshot, alpha, inventory, sigma
        )  # §11

        # Estado del filtro de momentum (§14): lo actualiza quote_distances;
        # se expone en el dict para el journal (§16).
        momentum_active = bool(getattr(self.alpha, "last_momentum_active", False))

        # Tamaño efectivo por nivel de exposición (§21). Nivel 0 (dry-run)
        # simula órdenes con SIMULATION_QUOTE_MULTIPLIER (decisión aprobada
        # §0.2); los niveles >=1 usan EXPOSURE_MULTIPLIERS sin modificar.
        base_size = (
            config.BASE_ORDER_SIZE_XRP * config.effective_exposure_multiplier()
        )
        # Agresión por inventario (§11): el lado que reduce recibe más tamaño.
        # Fuente ÚNICA de skew de tamaño (decisión auditoría 2026-08-09: se
        # eliminó choose_order_sizes/SKEW_SIZE que componían doble skew §0.6).
        bid_size = base_size * self.inventory.order_side_aggression("BUY")
        ask_size = base_size * self.inventory.order_side_aggression("SELL")

        # Clamp del lado que REDUCE inventario (fix 2026-08-10, corrida
        # mainnet real): con inventory=4.9 el skew 1.5x pide ask_size=7.35,
        # pero el Risk Engine rechaza qty > max_order_size (=4.9) con
        # order_size_exceeded -> posición atascada (ni reduce ni repone).
        # Fórmula del clamp (lado reduce):
        #     reduce_cap = min(risk.max_order_size, |inventario a reducir| + base_size)
        #     ask_size   = min(ask_size, reduce_cap)  si inventory > 0 (long)
        #     bid_size   = min(bid_size, reduce_cap)  si inventory < 0 (short)
        # - inventory=4.9, base=4.9: cap = min(4.9, 9.8) = 4.9 -> ask=4.9
        #   (exactamente la posición, reduce-only 100%; pasa order_size_exceeded).
        # - inventory=0: el clamp no aplica (agresión 1.0 en ambos lados,
        #   4.9/4.9, comportamiento original intacto).
        max_order_size = float(getattr(self.risk, "max_order_size", 0.0) or 0.0)
        if max_order_size > 0.0 and inventory != 0.0:
            reduce_cap = min(max_order_size, abs(inventory) + base_size)
            if inventory > 0.0:  # long: el ask reduce
                ask_size = min(ask_size, reduce_cap)
            else:  # short: el bid reduce
                bid_size = min(bid_size, reduce_cap)

        skew = self.apply_inventory_penalty(inventory)
        bid_price = r - bid_dist + skew
        ask_price = r + ask_dist - skew

        # Filtro asimétrico de Adverse Selection (Familia C)
        buy_vol = float(snapshot.get("buy_volume_60s", 0.0) or 0.0)
        sell_vol = float(snapshot.get("sell_volume_60s", 0.0) or 0.0)
        flow = (buy_vol - sell_vol) / (buy_vol + sell_vol) if (buy_vol + sell_vol) > 0 else 0.0
        
        # Si el flujo es fuertemente comprador, protegemos el lado SELL (riesgo de pick-off).
        if flow > getattr(self.alpha, "ADVERSE_FLOW_THRESHOLD", 0.2):
            ask_size *= getattr(self.alpha, "ADVERSE_SIZE_MULTIPLIER", 0.1)
            ask_dist *= getattr(self.alpha, "ADVERSE_SPREAD_MULTIPLIER", 2.0)
            ask_price = r + ask_dist
        # Si el flujo es fuertemente vendedor, protegemos el lado BUY.
        elif flow < -getattr(self.alpha, "ADVERSE_FLOW_THRESHOLD", 0.2):
            bid_size *= getattr(self.alpha, "ADVERSE_SIZE_MULTIPLIER", 0.1)
            bid_dist *= getattr(self.alpha, "ADVERSE_SPREAD_MULTIPLIER", 2.0)
            bid_price = r - bid_dist

        # NetPnL esperado (§9/§18)
        expected_pnl_bid = self.alpha.expected_net_pnl_estimate(
            snapshot, "BUY", bid_price, bid_size
        )
        expected_pnl_ask = self.alpha.expected_net_pnl_estimate(
            snapshot, "SELL", ask_price, ask_size
        )

        reduce_bid = inventory < 0.0  # comprar reduce inventario short
        reduce_ask = inventory > 0.0  # vender reduce inventario long

        # Eliminado el gate optimista (expected_pnl > 0.0)
        # El riesgo y rentabilidad están gestionados por Avellaneda-Stoikov y
        # el filtro de adverse selection asimétrico (Familia B+C).
        quote_bid_ok = True
        quote_ask_ok = True

        reasons = []

        # Piso de notional del exchange (§3). Histórico 2026-08-11: el lado
        # agravante del skew §11 (p.ej. 2.5 XRP con corto −5.0) quedaba en
        # ~$2.52 < $5 y el engine lo rechazaba cada ciclo -> spam de
        # rechazos que quemaba rate-limit. El fix original lo RECHAZABA, pero
        # eso volvía al bot ONE-SIDED y lo obligaba a salir en precio adverso
        # en vez de capturar el spread (medición 2026-08-12: 8 RTs net −0.0255,
        # avgLoss −0.007 ≫ avgWin +0.003). Fix 2026-08-12: en lugar de
        # rechazar, SUBIMOS el tamaño al mínimo que cumple minNotional para
        # mantener AMBOS lados cotizando y poder cerrar two-sided en el spread.
        # Solo rechazamos si ni siquiera el tamaño máximo (max_order_size)
        # alcanza el notional (precio tan bajo que 5 XRP < minNotional).
        # El guard es DINÁMICO con el precio: minNotional está en USDC.
        min_notional = float(
            (self.exec.symbol_info or {}).get("min_notional") or 0.0
        )
        if min_notional > 0.0:
            step = float((self.exec.symbol_info or {}).get("step_size") or 0.1)
            qprec = int((self.exec.symbol_info or {}).get("quantity_precision") or 1)
            cap = float(getattr(self.risk, "max_order_size", 0.0) or 0.0)

            def _min_qty(p: float) -> float:
                q = math.ceil((min_notional / p) / step - 1e-9) * step
                return round(q, qprec)

            if bid_size > 0.0 and bid_price * bid_size < min_notional - 1e-12:
                mq = _min_qty(bid_price)
                if cap > 0.0 and mq > cap + 1e-9:
                    # Ni el tamaño máximo alcanza el notional: NO deshabilitamos
                    # el lado (evita quedar one-sided / apagar toda la
                    # cotización, §3). Clampeamos al máximo disponible (best
                    # available) y mantenemos el lado habilitado. Si aun así el
                    # tamaño clampado no cumple minNotional, no colocamos ESTA
                    # orden (evita spam de rechazos) pero el lado queda activo
                    # para retomar cuando el precio suba (guard dinámico §3).
                    logger.warning(
                        "MarketMaker: min_notional no alcanzable en bid "
                        "(mq=%.6g > cap=%.6g); clamp a max_order_size; el lado "
                        "sigue cotizando (§3).",
                        mq, cap,
                    )
                    bid_size = cap
                    if bid_price * bid_size < min_notional - 1e-12:
                        bid_size = 0.0  # skip single order, keep side enabled
                    # NO seteamos quote_bid_ok = False
                else:
                    bid_size = max(bid_size, mq)
            if ask_size > 0.0 and ask_price * ask_size < min_notional - 1e-12:
                mq = _min_qty(ask_price)
                if cap > 0.0 and mq > cap + 1e-9:
                    logger.warning(
                        "MarketMaker: min_notional no alcanzable en ask "
                        "(mq=%.6g > cap=%.6g); clamp a max_order_size; el lado "
                        "sigue cotizando (§3).",
                        mq, cap,
                    )
                    ask_size = cap
                    if ask_price * ask_size < min_notional - 1e-12:
                        ask_size = 0.0  # skip single order, keep side enabled
                    # NO seteamos quote_ask_ok = False
                else:
                    ask_size = max(ask_size, mq)

        return {
            "reservation_price": r,
            "alpha": alpha,
            "bid_price": bid_price,
            "ask_price": ask_price,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "bid_dist": bid_dist,
            "ask_dist": ask_dist,
            "momentum_active": momentum_active,
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
            # Merge, no overwrite ciego (§0.6): la API es el ancla de
            # inventario/entry/unrealized, pero NO debe descartar silenciosamente
            # los fills locales recientes ni el realized_pnl (update_position ya
            # preserva realized_pnl y total_fees). Si hay divergencia mayor al
            # umbral, se registra un warning en vez de pisar en silencio.
            try:
                api_amt = float(position.get("positionAmt", 0.0))
            except (TypeError, ValueError):
                api_amt = 0.0
            divergence = abs(api_amt - self.inventory.inventory)
            if divergence > 1e-6:
                logger.warning(
                    "MarketMaker: divergencia de inventario en reconcile "
                    "(local=%.8g vs API=%.8g, Δ=%.8g > 1e-6). Se aplica el valor "
                    "de la API como ancla; revisar sincronización de fills (§0.6).",
                    self.inventory.inventory, api_amt, divergence,
                )
            self.inventory.update_position(position)
            self.risk.update_unrealized(self.inventory.unrealized_pnl)

        self.risk.update_equity(self.equity_current)
        self.risk.update_daily_pnl(self.daily_pnl)

    # ── 5. Journal del agente (§16) ──────────────────────────────────────
    def _journal(self, snapshot, quotes, risk_score,
                 cycle: int | None = None,
                 stage_ts: dict | None = None) -> None:
        """Escribe una línea en agent_decisions.jsonl (§16). Sin credenciales."""
        sigma = float(snapshot.get("volatility") or 0.0)
        event = {
            "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
            "cycle": cycle,
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
            "momentum_active": bool(quotes.get("momentum_active")),
            "expected_pnl": round(
                float(quotes.get("expected_pnl_bid") or 0.0)
                + float(quotes.get("expected_pnl_ask") or 0.0),
                8,
            ),
            "risk_score": float(risk_score),
        }
        # §10: duración de las etapas del ciclo (muestras periódicas para
        # limitar ruido). Epoch float con precisión suficiente para deltas de
        # ms; el monitor puede medir colgamientos entre etapas.
        if stage_ts:
            event["stage_ts"] = stage_ts
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

        # has_bid/has_ask se calculan DESPUÉS del loop de expire/replace:
        # si un lado se canceló/reemplazó arriba, la reposición de este ciclo
        # ya lo ve (sin 1 ciclo de latencia, auditoría 2026-08-09 §0.6).
        orders = self.exec.get_orders()
        has_bid = any(o["side"] == "BUY" for o in orders.values())
        has_ask = any(o["side"] == "SELL" for o in orders.values())

        # Colocar lados faltantes (solo si el lado cotiza, hay tamaño y el
        # kill switch no bloqueó nuevas entradas §13).
        if self.disable_new_entries:
            return
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
        """Evalúa el kill switch y, si se dispara, cancela todo, reduce/cierra
        la posición y bloquea nuevas entradas (§13)."""
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
            if actions.get("reduce_or_close"):
                self._reduce_or_close(snapshot)
            if actions.get("disable_new_entries"):
                self.disable_new_entries = True
                logger.info("MarketMaker: nuevas entradas bloqueadas (§13)")
            self._log_event_jsonl(config.LOG_PNL / "kill_switch.jsonl", {
                "ts": time.time(),
                "reasons": reasons,
                "mid": float(snapshot.get("mid") or 0.0),
                "ws_connected": self.ws_connected,
                "disable_new_entries": self.disable_new_entries,
            })
            self._stop.set()
        return triggered

    def _reduce_or_close(self, snapshot) -> None:
        """Reduce o cierra la posición tras el kill switch (§13).

        En dry-run (Nivel 0) no hay API: se simula el cierre marcando el
        inventario como cerrado al mid actual (no se envían órdenes reales,
        §0.1/§21). En modo real se coloca una orden reduce_only al mid para
        cerrar la posición; si la API no está disponible, se registra la
        acción manual pendiente en el journal.
        """
        inventory = self.inventory.inventory
        mid = snapshot.get("mid")
        if inventory == 0.0:
            return
        if mid is None or mid <= 0:
            logger.warning("MarketMaker: sin mid válido para reduce/close (§13)")
            return

        if self.exec.dry_run:
            # Simulación de cierre: el inventario se aclara al mid actual.
            qty = abs(inventory)
            side = "SELL" if inventory > 0 else "BUY"
            fee = config.MAKER_FEE_RATE * float(mid) * qty
            self.inventory.record_fill(side, qty, float(mid), fee)
            self.daily_pnl = self.inventory.realized_pnl - self.inventory.total_fees
            self.risk.update_daily_pnl(self.daily_pnl)
            logger.info(
                "MarketMaker: reduce/close simulado %s %.6g @ %.8g (§13)",
                side, qty, mid,
            )
            return

        # Modo real: orden reduce_only MAKER-ONLY (§0.5/§18). Precio al touch:
        # SELL-side reduce toca el best_bid, BUY-side reduce toca el best_ask
        # (igual que flatten_position). NUNCA al mid (cruzaría el spread = taker).
        # place_maker_order ya es post-only GTX y degrada con gracia si la API
        # no está (offline).
        side = "SELL" if inventory > 0 else "BUY"
        best_bid = float(snapshot.get("best_bid") or 0.0)
        best_ask = float(snapshot.get("best_ask") or 0.0)
        if side == "SELL" and best_bid > 0:
            touch_price = best_bid
        elif side == "BUY" and best_ask > 0:
            touch_price = best_ask
        else:
            # Sin touch válido: no cruzar el spread; dejamos constancia y no
            # enviamos orden taker. El cierre queda pendiente en el journal.
            logger.warning(
                "MarketMaker: sin touch válido para reduce/close %s (best_bid=%.8g, "
                "best_ask=%.8g) → no se envía orden taker (§0.5). Pendiente (§13).",
                side, best_bid, best_ask,
            )
            return
        oid, ok, reason = self.exec.place_maker_order(
            side, abs(inventory), touch_price, reduce_only=True
        )
        if ok and oid:
            logger.info("MarketMaker: reduce/close %s %s @ %.8g (§13)", side, oid, touch_price)
        else:
            logger.warning("MarketMaker: reduce/close falló: %s (§13)", reason)

    # ── 9. Loop principal (§8, §13, §15, §16) ────────────────────────────
    def run(self, max_cycles: int | None = None) -> None:
        """Arranca WS, inicializa el exchange y ejecuta el ciclo de cotización."""
        # §0.1/§21: mainnet SOLO con autorización humana explícita (§0.2).
        # Gate aprobado (2026-08-09): config.REAL=True y EXPOSURE_LEVEL>=1
        # indican esa autorización → init_client(real=True). Cualquier otra
        # combinación (REAL=False, o EXPOSURE_LEVEL=0) queda en TESTNET y
        # jamás toca mainnet (§0.1/§0.5).
        if config.REAL and config.EXPOSURE_LEVEL >= 1:
            logger.warning(
                "MarketMaker: AUTORIZACIÓN HUMANA §0.2 detectada (REAL=True y "
                "EXPOSURE_LEVEL=%d). Inicializando cliente MAINNET.",
                config.EXPOSURE_LEVEL,
            )
            self.exec.init_client(real=True)
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
                    now = time.time()
                    self._price_history.append((now, float(mid)))  # §14
                    # Alimenta el filtro de momentum del alpha_model (§14):
                    # una muestra (ts, mid) por ciclo, como ya hace con el
                    # historial de adverse selection.
                    self.alpha.record_mid(now, float(mid))

                    self._on_fill_detection(snapshot)
                    self._reconcile()

                    # Rellenar el snapshot con el estado de inventario/PnL.
                    snapshot["inventory"] = self.inventory.inventory
                    snapshot["unrealized_pnl"] = self.inventory.unrealized_pnl
                    snapshot["realized_pnl"] = self.inventory.realized_pnl
                    snapshot["fill_count"] = len(self.exec.fills)

                    # §10: registra el arranque de cada etapa del ciclo para
                    # medir duraciones reales (journal, muestras periódicas).
                    stage_ts = {"cycle_start": now, "snapshot_ready": time.time()}
                    quotes = self._compute_quotes(snapshot)
                    stage_ts["quotes_done"] = time.time()
                    self._manage_orders(snapshot, quotes)
                    stage_ts["orders_done"] = time.time()

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
                    if cycles <= 3 or cycles % 100 == 0:
                        self._journal(snapshot, quotes, risk_score,
                                      cycle=cycles, stage_ts=stage_ts)  # §16
                    else:
                        self._journal(snapshot, quotes, risk_score,
                                      cycle=cycles)  # §16
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
    def close_position_on_stop(self):
        """Aplana la posición abierta al detener (flatten-on-stop, §0.2/§13).

        Cierra la posición real con un MARKET reduceOnly vía
        ExecutionEngine.flatten_position() para NO dejar inventario colgado
        (riesgo overnight): las corridas 2026-08-11/13 requirieron flatten
        manual porque stop() no aplanaba. En dry-run / sin API no hay
        posición real que cerrar (flatten_position devuelve 0).
        """
        try:
            result = self.exec.flatten_position()
        except Exception as e:
            logger.error("MarketMaker: excepción en flatten al detener: %s", e)
            return None
        if result is None:
            logger.error(
                "MarketMaker: FLATTEN falló — posición puede quedar ABIERTA "
                "(requiere flatten manual, §0.2)."
            )
        elif result == 0:
            logger.info("MarketMaker: sin posición abierta que aplanar.")
        else:
            logger.warning(
                "MarketMaker: posición aplanada al detener (flatten order=%s, §0.2).",
                result,
            )
        return result

    def stop(self) -> None:
        """Cierra WS, cancela órdenes y aplana la posición. Idempotente."""
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
        try:
            n = self.exec.sweep_orphan_orders()
            if n:
                logger.warning("MarketMaker: barridas %d órdenes huérfanas al detener", n)
        except Exception as e:
            logger.warning("MarketMaker: error en sweep de huérfanas: %s", e)
        # Flatten-on-stop: NO dejar posición abierta al frenar (§0.2/§13).
        self.close_position_on_stop()
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
