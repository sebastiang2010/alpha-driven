"""Configuracion central del bot de market making (Binance Futures, XRPUSDC).

Este modulo es el UNICO lugar donde se definen las constantes de riesgo y
operativas. Los demas modulos (risk_engine, execution_engine, market_maker)
deben importar estas constantes de aqui; NO hardcodear simbolos ni numeros
de riesgo en ningun otro lugar (§1, §3, §0.4).

==============================================================================
RIESGO: PROPUESTA pendiente de confirmacion (§0.4)
------------------------------------------------------------------------------
Todos los valores del bloque "Presupuesto de riesgo" son PROPUESTAS
CONSERVADORAS que deben ser confirmadas por un humano antes de operar con
fondos reales. Sin esa confirmacion, el bot solo puede operar en testnet
(REAL = False) o en dry-run (EXPOSURE_LEVEL = 0). Marcar cualquier cambio
pendiente en STATUS.md (§0.3).
==============================================================================
"""

import pathlib

# ---------------------------------------------------------------------------
# Símbolo y entorno (§0.1, §3)
# ---------------------------------------------------------------------------

# Símbolo como parámetro de config, nunca hardcodeado en otro módulo (§3).
SYMBOL: str = "XRPUSDC"

# MAINNET autorizada por humano §0.2 (2026-08-10). No llamar
# init_client(real=True) ni set_testnet(False) en ningun modulo sin esa
# autorizacion explicita (§0.1).
REAL: bool = True

# ---------------------------------------------------------------------------
# Presupuesto de riesgo (§0.4) — PROPUESTA pendiente de confirmacion
# ---------------------------------------------------------------------------

# Pérdida máxima diaria permitida, en USDC.
MAX_DAILY_LOSS_USDC: float = 10.0

# Tamaño de posición nocional máximo permitido, en USDC.
MAX_POSITION_NOTIONAL_USDC: float = 25.0

# Drawdown máximo permitido desde el pico de equity (5%).
MAX_DRAWDOWN_PCT: float = 0.05

# Apalancamiento máximo usado. = máximo REAL del símbolo: 1/requiredMarginPercent
# (exchangeInfo fapi.binance.com verificado 2026-08-09: 5.0% → 20x). 75x es
# imposible: a 75x el margen inicial (1.33%) < margen de mantenimiento (2.5%).
# Autorizado por humano §0.4 (2026-08-09).
MAX_LEVERAGE_USED: int = 20

# Presupuestos de riesgo §0.4 CONFIRMADOS por humano (2026-08-09):
# MAX_DAILY_LOSS_USDC=10.0, MAX_POSITION_NOTIONAL_USDC=25.0,
# MAX_DRAWDOWN_PCT=0.05, MAX_LEVERAGE_USED=20, BASE_ORDER_SIZE_XRP=5.0.
# run_mainnet.py aborta si esto es False (§0.4: no operar con fondos reales
# con presupuestos "pendiente de confirmación").
BUDGETS_CONFIRMED: bool = True

# ---------------------------------------------------------------------------
# Niveles de exposición (§21)
# ---------------------------------------------------------------------------

# Nivel actual de exposición. Nivel 0 = dry-run (no enviar órdenes reales).
# Subir de nivel requiere autorización humana (§0.2).
# Autorizado por humano §0.2 (2026-08-09): Nivel 1 mainnet mínimo.
EXPOSURE_LEVEL: int = 1

# Multiplicador de tamaño por nivel. La exposición efectiva se calcula
# multiplicando el tamaño base por este multiplicador.
EXPOSURE_MULTIPLIERS: dict = {0: 0.0, 1: 1.0, 2: 2.0, 3: 4.0}

# Multiplicador de simulación para el Nivel 0 (dry-run) (§0.1/§21).
# Decisión aprobada (§0.2): en EXPOSURE_LEVEL=0 el bot NO queda bloqueado en
# tamaño 0: simula órdenes usando este multiplicador en lugar del 0.0 del
# nivel 0. Esto habilita la corrida integrada dry-run. Jamás se envían
# órdenes reales en Nivel 0 (§0.1/§21).
# PROPUESTA pendiente de confirmación (§0.4).
SIMULATION_QUOTE_MULTIPLIER: float = 1.0


