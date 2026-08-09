# alpha_model.py
"""
Modelo de alpha y re-cotizacion para el bot de market making de Binance
Futures (XRPUSDC).

Implementa el modelo de señales de corto plazo (§6) y la parte de
cotizaciones/riesgo-de-inventario (§8, §11) que corresponde al "alpha
model". NO toca el Risk Engine ni el execution (separamos responsables).

==============================================================================
CONVENCIONES DE TIEMPO / VOLATILIDAD (declaradas, §0.6: no mezclar escalas)
==============================================================================
- sigma (snapshot["volatility"]) es el desvío estandar de retornos log de
  mid por intervalo de muestreo de referencia. Consistente con market_state.py:
  NO anualizado, sin √T aplicado dentro de ese modulo.
- SAMPLING_INTERVAL_SEC = 5.0 es la constante de referencia que define ese
  intervalo (aunque el WS actualice mas seguido, tratamos cada sigma como
  si hubiese sido muestreado cada 5 s).
- INTERVALS_PER_MINUTE = 60 / SAMPLING_INTERVAL_SEC = 12.
  sigma_min = sigma * sqrt(INTERVALS_PER_MINUTE)   -> escala de MINUTOS.
  Se usa en reservation_price, donde T - t se expresa en MINUTOS.
- En quote_distances, el buffer de volatilidad sobre el horizonte de
  re-cotizacion es
      sigma_efectiva = sigma * sqrt(INTERVALS_PER_MINUTE) * sqrt(CYCLE_INTERVAL_SEC)
  Factor 1: pasa sigma de por-intervalo a por-minuto.
  Factor 2: amplia esa sigma de minutos al horizonte del ciclo en segundos.
  Cada √T se aplica UNA sola vez, siempre sobre sigma en su escala base;
  nunca se re-escala el resultado en otro punto (evita √T doble §0.6).
==============================================================================
"""

import math
from typing import Tuple

from . import config

# ---------------------------------------------------------------------------
# Pesos del alpha de corto plazo (§6) — PROPUESTA pendiente de confirmacion
# ---------------------------------------------------------------------------
MOMENTUM_WEIGHT: float = 0.30      # momentum normalizado del snapshot
IMBALANCE_WEIGHT: float = 0.20     # imbalance de volumen del depth
MICROPRICE_WEIGHT: float = 0.30    # (microprice - mid) / mid
TRADE_FLOW_WEIGHT: float = 0.20    # (buy - sell) / (buy + sell)

# Limite de la señal: ±1% del mid como maximo.
ALPHA_MAX: float = 0.01

# ---------------------------------------------------------------------------
# Parámetros de re-cotizacion (§8, §11) — PROPUESTA pendiente de confirmacion
# ---------------------------------------------------------------------------
GAMMA_INVENTORY_RISK: float = 0.5   # aversion al riesgo de inventario (f)
K_QUOTE: float = 1.0                # multiplicador de sigma_efectiva en quotes
ALPHA_QUOTE_FACTOR: float = 1.0     # cuanto ensancha |alpha| ambas puntas
INVENTORY_SKEW_MULTIPLIER: float = 0.5  # skew de precio por inventario normalizado
SKEW_SIZE: float = 0.6              # factor de reduccion del lado que aumenta inventario
MIN_QUOTE_DISTANCE: float = 1e-8    # piso para que (bid_dist, ask_dist) > 0

# ---------------------------------------------------------------------------
# Convencion de tiempo / volatilidad (§0.6)
# ---------------------------------------------------------------------------
SAMPLING_INTERVAL_SEC: float = 5.0             # intervalo de referencia de sigma
INTERVALS_PER_MINUTE: float = 60.0 / SAMPLING_INTERVAL_SEC  # = 12.0
DEFAULT_HORIZON_MINUTES: float = 5.0           # T - t default de la reserva


