#!/usr/bin/env python3
"""Genera reports/trade_status.txt con resumen de trades, PnL y estado del bot.

Solo lectura de logs (nunca toca el bot). Ejecutar cada 5 min via
reports/update_trade_status.sh. Criterio de rentabilidad: NetPnL (§18).
"""
import glob
import json
import os
import re
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
OUT = os.path.join(ROOT, "reports", "trade_status.txt")
FEE_FALLBACK = 0.0002  # solo si config.py no tiene MAKER_FEE_RATE (§0.4)


def now_local():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def local_ts(epoch):
    return datetime.fromtimestamp(epoch).strftime("%d/%m %H:%M:%S")


def load_jsonl(path):
    rows = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return rows


def maker_fee_rate():
    cfg = os.path.join(ROOT, "strategy", "config.py")
    if os.path.exists(cfg):
        with open(cfg, "r", encoding="utf-8") as f:
            m = re.search(r"MAKER_FEE_RATE\s*[:=]\s*([0-9.]+)", f.read())
            if m:
                return float(m.group(1))
    return FEE_FALLBACK


def main():
    fee_rate = maker_fee_rate()

    # ---- Fills reales (simulated=false) de toda la corrida real ----
    fills = []
    for path in glob.glob(os.path.join(LOGS, "fills", "fills_*.jsonl")):
        for r in load_jsonl(path):
            if r.get("status") == "FILLED" and not r.get("simulated", True):
                fills.append(r)
    fills.sort(key=lambda r: r.get("ts", 0))

    n_trades = len(fills)
    buy_qty = sum(r["fill_qty"] for r in fills if r["side"] == "BUY")
    sell_qty = sum(r["fill_qty"] for r in fills if r["side"] == "SELL")
    buy_not = sum(r["fill_qty"] * r["fill_price"] for r in fills if r["side"] == "BUY")
    sell_not = sum(r["fill_qty"] * r["fill_price"] for r in fills if r["side"] == "SELL")
    gross_pnl = sell_not - buy_not
    fees = (buy_not + sell_not) * fee_rate
    net_pnl = gross_pnl - fees
    avg_buy = buy_not / buy_qty if buy_qty else 0.0
    avg_sell = sell_not / sell_qty if sell_qty else 0.0

    # ---- Ultima decision ----
    dec_path = os.path.join(LOGS, "decisions", "agent_decisions.jsonl")
    decs = load_jsonl(dec_path)
    last_dec = decs[-1] if decs else {}
    inv = float(last_dec.get("inventory", 0.0)) if last_dec else 0.0

    freshness = None
    status = "SIN DATOS"
    if last_dec:
        raw = last_dec["timestamp"].replace("Z", "+00:00")
        if "+" not in raw:
            raw += "+00:00"
        dec_dt = datetime.fromisoformat(raw)
        age_s = (datetime.now(timezone.utc) - dec_dt).total_seconds()
        freshness = age_s
        status = "OPERATIVO" if age_s <= 300 else f"SIN SEÑAL HACE {int(age_s // 60)} MIN (posible caido)"

    # PnL no realizado si hay inventario abierto
    unreal = 0.0
    if last_dec and abs(inv) > 1e-9 and n_trades:
        mid = float(last_dec.get("mid_price", 0.0))
        if inv > 0:
            unreal = inv * (mid - avg_buy)
        else:
            unreal = inv * (avg_sell - mid)

    # ---- Ordenes de hoy ----
    orders = []
    for path in sorted(glob.glob(os.path.join(LOGS, "orders", "orders_*.jsonl"))):
        orders += load_jsonl(path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    orders_today = [o for o in orders if datetime.fromtimestamp(o.get("ts", 0), timezone.utc).strftime("%Y-%m-%d") == today]
    ev_counts = {}
    for o in orders_today:
        ev = o.get("event", "?")
        ev_counts[ev] = ev_counts.get(ev, 0) + 1

    # ---- Render ----
    L = []
    L.append("=== ALPHA-DRIVEN XRPUSDC FUTURES (mainnet) — ESTADO DE TRADES ===")
    L.append(f"Generado: {now_local()} local  |  {now_utc()} UTC")
    L.append("")
    L.append("--- RESUMEN DE TRADES (fills reales, simulated=false) ---")
    L.append(f"  Trades (fills):       {n_trades}")
    L.append(f"  Comprado:             {buy_qty:.1f} XRP en {len([r for r in fills if r['side']=='BUY'])} fills @ medio {avg_buy:.5f}")
    L.append(f"  Vendido:              {sell_qty:.1f} XRP en {len([r for r in fills if r['side']=='SELL'])} fills @ medio {avg_sell:.5f}")
    L.append(f"  Inventario actual:    {inv:.2f} XRP  ({'posicion cerrada' if abs(inv) < 1e-9 else 'posicion ABIERTA'})")
    L.append("")
    # ---- PnL BINANCE (fuente de verdad, si el fetch corrio hace poco) ----
    bnl = None
    bnl_path = os.path.join(ROOT, "reports", "binance_pnl.json")
    if os.path.exists(bnl_path):
        try:
            with open(bnl_path, "r", encoding="utf-8") as f:
                bnl = json.load(f)
        except (json.JSONDecodeError, OSError):
            bnl = None
    bnl_fresh = False
    if bnl and bnl.get("fetched_at_utc"):
        try:
            ft = datetime.fromisoformat(bnl["fetched_at_utc"].replace("Z", "+00:00"))
            bnl_fresh = (datetime.now(timezone.utc) - ft).total_seconds() <= 600
        except ValueError:
            bnl_fresh = False

    L.append("--- PNL BINANCE (fuente de verdad, API directa §18) ---")
    if bnl and bnl_fresh:
        L.append(f"  Trades segun exchange: {bnl['n_trades']}   (fetch {bnl['fetched_at_utc'][11:16]}Z)")
        L.append(f"  Realized PnL:        {bnl['realized_pnl']:+.6f}")
        L.append(f"  Commission:          {bnl['commission']:+.6f}  ({', '.join(bnl['commission_assets']) if bnl['commission_assets'] else 'n/a'})")
        L.append(f"  Funding fee:         {bnl['funding_fee']:+.6f}")
        L.append(f"  NET PnL (BINANCE):   {bnl['net_pnl']:+.6f}")
        for d, v in sorted(bnl.get("per_day", {}).items()):
            L.append(f"    {d}: trades={v['n']}  realized={v['realized']:+.6f}  comm={v['commission']:+.6f}  funding={v['funding']:+.6f}")
    else:
        L.append("  (no disponible — correr reports/fetch_binance_pnl.py)")
    L.append("")
    L.append("--- PNL ESTIMADO (logs locales — puede estar incompleto) ---")
    L.append(f"  Gross PnL:            {gross_pnl:+.4f}")
    L.append(f"  Fees estimadas:       -{fees:.4f}  (fee maker {fee_rate:.4%})")
    L.append(f"  Net PnL (estimado):   {net_pnl:+.4f}")
    if abs(unreal) > 1e-9:
        L.append(f"  PnL no realizado:     {unreal:+.4f} (marcado a mercado, inventario abierto)")
    L.append("")
    L.append("--- ULTIMOS TRADES BINANCE ---")
    if bnl and bnl_fresh and bnl.get("trades_raw"):
        for t in bnl["trades_raw"][-5:]:
            ts = datetime.fromtimestamp(t["time"] / 1000).strftime("%d/%m %H:%M:%S")
            rp = float(t.get("realizedPnl") or 0)
            L.append(f"  {ts}  {t['side']:<4} {t['qty']} @ {t['price']}  (rp={rp:+.6f})")
    else:
        L.append("  (sin datos de Binance)")
    L.append("")
    L.append("--- ULTIMOS FILLS (log local) ---")
    if fills:
        for r in fills[-5:]:
            L.append(f"  {local_ts(r['ts'])}  {r['side']:<4} {r['fill_qty']:.1f} @ {r['fill_price']:.5f}")
    else:
        L.append("  (sin fills reales aun)")
    L.append("")
    L.append("--- ORDENES HOY ---")
    if orders_today:
        ev_str = " | ".join(f"{k}: {v}" for k, v in sorted(ev_counts.items()))
        L.append(f"  Eventos: {len(orders_today)}   ({ev_str})")
    else:
        L.append("  (sin eventos de ordenes hoy)")

    rej = [o for o in orders_today if o.get("event") == "rejected"]
    L.append("")
    L.append("--- ULTIMOS RECHAZOS (motivo) ---")
    if rej:
        for o in rej[-3:]:
            reason = str(o.get("reason", "?")).replace("\u00a7", "§")
            L.append(f"  {local_ts(o.get('ts', 0))}  {o['side']:<4} {o.get('qty')} @ {o.get('price')}  ->  {reason[:80]}")
    else:
        L.append("  (sin rechazos hoy)")
    L.append("")
    L.append("--- ESTADO DEL BOT ---")
    L.append(f"  Estado:               {status}")
    if last_dec and freshness is not None:
        L.append(f"  Ultima decision:      {last_dec['timestamp']}Z  (hace {int(freshness // 60)}m {int(freshness % 60)}s)")
        L.append(f"  Regime:               {last_dec.get('regime')}  |  mid: {last_dec.get('mid_price')}  |  spread: {last_dec.get('spread')}")
        L.append(f"  Quotes:               bid {last_dec.get('bid_price')} ({last_dec.get('bid_size')}) / ask {last_dec.get('ask_price')} ({last_dec.get('ask_size')})")
        L.append(f"  Reason:               {last_dec.get('reason')}")
        L.append(f"  Alpha: {last_dec.get('alpha')} | expected_pnl: {last_dec.get('expected_pnl')} | risk_score: {last_dec.get('risk_score')} | momentum: {last_dec.get('momentum_active')}")
    L.append("")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"OK: {OUT}")


if __name__ == "__main__":
    main()
