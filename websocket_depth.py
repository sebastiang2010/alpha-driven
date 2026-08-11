# websocket_depth.py
"""
WebSocket de depth (top-N del order book) para Binance Futures.
Stream: {symbol}@depth{levels}@100ms → snapshots parciales del libro.
El callback recibe (bids, asks, depth, timestamp) con bids/asks como
listas [["precio","qty"], ...], igual formato que api.get_order_book().
"""

import json
import time
import logging
import threading
import websocket

logger = logging.getLogger("DepthWebSocket")

class DepthWebSocket:
    def __init__(self, symbol: str, on_depth, real: bool = False, depth_levels: int = 5):
        self.symbol = symbol.lower()
        self.on_depth = on_depth
        self.real = real
        self.depth_levels = depth_levels
        self.ws = None
        self._running = False
        self._thread = None

        base_ws = "wss://fstream.binance.com/public/ws" if real else "wss://stream.binancefuture.com/public/ws"
        self.url = f"{base_ws}/{self.symbol}@depth{self.depth_levels}@100ms"
        logger.info("DepthWebSocket URL: %s (real=%s, levels=%s)", self.url, real, depth_levels)

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
                self.ws.run_forever(ping_interval=10, ping_timeout=10)
            except Exception as e:
                logger.error("Depth WS fatal: %s", e)
            time.sleep(1)

    def _on_open(self, ws):
        logger.info("DepthWebSocket conectado para %s (top-%s)", self.symbol, self.depth_levels)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if 'b' in data and 'a' in data:
                self.on_depth(
                    bids=data['b'],
                    asks=data['a'],
                    depth=self.depth_levels,
                    timestamp=data.get('E', int(time.time() * 1000))
                )
        except Exception as e:
            logger.error("Depth parse error: %s", e)

    def _on_close(self, ws, code, msg):
        logger.warning("Depth WS cerrado code=%s msg=%s", code, msg)

    def _on_error(self, ws, error):
        logger.error("Depth WS error: %s", error)
