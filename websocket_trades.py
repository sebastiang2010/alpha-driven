# websocket_trades.py
"""
WebSocket de trades públicos (raw trades) para Binance Futures.
Adaptado a la nueva arquitectura: flujos públicos -> /public/ws
"""

import json
import time
import logging
import threading
import websocket

logger = logging.getLogger("TradeWebSocket")


class TradeWebSocket:
    """
    Mantiene un WebSocket al flujo de trades públicos (@trade).
    Soporta producción (fstream.binance.com/public/ws) y testnet (stream.binancefuture.com/public/ws).
    """

    def __init__(self, symbol: str, on_trade, real: bool = False):
        self.symbol = symbol.lower()
        self.on_trade = on_trade
        self.real = real
        self.ws = None
        self._running = False
        self._thread = None

        # Nueva URL base pública
        base_ws = "wss://fstream.binance.com/public/ws" if real else "wss://stream.binancefuture.com/public/ws"
        self.url = f"{base_ws}/{self.symbol}@trade"
        logger.info("TradeWebSocket URL: %s (real=%s)", self.url, real)

    # ── Control de inicio/parada ──────────────────────────────
    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        self._running = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    # ── Bucle principal de conexión ───────────────────────────
    def _run(self):
        while self._running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self.ws.run_forever(ping_interval=10, ping_timeout=10)
            except Exception as e:
                logger.error("Trade WS fatal: %s", e)
            time.sleep(1)   # esperar antes de reintentar

    # ── Callbacks del WebSocket ───────────────────────────────
    def _on_open(self, ws):
        logger.info("Trade WebSocket conectado para %s", self.symbol)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            logger.error("Trade JSON parse error: %s", e)
            return

        # Cada trade tiene el formato:
        # {
        #   "e": "trade",        // Event type
        #   "E": 123456789,      // Event time
        #   "s": "BTCUSDT",      // Symbol
        #   "t": 12345,          // Trade ID
        #   "p": "0.001",        // Price
        #   "q": "100",          // Quantity
        #   "b": 88,             // Buyer order ID
        #   "a": 50,             // Seller order ID
        #   "T": 123456785,      // Trade time
        #   "m": true,           // Is the buyer the market maker?
        #   "X": "MARKET"        // (Futures specific)
        # }
        self.on_trade(
            price=float(data['p']),
            qty=float(data['q']),
            is_buyer_maker=bool(data.get('m', False)),
            timestamp=data.get('E', data.get('T', int(time.time() * 1000)))
        )

    def _on_close(self, ws, code, msg):
        logger.warning("Trade WS cerrado code=%s msg=%s", code, msg)

    def _on_error(self, ws, error):
        logger.error("Trade WS error: %s", error)