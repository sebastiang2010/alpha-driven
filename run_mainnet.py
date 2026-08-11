#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_mainnet.py — Corrida MAINNET (Nivel 1+) sobre XRPUSDC.

§0.1/§0.2: mainnet SOLO con autorización humana EXPLÍCITA. Este script exige
dos señales independientes antes de tocar la cuenta real:
  1. Configuración: config.REAL=True Y config.EXPOSURE_LEVEL>=1 Y
     config.BUDGETS_CONFIRMED=True (§0.4: no operar con presupuestos
     "pendiente de confirmación").
  2. Confirmación interactiva del humano: escribir CONFIRMAR (primera orden
     mainnet §0.2). `--yes` la salta SOLO para supervisión directa/CI.

Flujo:
  1. Chequeos de configuración (§0.4/§0.1).
  2. Pre-flight con cliente mainnet ya inicializado:
       - reconciliar posición: se TOLERA una pre-existente dentro de los
         presupuestos (§0.4) — notional a mark price <= MAX_POSITION_NOTIONAL_USDC
         y |amt| <= 2*BASE_ORDER_SIZE_XRP — continuando como round trip
         (inventario inicial); FUERA de esos límites = posición no
         reconciliable §0.2 → detenerse y preguntar,
       - validar PERCENT_PRICE y requiredMarginPercent del exchangeInfo real
         (api.get_symbol — función real existente, §1),
       - verificar precio de marca disponible (api.get_mark_price),
       - set_leverage(MAX_LEVERAGE_USED).
     Nota de adaptación (§0.2): NO existe get_funding_rate en
     API_binance_futuros.py y no se escribe implementación paralela; el
     chequeo de funding en vivo se omite y el modelo usa FUNDING_RATE_PER_8H
     de config (misma fuente que alpha_model).
  3. run(max_cycles) — el run() re-inicializa cliente (idempotente) y ejecuta
     el ciclo de cotización real.
  4. Resumen con quality gate adaptado a mainnet: sin kill switch, sin errores
     del Risk Engine, decisions reason==ok con sizes>0, y SIN órdenes
     simuladas (dry-run) en el journal.

Exit codes: 0 = gate OK, 1 = gate falló, 2 = sin data de mercado,
            3 = pre-flight falló o autorización denegada,
            4 = otra instancia corriendo (lock single-instance §12).

Uso:
    python run_mainnet.py [--cycles N] [--yes]
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import pathlib
import sys
import threading
import time

import API_binance_futuros as api  # §1: única interfaz con Binance (funciones reales)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategy.config import (  # noqa: E402
    BASE_ORDER_SIZE_XRP,
    BUDGETS_CONFIRMED,
    CYCLE_INTERVAL_SEC,
    DIVERGENCE_MIN_DECISIONS,
    EXPOSURE_LEVEL,
    INSTANCE_LOCK_PATH,
    LOG_DECISIONS,
    LOG_DIR,
    LOG_FILLS,
    LOG_ORDERS,
    LOG_PNL,
    MAX_LEVERAGE_USED,
    MAX_POSITION_NOTIONAL_USDC,
    MONITOR_INTERVAL_SEC,
    REAL,
    STALL_STOP_SEC,
    STALL_WARN_SEC,
    SYMBOL,
)
from strategy.market_maker import MarketMaker  # noqa: E402

WATCHDOG_FLOOR_SECONDS = 125.0  # piso del techo duro (§13: SIEMPRE activo)


def _watchdog_seconds(max_cycles: int) -> float:
    """Techo duro de la corrida (§13), derivado de ``--cycles``.

    Un ciclo dura ~5-6 s: ``max_cycles * 6 + 30`` cubre el último ciclo y el
    resumen; 125 s de piso de seguridad para corridas cortas. El watchdog
    sigue siendo techo duro SIEMPRE activo (§13) — no es una estimación.
    """
    return max(WATCHDOG_FLOOR_SECONDS, float(max_cycles) * 6.0 + 30.0)


DEFAULT_CYCLES = 20

log = logging.getLogger("run_mainnet")


