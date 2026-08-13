# inventory_manager.py
"""
InventoryManager (§11) — gestión de inventario y PnL del market maker.

Binance Futures, XRPUSDC. Módulo independiente, sin dependencias externas.
Lee los límites de riesgo de `strategy/config.py` (§0.4): la exposición
efectiva y el nocional máximo de posición son constantes centrales, nunca
números improvisados acá.

Convención de signos (§0.6, NO confundir):
    inventory > 0  →  LONG  = compras netas (BUY incrementa inventory).
    inventory < 0  →  SHORT = ventas netas (SELL decrementa inventory).
    BUY  → inventory += qty.
    SELL → inventory -= qty.

Realized PnL por media móvil (moving average, §11):
    - Agregar en la MISMA dirección que la posición (o partir de 0) →
      NO realiza PnL, solo re-promedia el entry_price.
    - REDUCIR (dirección opuesta sin cruzar) → realiza PnL sobre la
      cantidad reducida con el entry_price vigente:
          cierre de LONG :  (price - entry_price) * qty_reducida
          cierre de SHORT:  (entry_price - price) * qty_reducida
    - CRUZAR (dirección opuesta supera la posición) → la parte que cierra
      realiza PnL y la parte que cruza abre una nueva posición con
      entry_price = price.
    - Las fees se acumulan por separado en total_fees (nunca se restan del
      realized_pnl acá; ese neteo lo hace el reporte de NetPnL, §18).
"""

from . import config


