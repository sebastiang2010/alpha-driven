# AGENTS.md

## Contexto
- Bot de **market making adaptativo** para **Binance Futures (USDS-M)** sobre **XRPUSDC**, en Python.
- Especificación completa: `market_making_xrpusdc_futures.md` (§§0–24). La **Sección 0 son reglas no negociables** y tiene prioridad sobre el resto del documento.
- Estado: infraestructura de API/WebSocket ya existe; `strategy/` implementado (módulos base + integrador), con `config.py`, `logs/`, `reports/` y `STATUS.md` en su lugar. Tests del Risk Engine y del execution engine: **27/27 OK**.
- Repo git ya inicializado (el checklist §24 asume `git diff`/`git log`; ambos operativos). Mantener commits chicos y frecuentes (§0.3).
- **GitHub**: repositorio remoto en `https://github.com/sebastiang2010/alpha-driven` (origin). NO hacer push a main sin autorización humana; los commits se hacen localmente salvo indicación contraria. Nunca pushear credenciales ni `.env` (§0.5).

## Reglas no negociables (§0)
- **Testnet primero. Mainnet SOLO con autorización humana explícita** (§0.1). No llamar `init_client(real=True)` ni `set_testnet(False)` sin esa autorización; si el sistema queda listo para mainnet, detenerse y reportarlo.
- **Credenciales** (§0.5): `API_binance_futuros.py` tiene claves hardcodeadas (testnet **y mainnet reales**). Nunca loguearlas, imprimirlas, commitearlas ni rotarlas sin preguntar (§0.2). Mantener `.env` en `.gitignore`.
- **Detenerse y preguntar** (§0.2) ante: primera orden mainnet, subir de Nivel de exposición, kill switch activado, falta una función de API (no escribir implementación paralela), posición no reconciliable, credenciales nuevas.
- **Presupuesto de riesgo** como constantes en `config.py`, no números improvisados (§0.4): `MAX_DAILY_LOSS_USDC`, `MAX_POSITION_NOTIONAL_USDC`, `MAX_DRAWDOWN_PCT`, `MAX_LEVERAGE_USED`. Si no están definidos: proponer valores conservadores, marcarlos "pendiente de confirmación" en `STATUS.md`, y no operar con fondos reales.
- **Decisión de diseño aprobada — Nivel 0 / dry-run con simulación**: cuando `EXPOSURE_LEVEL=0`, el bot NO queda bloqueado en tamaño 0: puede simular órdenes usando una constante de simulación (`SIMULATION_QUOTE_MULTIPLIER` en `config.py`) en lugar del multiplicador `0.0` del nivel 0. Esto habilita la corrida integrada dry-run. Los niveles reales (>=1) NO se modifican y subirlos sigue requiriendo autorización humana (§0.2). En Nivel 0 jamás se envían órdenes reales (§0.1/§21).
- **Promo 0 fees en XRPUSDC (confirmada por humano 2026-08-11; la API reporta `commission=0.0` y `funding=0.0` — coherente con la promo)**: **priorizar SIEMPRE órdenes MAKER** (post-only `GTX`, que es el comportamiento actual del bot §2). No degradar a taker (IOC/FOK) por conveniencia de ejecución: con 0 fees el edge completo está en el lado maker y el taker no aporta nada. Si la promo termina (vuelve `MAKER_FEE_RATE=0.0002`), **detenerse y reportar** antes de seguir operando igual: hay que re-evaluar el piso de spread (`MIN_SPREAD_TICKS=8`) y los costos §18.
- Mantener **`STATUS.md`** actualizado cada 15–20 min y commits chicos y frecuentes (§0.3).

## Autonomía del desarrollo
Directiva aprobada por el usuario: dentro de **testnet/dry-run**, el orquestador TOMA decisiones operativas y de diseño sin preguntar:
- Corregir bugs, agregar tests, refactorizar, elegir convenciones, commitear chico y frecuente (§0.3).
- **SOLO se pide autorización humana para**: primera orden mainnet, `real=True` / salir de testnet, subir nivel de exposición real, rotar/agregar credenciales, operar con fondos reales, o cualquier acción irreversible sobre la cuenta.
- Los presupuestos de riesgo propuestos siguen "pendiente de confirmación" hasta que el humano los confirme (§0.4), pero eso NO bloquea el desarrollo en testnet/dry-run.

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
- Órdenes maker: `timeInForce='GTX'` (post-only), `positionSide='BOTH'`, `reduceOnly` según corresponda. **Siempre maker-first** — el par XRPUSDC tiene promo de 0 fees (regla §0, 2026-08-11): no usar taker mientras dure la promo.
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
