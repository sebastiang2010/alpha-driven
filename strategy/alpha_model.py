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
#
# HALLazgo gate 2026-08-09: los pesos originales (0.30/0.20/0.30/0.20) con
# ALPHA_MAX=0.01 producian saturación permanente: con imbalance tipico ~0.077,
# IMBALANCE_WEIGHT*imbalance = 0.0154 ya superaba ALPHA_MAX=0.01 -> alpha
# pinneado en ±1% SIEMPRE (señal degenerada). Se re-escala la señal a un
# rango de ~2x el spread tipico de XRPUSDC (~0.0003) para que sea accionable:
#   momentum (escala ~1e-3)  -> peso 0.30 da ~0.0002
#   imbalance (escala 0..1)  -> peso 0.003 da ~0.0002 con imbalance 0.077
#   microprice_term (~1e-4)  -> peso 0.30 da ~0.00003
#   flow (escala 0..1)       -> peso 0.003 da ~0.0002 con flow 0.07
# Combinados tipicos ~0.0004-0.0008 (4-8 ticks) sobre ALPHA_MAX=0.0005 (5
# ticks en XRPUSDC). VALORES CONSERVADORES: recalibrar con mas datos (§0.4).
# ---------------------------------------------------------------------------
MOMENTUM_WEIGHT: float = 0.30      # momentum normalizado del snapshot
IMBALANCE_WEIGHT: float = 0.003    # imbalance de volumen del depth (0..1)
MICROPRICE_WEIGHT: float = 0.30    # (microprice - mid) / mid
TRADE_FLOW_WEIGHT: float = 0.003   # (buy - sell) / (buy + sell) (0..1)

# Limite de la señal: ±0.0005 (~5 ticks / ~2x spread tipico de XRPUSDC).
# Antes ±0.01 (±1%) = ~100x el spread -> saturación permanente (hallazgo
# gate 2026-08-09). PENDIENTE DE CONFIRMACION (§0.4).
ALPHA_MAX: float = 0.0005

# ---------------------------------------------------------------------------
# Parámetros de re-cotizacion (§8, §11) — PROPUESTA pendiente de confirmacion
# ---------------------------------------------------------------------------
GAMMA_INVENTORY_RISK: float = 0.5   # aversion al riesgo de inventario (f)
K_QUOTE: float = 1.0                # multiplicador de sigma_efectiva en quotes
# ALPHA_QUOTE_FACTOR: §8 define r = S + alpha; el alpha ya desplaza la
# reserva (reservation_price). Sumar |alpha| a la distancia (como hacia el
# valor 1.0 anterior) aplicaba el alpha DOS veces: con alpha=±1% el lado
# alejado quedaba a ±2% del mid y el cercano sin borde (gate 2026-08-09,
# run 22:26 UTC: ask=1.0622 con mid=1.04215). Se fija en 0.0 para respetar
# §8. PENDIENTE DE CONFIRMACION (§0.4).
ALPHA_QUOTE_FACTOR: float = 0.0     # cuanto ensancha |alpha| ambas puntas
INVENTORY_SKEW_MULTIPLIER: float = 0.5  # skew de precio por inventario normalizado
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

    # ── 4. Estimacion de NetPnL esperado (§9/§18) ───────────────────
    def expected_net_pnl_estimate(
        self,
        snapshot: dict,
        side: str,
        price: float,
        qty: float,
        maker_fee: float | None = None,
        funding_rate_per_8h: float | None = None,
        expected_hold_sec: float | None = None,
        slippage_rate: float | None = None,
    ) -> float:
        """
        Spread capturado esperado menos costos (§18):
            NetPnL = GrossPnL - fees - funding - slippage

        bid: (mid - price) * qty - price*qty*(fee + funding + slippage)
        ask: (price - mid) * qty - price*qty*(fee + funding + slippage)

        Modelo de costos (decisión 2026-08-09, pendiente de confirmación §0.4):
          - fees: MAKER_FEE_RATE por lado (fee maker real Binance VIP0, 0.0002).
          - funding: PROPORCIONAL al tiempo de tenencia esperado. Binance cobra
            funding cada 8 h sobre el nocional (no por fill):
                funding = FUNDING_RATE_PER_8H * (EXPECTED_HOLD_SEC / FUNDING_INTERVAL_SEC)
            Con defaults (0.0001, 300 s, 28,800 s): funding ≈ 0.00000104 por
            trade (antes era 0.0001 fijo por fill — sobreestimaba ~1000x).
          - slippage: 0 para órdenes MAKER (post-only GTX nunca cruzan el
            spread). SLIPPAGE_MAKER_BPS = 0.0 en config; > 0 solo si se quiere
            un colchón conservador de adverse selection.

        Los costos por defecto salen de config (fuente única §0.4):
            MAKER_FEE_RATE, FUNDING_RATE_PER_8H, EXPECTED_HOLD_SEC,
            FUNDING_INTERVAL_SEC, SLIPPAGE_MAKER_BPS.
        Se pueden anular por parámetro (tests).

        Acepta side en cualquiera de las dos convenciones usadas en el repo:
        "bid"/"ask" (risk_engine) o "BUY"/"SELL" (execution_engine). Evita
        el bug de convención inconsistente (§0.6) que devolvía 0.0 siempre.
        """
        mid = snapshot.get("mid")
        if mid is None or mid <= 0:
            return 0.0

        fee_rate = max(float(maker_fee if maker_fee is not None
                              else getattr(config, "MAKER_FEE_RATE", 0.0002)), 0.0)
        funding_rate = max(float(
            funding_rate_per_8h if funding_rate_per_8h is not None
            else getattr(config, "FUNDING_RATE_PER_8H", 0.0)), 0.0)
        hold_sec = max(float(
            expected_hold_sec if expected_hold_sec is not None
            else getattr(config, "EXPECTED_HOLD_SEC", 0.0)), 0.0)
        interval_sec = float(getattr(config, "FUNDING_INTERVAL_SEC", 8.0 * 3600.0))
        funding = funding_rate * (hold_sec / interval_sec) if interval_sec > 0 else 0.0
        slippage = max(float(slippage_rate if slippage_rate is not None
                             else getattr(config, "SLIPPAGE_MAKER_BPS", 0.0)), 0.0)
        cost_rate = fee_rate + funding + slippage
        costs = float(price) * float(qty) * cost_rate

        side_norm = str(side).upper()
        if side_norm in ("BID", "BUY"):
            return (float(mid) - float(price)) * float(qty) - costs
        if side_norm in ("ASK", "SELL"):
            return (float(price) - float(mid)) * float(qty) - costs
        return 0.0