class AlphaModel:
    """
    Señal de corto plazo y derivados de cotizacion.

    Uso (en el ciclo del market_maker):
        am = AlphaModel()
        snap = market_state.get_snapshot()
        alpha = am.compute_alpha(snap)
        r = am.reservation_price(snap, alpha, snap["inventory"], snap["volatility"])
        bid_dist, ask_dist = am.quote_distances(snap, alpha, snap["inventory"], snap["volatility"])
    """

    @staticmethod
    def _safe_div(numer: float, denom: float) -> float:
        """División segura: 0.0 si el denominador es 0."""
        return numer / denom if denom else 0.0

    # ── 1. Señal de corto plazo (§6) ────────────────────────────────
    def compute_alpha(self, snapshot: dict) -> float:
        """
        Señal compuesta de corto plazo, acotada a [-ALPHA_MAX, ALPHA_MAX].

        Componentes (pesos en constantes módulo-nivel, propuesta pendiente):
            momentum              * MOMENTUM_WEIGHT
          + imbalance             * IMBALANCE_WEIGHT
          + (microprice-mid)/mid  * MICROPRICE_WEIGHT
          + (buy-sell)/(buy+sell) * TRADE_FLOW_WEIGHT
        """
        mid = snapshot.get("mid")
        if mid is None or mid <= 0:
            return 0.0

        momentum = float(snapshot.get("momentum", 0.0) or 0.0)
        imbalance = float(snapshot.get("imbalance", 0.0) or 0.0)
        microprice = float(snapshot.get("microprice", 0.0) or 0.0)
        buy_vol = float(snapshot.get("buy_volume_60s", 0.0) or 0.0)
        sell_vol = float(snapshot.get("sell_volume_60s", 0.0) or 0.0)

        # microprice == 0.0 significa "sin datos de bookTicker": termino nulo.
        micro_term = (microprice - mid) / mid if microprice > 0 else 0.0
        flow_term = self._safe_div(buy_vol - sell_vol, buy_vol + sell_vol)

        alpha = (
            MOMENTUM_WEIGHT * momentum
            + IMBALANCE_WEIGHT * imbalance
            + MICROPRICE_WEIGHT * micro_term
            + TRADE_FLOW_WEIGHT * flow_term
        )
        return max(-ALPHA_MAX, min(ALPHA_MAX, alpha))

    # ── 2. Precio de reserva estilo Avellaneda-Stoikov modificado (§8) ──
    def reservation_price(
        self,
        snapshot: dict,
        alpha: float,
        inventory: float,
        sigma: float,
        T_minutes: float = DEFAULT_HORIZON_MINUTES,
    ) -> float:
        """
        r_t = S_t + alpha_t - f(I_t, sigma_t, T - t)

        f = GAMMA_INVENTORY_RISK * inventory * sigma_min^2 * (T - t)

        Convencion: T - t en MINUTOS y sigma convertido a escala de minutos
        (sigma_min = sigma * sqrt(INTERVALS_PER_MINUTE)). Inventario long
        (inventory > 0) -> f > 0 -> reservation price BAJA (menos agresivo
        en bid); inventario short -> sube.
        """
        mid = snapshot.get("mid")
        if mid is None or mid <= 0:
            return 0.0

        sigma_min = sigma * math.sqrt(INTERVALS_PER_MINUTE)
        horizon_min = max(float(T_minutes), 0.0)
        inventory_penalty = (
            GAMMA_INVENTORY_RISK * inventory * sigma_min * sigma_min * horizon_min
        )
        return float(mid) + float(alpha) - inventory_penalty

    # ── 3. Distancias de cotizacion desde mid (§11) ─────────────────
    def quote_distances(
        self,
        snapshot: dict,
        alpha: float,
        inventory: float,
        sigma: float,
        spread_mult: float = 1.0,
    ) -> Tuple[float, float]:
        """
        (bid_dist, ask_dist) > 0 desde el mid.

        base = (spread/2)*spread_mult + K_QUOTE*sigma_efectiva
               + ALPHA_QUOTE_FACTOR*|alpha|
        skew = (inventory*mid / MAX_POSITION_NOTIONAL_USDC)
               * INVENTORY_SKEW_MULTIPLIER * sigma_efectiva

        bid_dist = base + skew      (inventario long -> bid mas lejos)
        ask_dist = base - skew      (inventario long -> ask mas cerca)

        Documentacion de sigma_efectiva (un solo √T por escala, §0.6):
            sigma_efectiva = sigma * sqrt(INTERVALS_PER_MINUTE) * sqrt(CYCLE_INTERVAL_SEC)
            [sigma base: por intervalo de muestreo] -> [escala minutos]
            -> [horizonte del ciclo de re-cotizacion en segundos].
        """
        mid = snapshot.get("mid")
        if mid is None or mid <= 0:
            return MIN_QUOTE_DISTANCE, MIN_QUOTE_DISTANCE

        spread = float(snapshot.get("spread", 0.0) or 0.0)
        sigma_efectiva = (
            float(sigma)
            * math.sqrt(INTERVALS_PER_MINUTE)
            * math.sqrt(config.CYCLE_INTERVAL_SEC)
        )

        base = (
            (spread / 2.0) * max(float(spread_mult), 0.0)
            + K_QUOTE * sigma_efectiva
            + ALPHA_QUOTE_FACTOR * abs(float(alpha))
        )

        notional_ratio = self._safe_div(
            float(inventory) * float(mid), config.MAX_POSITION_NOTIONAL_USDC
        )
        inventory_skew = notional_ratio * INVENTORY_SKEW_MULTIPLIER * sigma_efectiva

        bid_dist = max(base + inventory_skew, MIN_QUOTE_DISTANCE)
        ask_dist = max(base - inventory_skew, MIN_QUOTE_DISTANCE)
        return bid_dist, ask_dist

    # ── 4. Tamanos de orden con skew de inventario (§11) ────────────
    def choose_order_sizes(
        self, snapshot: dict, inventory: float, base_size: float
    ) -> Tuple[float, float]:
        """
        (bid_size, ask_size) con skew de tamaño: el lado que REDUCE
        inventario recibe el tamaño completo; el que lo aumenta recibe
        base_size * SKEW_SIZE. Nunca cero (SKEW_SIZE > 0).
        """
        if base_size <= 0:
            return 0.0, 0.0

        inv = float(inventory)
        if inv > 0:      # long -> reducir en ask (vender)
            return base_size * SKEW_SIZE, base_size
        if inv < 0:      # short -> reducir en bid (comprar)
            return base_size, base_size * SKEW_SIZE
        return base_size, base_size

    # ── 5. Estimacion de NetPnL esperado (§9/§18) ───────────────────
    def expected_net_pnl_estimate(
        self,
        snapshot: dict,
        side: str,
        price: float,
        qty: float,
        maker_fee: float = 0.0002,
    ) -> float:
        """
        Spread capturado esperado menos fees maker (sobre nocional).

        bid: (mid - price) * qty - price * qty * maker_fee
        ask: (price - mid) * qty - price * qty * maker_fee
        """
        mid = snapshot.get("mid")
        if mid is None or mid <= 0:
            return 0.0

        fee_rate = max(float(maker_fee), 0.0)
        fees = float(price) * float(qty) * fee_rate

        if side == "bid":
            return (float(mid) - float(price)) * float(qty) - fees
        if side == "ask":
            return (float(price) - float(mid)) * float(qty) - fees
        return 0.0
