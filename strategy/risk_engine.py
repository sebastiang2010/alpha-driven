# risk_engine.py
"""
Risk Engine para el bot de market making de Binance Futures (XRPUSDC).

Cumple §12: módulo independiente que lee TODOS los límites de config.py
(jamás hardcodeados) y rechaza propuestas de órdenes o condiciones de
mercado/estado que violen alguno de esos límites. Los tests unitarios que
verifican los rechazos viven en strategy/tests/test_risk_engine.py (§12).

==============================================================================
REGLA CRÍTICA (§12): cualquier propuesta que viole un límite DEBE devolver
allowed=False. Los límites se leen de config — si config no tuviera una
constante, se usa un valor propuesto conservador marcado como
"propuesta pendiente de confirmación (§0.4)".
==============================================================================

Límites aplicados (todos derivados de config salvo MAX_VOLATILITY):
    - maximum position notional  -> config.MAX_POSITION_NOTIONAL_USDC
    - maximum inventory (XRP)     -> MAX_POSITION_NOTIONAL_USDC / precio_ref
    - maximum daily loss          -> config.MAX_DAILY_LOSS_USDC
    - maximum drawdown            -> config.MAX_DRAWDOWN_PCT sobre equity
    - maximum unrealized loss     -> max(2.0, 0.5 * MAX_DAILY_LOSS_USDC)
                                     (PROPUESTA pendiente de confirmación §0.4)
    - maximum order size          -> BASE_ORDER_SIZE_XRP *
                                     EXPOSURE_MULTIPLIERS[EXPOSURE_LEVEL]
                                     (nivel 0 => multiplicador 0.0 => sin órdenes)
    - maximum open orders         -> config.MAX_OPEN_ORDERS
    - maximum exposure            -> config.MAX_POSITION_NOTIONAL_USDC
    - maximum volatility          -> MAX_VOLATILITY (constante propia,
                                     PROPUESTA pendiente de confirmación §0.4)
"""

from typing import Dict, List, Optional, Tuple

from . import config

# ---------------------------------------------------------------------------
# Constantes propias del Risk Engine (§0.4 — propuestas pendientes)
# ---------------------------------------------------------------------------

# Volatilidad máxima admitida para cotizar. Valor propuesto conservador;
# si config llegara a definirla, se prefiere la de config.
MAX_VOLATILITY: float = 0.05  # propuesta pendiente de confirmación (§0.4)

# Margen de seguridad sobre la posición máxima: por encima de esto, el kill
# switch considera que la exposición es "inesperada" (§13).
UNEXPECTED_EXPOSURE_MULTIPLIER: float = 1.5

# Umbral de errores consecutivos que dispara el kill switch (§13).
MAX_ERROR_COUNT: int = 5

# Multiplicador propuesto para derivar el límite de pérdida no realizada.
UNREALIZED_LOSS_FACTOR: float = 0.5
UNREALIZED_LOSS_FLOOR_USDC: float = 2.0

# Razon strings usados en las listas de `reasons`.
REASON_POSITION_NOTIONAL = "position_notional_exceeded"
REASON_INVENTORY = "inventory_exceeded"
REASON_ORDER_SIZE = "order_size_exceeded"
REASON_MAX_OPEN_ORDERS = "max_open_orders_reached"
REASON_DAILY_LOSS = "daily_loss_exceeded"
REASON_DRAWDOWN = "drawdown_exceeded"
REASON_UNREALIZED_LOSS = "unrealized_loss_exceeded"
REASON_VOLATILITY = "volatility_exceeded"
REASON_EXPOSURE = "exposure_exceeded"
REASON_INVALID_ORDER = "invalid_order"

REASON_KS_DAILY_LOSS = "daily_loss_exceeded"
REASON_KS_DRAWDOWN = "drawdown_exceeded"
REASON_KS_UNREALIZED_LOSS = "unrealized_loss_exceeded"
REASON_KS_REALIZED_LOSS_GUARD = "realized_loss_guard_exceeded"
REASON_KS_WS_DISCONNECTED = "ws_disconnected"
REASON_KS_TOO_MANY_ERRORS = "too_many_errors"
REASON_KS_MID_INVALID = "mid_invalid"
REASON_KS_UNEXPECTED_EXPOSURE = "unexpected_exposure"


