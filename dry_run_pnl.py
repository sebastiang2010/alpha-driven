"""Dry-run de PnL (Monte Carlo) usando la logica REAL de AlphaModel.quote_distances.

No envia ordenes, no usa red. Simula fills contra una trayectoria de precios
sintetica con volatilidad configurable, y aplica el guard real LOSS_GUARD_USDC.

Uso:
    python dry_run_pnl.py [sigma_por_step] [n_steps] [n_paths]
"""
import sys, os, math, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy.alpha_model import AlphaModel
from strategy import config
import strategy.alpha_model as alpha_mod

MODEL = AlphaModel()

BASE = config.BASE_ORDER_SIZE_XRP
GUARD = config.LOSS_GUARD_USDC
TICK = config.TICK_SIZE_XRPUSDC
SPREAD_TICKS = config.MIN_SPREAD_TICKS
MAX_QTY = config.MAX_POSITION_NOTIONAL_USDC  # ~25 XRP @1.0
CYCLE = config.CYCLE_INTERVAL_SEC


def round_tick(x):
    return round(x / TICK) * TICK


def advance(mid, sigma, rng, jump_prob=0.0, jump_sigma=0.0,
            kappa=0.0, ou_sigma=0.0, level=1.0):
    dt = CYCLE
    if kappa > 0 and ou_sigma > 0:
        # Ornstein-Uhlenbeck en log-precio (mean-reverting, representa XRP intradia)
        lm = math.log(mid)
        lm += -kappa * (lm - math.log(level)) * dt + ou_sigma * math.sqrt(dt) * rng.gauss(0.0, 1.0)
        r = lm - math.log(mid)
    else:
        r = rng.gauss(0.0, sigma)
        if jump_prob > 0 and rng.random() < jump_prob:
            r += rng.gauss(0.0, jump_sigma)
    return mid * math.exp(r)


def update_position(inv, avg, qty, price, is_buy):
    """Devuelve (inv, avg, realized_delta). avg = precio de entrada POSITIVO."""
    realized = 0.0
    if is_buy:
        if inv >= 0:  # suma a long (o abre desde 0)
            inv_new = inv + qty
            avg_new = price if inv == 0 else (inv * avg + qty * price) / inv_new
        else:  # reduce short
            close = min(qty, -inv)
            realized = close * (avg - price)  # corto gana si avg > price
            inv_new = inv + close
            avg_new = avg if inv_new < 0 else 0.0
    else:  # sell
        if inv <= 0:  # suma a short (o abre desde 0)
            inv_new = inv - qty
            avg_new = price if inv == 0 else ((-inv) * avg + qty * price) / (-inv_new)
        else:  # reduce long
            close = min(qty, inv)
            realized = close * (price - avg)  # long gana si price > avg
            inv_new = inv - close
            avg_new = avg if inv_new > 0 else 0.0
    return inv_new, avg_new, realized


