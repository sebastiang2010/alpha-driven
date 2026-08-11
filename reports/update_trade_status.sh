#!/usr/bin/env bash
# Actualiza reports/trade_status.txt cada 5 minutos (300 s).
# Lanzamiento:  powershell -Command "Start-Process -FilePath 'C:\Program Files\Git\bin\bash.exe' -ArgumentList 'reports/update_trade_status.sh' -WindowStyle Hidden"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
while true; do
  python reports/fetch_binance_pnl.py >> logs/trade_status_updater.log 2>&1
  python reports/trade_status.py >> logs/trade_status_updater.log 2>&1
  sleep 300
done
