#!/usr/bin/env python3
"""Descarga trades e income REALES de Binance (XRPUSDC) y calcula el PnL independiente.

Fuente de verdad (§18): userTrades + income history del exchange, NO los logs locales.
Solo lectura; nunca imprime ni persiste credenciales (§0.5). No toca el bot.
Escribe: reports/binance_pnl.json  (consumido por reports/trade_status.py)
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import API_binance_futuros as api  # noqa: E402
from binance.exceptions import BinanceAPIException  # noqa: E402

SYMBOL = "XRPUSDC"
OUT = os.path.join(ROOT, "reports", "binance_pnl.json")
# Mainnet opero por primera vez el 2026-08-10; cubrir con margen desde 2026-08-09 22:00Z
START_MS = int(datetime(2026, 8, 9, 22, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def is_err(r):
    return isinstance(r, BinanceAPIException)


def main():
    api.init_client(real=True)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    trades = api.get_my_trades(SYMBOL, limit=1000, startTime=START_MS, endTime=now_ms)
    if is_err(trades):
        print(f"ERROR userTrades: {trades}")
        sys.exit(1)
    assert isinstance(trades, list)

    income = api.get_income_history(symbol=SYMBOL, limit=1000, startTime=START_MS, endTime=now_ms)
    if is_err(income):
        print(f"ERROR income: {income}")
        sys.exit(1)
    assert isinstance(income, list)

    # ---- Calculos independientes ----
    realized = sum(float(t.get("realizedPnl") or 0) for t in trades)
    commission = sum(float(t.get("commission") or 0) for t in trades)
    comm_asset = {t.get("commissionAsset") for t in trades if float(t.get("commission") or 0) != 0}

    bot_trades = [t for t in trades if str(t.get("clientOrderId", "")).startswith("MM-")]
    other_trades = [t for t in trades if not str(t.get("clientOrderId", "")).startswith("MM-")]

    funding = sum(float(i.get("income") or 0) for i in income if i.get("incomeType") == "FUNDING_FEE")
    net = realized + commission + funding

    # Por dia (local)
    per_day = defaultdict(lambda: {"realized": 0.0, "commission": 0.0, "funding": 0.0, "n": 0})
    for t in trades:
        d = datetime.fromtimestamp(int(t["time"]) / 1000).strftime("%d/%m")
        per_day[d]["realized"] += float(t.get("realizedPnl") or 0)
        per_day[d]["commission"] += float(t.get("commission") or 0)
        per_day[d]["n"] += 1
    for i in income:
        if i.get("incomeType") == "FUNDING_FEE":
            d = datetime.fromtimestamp(int(i["time"]) / 1000).strftime("%d/%m")
            per_day[d]["funding"] += float(i.get("income") or 0)

    result = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": SYMBOL,
        "n_trades": len(trades),
        "n_trades_bot": len(bot_trades),
        "n_trades_otros": len(other_trades),
        "realized_pnl": round(realized, 6),
        "commission": round(commission, 6),
        "commission_assets": sorted(comm_asset),
        "funding_fee": round(funding, 6),
        "net_pnl": round(net, 6),
        "first_trade_time": min((int(t["time"]) for t in trades), default=None),
        "last_trade_time": max((int(t["time"]) for t in trades), default=None),
        "per_day": {d: {k: round(v, 6) for k, v in vals.items()} for d, vals in sorted(per_day.items())},
        "trades_raw": [
            {
                "orderId": t.get("orderId"),
                "clientOrderId": t.get("clientOrderId"),
                "time": t.get("time"),
                "side": "BUY" if t.get("buyer") else "SELL",
                "price": t.get("price"),
                "qty": t.get("qty"),
                "realizedPnl": t.get("realizedPnl"),
                "commission": t.get("commission"),
                "commissionAsset": t.get("commissionAsset"),
                "maker": t.get("maker"),
            }
            for t in trades
        ],
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # Resumen en consola (sin credenciales)
    print(f"=== PnL BINANCE (independiente, {SYMBOL}) ===")
    print(f"  Trades totales:    {len(trades)}  (bot MM-: {len(bot_trades)}, otros: {len(other_trades)})")
    print(f"  Realized PnL:      {realized:+.6f}")
    print(f"  Commission:        {commission:+.6f}  ({', '.join(sorted(comm_asset)) if comm_asset else 'n/a'})")
    print(f"  Funding fee:       {funding:+.6f}")
    print(f"  NET PnL:           {net:+.6f}")
    print("  Por dia:")
    for d, v in sorted(per_day.items()):
        print(f"    {d}: trades={v['n']}  realized={v['realized']:+.6f}  comm={v['commission']:+.6f}  funding={v['funding']:+.6f}")
    print(f"OK: {OUT}")


if __name__ == "__main__":
    main()