def run_path(sigma, n_steps, seed, base_price=1.0, jump_prob=0.0, jump_sigma=0.0,
              kappa=0.0, ou_sigma=0.0):
    rng = random.Random(seed)
    mid = base_price
    prev_mid = mid
    inv, avg, realized = 0.0, 0.0, 0.0
    t = 0.0
    round_trips = 0
    stopped = False
    last_sign = 0
    for _ in range(n_steps):
        spread = config.MIN_SPREAD_TICKS * TICK
        snapshot = {
            "mid": mid,
            "mid_price": mid,
            "best_bid": mid - spread / 2,
            "best_ask": mid + spread / 2,
            "spread": spread,
            "volatility": sigma,
            "order_imbalance": 0.0,
            "momentum": 0.0,
            "microprice": mid,
            "buy_volume": 1.0,
            "sell_volume": 1.0,
            "tick_size": TICK,
            "inventory": inv,
        }
        alpha = MODEL.compute_alpha(snapshot)
        res = MODEL.quote_distances(snapshot, alpha, inv, sigma, now_sec=t)
        prev_mid = mid
        new_mid = advance(mid, sigma, rng, jump_prob, jump_sigma, kappa, ou_sigma, base_price)
        t += CYCLE
        if res is None:
            mid = new_mid
            continue
        bid_dist, ask_dist = res
        bid = round_tick(mid - bid_dist)
        ask = round_tick(mid + ask_dist)
        # simula fill: si el precio cae hasta el bid -> compra; sube hasta el ask -> vende
        filled = False
        if new_mid <= bid and inv < MAX_QTY:
            q = min(BASE, MAX_QTY - inv)
            if q > 0:
                inv, avg, rd = update_position(inv, avg, q, bid, True)
                realized += rd
                filled = True
        elif new_mid >= ask and inv > -MAX_QTY:
            q = min(BASE, MAX_QTY + inv)
            if q > 0:
                inv, avg, rd = update_position(inv, avg, q, ask, False)
                realized += rd
                filled = True
        if filled:
            s = 1 if inv > 0 else (-1 if inv < 0 else 0)
            if last_sign != 0 and s == 0:
                round_trips += 1
            last_sign = s
        mid = new_mid
        if realized < -GUARD:
            stopped = True
            inv, avg, realized = 0.0, 0.0, realized  # flatten, mantiene pnl acumulado
            break
    return realized, round_trips, stopped


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Dry-run PnL Monte Carlo (quote logic real)")
    ap.add_argument("sigma", nargs="?", type=float, default=0.0005)
    ap.add_argument("n_steps", nargs="?", type=int, default=3000)
    ap.add_argument("n_paths", nargs="?", type=int, default=300)
    ap.add_argument("--jp", type=float, default=0.02, help="prob salto por paso")
    ap.add_argument("--js", type=float, default=0.01, help="sigma del salto")
    ap.add_argument("--kquote", type=float, default=None, help="override config.K_QUOTE")
    ap.add_argument("--floor", type=int, default=None, help="override MIN_SPREAD_TICKS")
    ap.add_argument("--msm", type=float, default=None, help="override MOMENTUM_SPREAD_MULTIPLIER")
    ap.add_argument("--kappa", type=float, default=0.0, help="OU mean-reversion (0=RW)")
    ap.add_argument("--ou-sigma", type=float, default=0.0, help="OU vol por step")
    ap.add_argument("--guard", type=float, default=None, help="override LOSS_GUARD_USDC")
    args = ap.parse_args()

    if args.kquote is not None:
        alpha_mod.K_QUOTE = args.kquote
    if args.floor is not None:
        config.MIN_SPREAD_TICKS = args.floor
    if args.msm is not None:
        config.MOMENTUM_SPREAD_MULTIPLIER = args.msm
    if args.guard is not None:
        global GUARD
        GUARD = args.guard

    sigma, n_steps, n_paths, jp, js = args.sigma, args.n_steps, args.n_paths, args.jp, args.js
    pnls, rts, stops = [], [], 0
    for i in range(n_paths):
        p, rt, st = run_path(sigma, n_steps, i, jump_prob=jp, jump_sigma=js,
                             kappa=args.kappa, ou_sigma=args.ou_sigma)
        pnls.append(p)
        rts.append(rt)
        if st:
            stops += 1
    pnls.sort()
    mean = sum(pnls) / len(pnls)
    med = pnls[len(pnls) // 2]
    win = sum(1 for p in pnls if p > 0) / len(pnls)
    avg_rt = sum(rts) / len(rts)
    print("K_QUOTE=%.2f floor=%d msm=%.1f | sigma=%.4f jump_p=%.3f jump_s=%.4f steps=%d paths=%d"
          % (alpha_mod.K_QUOTE, config.MIN_SPREAD_TICKS, config.MOMENTUM_SPREAD_MULTIPLIER, sigma, jp, js, n_steps, n_paths))
    print("  PnL medio      : %+.4f USDC" % mean)
    print("  PnL mediano    : %+.4f USDC" % med)
    print("  Win-rate       : %.1f%%" % (win * 100))
    print("  Round-trips    : %.1f promedio" % avg_rt)
    print("  Guard disparado: %.1f%% de los paths" % (stops / n_paths * 100))


if __name__ == "__main__":
    main()
