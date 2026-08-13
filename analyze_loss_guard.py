"""Analisis numerico del guard de perdida LOSS_GUARD_USDC=0.02.

Usa parametros reales de strategy/config.py. Sin red, sin ordenes.
"""
from strategy import config

BASE = config.BASE_ORDER_SIZE_XRP        # 5.0 XRP por orden
GUARD = config.LOSS_GUARD_USDC           # 0.02 USDC
TICK = config.TICK_SIZE_XRPUSDC          # 0.0001
SPREAD_TICKS = config.MIN_SPREAD_TICKS   # valor dinámico (12 desde 2026-08-12)
MAX_NOTIONAL = config.MAX_POSITION_NOTIONAL_USDC  # 25 USDC
FEE = config.MAKER_FEE_RATE              # 0.0 (promo)

price_ref = 1.0
tick_value = TICK * price_ref            # USDC por tick por 1 XRP

# Ganancia bruta por round-trip (compra bid, vende ask, spread de piso 8 ticks)
per_rt_gross = BASE * SPREAD_TICKS * tick_value

# Ticks adversos para perder GUARD con distintos tamanos de inventario
def adverse_ticks(qty):
    return GUARD / (qty * tick_value)

qty_base = BASE
qty_max = MAX_NOTIONAL / price_ref

print("=== GUARD DE PERDIDA LOSS_GUARD_USDC = %.2f USDC ===" % GUARD)
print("Parametros (config real):")
print("  BASE_ORDER_SIZE_XRP      = %.1f" % BASE)
print("  MIN_SPREAD_TICKS         = %d" % SPREAD_TICKS)
print("  TICK_SIZE                = %.4f" % TICK)
print("  MAX_POSITION_NOTIONAL    = %.1f USDC (~%.0f XRP @1.0)" % (MAX_NOTIONAL, qty_max))
print("  MAKER_FEE_RATE           = %.4f (promo 0-fee)" % FEE)
print()
print("Ganancia bruta por round-trip (5 XRP, spread 8 ticks @ ~1.0):")
print("  = 5 * 8 * 0.0001 = %.4f USDC" % per_rt_gross)
print("  -> para recuperar el colchon de %.2f USDC necesita ~%.0f round-trips ganadores"
      % (GUARD, GUARD / per_rt_gross))
print()
print("Movimiento adverso para disparar el guard (perdida acumulada < -%.2f):" % GUARD)
print("  con inventario 5 XRP  : %.0f ticks = %.4f = %.2f%% del precio" %
      (adverse_ticks(qty_base), adverse_ticks(qty_base)*tick_value, adverse_ticks(qty_base)*tick_value*100))
print("  con inventario 25 XRP : %.0f ticks = %.4f = %.2f%% del precio" %
      (adverse_ticks(qty_max), adverse_ticks(qty_max)*tick_value, adverse_ticks(qty_max)*tick_value*100))
print()
print("Comportamiento del guard:")
print("  - Se mide sobre daily_pnl = realized_pnl - total_fees, desde el inicio del run.")
print("  - Se RESETEA cada vez que el proceso se relanza (no es drawdown desde el pico).")
print("  - Temprano en el run (sin colchon): 40 ticks adversos en 5 XRP lo detienen.")
print("  - Tras varios RT ganadores: absorbe algunos cierres adversos hasta pasar -%.2f." % GUARD)
print("  - Con inventario cerca del maximo (25 XRP): solo 8 ticks adversos lo detienen.")
print("  - Efecto: alta fragilidad justo despues de un reinicio; el bot se detiene")
print("    ante la primera rafaga adversa de ~0.4%% si aun no acumulo colchon positivo.")
