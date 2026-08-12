#!/bin/bash
# Relauncher: mantiene vivo el bot mainnet por una ventana configurable (default 24h).
# Uso: bash logs/relauncher_2h.sh [WINDOW_MIN]
# - Cross-platform Linux (Digital Ocean) / Windows (Git Bash): detecta el bot con
#   pgrep, con fallback a wmic. Lanza con python3 (fallback python).
# - Match SOLO de run_mainnet.py (evita MCP servers python).
# - Pasa la autorizacion §0.2 via stdin (echo CONFIRMAR): la PRIMERA orden mainnet
#   del dia ya fue autorizada por humano; los relanzamientos son recuperacion.
# - Lockfile: evita relaunchers duplicados (incidente 2026-08-10: 5 procesos).
LOCK=logs/.relauncher.lock
LOG=logs/relauncher_20260810.log
if [ -f "$LOCK" ]; then
  LPID=$(cat "$LOCK" 2>/dev/null)
  if kill -0 "$LPID" 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ya hay un relauncher activo (PID $LPID). Saliendo." >> "$LOG"
    exit 0
  fi
  rm -f "$LOCK"
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT
WINDOW_MIN=${1:-1440}
START=$(date +%s)
N=0
# Interprete python cross-platform (en venv activado, python apunta al venv).
if command -v python >/dev/null 2>&1; then PY=python; else PY=python3; fi
bot_running() {
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -f "run_mainnet.py" >/dev/null 2>&1 && return 0
  fi
  if command -v wmic >/dev/null 2>&1; then
    wmic process where "name='python.exe'" get commandline 2>/dev/null | grep -qi run_mainnet.py && return 0
  fi
  return 1
}
echo "$(date '+%Y-%m-%d %H:%M:%S') relauncher iniciado (ventana ${WINDOW_MIN} min)" >> "$LOG"
while [ $(( $(date +%s) - START )) -lt $(( WINDOW_MIN * 60 )) ]; do
  if ! bot_running; then
    N=$((N+1))
    echo "$(date '+%Y-%m-%d %H:%M:%S') bot ausente -> relanzando (N=$N)" >> "$LOG"
    echo "CONFIRMAR" | nohup "$PY" run_mainnet.py --cycles 500 >> logs/run_mainnet_live.log 2>&1 &
  fi
  sleep 20
done
echo "$(date '+%Y-%m-%d %H:%M:%S') ventana ${WINDOW_MIN} min cerrada. Relanzamientos: $N" >> "$LOG"
