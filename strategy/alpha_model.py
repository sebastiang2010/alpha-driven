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

FILTRO DE MOMENTUM (§14, ver config.MOMENTUM_*):
- El historial de mid (ring buffer de (ts, mid)) lo alimenta el orquestador
  via AlphaModel.record_mid() una vez por ciclo; quote_distances lo consulta.
- Regla: si |mid_actual - mid_inicio_ventana| >= MOMENTUM_MAX_TICKS * tick_size
  dentro de MOMENTUM_WINDOW_SECONDS (o el cooldown de
  MOMENTUM_COOLDOWN_SECONDS sigue corriendo), el piso de spread efectivo pasa
  de MIN_SPREAD_TICKS a MIN_SPREAD_TICKS * MOMENTUM_SPREAD_MULTIPLIER.
- SIMETRICO: usa |Δmid|, nunca el signo del movimiento (no asume dirección).
==============================================================================
"""

import math
import time
from collections import deque
from typing import Tuple

from . import config

# ---------------------------------------------------------------------------
# Pesos del alpha de corto plazo (§6)
# ---------------------------------------------------------------------------
MOMENTUM_WEIGHT: float = 0.0
IMBALANCE_WEIGHT: float = 0.0
MICROPRICE_WEIGHT: float = 0.0
TRADE_FLOW_WEIGHT: float = 0.0

ALPHA_MAX: float = 0.0005

# ---------------------------------------------------------------------------
# Filtro asimétrico de Adverse Selection (Familia C)
# ---------------------------------------------------------------------------
# Umbral de flujo direccional para considerar el régimen asimétrico
# (buy - sell) / (buy + sell)
ADVERSE_FLOW_THRESHOLD: float = 0.2
ADVERSE_SPREAD_MULTIPLIER: float = 2.0
ADVERSE_SIZE_MULTIPLIER: float = 0.1

# ---------------------------------------------------------------------------
# Parámetros de re-cotizacion (§8, §11)
# ---------------------------------------------------------------------------
GAMMA_INVENTORY_RISK: float = 1.0
K_QUOTE: float = 1.0
ALPHA_QUOTE_FACTOR: float = 0.0
INVENTORY_SKEW_MULTIPLIER: float = 2.0
MIN_QUOTE_DISTANCE: float = 1e-8

# ---------------------------------------------------------------------------
# Convencion de tiempo / volatilidad (§0.6)
# ---------------------------------------------------------------------------
SAMPLING_INTERVAL_SEC: float = 5.0
INTERVALS_PER_MINUTE: float = 60.0 / SAMPLING_INTERVAL_SEC
DEFAULT_HORIZON_MINUTES: float = 5.0

# ── Filtro de momentum anti-adverse-selection (§14) ───────────────────
_MID_HISTORY_MAXLEN: int = 256


class AlphaModel:
    """
    Señal de corto plazo y derivados de cotizacion.
    """

    def __init__(self) -> None:
        self._mid_history = deque(maxlen=_MID_HISTORY_MAXLEN)
        self._momentum_detected_at: float | None = None
        self.last_momentum_active: bool = False

    def record_mid(self, ts_sec, mid) -> None:
        try:
            ts = float(ts_sec)
            price = float(mid)
        except (TypeError, ValueError):
            return
        if price <= 0 or ts < 0:
            return
        self._mid_history.append((ts, price))

    def _prune_mid_history(self, now_sec: float) -> None:
        cutoff = now_sec - config.MOMENTUM_WINDOW_SECONDS
        while self._mid_history and self._mid_history[0][0] < cutoff:
            self._mid_history.popleft()

    def _momentum_detected(self, now_sec: float, tick_size: float) -> bool:
        if not config.MOMENTUM_ENABLED:
            return False
        self._prune_mid_history(now_sec)
        if len(self._mid_history) < 2:
            return False
        oldest = self._mid_history[0][1]
        newest = self._mid_history[-1][1]
        if oldest <= 0.0 or newest <= 0.0 or tick_size <= 0.0:
            return False
        threshold = float(config.MOMENTUM_MAX_TICKS) * float(tick_size)
        return abs(newest - oldest) >= threshold - float(tick_size) * 1e-6

    def _momentum_active(self, now_sec: float, tick_size: float) -> bool:
        if not config.MOMENTUM_ENABLED:
            self.last_momentum_active = False
            return False
        if self._momentum_detected(now_sec, tick_size):
            self._momentum_detected_at = float(now_sec)
            self.last_momentum_active = True
            return True
        if (
            self._momentum_detected_at is not None
            and now_sec - self._momentum_detected_at < config.MOMENTUM_COOLDOWN_SECONDS
        ):
            self.last_momentum_active = True
            return True
        self.last_momentum_active = False
        return False

    @staticmethod
    def _safe_div(numer: float, denom: float) -> float:
        return numer / denom if denom else 0.0

    def compute_alpha(self, snapshot: dict) -> float:
        """
        Señal de corto plazo ELIMINADA según autopsia (sin evidencia direccional).
        Retorna 0.0 para no añadir ruido direccional al modelo A-S.
        """
        return 0.0

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
        now_sec: float | None = None,
    ) -> Tuple[float, float]:
        """
        (bid_dist, ask_dist) > 0 desde el mid.

        base = (spread/2)*spread_mult + K_QUOTE*sigma_efectiva
               + ALPHA_QUOTE_FACTOR*|alpha|
        skew = (inventory*mid / MAX_POSITION_NOTIONAL_USDC)
               * INVENTORY_SKEW_MULTIPLIER * sigma_efectiva

        bid_dist = base + skew      (inventario long -> bid mas lejos)
        ask_dist = base - skew      (inventario long -> ask mas cerca)

        Piso de spread (§11): si bid_dist + ask_dist <
        MIN_SPREAD_TICKS * tick_size, se ensancha simetricamente desde el
        mid (extra/2 a cada lado); el skew de inventario se preserva intacto.
        Con la promo 0 fees (MAKER_FEE_RATE=0.0) el piso ya no cubre fees
        sino adverse selection; por eso MIN_SPREAD_TICKS=20 (subido de 8->12 el 2026-08-12, 12->16 y 16->20 el 2026-08-20
        2026-08-12 tras medir -0.00024/RT con 8 ticks).

        Filtro de momentum (§14): si el mid se movió >= MOMENTUM_MAX_TICKS
        dentro de MOMENTUM_WINDOW_SECONDS (o el cooldown aún corre), el
        piso efectivo pasa a MIN_SPREAD_TICKS * MOMENTUM_SPREAD_MULTIPLIER
        (20 -> 40 ticks). El filtro es SIMÉTRICO (no direccional) y el skew
        de inventario se aplica igual, por encima del piso. now_sec solo
        existe para hacer deterministas los tests (default: reloj real).

        Documentacion de sigma_efectiva (un solo √T por escala, §0.6):
            market_state entrega sigma en base de 5s (SAMPLING_INTERVAL_SEC).
            Para el buffer del quote se escala esa sigma de 5s a la base por
            MINUTO y luego al horizonte de re-cotizacion (ciclo), en la MISMA
            convencion que reservation_price (sigma_min = sigma * sqrt(INTERVALS_PER_MINUTE)):
                horizon_min = CYCLE_INTERVAL_SEC / 60
                sigma_efectiva = sigma * sqrt(horizon_min * INTERVALS_PER_MINUTE)
            Con CYCLE=SAMPLING=5s queda sigma_efectiva = sigma (factor 1.0), pero
            ahora la escala es explicita y consistente: NO se vuelve a aplicar
            sqrt(CYCLE/SAMPLING) (ratio 1.0 redundante) ni doble sqrt(T).
        """
        mid = snapshot.get("mid")
        if mid is None or mid <= 0:
            return MIN_QUOTE_DISTANCE, MIN_QUOTE_DISTANCE

        if now_sec is None:
            now_sec = time.time()

        spread = float(snapshot.get("spread", 0.0) or 0.0)
        # Un solo sqrt(T) (§0.6), en la convencion de reservation_price: sigma ya
        # viene en base 5s (SAMPLING_INTERVAL_SEC) y se escala al horizonte de
        # re-cotizacion (ciclo) via base por-minuto. Con CYCLE=SAMPLING=5s el
        # factor es 1.0 (NO es un ratio redundante ni doble sqrt(T)).
        quote_horizon_min = float(config.CYCLE_INTERVAL_SEC) / 60.0
        sigma_efectiva = float(sigma) * math.sqrt(quote_horizon_min * INTERVALS_PER_MINUTE)

        base = (
            (spread / 2.0) * max(float(spread_mult), 0.0)
            + K_QUOTE * sigma_efectiva
            + ALPHA_QUOTE_FACTOR * abs(float(alpha))
        )

        notional_ratio = self._safe_div(
            float(inventory) * float(mid), config.MAX_POSITION_NOTIONAL_USDC
        )
        inventory_skew = notional_ratio * INVENTORY_SKEW_MULTIPLIER * sigma_efectiva

        bid_dist = base + inventory_skew
        ask_dist = base - inventory_skew

        # Piso de spread (§11): el round trip mainnet 2026-08-10 perdió
        # -0.0113 USDC por adverse selection porque no existía piso
        # estructural. Con la promo 0 fees (MAKER_FEE_RATE=0.0) el piso ya
        # no cubre fees sino adverse selection. Medición 2026-08-12: con 8
        # ticks el NET fue -0.01954 USDC (-0.00024/RT); se subió a 12 ticks.
        # Si el spread calculado por volatilidad queda por debajo, se
        # ensancha SIMETRICAMENTE desde el mid (extra/2 a cada lado): el
        # skew de inventario ya está incluido en bid_dist/ask_dist y se
        # preserva intacto (se aplica después del piso).
        tick_size = float(snapshot.get("tick_size") or config.TICK_SIZE_XRPUSDC)
        # Filtro de momentum (§14): el piso se MULTIPLICA (no solo se
        # ensancha el spread calculado). Mismo tick_size que el piso base.
        if self._momentum_active(float(now_sec), tick_size):
            floor_ticks = (
                float(config.MIN_SPREAD_TICKS) * float(config.MOMENTUM_SPREAD_MULTIPLIER)
            )
        else:
            floor_ticks = float(config.MIN_SPREAD_TICKS)
        min_spread = floor_ticks * tick_size
        if bid_dist + ask_dist < min_spread:
            extra = (min_spread - bid_dist - ask_dist) / 2.0
            bid_dist += extra
            ask_dist += extra

        bid_dist = max(bid_dist, MIN_QUOTE_DISTANCE)
        ask_dist = max(ask_dist, MIN_QUOTE_DISTANCE)
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

        # Valor justo = precio de reserva (§8): r = mid + alpha - penalty.
        # Medir el edge contra r, NO contra el mid crudo: el sesgo de alpha
        # (y el skew de inventario) ya estan incorporados en r, por lo que el
        # spread capturado es bid_dist/ask_dist (> 0 por el piso). Medir contra
        # mid castigaba el sesgo direccional y rechazaba siempre un lado cuando
        # habia senal (bug: el bot no podia cotizar two-sided). Ver reporte de
        # medicion en testnet 2026-08-12.
        alpha = self.compute_alpha(snapshot)
        inventory = float(snapshot.get("inventory", 0.0) or 0.0)
        sigma = float(snapshot.get("volatility", 0.0) or 0.0)
        fair = self.reservation_price(snapshot, alpha, inventory, sigma)
        if fair is None or fair <= 0:
            fair = float(mid)

        side_norm = str(side).upper()
        if side_norm in ("BID", "BUY"):
            return (fair - float(price)) * float(qty) - costs
        if side_norm in ("ASK", "SELL"):
            return (float(price) - fair) * float(qty) - costs
        return 0.0