def _as_float(value, default: float = 0.0) -> float:
    """Coerción segura a float (None -> default)."""
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class RiskEngine:
    """
    Motor de riesgo independiente: lee límites de config y valida propuestas.

    Uso (en el market_maker):
        re = RiskEngine(max_order_size_override=None, reference_equity=1000.0)
        allowed, reasons = re.check_order(symbol, side, qty, price, notional,
                                          open_orders_count, state_snapshot)
        triggered, reasons = re.check_kill_switch(state_snapshot,
                                                  ws_connected, error_count)
    """

    def __init__(
        self,
        max_order_size_override: Optional[float] = None,
        reference_equity: float = 1000.0,
    ):
        # Límites derivados de config (NUNCA hardcodeados, §0.4/§12).
        self.max_position_notional = float(config.MAX_POSITION_NOTIONAL_USDC)
        self.max_daily_loss = float(config.MAX_DAILY_LOSS_USDC)
        self.max_drawdown_pct = float(config.MAX_DRAWDOWN_PCT)
        self.max_open_orders = int(config.MAX_OPEN_ORDERS)
        self.max_exposure = float(config.MAX_POSITION_NOTIONAL_USDC)

        # Límite de pérdida no realizada: propuesta (§0.4).
        self.max_unrealized_loss = max(
            UNREALIZED_LOSS_FLOOR_USDC,
            UNREALIZED_LOSS_FACTOR * float(config.MAX_DAILY_LOSS_USDC),
        )

        # Guard de pérdida realizada ajustado (§0.2 / "el bot no da pérdida"):
        # frena y aplana ante cualquier pérdida neta realizada. PROPUESTA
        # pendiente de confirmación (§0.4).
        self.loss_guard = float(config.LOSS_GUARD_USDC)

        # Volatilidad máxima: se prefiere config si algún día la define.
        self.max_volatility = float(getattr(config, "MAX_VOLATILITY", MAX_VOLATILITY))

        # Tamaño máximo de orden por nivel de exposición. Nivel 0 (dry-run)
        # usa SIMULATION_QUOTE_MULTIPLIER (decisión aprobada §0.2: simula
        # órdenes en vez del multiplicador 0.0 del nivel 0). Los niveles
        # reales (>=1) usan EXPOSURE_MULTIPLIERS y NO se modifican.
        config_max_order_size = (
            float(config.BASE_ORDER_SIZE_XRP)
            * config.effective_exposure_multiplier()
        )
        # override SOLO para tests (§12): permite anular el tamaño máximo.
        if max_order_size_override is not None:
            self.max_order_size = float(max_order_size_override)
        else:
            self.max_order_size = config_max_order_size

        # Equity de referencia para drawdown (por defecto 1000 USDC).
        self.reference_equity = float(reference_equity)

        # Estado interno (alimentado por los setters).
        self._state: Dict[str, float] = {
            "inventory": 0.0,
            "mid": 0.0,
            "daily_pnl": 0.0,
            "drawdown": 0.0,
            "unrealized_pnl": 0.0,
            "error_count": 0,
            "current_position_notional": 0.0,
            "equity": self.reference_equity,
            "peak_equity": self.reference_equity,
        }

    # ── Límites derivados ───────────────────────────────────────────────

    def max_inventory(self, price_ref: float) -> float:
        """Inventario máximo en XRP = max_position_notional / precio_ref."""
        price_ref = _as_float(price_ref)
        if price_ref <= 0:
            return 0.0
        return self.max_position_notional / price_ref

    # ── Merge de snapshot con estado interno ────────────────────────────

    def _merged(self, state_snapshot: Optional[dict]) -> dict:
        """Estado efectivo: estado interno con el snapshot por encima."""
        merged = dict(self._state)
        if state_snapshot:
            merged.update(state_snapshot)
        return merged

    # ── Cálculos de estado derivado ─────────────────────────────────────

    def _compute_drawdown(self, snap: dict) -> float:
        """Drawdown: usa el key 'drawdown' si viene; si no, lo deriva de
        peak_equity/current_equity (o de la equity de referencia)."""
        dd = snap.get("drawdown")
        if dd is not None:
            return _as_float(dd)
        peak = snap.get("peak_equity")
        if peak is None:
            peak = snap.get("equity", self.reference_equity)
        peak = _as_float(peak)
        current = snap.get("current_equity")
        if current is None:
            current = snap.get("equity", peak)
        current = _as_float(current)
        if peak > 0:
            return max(0.0, (peak - current) / peak)
        return 0.0

    @staticmethod
    def _loss_from_pnl(pnl_value) -> float:
        """Convierte un PnL (que puede ser negativo) en una pérdida positiva."""
        pnl = _as_float(pnl_value)
        return max(0.0, -pnl) if pnl < 0 else 0.0

    # ── Validación de propuesta de orden (§12) ──────────────────────────

    def check_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        notional: float,
        open_orders_count: int,
        state_snapshot: Optional[dict] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Verifica TODOS los límites para una propuesta de orden.

        Rechaza (allowed=False + reason) si:
            - notional propuesto + posición actual > max position notional
            - inventario proyectado > inventario máximo (derivado de precio)
            - qty > max order size (nivel 0 => cualquier qty>0)
            - open_orders_count >= MAX_OPEN_ORDERS
            - daily_loss >= MAX_DAILY_LOSS_USDC
            - drawdown >= MAX_DRAWDOWN_PCT
            - unrealized_loss >= max unrealized
            - volatility > MAX_VOLATILITY
            - exposición total proyectada > max exposure
            - qty/price/notional no positivos (orden inválida)
        """
        reasons: List[str] = []
        snap = self._merged(state_snapshot)

        qty = _as_float(qty)
        price = _as_float(price)
        notional = _as_float(notional)
        open_orders_count = int(_as_float(open_orders_count))

        # Orden inválida.
        if qty <= 0 or price <= 0 or notional <= 0:
            reasons.append(REASON_INVALID_ORDER)

        current_position_notional = _as_float(
            snap.get("current_position_notional")
        )
        inventory = _as_float(snap.get("inventory"))

        # Posición nocional proyectada según el lado de la orden.
        if side == "ask":
            projected_notional = current_position_notional - notional
            projected_inventory = inventory - qty
        else:  # 'bid' (y cualquier otro lado) suma posición larga.
            projected_notional = current_position_notional + notional
            projected_inventory = inventory + qty

        # Orden REDUCTORA: acerca el inventario a cero (vende si está long,
        # compra si está short). Siempre debe permitirse para poder DESARMAR
        # la posición; si no, el market maker se congela en máximo inventario
        # (ver run3: 16 min congelado sin poder hacer SELL para reducir).
        is_reducing = (side == "ask" and inventory > 0) or (
            side == "bid" and inventory < 0
        )

        # 1. Notional propuesto + posición actual > max position notional.
        if abs(projected_notional) > self.max_position_notional:
            reasons.append(REASON_POSITION_NOTIONAL)

        # 2. Inventario máximo (derivado en XRP desde el precio).
        # Se salta si la orden REDUCE (debe poder desarmar).
        if (
            not is_reducing
            and price > 0
            and abs(projected_inventory) * price > self.max_position_notional
        ):
            reasons.append(REASON_INVENTORY)

        # 3. Tamaño máximo de orden (nivel 0 => rechaza toda qty>0).
        if qty > self.max_order_size:
            reasons.append(REASON_ORDER_SIZE)

        # 4. Máximo de órdenes abiertas.
        if open_orders_count >= self.max_open_orders:
            reasons.append(REASON_MAX_OPEN_ORDERS)

        # 5. Pérdida diaria.
        daily_loss = self._loss_from_pnl(snap.get("daily_pnl"))
        if daily_loss >= self.max_daily_loss:
            reasons.append(REASON_DAILY_LOSS)

        # 6. Drawdown.
        if self._compute_drawdown(snap) >= self.max_drawdown_pct:
            reasons.append(REASON_DRAWDOWN)

        # 7. Pérdida no realizada.
        unrealized_loss = self._loss_from_pnl(snap.get("unrealized_pnl"))
        if unrealized_loss >= self.max_unrealized_loss:
            reasons.append(REASON_UNREALIZED_LOSS)

        # 8. Volatilidad.
        volatility = _as_float(snap.get("volatility"))
        if volatility > self.max_volatility:
            reasons.append(REASON_VOLATILITY)

        # 9. Exposición total proyectada (bruta, incluye ambos lados).
        # Se salta si la orden REDUCE (debe poder desarmar). En otro caso se
        # usa el nocional PROYECTADO (no el actual) para no penalizar una orden
        # que reduce la exposición al sumar su notional al actual.
        if not is_reducing:
            gross_exposure = abs(projected_notional) + abs(notional)
            if gross_exposure > self.max_exposure:
                reasons.append(REASON_EXPOSURE)

        allowed = len(reasons) == 0
        return allowed, reasons

    # ── Kill switch (§13) ───────────────────────────────────────────────

    def check_kill_switch(
        self,
        state_snapshot: Optional[dict],
        ws_connected: bool,
        error_count: int,
    ) -> Tuple[bool, List[str]]:
        """
        Dispara el kill switch (halt de operaciones) ante:
            - daily loss excedido
            - drawdown excedido
            - unrealized_pnl excedido
            - WebSocket desconectado (ws_connected=False)
            - error_count >= 5
            - mid <= 0 (precio anómalo)
            - current_position_notional > MAX_POSITION_NOTIONAL_USDC * 1.5
        """
        reasons: List[str] = []
        snap = self._merged(state_snapshot)

        # Pérdida diaria.
        daily_loss = self._loss_from_pnl(snap.get("daily_pnl"))
        if daily_loss >= self.max_daily_loss:
            reasons.append(REASON_KS_DAILY_LOSS)

        # Guard de pérdida realizada ajustado (§0.2 / "el bot no da pérdida"):
        # el PnL diario realizado neto (realized - fees) cayó por debajo del
        # umbral ajustado. Frena y aplana para no acumular pérdida material.
        if daily_loss >= self.loss_guard:
            reasons.append(REASON_KS_REALIZED_LOSS_GUARD)

        # Drawdown.
        if self._compute_drawdown(snap) >= self.max_drawdown_pct:
            reasons.append(REASON_KS_DRAWDOWN)

        # Pérdida no realizada.
        unrealized_loss = self._loss_from_pnl(snap.get("unrealized_pnl"))
        if unrealized_loss >= self.max_unrealized_loss:
            reasons.append(REASON_KS_UNREALIZED_LOSS)

        # WebSocket desconectado (§13).
        if not ws_connected:
            reasons.append(REASON_KS_WS_DISCONNECTED)

        # Demasiados errores consecutivos (§13).
        effective_errors = max(
            int(error_count), int(_as_float(snap.get("error_count")))
        )
        if effective_errors >= MAX_ERROR_COUNT:
            reasons.append(REASON_KS_TOO_MANY_ERRORS)

        # Precio anómalo: mid <= 0.
        mid = _as_float(snap.get("mid"))
        if mid <= 0:
            reasons.append(REASON_KS_MID_INVALID)

        # Exposición inesperada.
        current_position_notional = _as_float(snap.get("current_position_notional"))
        if current_position_notional > self.max_position_notional * UNEXPECTED_EXPOSURE_MULTIPLIER:
            reasons.append(REASON_KS_UNEXPECTED_EXPOSURE)

        triggered = len(reasons) > 0
        return triggered, reasons

    # ── Acciones post-trigger (§13) ─────────────────────────────────────

    @staticmethod
    def actions_on_trigger(reasons: List[str]) -> dict:
        """Acciones a ejecutar cuando el kill switch se dispara."""
        return {
            "cancel_all": True,
            "reduce_or_close": True,
            "disable_new_entries": True,
        }

    # ── Setters de estado interno ───────────────────────────────────────

    def update_daily_pnl(self, pnl: float) -> None:
        """Actualiza el PnL diario acumulado (puede ser negativo)."""
        self._state["daily_pnl"] = _as_float(pnl)

    def update_equity(self, equity: float) -> None:
        """Actualiza la equity actual y, si corresponde, el pico."""
        equity = _as_float(equity)
        self._state["equity"] = equity
        self._state["peak_equity"] = max(
            _as_float(self._state.get("peak_equity")), equity
        )

    def update_unrealized(self, unrealized: float) -> None:
        """Actualiza el PnL no realizado (puede ser negativo)."""
        self._state["unrealized_pnl"] = _as_float(unrealized)

    def record_error(self) -> None:
        """Incrementa el contador de errores consecutivos."""
        self._state["error_count"] = int(_as_float(self._state.get("error_count"))) + 1

    def reset_error_count(self) -> None:
        """Reinicia el contador de errores consecutivos."""
        self._state["error_count"] = 0

    def update_price(self, mid: float) -> None:
        """Actualiza el mid price vigente."""
        self._state["mid"] = _as_float(mid)

    def get_state(self) -> dict:
        """Devuelve una copia del estado interno actual."""
        return dict(self._state)
