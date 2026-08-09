#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_dry_run.py — Corrida integrada DRY-RUN (Nivel 0) sobre XRPUSDC.

§0.1/§21: Nivel 0 (EXPOSURE_LEVEL=0) con dry_run=True. JAMÁS se envían
órdenes reales ni se toca mainnet. El cliente se inicializa SOLO en testnet
(init_client(real=False) interno de MarketMaker.run), y las órdenes pasan
por place_maker_order -> short-circuit "SIMULATED (dry-run)".

Qué hace:
  1. Instancia MarketMaker(dry_run=True).
  2. Llama a run(max_cycles=N) — el propio run() arma los 3 websockets
     (bookTicker/depth/trades) en TESTNET (real=config.REAL=False), inicializa
     cliente testnet, specs del símbolo, y ejecuta el ciclo de cotización.
  3. Watchdog de seguridad: si el loop se excede (125 s), fuerza mm.stop()
     (idempotente: cierra WS + cancela simuladas).
  4. Resumen post-run con verificación del quality gate:
       - decisions nuevas con bid_size/ask_size > 0 (regresión del bug
         expected_net_pnl_non_positive con sizes 0.0),
       - reason == "ok",
       - kill switch NO disparado (por diseño solo si WS cae > WS_STALE_SEC),
       - error_count del Risk Engine == 0,
       - órdenes SIMULATED (no reales), fills simulados.
  5. Exit codes: 0 = gate OK, 2 = sin data de mercado (degradación offline),
     1 = gate falló.

Uso:
    python run_dry_run.py [--cycles N]        # N=20 por defecto (~100 s)
    python run_dry_run.py --cycles 10         # ~50 s

Sin autorización humana (§0.1) este script nunca inicializa mainnet.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import pathlib
import sys
import threading
import time

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategy.config import (  # noqa: E402
    CYCLE_INTERVAL_SEC,
    EXPOSURE_LEVEL,
    LOG_DECISIONS,
    LOG_FILLS,
    LOG_DIR,
    LOG_ORDERS,
    LOG_PNL,
    SYMBOL,
)
from strategy.market_maker import MarketMaker  # noqa: E402

WATCHDOG_SECONDS = 125.0  # techo duro de la corrida (60-120 s objetivo)
DEFAULT_CYCLES = 20       # 20 ciclos * CYCLE_INTERVAL_SEC(~5 s) ≈ 100 s

log = logging.getLogger("run_dry_run")


def _setup_logging(log_path: pathlib.Path) -> None:
    """Root logger a stdout + archivo; neutraliza handlers duplicados de los
    módulos (que registran su propio StreamHandler al importar)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]
    # Quitar StreamHandlers de los loggers de módulos para evitar líneas duplicadas
    # en stdout (sus records propagan al root, que ya tiene stdout + archivo).
    for name in list(logging.Logger.manager.loggerDict):
        lg = logging.getLogger(name)
        lg.handlers = [h for h in lg.handlers if not isinstance(h, logging.StreamHandler)]
        lg.propagate = True


def _read_lines(path: pathlib.Path) -> list[dict]:
    """Lee un JSONL existente; devuelve [] si no existe o está corrupto."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log.warning("no se pudo leer %s: %s", path, e)
    return out


