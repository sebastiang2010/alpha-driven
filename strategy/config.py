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

# Testnet por defecto (§0.1). NUNCA cambiar a True sin autorizacion humana
# explicita (§0.1). No llamar init_client(real=True) ni set_testnet(False)
# en ningun modulo sin esa autorizacion.
REAL: bool = False

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

# Cantidad mínima típica para XRPUSDC futures. El execution_engine la valida
# contra los filtros reales del símbolo consultados dinámicamente (§3).
# 5.0 XRP ≈ $5.17 a $1.0330: mínimo seguro sobre minNotional $5 (el mínimo
# viable exacto es 4.9 XRP = $5.0617). Autorizado por humano §0.4 (2026-08-09).
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
