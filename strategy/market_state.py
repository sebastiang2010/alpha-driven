# market_state.py
"""
Market State para el bot de market making de Binance Futures (XRPUSDC).

Construye continuamente un estado del mercado (§7 de la especificación)
a partir de los WebSocket ya existentes:

- BookTickerWebSocket -> on_bookticker(bid, bid_qty, ask, ask_qty, ts)
- DepthWebSocket      -> on_depth(bids, asks, depth, ts)
- TradeWebSocket      -> on_trade(price, qty, is_buyer_maker, ts)

Los callbacks corren en hilos distintos del WS: TODO el estado compartido
está protegido por un threading.Lock. Este módulo NO envía órdenes ni llama
a la API REST (eso es del execution_engine).

Convención de volatilidad (declarada, consistente, sin √T doble §0.6):
    sigma = desvío estándar muestral de los retornos logarítmicos
            ln(p_t / p_{t-1}) entre muestras consecutivas de mid price
            dentro de VOLATILITY_WINDOW_SEC.
    Unidades: retorno por intervalo de muestreo del WS (NO anualizado).
    Reescala a intervalo fijo: sigma_ref = sigma_evento *
            sqrt(SAMPLING_INTERVAL_SEC / avg_interval), donde
            SAMPLING_INTERVAL_SEC = 5.0 (config) es la referencia que usa
            alpha_model. Así el WS puede actualizar más rápido que 5 s sin
            romper la consistencia de escala (§0.6).
    Es la volatilidad "efectiva" del período; NO se multiplica por √T en
    ningún punto de este módulo (√T solo se aplica donde el consumidor
    anualiza, nunca dos veces).
"""

import os
import sys
import math
import threading
import logging
import statistics
from collections import deque

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)

# Imports de la raíz del proyecto (módulos WebSocket existentes).
from websocket_bookticker import BookTickerWebSocket
from websocket_depth import DepthWebSocket
from websocket_trades import TradeWebSocket

# Config central (§0.4): SAMPLING_INTERVAL_SEC define el intervalo de
# referencia al que se reescala sigma (§0.6).
try:
    from strategy import config
except ImportError:
    import config

logger = logging.getLogger("MarketState")

# ── Ventanas temporales (segundos) ─────────────────────────────────
TRADE_WINDOW_SEC = 60.0          # ventana de trade flow / arrival rate
VOLATILITY_WINDOW_SEC = 60.0     # ventana de muestras de mid para sigma
ALPHA_WINDOW_SEC = 15.0          # ventana de momentum
_MAXLEN = 10000                  # maxlen generoso para las deques

# Referencia de tiempo para volatilidad (V2: ref_s única congelada)
# SAMPLING_INTERVAL_SEC = 5.0 es el intervalo de referencia (ref_s)
# desde config. No se define constante local duplicada.

# Separa "sin datos" de "spread = 0": límite por debajo del cual el
# mid se considera degenerado (XRPUSDC cotiza muy por encima de esto).
_MIN_PRICE = 1e-9