def _summary(main_logger, start_iso: str, start_wall: float, mm: MarketMaker) -> int:
    """Verifica el quality gate y devuelve el exit code (0/1/2)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    journal = _read_lines(LOG_DECISIONS / "agent_decisions.jsonl")
    orders = _read_lines(LOG_ORDERS / f"orders_{now:%Y%m%d}.jsonl")
    fills = _read_lines(LOG_FILLS / f"fills_{now:%Y%m%d}.jsonl")
    kill_lines = _read_lines(LOG_PNL / "kill_switch.jsonl")

    # Decisions usan timestamp ISO; orders/fills/kill_switch usan ts epoch float.
    new_dec = [ev for ev in journal if str(ev.get("timestamp", "")) >= start_iso]
    new_orders = [ev for ev in orders if float(ev.get("ts", 0.0) or 0.0) >= start_wall]
    new_fills = [ev for ev in fills if float(ev.get("ts", 0.0) or 0.0) >= start_wall]
    new_kill = [ev for ev in kill_lines if float(ev.get("ts", 0.0) or 0.0) >= start_wall]

    elapsed = time.time() - start_wall
    main_logger.info("=" * 70)
    main_logger.info("RESUMEN DRY-RUN  [%s]  (%.1f s)", SYMBOL, elapsed)
    main_logger.info("  EXPOSURE_LEVEL=%s  dry_run=%s  real=%s  ws_connected=%s",
                     EXPOSURE_LEVEL, mm.dry_run, mm.exec.real, mm.ws_connected)
    main_logger.info("  decisions nuevas : %d", len(new_dec))
    main_logger.info("  orders nuevas    : %d   fills nuevos: %d   kill_switch: %d",
                     len(new_orders), len(new_fills), len(new_kill))

    bid_sizes = [float(ev.get("bid_size") or 0.0) for ev in new_dec]
    ask_sizes = [float(ev.get("ask_size") or 0.0) for ev in new_dec]
    mids = [float(ev.get("mid_price") or 0.0) for ev in new_dec]
    ok_reasons = [ev for ev in new_dec if ev.get("reason") == "ok"]
    non_ok = {str(ev.get("reason")) for ev in new_dec if ev.get("reason") != "ok"}
    statuses = {str(ev.get("event") or ev.get("status") or "?") for ev in new_orders}

    if new_dec:
        main_logger.info("  mid min/max     : %.5f / %.5f",
                         min(mids), max(mids))
        main_logger.info("  bid_size max    : %.4f   (objetivo > 0)", max(bid_sizes or [0.0]))
        main_logger.info("  ask_size max    : %.4f   (objetivo > 0)", max(ask_sizes or [0.0]))
        main_logger.info("  reason==ok      : %d / %d", len(ok_reasons), len(new_dec))
        if non_ok:
            main_logger.info("  reasons != ok   : %s", sorted(non_ok))
    else:
        main_logger.warning("  SIN decisions: no llegó data de mercado (degradación offline).")

    if new_orders:
        main_logger.info("  statuses órdenes: %s", sorted(statuses))
    if new_kill:
        for ev in new_kill[-3:]:
            main_logger.warning("  kill_switch line: %s", json.dumps(ev, default=str)[:200])

    # ── Quality gate ──────────────────────────────────────────────────────
    gate_fail = []
    if not new_dec:
        main_logger.warning("GATE: SIN data de mercado -> degradación offline (exit 2).")
        return 2
    if max(bid_sizes or [0.0]) <= 0 or max(ask_sizes or [0.0]) <= 0:
        gate_fail.append("bid_size/ask_size deben ser > 0 (bug sizes=0.0)")
    if not ok_reasons:
        gate_fail.append("ninguna decision con reason==ok")
    if new_kill:
        gate_fail.append("kill switch disparado durante la corrida (revisar motivo)")
    risk_errors = int(mm.risk.get_state().get("error_count", 0))
    if risk_errors > 0:
        gate_fail.append(f"error_count del Risk Engine = {risk_errors}")
    # Eventos de órdenes esperados en dry-run (execution_engine._log_order_event):
    # placed / canceled / sync_removed / rejected / error. Cualquier otro es raro.
    expected_events = {"placed", "canceled", "sync_removed", "rejected", "error"}
    unexpected_events = sorted(statuses - expected_events)
    if unexpected_events:
        gate_fail.append(f"eventos de órdenes inesperados: {unexpected_events}")

    if gate_fail:
        for reason in gate_fail:
            main_logger.error("GATE FAIL: %s", reason)
        main_logger.error("Exit code 1.")
        return 1

    main_logger.info("GATE OK: sizes > 0, reason==ok, sin kill switch, sin errores.")
    main_logger.info("=" * 70)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corrida dry-run Nivel 0 (testnet/simulación)")
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES,
                        help=f"máx ciclos (~{CYCLE_INTERVAL_SEC}s c/u); default {DEFAULT_CYCLES}")
    args = parser.parse_args(argv)

    log_path = LOG_DIR / "run_dry_run.log"
    _setup_logging(log_path)
    start_iso = datetime.datetime.utcnow().isoformat(timespec="seconds")
    start_wall = time.time()

    log.info("Inicio dry-run: cycles=%d, exposure_level=%s, real=%s",
             args.cycles, EXPOSURE_LEVEL, False)
    log.info("Log a archivo: %s", log_path)

    mm = MarketMaker(dry_run=True)  # §0.1/§21: jamás órdenes reales en Nivel 0

    def _watchdog():
        time.sleep(WATCHDOG_SECONDS)
        if not mm._stop.is_set():
            log.warning("Watchdog: %.0f s excedidos, forzando stop (idempotente).",
                        WATCHDOG_SECONDS)
            mm.stop()

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()

    exit_code = 1
    try:
        mm.run(max_cycles=args.cycles)  # incluye finally: self.stop()
        exit_code = _summary(log, start_iso, start_wall, mm)
    except KeyboardInterrupt:
        log.warning("Interrupción manual; deteniendo de forma limpia...")
        mm.stop()
        exit_code = _summary(log, start_iso, start_wall, mm)
    except Exception as e:
        log.exception("Error inesperado en la corrida: %s", e)
        mm.stop()
        exit_code = 1
    finally:
        log.info("Fin dry-run. Exit code=%d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