def effective_exposure_multiplier() -> float:
    """Multiplicador efectivo según el nivel de exposición actual.

    Nivel 0 (dry-run): usa SIMULATION_QUOTE_MULTIPLIER (decisión aprobada
    §0.2 — simula órdenes en vez de bloquear a tamaño 0).
    Niveles >= 1: usa EXPOSURE_MULTIPLIERS[level] (sin cambios, subir de
    nivel requiere autorización humana §0.2).
    """
    level = int(EXPOSURE_LEVEL)
    if level == 0:
        return float(SIMULATION_QUOTE_MULTIPLIER)
    return float(EXPOSURE_MULTIPLIERS.get(level, 0.0))

# ---------------------------------------------------------------------------
# Tamaños de orden (§3)
# ---------------------------------------------------------------------------

# Cantidad mínima para XRPUSDC futures. El execution_engine la valida contra
# los filtros reales del símbolo consultados dinámicamente (§3).
# AUTORIZADO por humano §0.2 (2026-08-10/11, "usa 5.0 XRP"): minNotional $5
# confirmado por humano → 5.0 XRP × ~$1.02 ≈ $5.10 > $5 (margen 2%).
# Antes: 4.9 XRP ≈ $5.05 (marginal; el lado reduce de 2.4 XRP ≈ $2.44 < $5
# era rechazado → 0 órdenes todo el día, posición 4.9 atascada).
# NOTA: el algoritmo de cálculo de tamaños queda para revisión dedicada
# (el humano pidió NO tocarlo en esta iteración).
BASE_ORDER_SIZE_XRP: float = 5.0

# ---------------------------------------------------------------------------
# Ciclo principal
# ---------------------------------------------------------------------------

# Intervalo del ciclo principal del market maker, en segundos.
CYCLE_INTERVAL_SEC: float = 5.0

# ---------------------------------------------------------------------------
# Gestión de órdenes
# ---------------------------------------------------------------------------

# Vida mínima/máxima de una orden antes de permitir su reemplazo (segundos).
MIN_ORDER_LIFETIME_SEC: int = 15
MAX_ORDER_LIFETIME_SEC: int = 60

# Máximo de órdenes abiertas simultáneas (2 bid + 2 ask).
MAX_OPEN_ORDERS: int = 4

# ---------------------------------------------------------------------------
# Sincronización de estado (§0.6)
# ---------------------------------------------------------------------------

# Frecuencia de sincronización de estado/inventario. NO sincronizar en cada
# ciclo para evitar rate-limit (§0.6). En segundos.
STATE_SYNC_INTERVAL_SEC: int = 30

# ---------------------------------------------------------------------------
# WebSocket (§13)
# ---------------------------------------------------------------------------

# Máximo de reintentos de conexión antes de activar el kill switch (§13).
WS_MAX_RECONNECT_ATTEMPTS: int = 5

# ---------------------------------------------------------------------------
# Volatilidad y re-cotización
# ---------------------------------------------------------------------------

# Ventana para el cálculo de sigma de corto plazo (segundos).
VOLATILITY_WINDOW_SEC: int = 60

# Ventana para el cálculo de alfa (segundos).
ALPHA_WINDOW_SEC: int = 30

# Intervalo de muestreo de referencia para sigma (§0.6). market_state.py
# reescala la volatilidad por evento del WS a este intervalo fijo; alpha_model
# lo usa como base de sus conversiones de escala (nunca √T doble).
SAMPLING_INTERVAL_SEC: float = 5.0

# Si el mid se mueve más que este porcentaje, re-cotizar (≈1 tick).
QUOTE_UPDATE_THRESHOLD_PCT: float = 0.001

# ---------------------------------------------------------------------------
# Piso de spread (breakeven contra fees maker) — aprobado por humano §0.2
# ---------------------------------------------------------------------------

