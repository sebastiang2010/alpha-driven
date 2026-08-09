# AGENTS.md

## Contexto
- Bot de **market making adaptativo** para **Binance Futures (USDS-M)** sobre **XRPUSDC**, en Python.
- Especificación completa: `market_making_xrpusdc_futures.md` (§§0–24). La **Sección 0 son reglas no negociables** y tiene prioridad sobre el resto del documento.
- Estado: infraestructura de API/WebSocket ya existe; aún NO existen `strategy/`, `logs/`, `reports/`, `STATUS.md`, `config.py`. Ese layout es el entregable objetivo (§23).
- **Todavía no es un repo git** (el checklist §24 asume `git diff`/`git log`; inicializar git antes del primer commit).

## Reglas no negociables (§0)
- **Testnet primero. Mainnet SOLO con autorización humana explícita** (§0.1). No llamar `init_client(real=True)` ni `set_testnet(False)` sin esa autorización; si el sistema queda listo para mainnet, detenerse y reportarlo.
- **Credenciales** (§0.5): `API_binance_futuros.py` tiene claves hardcodeadas (testnet **y mainnet reales**). Nunca loguearlas, imprimirlas, commitearlas ni rotarlas sin preguntar (§0.2). Mantener `.env` en `.gitignore`.
- **Detenerse y preguntar** (§0.2) ante: primera orden mainnet, subir de Nivel de exposición, kill switch activado, falta una función de API (no escribir implementación paralela), posición no reconciliable, credenciales nuevas.
- **Presupuesto de riesgo** como constantes en `config.py`, no números improvisados (§0.4): `MAX_DAILY_LOSS_USDC`, `MAX_POSITION_NOTIONAL_USDC`, `MAX_DRAWDOWN_PCT`, `MAX_LEVERAGE_USED`. Si no están definidos: proponer valores conservadores, marcarlos "pendiente de confirmación" en `STATUS.md`, y no operar con fondos reales.
- Mantener **`STATUS.md`** actualizado cada 15–20 min y commits chicos y frecuentes (§0.3).

## Arquitectura
- `API_binance_futuros.py` es **la única interfaz** con Binance Futures (§1). Analizarlo completo antes de escribir código; no duplicar funciones que ya existan.
- WebSocket para datos en tiempo real; REST solo para config, estado, órdenes, recuperación y reconciliación (§2). Reconexión con backoff; si se superan los reintentos, activar el kill switch (§13).
- Instrumento: **XRPUSDC**, con el símbolo como parámetro de config (no hardcodeado). Consultar las specs reales del símbolo (precisiones, tick/step size, min quantity/notional) dinámicamente — nunca asumirlas (§3).
- Risk Engine (`risk_engine.py`) debe ser un módulo independiente que lea límites de `config.py`, con tests unitarios que verifiquen rechazos (§12).

## Módulos WebSocket existentes
URLs: real `wss://fstream.binance.com/public/ws`, testnet `wss://stream.binancefuture.com/public/ws`. Reintento cada 5 s con `ping_interval=20`.

- `websocket_bookticker.py` → `BookTickerWebSocket(symbol, on_bookticker, real=False)`, stream `{symbol}@bookTicker`. Callback: `on_bookticker(bid_price, bid_qty, ask_price, ask_qty, timestamp)`.
- `websocket_depth.py` → `DepthWebSocket(symbol, on_depth, real=False, depth_levels=5)`, stream `{symbol}@depth{levels}@100ms`. Callback: `on_depth(bids, asks, depth, timestamp)` con `bids`/`asks` como `[["precio","qty"],...]` (mismo formato que `api.get_order_book()`).
- `websocket_trades.py` → `TradeWebSocket(symbol, on_trade, real=False)`, stream `{symbol}@trade`. Callback: `on_trade(price, qty, is_buyer_maker, timestamp)`.

## Peculiaridades de la API
- Los métodos devuelven `BinanceAPIException` como resultado en vez de lanzarla — el caller debe chequear el tipo de retorno.
- Órdenes maker: `timeInForce='GTX'` (post-only), `positionSide='BOTH'`, `reduceOnly` según corresponda.
- `_call_with_retry(func, retries=2, delay=0.5, ...)` envuelve las llamadas críticas.
- Sincronización de tiempo: `adjust_client_time()` + hilo daemon cada 300 s que fija `client.timestamp_offset`.
- **Bug latente**: `close_listen_key` está definida como método de instancia (`def close_listen_key(self, listen_key)` → `self.client`) en un módulo de funciones sueltas; llamarla como función del módulo rompe.

## Errores conocidos a evitar (§0.6) — cubrir con tests/assertions
- Convenciones de volatilidad/tiempo inconsistentes (√T aplicada dos veces, sigma anualizada vs efectiva).
- Multiplicadores/tamaños de inventario mal propagados entre cálculo de quotes y envío de órdenes.
- IDs de órdenes compartidos bid/ask sin purgar (órdenes fantasma acumuladas).
- Sincronización de estado/inventario cada ciclo sin control de frecuencia (rate-limit).
- Race conditions sobre listas de IDs de órdenes compartidas entre tareas concurrentes.

## Testing y criterio de éxito
- No hay framework de tests configurado. Se exigen tests unitarios para el **Risk Engine** (§12) y una prueba explícita del **kill switch** (§13) antes de confiar en ellos.
- Rentabilidad = **`NetPnL > 0`** donde `NetPnL = GrossPnL − fees − funding − slippage` (§18). Una operación ganadora NO es evidencia (§19). No usar RL directamente sobre leverage/posición/pérdida/kill switch (§17).

## Operaciones
- En la misma VPS corren otros bots en producción (grid bot, bot A-S en BTC/USDC). **No tocar ni reiniciar esos procesos.** El proceso nuevo debe correr aislado: venv propio, logs propios, sin compartir puertos, archivos ni el límite de rate-limit (§0.3).
