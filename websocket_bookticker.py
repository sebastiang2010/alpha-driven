# websocket_bookticker.py
"""
WebSocket de bookTicker (Best Bid / Best Ask) para Binance Futures.
"""

import json
import time
import logging
import threading
import websocket

logger = logging.getLogger("BookTickerWebSocket")

class BookTickerWebSocket:
    def __init__(self, symbol: str, on_bookticker, real: bool = False):
        self.symbol = symbol.lower()
        self.on_bookticker = on_bookticker
        self.real = real
        self.ws = None
        self._running = False
        self._thread = None

        base_ws = "wss://fstream.binance.com/public/ws" if real else "wss://stream.binancefuture.com/public/ws"
        self.url = f"{base_ws}/{self.symbol}@bookTicker"
        logger.info("BookTickerWebSocket URL: %s (real=%s)", self.url, real)

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
                self.ws.run_forever(ping_interval=10, ping_timeout=5)  # websocket-client exige ping_interval > ping_timeout
            except Exception as e:
                logger.error("BookTicker WS fatal: %s", e)
            time.sleep(1)

    def _on_open(self, ws):
        logger.info("BookTicker WebSocket conectado para %s", self.symbol)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if 'b' in data and 'a' in data:
                self.on_bookticker(
                    bid_price=float(data['b']),
                    bid_qty=float(data['B']),
                    ask_price=float(data['a']),
                    ask_qty=float(data['A']),
                    timestamp=data.get('E', int(time.time() * 1000))
                )
        except Exception as e:
            logger.error("BookTicker parse error: %s", e)

    def _on_close(self, ws, code, msg):
        logger.warning("BookTicker WS cerrado code=%s msg=%s", code, msg)

    def _on_error(self, ws, error):
        logger.error("BookTicker WS error: %s", error)