# Tick size de XRPUSDC (0.0001 USDC, verificado contra exchangeInfo §3).
# Fallback para alpha_model cuando el snapshot no trae tick_size dinámico;
# si market_state llega a exponer tick_size, ese valor tiene prioridad
# (specs dinámicas §3 — nunca asumir, pero no hay otra fuente hoy).
TICK_SIZE_XRPUSDC: float = 0.0001

# Piso mínimo de spread total cotizado (bid_dist + ask_dist), en ticks.
# Breakeven con fee maker VIP0 (MAKER_FEE_RATE=0.0002) y tick 0.0001, para
# el precio de referencia de la corrida mainnet (P ≈ 1.026):
#     N_breakeven = 2 * f_maker * P / tick = 2*0.0002*1.026/0.0001 = 4.1 ticks
#     -> mínimo operativo N_min = 5 ticks (redondeo hacia arriba).
# Piso aprobado por humano (2026-08-10, tras round trip mainnet con adverse
# selection -0.0113 USDC: no existía piso estructural): 8 ticks = 1.95x las
# fees maker -> margen de seguridad; el spread por volatilidad puede ser
# mayor y entonces manda el cálculo previo.
MIN_SPREAD_TICKS: int = 8

# ---------------------------------------------------------------------------
# Filtro de momentum anti-adverse-selection (§14) — PROPUESTA pendiente de
# confirmacion (§0.4)
# ---------------------------------------------------------------------------
#
# Evidencia (2026-08-10, mainnet): con el piso de 8 ticks el mercado cayó
# ~0.9% en ~50 min; el bid se llenó primero (compras en caída) y el ask no
# (RTs -0.0118 y -0.0128 USDC). En mercado tranquilo el piso da
# +0.0004..+0.0014 neto. Este filtro detecta movimiento direccional del mid
# y ENSANCHA EL PISO (no solo el spread calculado): si hay momentum, el piso
# efectivo pasa a MIN_SPREAD_TICKS * MOMENTUM_SPREAD_MULTIPLIER (8 -> 16
# ticks). Es SIMETRICO (no direccional): no asume hacia dónde va el precio,
# solo que un movimiento >= MOMENTUM_MAX_TICKS en la ventana hace más
# probable que un fill inmediato sea adverso. El cooldown evita alternar
# rápido entre 8 y 16 ticks cuando el mid oscila alrededor del umbral.

# Activa/desactiva el filtro completo (sin borrar el historial).
MOMENTUM_ENABLED: bool = True

# Ventana de observación del mid (segundos): mid_actual vs mid al inicio de
# la ventana. Con CYCLE_INTERVAL_SEC=5, ~6 muestras por ventana (suficiente
# para un filtro de régimen; no es una medida de alta frecuencia).
MOMENTUM_WINDOW_SECONDS: float = 30.0

# Movimiento mínimo del mid (en ticks, tick_size=0.0001) dentro de la
# ventana para declarar momentum. 8 ticks = mismo orden que el piso de
# spread: un movimiento de 1 piso completo en 30 s es claramente direccional.
MOMENTUM_MAX_TICKS: float = 8.0

# Multiplicador del piso cuando hay momentum: piso efectivo =
# MIN_SPREAD_TICKS * MOMENTUM_SPREAD_MULTIPLIER = 16 ticks (4x las fees
# maker, cubriendo el gap típico observado de 19 ticks entre entrada y
# salida en el RT adverso del 2026-08-10).
MOMENTUM_SPREAD_MULTIPLIER: float = 2.0

# Una vez detectado momentum, el spread ampliado se mantiene al menos este
# tiempo aunque el mid se calme (evita alternar 8/16 ticks en cada ciclo).
MOMENTUM_COOLDOWN_SECONDS: float = 60.0

# ---------------------------------------------------------------------------
# Kill switch (§13)
# ---------------------------------------------------------------------------

# Límite acumulado de fees (USDC) que activa el kill switch.
KILL_SWITCH_FEE_LIMIT_USDC: float = 1.0