class MarketState:
    """
    Estado continuo del mercado, protegido por threading.Lock.

    Uso:
        ms = MarketState("xrpusdc")
        ms.start_ws()
        # ... en el ciclo de market_maker:
        snap = ms.get_snapshot()
        ms.close_ws()
    """

    def __init__(self, symbol: str, real: bool = False):
        self.symbol = symbol.lower()
        self.real = real

        self._lock = threading.Lock()

        # BookTicker (mejor bid/ask del libro).
        self.best_bid = None
        self.best_bid_qty = None
        self.best_ask = None
        self.best_ask_qty = None

        # Depth (top-N del libro, formato [["precio","qty"], ...]).
        self.bids = []
        self.asks = []
        self.depth_levels = 5
        self.bid_volume_top = 0.0
        self.ask_volume_top = 0.0

        # Trade flow: (ts_segundos, qty, "buy"|"sell").
        self._trade_flow = deque(maxlen=_MAXLEN)
        # Muestras de mid price: (ts_segundos, mid) para volatilidad/momentum.
        self._mid_samples = deque(maxlen=_MAXLEN)

        self.last_update_ts = None

        self._ws_bookticker = None
        self._ws_depth = None
        self._ws_trades = None

    # ── Derivados del bookTicker ────────────────────────────────────
    def _mid(self):
        """Mid price (solo con bookTicker válido)."""
        if not self.best_bid or not self.best_ask:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    def _spread(self):
        """Spread absoluto (0.0 si no hay datos)."""
        if not self.best_bid or not self.best_ask:
            return 0.0
        return max(self.best_ask - self.best_bid, 0.0)

    def _spread_pct(self):
        """Spread relativo al mid (0.0 si no hay datos)."""
        mid = self._mid()
        if not mid or mid <= _MIN_PRICE:
            return 0.0
        return self._spread() / mid

    # ── Helpers de división segura ─────────────────────────────────
    @staticmethod
    def _safe_div(numer, denom):
        return numer / denom if denom else 0.0

    @staticmethod
    def _to_sec(ts_ms):
        """Convierte timestamp de ms (entero) a segundos (float)."""
        try:
            return float(ts_ms) / 1000.0
        except (TypeError, ValueError):
            return None

    # ── Mantenimiento de ventanas temporales ────────────────────────
    def _prune_deque(self, dq, now_sec, window_sec):
        """Descarta entradas más viejas que window_sec (asume lock tomado)."""
        cutoff = now_sec - window_sec
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    # ── Callbacks de actualización (hilos del WS) ──────────────────
    def update_bookticker(self, bid_price, bid_qty, ask_price, ask_qty, timestamp):
        """Callback del BookTickerWebSocket."""
        with self._lock:
            self.best_bid = float(bid_price)
            self.best_bid_qty = float(bid_qty)
            self.best_ask = float(ask_price)
            self.best_ask_qty = float(ask_qty)
            self.last_update_ts = timestamp

            mid = self._mid()
            ts_sec = self._to_sec(timestamp)
            if mid and ts_sec is not None:
                self._mid_samples.append((ts_sec, mid))

    def update_depth(self, bids, asks, depth, timestamp):
        """Callback del DepthWebSocket. bids/asks: [[precio, qty], ...]."""
        with self._lock:
            self.bids = [[float(p), float(q)] for p, q in bids]
            self.asks = [[float(p), float(q)] for p, q in asks]
            self.depth_levels = int(depth)
            self.bid_volume_top = sum(q for _, q in self.bids)
            self.ask_volume_top = sum(q for _, q in self.asks)
            self.last_update_ts = timestamp

    def update_trade(self, price, qty, is_buyer_maker, timestamp):
        """Callback del TradeWebSocket.

        En el stream de Binance `m` = is_buyer_maker: si el buyer es el
        maker, el agresor vendió. Por lo tanto:
            buy_volume  suma qty cuando NOT is_buyer_maker (agresor compró);
            sell_volume suma qty cuando     is_buyer_maker (agresor vendió).
        """
        ts_sec = self._to_sec(timestamp)
        if ts_sec is None:
            return
        side = "buy" if not is_buyer_maker else "sell"
        with self._lock:
            self._trade_flow.append((ts_sec, float(qty), side))
            self.last_update_ts = timestamp

    # ── Derivados que requieren lectura completa bajo lock ─────────
    @staticmethod
    def _volatility_of(samples):
        """sigma pura (sin mutación): stdev muestral de logrets × sqrt(ref/dt_bar).

        `samples`: secuencia [(ts_sec, mid), ...] YA filtrada a la ventana.
        <3 muestras, <2 retornos, sin intervalos o dt_bar<=0 → 0.0.
        Misma fórmula que _compute_volatility (V3: sin podar la deque
        compartida — antes el poda de momentum a 15s recortaba el historial
        de sigma entre consultas sucesivas).
        """
        if len(samples) < 3:
            return 0.0
        returns = []
        prev_price = samples[0][1]
        prev_ts = samples[0][0]
        intervals = []
        for ts, price in list(samples)[1:]:
            if prev_price > _MIN_PRICE and price > _MIN_PRICE:
                returns.append(math.log(price / prev_price))
                if ts > prev_ts:
                    intervals.append(ts - prev_ts)
            prev_price = price
            prev_ts = ts
        if len(returns) < 2 or not intervals:
            return 0.0
        try:
            sigma_event = statistics.stdev(returns)
        except statistics.StatisticsError:
            return 0.0
        avg_interval = sum(intervals) / len(intervals)
        if avg_interval <= 0.0:
            return 0.0
        ref_interval = float(getattr(config, "SAMPLING_INTERVAL_SEC", 5.0))
        # V2: sigma_ref = sigma_event * sqrt(ref_s / dt_bar) — un solo escalado
        return sigma_event * math.sqrt(ref_interval / avg_interval)

    @staticmethod
    def _momentum_of(samples):
        """Momentum puro (sin mutación): (newest-oldest)/newest, <2 → 0.0."""
        if len(samples) < 2:
            return 0.0
        oldest = samples[0][1]
        newest = samples[-1][1]
        if newest <= _MIN_PRICE:
            return 0.0
        return (newest - oldest) / newest

    def _compute_volatility(self, now_sec):
        """
        sigma (§7): desvío estándar muestral de retornos log de mid.

        Se usan muestras de mid tomadas en cada actualización del
        bookTicker/depth dentro de VOLATILITY_WINDOW_SEC. Retorno log:
        r_i = ln(p_i / p_{i-1}). El std muestral (n-1) de esos retornos es
        el sigma por evento del WS.

        Reescala a intervalo fijo ref_s (§0.6, V2): el WS puede actualizar más rápido
        que SAMPLING_INTERVAL_SEC; para que sigma sea consistente con la
        referencia única congelada (ref_s = SAMPLING_INTERVAL_SEC), se multiplica por
        sqrt(ref_s / avg_interval). Si no hay suficientes
        muestras o el intervalo medio no es positivo, devuelve 0.0.

        V2: usa una sola referencia ref_s desde config (no constante local duplicada).
        V3: vista filtrada (no destructiva) — valores idénticos al poda anterior.
        """
        window = [s for s in self._mid_samples
                  if s[0] >= now_sec - VOLATILITY_WINDOW_SEC]
        return self._volatility_of(window)

    def _compute_momentum(self, now_sec):
        """
        Momentum (§7): diferencia del mid normalizada por el nivel.

        (mid_ahora - mid_hace_ALPHA_WINDOW_SEC) / mid_ahora, usando la
        muestra más reciente y la más antigua dentro de la ventana.
        V3: vista filtrada (no destructiva) — valores idénticos al poda anterior.
        """
        window = [s for s in self._mid_samples
                  if s[0] >= now_sec - ALPHA_WINDOW_SEC]
        return self._momentum_of(window)

    # ── Snapshot para market_maker ─────────────────────────────────
    def get_snapshot(self, now_sec=None, *, offline_windows=None):
        """
        Copia segura de todo el estado (lock adquirido, copiado, liberado).
        El contrato del dict es estable: inventory/pnl los rellena
        market_maker, acá solo se deja el espacio definido.

        Dos rutas separadas (W1):
        - Producción (default, sin args): conducta ORIGINAL exacta — podas
          destructivas incluidas (primero volatilidad a 60s, luego momentum
          a 15s). La usa market_maker (get_snapshot() sin args).
        - Offline (now_sec + offline_windows explícitos): consultas NO
          destructivas con ventanas configurables; rechaza consultas
          anteriores al estado consumido (W3). La usa el coordinador
          offline con las ventanas de strategy.config.
        """
        with self._lock:
            if now_sec is None:
                if offline_windows is None:
                    return self._snapshot_legacy()
                raise ValueError(
                    "offline get_snapshot requires an explicit now_sec"
                )
            if offline_windows is None:
                raise ValueError(
                    "offline get_snapshot requires explicit offline_windows "
                    "{'trade':.., 'volatility':.., 'momentum':..}"
                )
            return self._snapshot_offline(float(now_sec), offline_windows)

    def _snapshot_legacy(self):
        """Ruta producción ORIGINAL (asume lock tomado). Podas destructivas
        en el orden histórico: trade flow → volatilidad (60s) → momentum
        (15s). Idéntica a la versión pre-V3 caracter por caracter en
        comportamiento."""
        now_sec = self._to_sec(self.last_update_ts) or 0.0

        # trade flow en los últimos TRADE_WINDOW_SEC
        self._prune_deque(self._trade_flow, now_sec, TRADE_WINDOW_SEC)
        buy_vol = sum(q for _, q, s in self._trade_flow if s == "buy")
        sell_vol = sum(q for _, q, s in self._trade_flow if s == "sell")
        arrival = len(self._trade_flow)

        snapshot = self._snapshot_levels()
        # Orden histórico: volatilidad poda a 60s, momentum recorta a 15s.
        self._prune_deque(self._mid_samples, now_sec, VOLATILITY_WINDOW_SEC)
        snapshot["volatility"] = self._volatility_of(self._mid_samples)
        self._prune_deque(self._mid_samples, now_sec, ALPHA_WINDOW_SEC)
        snapshot["momentum"] = self._momentum_of(self._mid_samples)
        snapshot["buy_volume_60s"] = buy_vol
        snapshot["sell_volume_60s"] = sell_vol
        snapshot["trade_arrival_rate"] = arrival
        return snapshot

    def _snapshot_offline(self, now_sec, windows):
        """Ruta offline (asume lock tomado). Vistas filtradas no
        destructivas con ventanas EXPLÍCITAS; mid/spread/imbalance/
        microprice son niveles actuales (sin libro histórico: el pasado se
        prueba por reproducción cronológica, W3)."""
        try:
            trade_w = float(windows["trade"])
            vol_w = float(windows["volatility"])
            mom_w = float(windows["momentum"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "offline_windows must be {'trade':.., 'volatility':.., "
                "'momentum':..} in seconds"
            )
        consumed = self.last_update_ts
        if consumed is not None:
            consumed_s = float(consumed) / 1000.0
            if now_sec < consumed_s:
                raise ValueError(
                    f"regressive offline query: {now_sec}s < consumed "
                    f"{consumed_s}s (reproduce the past "
                    "chronologically instead)"
                )
        flow = [t for t in self._trade_flow if t[0] >= now_sec - trade_w]
        snapshot = self._snapshot_levels()
        snapshot["buy_volume_60s"] = sum(q for _, q, s in flow if s == "buy")
        snapshot["sell_volume_60s"] = sum(q for _, q, s in flow if s == "sell")
        snapshot["trade_arrival_rate"] = len(flow)
        snapshot["volatility"] = self._volatility_of(
            [s for s in self._mid_samples if s[0] >= now_sec - vol_w])
        snapshot["momentum"] = self._momentum_of(
            [s for s in self._mid_samples if s[0] >= now_sec - mom_w])
        return snapshot

    def _snapshot_levels(self):
        """Niveles actuales + contrato base del dict (asume lock tomado)."""
        mid = self._mid()
        spread = self._spread()
        spread_pct = self._spread_pct()

        # imbalance desde el depth (división segura)
        imbalance = self._safe_div(
            self.bid_volume_top - self.ask_volume_top,
            self.bid_volume_top + self.ask_volume_top,
        )

        # microprice §7: (Ask*BidSize + Bid*AskSize) / (BidSize + AskSize)
        microprice = 0.0
        if (self.best_bid is not None and self.best_ask is not None
                and self.best_bid_qty is not None and self.best_ask_qty is not None):
            microprice = self._safe_div(
                self.best_ask * self.best_bid_qty + self.best_bid * self.best_ask_qty,
                self.best_bid_qty + self.best_ask_qty,
            )

        return {
            "ts": self.last_update_ts,
            "mid": mid,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": spread,
            "spread_pct": spread_pct,
            "imbalance": imbalance,
            "microprice": microprice,
            "bid_volume_top": self.bid_volume_top,
            "ask_volume_top": self.ask_volume_top,
            "buy_volume_60s": 0.0,
            "sell_volume_60s": 0.0,
            "trade_arrival_rate": 0,
            "volatility": 0.0,
            "momentum": 0.0,
            "inventory": 0.0,       # lo rellena market_maker
            "unrealized_pnl": 0.0,  # lo rellena market_maker
            "realized_pnl": 0.0,    # lo rellena market_maker
            "fill_count": 0,        # lo rellena market_maker
        }

    # ── Ciclo de vida de los WebSockets ────────────────────────────
    def start_ws(self):
        """Instancia y arranca los tres WS con los callbacks de estado."""
        self._ws_bookticker = BookTickerWebSocket(
            self.symbol, on_bookticker=self.update_bookticker, real=self.real
        )
        self._ws_depth = DepthWebSocket(
            self.symbol, on_depth=self.update_depth, real=self.real, depth_levels=5
        )
        self._ws_trades = TradeWebSocket(
            self.symbol, on_trade=self.update_trade, real=self.real
        )
        self._ws_bookticker.start()
        self._ws_depth.start()
        self._ws_trades.start()
        logger.info("MarketState: WS iniciados para %s (real=%s)", self.symbol, self.real)

    def close_ws(self):
        """Detiene los WebSockets (aplica a los que existan)."""
        for ws in (self._ws_bookticker, self._ws_depth, self._ws_trades):
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        logger.info("MarketState: WS detenidos para %s", self.symbol)