class InventoryManager:
    """Estado de inventario y PnL del market maker (§11)."""

    def __init__(self) -> None:
        self.inventory: float = 0.0
        self.entry_price: float | None = None
        self.realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.total_fees: float = 0.0

    @staticmethod
    def _safe_div(numer: float, denom: float) -> float:
        """División segura: denominador 0 o None → 0.0."""
        if not denom:
            return 0.0
        return numer / denom

    @staticmethod
    def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
        """Acota value al rango [lo, hi]."""
        return max(lo, min(hi, value))

    # ── Estado ─────────────────────────────────────────────────────
    def update_position(self, position_info: dict | None) -> None:
        """Sincroniza el estado desde la respuesta de la API (§2).

        position_info: dict con claves 'positionAmt', 'entryPrice',
        'unRealizedProfit' (formato de get_position). Si es None o la
        posición es cero → inventario plano (inventory = 0, sin entry).
        """
        if position_info is None:
            self.inventory = 0.0
            self.entry_price = None
            self.unrealized_pnl = 0.0
            return

        try:
            amt = float(position_info.get("positionAmt", 0.0))
        except (TypeError, ValueError):
            amt = 0.0

        if amt == 0.0:
            self.inventory = 0.0
            self.entry_price = None
            self.unrealized_pnl = 0.0
            return

        self.inventory = amt
        self.entry_price = float(position_info.get("entryPrice", 0.0))
        self.unrealized_pnl = float(position_info.get("unRealizedProfit", 0.0))

    def record_fill(self, side: str, qty: float, price: float, fee: float) -> None:
        """Registra un fill y actualiza inventario, entry y realized_pnl.

        side: 'BUY' o 'SELL' (Binance Futures). BUY suma +qty al
        inventario, SELL lo decrementa en qty (§0.6).
        """
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side inválido: {side!r} (esperado 'BUY' o 'SELL')")

        qty = float(qty)
        price = float(price)
        fee = float(fee)
        signed_qty = qty if side == "BUY" else -qty

        prev = self.inventory
        new = prev + signed_qty
        self.total_fees += fee

        # Defensa: inventario sin entry definido se trata como apertura.
        if self.entry_price is None:
            self.inventory = new
            self.entry_price = price if new != 0.0 else None
            return

        # Apertura desde plano: sin realización de PnL.
        if prev == 0.0:
            self.inventory = new
            self.entry_price = price if new != 0.0 else None
            return

        same_direction = (prev > 0.0 and signed_qty > 0.0) or (
            prev < 0.0 and signed_qty < 0.0
        )

        if same_direction:
            # Agregar en la misma dirección: sin realización, re-promedia
            # el entry_price (media ponderada por cantidad absoluta).
            prev_abs = abs(prev)
            new_abs = abs(new)
            self.entry_price = self._safe_div(
                self.entry_price * prev_abs + price * abs(signed_qty), new_abs
            )
            self.inventory = new
            return

        # Dirección opuesta: reduce o cruza (§11).
        reduction = min(abs(signed_qty), abs(prev))
        if prev > 0.0:
            # Cierre de LONG: (price - entry_price) * qty_reducida.
            self.realized_pnl += (price - self.entry_price) * reduction
        else:
            # Cierre de SHORT: (entry_price - price) * qty_reducida.
            self.realized_pnl += (self.entry_price - price) * reduction

        self.inventory = new
        if new == 0.0:
            self.entry_price = None
        elif new * prev < 0.0:
            # Cruzó: la parte restante abre nueva posición a este precio.
            self.entry_price = price
        # Reducción parcial: se conserva el entry_price de la posición.

    # ── Derivados para el market maker (§11) ───────────────────────
    def inventory_skew(self, snapshot_imbalance: float, mid_price: float) -> float:
        """Skew de inventario en [-1, 1] combinado con el imbalance.

        - inventario LONG grande  → skew negativo (favorece reducir,
          cotizar más agresivo en el ask).
        - inventario SHORT grande → skew positivo.
        - target = MAX_POSITION_NOTIONAL_USDC / 4: si
          |inventory * mid| >= target el sesgo puro satura a ±1.
        - Luego se combina con el imbalance de mercado (0.3x) y todo se
          acota a [-1, 1].
        """
        mid = float(mid_price or 0.0)
        target = config.MAX_POSITION_NOTIONAL_USDC / 4.0

        # División segura: sin mid o sin target no hay sesgo de inventario.
        base = -self._safe_div(self.inventory * mid, target) if target else 0.0
        base = self._clamp(base)

        skew = base + 0.3 * float(snapshot_imbalance)
        return self._clamp(skew)

    def is_inventory_healthy(self, max_notional: float, mid_price: float) -> bool:
        """True si |inventory * mid_price| <= max_notional."""
        return abs(self.inventory * float(mid_price or 0.0)) <= float(max_notional)

    def order_side_aggression(self, side: str) -> float:
        """Factor de agresión en [0.5, 1.5] para un side dado.

        - side='BUY' con inventario long  → 0.5 (menos agresivo: no sumar).
        - side='BUY' con inventario short → 1.5 (más agresivo: reducir).
        - Inverso para side='SELL'.
        - Inventario ~0 (deadzone de media orden base) → 1.0 (neutro).
        """
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side inválido: {side!r} (esperado 'BUY' o 'SELL')")

        deadzone = 0.5 * config.BASE_ORDER_SIZE_XRP
        if abs(self.inventory) <= deadzone:
            return 1.0

        if side == "BUY":
            return 0.5 if self.inventory > 0.0 else 1.5
        # SELL
        return 1.5 if self.inventory > 0.0 else 0.5

    def position_within_limits(
        self, proposed_qty: float, side: str, mid_price: float
    ) -> bool:
        """True si la posición PROYECTADA no supera el nocional máximo (§11).

        inventory_proyectado = inventory + qty (BUY) o inventory - qty (SELL).
        Se evalúa en magnitud (|proyectado * mid|) para acotar también el
        lado corto: nunca permitir inventario ilimitado en ningún sentido.
        """
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side inválido: {side!r} (esperado 'BUY' o 'SELL')")

        mid = float(mid_price or 0.0)
        qty = float(proposed_qty)
        projected = self.inventory + qty if side == "BUY" else self.inventory - qty
        return abs(projected * mid) <= config.MAX_POSITION_NOTIONAL_USDC

    def get_state(
        self, snapshot_imbalance: float = 0.0, mid_price: float = 0.0
    ) -> dict:
        """Snapshot completo del estado para logging/reportes (§15)."""
        return {
            "inventory": self.inventory,
            "entry_price": self.entry_price,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_fees": self.total_fees,
            "skew": self.inventory_skew(snapshot_imbalance, mid_price),
        }