def _setup_logging(log_path: pathlib.Path) -> None:
    """Root logger a stdout + archivo; neutraliza handlers duplicados."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]
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

# ── Pre-flight (§0.2/§0.4/§3) ──────────────────────────────────────────────

def _lock_is_stale(pid: int) -> bool:
    """True si el PID del lock ya no existe en el sistema."""
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        return True


def _acquire_instance_lock() -> bool:
    """Lock single-instance (§12): O_EXCL + PID. Aborta (False) si otra
    instancia está corriendo (clase de bug de la corrida 2026-08-11: 2
    procesos compartiendo XRPUSDC -> -2011 y orden huérfana)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        fd = os.open(INSTANCE_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid = int(INSTANCE_LOCK_PATH.read_text().strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if pid and _lock_is_stale(pid):
            log.warning("Lock previo con PID muerto (%d): lo reemplazo.", pid)
            try:
                INSTANCE_LOCK_PATH.unlink()
            except OSError:
                return False
            return _acquire_instance_lock()
        log.error("Otra instancia de run_mainnet corriendo (PID %s). Abortando (exit 4).",
                  pid if pid else "desconocido")
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    log.info("Lock single-instance adquirido: %s (PID %d)", INSTANCE_LOCK_PATH, os.getpid())
    return True


def _release_instance_lock() -> None:
    """Libera el lock single-instance (solo si el PID es el nuestro)."""
    try:
        pid = int(INSTANCE_LOCK_PATH.read_text().strip() or "0")
    except (OSError, ValueError):
        pid = 0
    if pid == os.getpid():
        try:
            INSTANCE_LOCK_PATH.unlink()
            log.info("Lock single-instance liberado.")
        except OSError as e:
            log.warning("No pude liberar el lock: %s", e)


def _start_monitor(mm: MarketMaker, start_iso: str, start_wall: float) -> threading.Thread:
    """Hilo de monitor ligero (§11): heartbeat + stall/divergencia.

    - Stall: el journal de decisiones no avanza mientras el WS está conectado
      (data fresca pero el loop no avanza → colgamiento real). Warning a
      STALL_WARN_SEC; stop() conservador (cierra órdenes) a STALL_STOP_SEC.
      Si el WS está caído el bot espera reconexión legítimamente: no se
      detiene (lo cubre el techo duro del watchdog §13).
    - Divergencia decisión/ejecución: >= DIVERGENCE_MIN_DECISIONS con
      reason==ok en la ventana y 0 eventos de órdenes → el execution_engine
      podría estar colgado tras los quotes.
    """
    def _loop() -> None:
        prev_divergent = False
        last_stall_warn = 0.0
        while not mm._stop.is_set():
            time.sleep(MONITOR_INTERVAL_SEC)
            try:
                dec = _read_lines(LOG_DECISIONS / "agent_decisions.jsonl")
                new_dec = [ev for ev in dec if str(ev.get("timestamp", "")) >= start_iso]
                orders = _read_lines(LOG_ORDERS / f"orders_{datetime.datetime.now():%Y%m%d}.jsonl")
                new_orders = [ev for ev in orders if float(ev.get("ts", 0.0) or 0.0) >= start_wall]
                fills = _read_lines(LOG_FILLS / f"fills_{datetime.datetime.now():%Y%m%d}.jsonl")
                new_fills = [ev for ev in fills if float(ev.get("ts", 0.0) or 0.0) >= start_wall]

                ok_dec = [ev for ev in new_dec if ev.get("reason") == "ok"]
                order_events = [ev for ev in new_orders
                                if str(ev.get("event") or "") in
                                ("placed", "canceled", "rejected", "error", "sweep_canceled")]

                # Stall: edad del último evento de decisión.
                stall_age = None
                if new_dec:
                    last = max(str(ev.get("timestamp", "")) for ev in new_dec)
                    try:
                        last_dt = datetime.datetime.fromisoformat(last)
                        stall_age = (datetime.datetime.utcnow() - last_dt).total_seconds()
                    except ValueError:
                        pass

                log.info("[monitor] decisions=%d ok=%d orders=%d fills=%d ws=%s stall_age=%s",
                         len(new_dec), len(ok_dec), len(order_events), len(new_fills),
                         mm.ws_connected,
                         f"{stall_age:.0f}s" if stall_age is not None else "n/a")

                if (mm.ws_connected and stall_age is not None
                        and stall_age > STALL_STOP_SEC):
                    log.critical("[monitor] STALL: %d s sin decisiones con WS "
                                 "conectado. Stop conservador (cierra órdenes).",
                                 int(stall_age))
                    mm.stop()
                    break
                if (mm.ws_connected and stall_age is not None
                        and stall_age > STALL_WARN_SEC
                        and time.time() - last_stall_warn > 60.0):
                    log.warning("[monitor] stall sospechoso: %d s sin decisiones "
                                "con WS conectado.", int(stall_age))
                    last_stall_warn = time.time()

                # Divergencia: muchas decisiones ok sin ningún evento de órdenes.
                cutoff = time.time() - 60.0
                ok_in_window = sum(1 for ev in ok_dec if _iso_age_sec(ev.get("timestamp")) <= 60.0)
                orders_in_window = sum(1 for ev in order_events if ev.get("ts", 0.0) >= cutoff)
                fills_in_window = sum(1 for ev in new_fills if ev.get("ts", 0.0) >= cutoff)
                divergent = (ok_in_window >= DIVERGENCE_MIN_DECISIONS
                             and orders_in_window == 0 and fills_in_window == 0)
                if divergent and not prev_divergent:
                    log.warning("[monitor] DIVERGENCIA: %d decisiones reason==ok en 60 s "
                                "con 0 eventos de órdenes/fills — posible colgamiento "
                                "del execution_engine.", ok_in_window)
                prev_divergent = divergent
            except Exception as e:  # nunca matar el monitor ni la corrida
                log.exception("[monitor] error interno: %s", e)

    t = threading.Thread(target=_loop, name="monitor", daemon=True)
    t.start()
    return t


def _iso_age_sec(iso: str | None) -> float:
    """Edad en segundos de un timestamp ISO (utcnow), o +inf si no parsea."""
    try:
        dt = datetime.datetime.fromisoformat(str(iso))
        return (datetime.datetime.utcnow() - dt).total_seconds()
    except ValueError:
        return float("inf")


def _preflight(mm: MarketMaker) -> list[str]:
    """Chequeos previos a la primera orden mainnet. Devuelve lista de errores."""
    errors: list[str] = []

    # 1. Cliente mainnet + filtros reales (§3: nunca asumir specs).
    if not mm.exec.init_client(real=True):
        errors.append("init_client(real=True) falló (cliente mainnet no disponible)")
        return errors
    mm.exec.init_symbol_info()

    # 1.5 Órdenes huérfanas del bot (prefijo MM-) fuera de tracking local
    #     (§0.6): clase de bug de la corrida 2026-08-11 (orden de un proceso
    #     anterior que quedó resting). Cancelarlas antes de operar evita
    #     -2011 y que el bot conviva con órdenes que no controla.
    try:
        n = mm.exec.sweep_orphan_orders()
        if n:
            log.warning("Pre-flight: %d órdenes huérfanas canceladas (sweep §0.6)", n)
    except Exception as e:
        errors.append(f"sweep_orphan_orders falló: {e}")

    # 2. Precio de marca disponible (los quotes se anclan al mid real).
    #    Se obtiene ANTES del chequeo de posición: hace falta para valuar el
    #    notional de una posición pre-existente (§0.2/§0.4).
    mark = api.get_mark_price(SYMBOL)
    if not mark or mark <= 0:
        errors.append(f"api.get_mark_price({SYMBOL}) no disponible — no se puede operar")
        mark = None
    else:
        log.info("Pre-flight: mark price %s = %.6f", SYMBOL, mark)

    # 3. Posición reconciliada (§0.2: posición no reconciliable = detenerse).
    #    Espíritu §0.2: una posición dentro de los presupuestos de riesgo (§0.4)
    #    es RECONCILIABLE — exigir posición == 0 impediría la reanudación tras un
    #    fill real (p.ej. la cuenta mainnet con 4.9 XRP abiertos). Se continúa
    #    como round trip (inventario inicial) si el notional a mark price <=
    #    MAX_POSITION_NOTIONAL_USDC y |amt| <= 2 * BASE_ORDER_SIZE_XRP.
    #    FUERA de esos límites (o sin mark price para valuar) = posición no
    #    reconciliable §0.2 → abortar y preguntar al humano.
    pos = mm.exec.reconcile_position()
    if pos is None:
        errors.append("reconcile_position() devolvió None (no se puede reconciliar la posición)")
    else:
        amt = float(pos.get("positionAmt", 0.0) or 0.0)
        if abs(amt) <= 1e-9:
            log.info("Pre-flight: posición de %s reconciliada en 0.", SYMBOL)
        elif mark is not None and (
            abs(amt) * mark <= MAX_POSITION_NOTIONAL_USDC
            and abs(amt) <= 2.0 * BASE_ORDER_SIZE_XRP
        ):
            log.warning(
                "Pre-flight: posición pre-existente de %.4f %s (~$%.2f) dentro de "
                "límites de riesgo (notional<=$%.2f USDC, |amt|<=%.2f %s); "
                "continuando como round trip (inventario inicial).",
                amt, SYMBOL, abs(amt) * mark,
                MAX_POSITION_NOTIONAL_USDC, 2.0 * BASE_ORDER_SIZE_XRP, SYMBOL,
            )
        elif mark is None:
            errors.append(
                f"posición abierta {amt} {SYMBOL} sin mark price para valuar — "
                "posición no reconciliable (§0.2): detenerse y preguntar"
            )
        else:
            errors.append(
                f"posición abierta {amt} {SYMBOL} fuera de límites reconciliables "
                f"(|amt|={abs(amt):.4f} > {2.0 * BASE_ORDER_SIZE_XRP:.2f} XRP o "
                f"notional ${abs(amt) * mark:.2f} > ${MAX_POSITION_NOTIONAL_USDC:.2f} USDC) — "
                "posición no reconciliable (§0.2): detenerse y preguntar"
            )

    # 4. exchangeInfo real: PERCENT_PRICE + leverage máximo real.
    info = api.get_symbol(SYMBOL)
    if info is None:
        errors.append(f"api.get_symbol({SYMBOL}) no encontró el símbolo en exchange_info")
    else:
        req_margin = info.get("requiredMarginPercent")
        if req_margin:
            max_real_lev = int(1.0 / (float(req_margin) / 100.0))
            log.info("Pre-flight: requiredMarginPercent=%s%% → leverage máx real=%dx",
                     req_margin, max_real_lev)
            if MAX_LEVERAGE_USED > max_real_lev:
                errors.append(
                    f"MAX_LEVERAGE_USED={MAX_LEVERAGE_USED} excede el máximo real "
                    f"{max_real_lev}x (requiredMarginPercent={req_margin}%)"
                )
        else:
            log.warning("Pre-flight: exchangeInfo sin requiredMarginPercent; sin validar.")
        filters = {f.get("filterType"): f for f in info.get("filters", []) if f.get("filterType")}
        pp = filters.get("PERCENT_PRICE")
        if pp:
            try:
                mult_up = float(pp.get("multiplierUp", 1.0))
                mult_down = float(pp.get("multiplierDown", 1.0))
                log.info("Pre-flight: PERCENT_PRICE up=%.4f down=%.4f", mult_up, mult_down)
                if (mult_up - 1.0) < 0.001 or (1.0 - mult_down) < 0.001:
                    errors.append(
                        f"PERCENT_PRICE demasiado estrecho (up={mult_up}, down={mult_down}): "
                        "el spread del bot no cabe en el rango permitido"
                    )
            except (TypeError, ValueError) as e:
                errors.append(f"PERCENT_PRICE mal formado: {pp} ({e})")
        else:
            log.warning("Pre-flight: símbolo sin filtro PERCENT_PRICE (interesante).")

    # 4. Precio de marca disponible (los quotes se anclan al mid real).
    mark = api.get_mark_price(SYMBOL)
    if not mark or mark <= 0:
        errors.append(f"api.get_mark_price({SYMBOL}) no disponible — no se puede operar")
    else:
        log.info("Pre-flight: mark price %s = %.6f", SYMBOL, mark)

    # 5. Funding: adaptación §0.2 — no existe get_funding_rate; se omite el
    #    chequeo en vivo y el modelo usa FUNDING_RATE_PER_8H de config.
    log.warning("Pre-flight: sin helper get_funding_rate en la API (no se implementa "
                "en paralelo §0.2). Funding en vivo no verificado; se usa el de config.")

    # 6. Leverage aplicado explícitamente (idempotente con el run()).
    mm.exec.set_leverage(MAX_LEVERAGE_USED)

    return errors

# ── Resumen + quality gate (mainnet) ───────────────────────────────────────
def _summary(main_logger, start_iso: str, start_wall: float, mm: MarketMaker) -> int:
    """Verifica el quality gate de mainnet y devuelve el exit code (0/1/2)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    journal = _read_lines(LOG_DECISIONS / "agent_decisions.jsonl")
    orders = _read_lines(LOG_ORDERS / f"orders_{now:%Y%m%d}.jsonl")
    fills = _read_lines(LOG_FILLS / f"fills_{now:%Y%m%d}.jsonl")
    kill_lines = _read_lines(LOG_PNL / "kill_switch.jsonl")

    new_dec = [ev for ev in journal if str(ev.get("timestamp", "")) >= start_iso]
    new_orders = [ev for ev in orders if float(ev.get("ts", 0.0) or 0.0) >= start_wall]
    new_fills = [ev for ev in fills if float(ev.get("ts", 0.0) or 0.0) >= start_wall]
    new_kill = [ev for ev in kill_lines if float(ev.get("ts", 0.0) or 0.0) >= start_wall]

    elapsed = time.time() - start_wall
    main_logger.info("=" * 70)
    main_logger.info("RESUMEN MAINNET  [%s]  (%.1f s)", SYMBOL, elapsed)
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
        main_logger.info("  mid min/max     : %.5f / %.5f", min(mids), max(mids))
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

    # ── Quality gate (mainnet) ────────────────────────────────────────────
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
    # En mainnet NINGUNA orden puede ser simulada (dry-run). El journal de
    # órdenes en dry-run marca el evento con nota de simulación; buscamos esa
    # señal en cualquier campo del evento.
    simul = [ev for ev in new_orders if "simul" in json.dumps(ev, default=str).lower()]
    if simul:
        gate_fail.append(f"{len(simul)} órdenes marcadas como simuladas en una corrida mainnet")

    if gate_fail:
        for reason in gate_fail:
            main_logger.error("GATE FAIL: %s", reason)
        main_logger.error("Exit code 1.")
        return 1

    main_logger.info("GATE OK: sizes > 0, reason==ok, sin kill switch, sin errores, sin simuladas.")
    main_logger.info("=" * 70)
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corrida mainnet Nivel 1+ (XRPUSDC)")
    parser.add_argument("--cycles", type=int, default=DEFAULT_CYCLES,
                        help=f"máx ciclos (~{CYCLE_INTERVAL_SEC}s c/u); default {DEFAULT_CYCLES}")
    parser.add_argument("--yes", action="store_true",
                        help="saltar la confirmación interactiva (SOLO supervisión directa)")
    args = parser.parse_args(argv)

    # ── Chequeos de configuración (§0.4/§0.1) ─────────────────────────────
    if not REAL:
        log.error("config.REAL=False: no es una corrida mainnet. Abortando.")
        return 3
    if EXPOSURE_LEVEL < 1:
        log.error("config.EXPOSURE_LEVEL=%s < 1: mainnet solo en Nivel 1+. Abortando.", EXPOSURE_LEVEL)
        return 3
    if not BUDGETS_CONFIRMED:
        log.error("config.BUDGETS_CONFIRMED=False: presupuestos §0.4 sin confirmar. "
                  "No se opera con fondos reales. Abortando.")
        return 3

    # ── Instancia única (§12) ─────────────────────────────────────────────
    if not _acquire_instance_lock():
        return 4

    # Log por corrida (§13): nombre único con timestamp; no se mezclan
    # corridas en un mismo archivo (clase de bug 2026-08-11).
    log_path = LOG_DIR / f"run_mainnet_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
    _setup_logging(log_path)
    start_iso = datetime.datetime.utcnow().isoformat(timespec="seconds")
    start_wall = time.time()

    log.info("Inicio mainnet: cycles=%d, exposure_level=%s, real=%s, leverage=%dx",
             args.cycles, EXPOSURE_LEVEL, REAL, MAX_LEVERAGE_USED)
    log.info("Log a archivo: %s", log_path)

    # ── Confirmación humana (primera orden mainnet §0.2) ──────────────────
    if not args.yes:
        print("=" * 70)
        print("PRIMERA ORDEN MAINNET — requiere autorización humana explícita (§0.2).")
        print(f"  Símbolo : {SYMBOL}")
        print(f"  Leverage: {MAX_LEVERAGE_USED}x   Exposure: nivel {EXPOSURE_LEVEL}")
        print(f"  Presupuestos §0.4 confirmados: sí")
        r = input('Escribí CONFIRMAR para autorizar: ').strip()
        if r != "CONFIRMAR":
            log.warning("Autorización denegada. Abortando sin tocar mainnet.")
            return 3
        log.info("Autorización humana CONFIRMADA.")
    else:
        log.warning("--yes: confirmación humana saltada (solo bajo supervisión directa).")

    mm = MarketMaker(dry_run=False)

    # ── Pre-flight ────────────────────────────────────────────────────────
    preflight_errors = _preflight(mm)
    if preflight_errors:
        for err in preflight_errors:
            log.error("PRE-FLIGHT FAIL: %s", err)
        log.error("Pre-flight con errores. Abortando (exit 3).")
        return 3
    log.info("Pre-flight OK. Iniciando ciclo de cotización mainnet...")

    # Techo duro §13 derivado de --cycles (ver _watchdog_seconds).
    watchdog_seconds = _watchdog_seconds(args.cycles)

    def _watchdog():
        time.sleep(watchdog_seconds)
        if not mm._stop.is_set():
            log.warning("Watchdog: %.0f s excedidos, forzando stop (idempotente).",
                        watchdog_seconds)
            mm.stop()

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()

    # Monitor ligero (§11): heartbeat + stall/divergencia (no reemplaza el
    # techo duro del watchdog §13, lo complementa para colgamientos cortos).
    monitor = _start_monitor(mm, start_iso, start_wall)

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
        monitor.join(timeout=5)
        _release_instance_lock()
        log.info("Fin mainnet. Exit code=%d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