# ---------------------------------------------------------------------------
# Costos de transacción para NetPnL (§18) — PROPUESTA pendiente de confirmacion
# ---------------------------------------------------------------------------

# Fee maker de referencia (proporción del nocional). Fuente única (§0.4):
# market_maker y alpha_model lo importan de acá, NO lo redefinen.
# Fee maker real de Binance Futures (VIP0): 0.0002 por lado.
MAKER_FEE_RATE: float = 0.0002

# Funding de Binance Futures: se cobra PERIODICAMENTE, cada 8 h, sobre el
# nocional de la posición (no por fill). Para el NetPnL esperado (§18) se
# modela proporcional al tiempo de tenencia esperado:
#     funding_por_trade = FUNDING_RATE_PER_8H * (EXPECTED_HOLD_SEC / FUNDING_INTERVAL_SEC)
# Referencia: Binance USDⓈ-M Futures funding (tasa por intervalo de 8 h).
# Con los defaults: 0.0001 * (300 / 28800) ≈ 0.00000104 por trade
# (antes se aplicaba 0.0001 fijo por fill — sobreestimaba ~1000x el costo).
# PROPUESTA pendiente de confirmación (§0.4).
FUNDING_RATE_PER_8H: float = 0.0001
FUNDING_INTERVAL_SEC: float = 8.0 * 3600.0   # 28,800 s (cobro periódico Binance)
EXPECTED_HOLD_SEC: float = 300.0             # tenencia esperada por posición (maker)

# Slippage para órdenes MAKER (post-only GTX): 0 por definición — una orden
# maker nunca se ejecuta como taker, así que no paga half-spread de cruce.
# Referencia: Binance order types, timeInForce=GTX (post-only).
# Mantener > 0 solo si se quiere un colchón conservador de adverse selection.
# PROPUESTA pendiente de confirmación (§0.4).
SLIPPAGE_MAKER_BPS: float = 0.0

# ---------------------------------------------------------------------------
# Paths de logging (§15) — relativos al directorio del proyecto
# ---------------------------------------------------------------------------

# Directorio raíz del proyecto (padre de strategy/).
PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent

# Directorios base de logs y reportes.
LOG_DIR: pathlib.Path = PROJECT_ROOT / "logs"
REPORT_DIR: pathlib.Path = PROJECT_ROOT / "reports"

# Subdirectorios de logging por categoría.
LOG_MARKET_DATA: pathlib.Path = LOG_DIR / "market_data"
LOG_ORDERS: pathlib.Path = LOG_DIR / "orders"
LOG_FILLS: pathlib.Path = LOG_DIR / "fills"
LOG_PNL: pathlib.Path = LOG_DIR / "pnl"
LOG_DECISIONS: pathlib.Path = LOG_DIR / "decisions"

# ---------------------------------------------------------------------------
# Operación: instancia única y watchdog ligero (§11, §12)
# ---------------------------------------------------------------------------

# Lock single-instance (§12): run_mainnet.py lo crea con O_EXCL + PID y aborta
# (exit code 4) si otra instancia está corriendo. Previene la clase de bug de la
# corrida 2026-08-11 (2 procesos compartiendo XRPUSDC -> -2011 y orden huérfana).
INSTANCE_LOCK_PATH: pathlib.Path = LOG_DIR / "run_mainnet.lock"

# Heartbeat del monitor (§11): cada cuánto reporta progreso el hilo monitor.
MONITOR_INTERVAL_SEC: float = 15.0

# Sin decisiones nuevas en el journal por este tiempo -> warning de diagnóstico.
STALL_WARN_SEC: float = 60.0

# Sin decisiones nuevas por este tiempo -> stop() conservador (cierra órdenes).
STALL_STOP_SEC: float = 180.0

# Divergencia decisión/ejecución (§11): si hay >= N decisiones reason==ok en la
# ventana y 0 eventos de órdenes -> posible colgamiento del execution_engine.
DIVERGENCE_MIN_DECISIONS: int = 8
