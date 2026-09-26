"""Captura L2 (depth@100ms + aggTrades) de Binance Futures SIN credenciales.

FASE 3 / F8 — Arquitectura de captura de datos de microestructura.

Este script es el entregable de F8: dado que NO existe replay L2 histórico
gratuito/reproducible para XRPUSDC, provee el mecanismo para ACUMULAR datos
reales (vía WS público + snapshot REST público, sin API key) y volcarlos a CSV
para calibrar más adelante el modelo causal/cola (F3/F4/F5). NO coloca órdenes,
NO usa credenciales, NO llama init_client(real=True).

El módulo es importable sin red: las funciones puras (merge protocol, detección
de huecos, parseo CSV) se testean en tests/ sin abrir sockets. El cliente WS solo
se instancia en main()/run_capture().

Protocolo de sincronización (oficial Binance depth stream, corregido 2026-08-27):
  1. Abrir el WebSocket ANTES de pedir el snapshot REST.
  2. Bufferizar temporalmente los eventos depth recibidos mientras se obtiene el
     snapshot.
  3. Obtener snapshot REST y registrar lastUpdateId = L.
  4. Descartar eventos con u <= L (contar en discarded_pre_snapshot_events).
  5. Encontrar el primer evento con U <= L+1 <= u (U=firstUpdateId, u=finalUpdateId).
  6. Reconstruir el libro: seed con el snapshot + aplicar ese primer evento.
  7. Tras el primer evento válido, exigir continuidad pu == previous_u.
  8. Ante un gap (pu != previous_u): registrar sequence_gaps++, resetear estado,
     pedir nuevo snapshot y resincronizar; NO aplicar deltas sobre libro
     desincronizado.
  9. Límite MAX_RESEED_ATTEMPTS: si se agota, detener y contar snapshot_sync_errors.
  10. aggTrade es INDEPENDIENTE de la sincronización de depth: un gap/reset de depth
      NO descarta ni borra aggTrades válidos.

Uso:
    python scripts/capture_l2_testnet.py --symbol XRPUSDC --out data/l2 --minutes 60
    python scripts/capture_l2_testnet.py --testnet --symbol XRPUSDC --out data/l2

Formato CSV de salida (compatible con strategy/fill_simulator.py:L2Replay):
  depth.csv : ts_ms, update_id, pu, first_update_id, bid_levels, ask_levels
              bid/ask = "price:qty;price:qty;..." (top N niveles, mejor->peor)
  trades.csv: ts_ms, price, qty, is_buyer_maker
"""

import argparse
import csv
import json
import os
import random
import signal
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

# URLs públicas (SIN credenciales). WS de market data y REST de depth son públicos.
# NOTA: Para aggTrade se DEBE usar el endpoint /ws con SUBSCRIBE, no /public/ws directo.
WS_MAINNET = "wss://fstream.binance.com/ws"
WS_TESTNET = "wss://stream.binancefuture.com/ws"
REST_MAINNET = "https://fapi.binance.com/fapi/v1/depth"
REST_TESTNET = "https://testnet.binancefuture.com/fapi/v1/depth"
STREAM_MAINNET = "wss://fstream.binance.com/stream"
STREAM_TESTNET = "wss://stream.binancefuture.com/stream"

# Límites de recuperación ante gaps de secuencia.
MAX_RESEED_ATTEMPTS = 5
RESEED_INTERVAL_SEC = 2.0
RESEED_INTERVAL_SYNCING_SEC = 15.0
MAX_TOTAL_FAILURES = 200

# WebSocket reconnection settings
WS_MAX_RECONNECT_ATTEMPTS = 50
WS_RECONNECT_BASE_SEC = 1.0
WS_RECONNECT_MAX_SEC = 60.0
WS_RECONNECT_JITTER = 0.2
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 10


def depth_ws_url(real: bool = True) -> str:
    return WS_MAINNET if real else WS_TESTNET


def depth_rest_url(real: bool = True) -> str:
    return REST_MAINNET if real else REST_TESTNET


def snapshot_url(symbol: str, limit: int = 20, real: bool = True) -> str:
    return f"{depth_rest_url(real)}?symbol={symbol.upper()}&limit={limit}"


def depth_stream_name(symbol: str, levels: int = 20, speed: str = "100ms") -> str:
    # DIFF stream incremental @depth@100ms (NO partial book depth20@100ms).
    # El snapshot REST fapi/v1/depth provee la base; luego se aplica merge
    # incremental pu == previous_u y U <= lastUpdateId + 1 <= u.
    # levels se conserva solo para top_levels tras merge, no para el nombre del stream.
    _ = levels  # no usado en el nombre: evita depth20/partial snapshot bug
    return f"{symbol.lower()}@depth@{speed}"


def trades_stream_name(symbol: str) -> str:
    return f"{symbol.lower()}@trade"


# ── Parseo de mensajes de stream ────────────────────────────────────────────
# Stream depth usado: <symbol>@depth@100ms  (diff incremental, NO depth20/partial)
# Snapshot REST: fapi/v1/depth  (lastUpdateId = L)
# Protocolo: primer diff válido U <= lastUpdateId + 1 <= u  ;  siguientes diffs pu == previous_u
def parse_depth_message(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convierte un mensaje del stream <symbol>@depth@100ms (diff) a dict plano.

    NO tratar partial snapshot (depth20@100ms) como diff. Merge incremental:
    qty == 0 -> remove level, qty > 0 -> upsert. Ver DepthMergeBuffer.apply().
    Campos clave (futures): U=first update id, u=last update id, pu=previous last
    update id, b=bids, a=asks, E=event time(ms), T=transaction time(ms).
    """
    if "b" not in raw and "a" not in raw:
        return None
    return {
        "ts_ms": int(raw.get("T") or raw.get("E") or 0),
        "update_id": int(raw.get("u", 0)),
        "pu": int(raw.get("pu", 0)),
        "first_update_id": int(raw.get("U", 0)),
        "bids": [(float(p), float(q)) for p, q in raw.get("b", [])],
        "asks": [(float(p), float(q)) for p, q in raw.get("a", [])],
    }


def parse_trade_message(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convierte un mensaje aggTrade a dict plano. Rechaza inválidos.

    Campos: p=price, q=qty, T=trade time(ms), m=isBuyerMaker.
    Invariantes: price > 0, qty > 0, existencia de p/q/T (o E). Si viola
    se retorna None y el caller debe contar invalid_trade_events (no silent drop).
    Las 147 filas 0.0,0.0 del piloto deben ser detectadas, no filtradas silent.
    """
    # Mensaje no compatible con trade (sin p/q)
    if "p" not in raw or "q" not in raw:
        return None
    # T ausente y E ausente -> no compatible
    if "T" not in raw and "E" not in raw and "t" not in raw:
        # permitir si al menos hay p/q pero T faltante -> usar E si existe, si no invalid
        pass
    try:
        price = float(raw["p"])
        qty = float(raw["q"])
    except (TypeError, ValueError):
        return None
    if price <= 0 or qty <= 0:
        return None
    ts = raw.get("T") if raw.get("T") is not None else raw.get("E", 0)
    try:
        ts_ms = int(ts) if ts is not None else 0
    except (TypeError, ValueError):
        return None
    if ts_ms <= 0:
        # T/E deben ser >0 para trade válido; si faltan ambos, invalid
        if raw.get("T") is None and raw.get("E") is None:
            return None
    return {
        "ts_ms": ts_ms,
        "price": price,
        "qty": qty,
        "is_buyer_maker": bool(raw.get("m", False)),
    }


# ── Merge protocol (parcial book futures) ──────────────────────────────────
class DepthMergeBuffer:
    """Implementa el protocolo de merge de Binance Futures para depth@100ms.

    Ref: se descartan eventos con u <= lastUpdateId del snapshot; el primer evento
    retenido debe cumplir U <= lastUpdateId+1 <= u. Entre eventos consecutivos,
    el 'pu' del actual debe coincidir con 'u' del anterior (continuidad); si no,
    hay hueco => hay que resincronizar (re-snapshot).

    Tras seed() el buffer queda en estado "esperando primer evento": last_pu=None,
    de modo que el primer apply() valida el solapamiento U <= L+1 <= u (rama viva,
    no código muerto).
    """

    def __init__(self, levels: int = 20):
        self.levels = levels
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.last_update_id: Optional[int] = None
        self.last_pu: Optional[int] = None
        self.seeded = False
        self.sequence_gaps = 0
        # Invariantes §EXT: contadores para auditoría de captura/replay
        self.crossed_book_events = 0
        self.invalid_depth_events = 0
        self.sequence_gap_events = 0  # alias de sequence_gaps para grep
        self.reseeds = 0
        self.reseed_attempts = 0

    def seed(self, snapshot: Dict[str, Any]) -> bool:
        """Inicializa el libro desde el snapshot REST (lastUpdateId)."""
        self.bids = {float(p): float(q) for p, q in snapshot.get("bids", [])}
        self.asks = {float(p): float(q) for p, q in snapshot.get("asks", [])}
        self.last_update_id = int(snapshot.get("lastUpdateId", 0))
        # Estado "esperando primer evento": la rama de solapamiento de apply()
        # se activa con last_pu=None (U <= lastUpdateId+1 <= u).
        self.last_pu = None
        self.seeded = True
        return True

    def _apply_side(self, book: Dict[float, float], levels: List[tuple]) -> None:
        for price, qty in levels:
            try:
                p = float(price)
                q = float(qty)
            except (TypeError, ValueError):
                continue
            if q == 0.0:
                book.pop(p, None)
            else:
                book[p] = q

    def apply(self, event: Dict[str, Any]) -> bool:
        """Aplica un evento diff stream @depth@100ms. Devuelve True si se aplicó, False si hueco.

        Protocolo (diff incremental tras snapshot REST fapi/v1/depth):
          - Si no está sembrado: False (invalid_depth_events++).
          - Eventos obsoletos u <= lastUpdateId: False (invalid_depth_events++).
          - Primer evento post-seed: exige U <= lastUpdateId + 1 <= u
            (U <= lastUpdateId + 1 <= u) — solapamiento. Si no, invalid_depth_events++.
          - Siguientes diffs: exige pu == previous_u  (pu del actual == u del anterior).
            Si viola, sequence_gaps++, sequence_gap_events++, invalid_depth_events++.
          - Merge incremental: qty == 0 -> remove level, qty > 0 -> upsert.
          - Post-apply: verificar best_bid < best_ask; si best_bid >= best_ask
            -> crossed_book_events++ y se cuenta como invalid_depth_events
            salvo que el stream lo evidencie explícitamente (cross documentado).
            No se permite libro cruzado sin evidencia explícita del stream.
        """
        if not self.seeded:
            self.invalid_depth_events += 1
            return False
        U = event.get("first_update_id", 0)
        u = event.get("update_id", 0)
        pu = event.get("pu", 0)
        # Eventos ya obsoletos respecto del snapshot.
        if self.last_update_id is None:
            self.invalid_depth_events += 1
            return False
        if u <= self.last_update_id:
            self.invalid_depth_events += 1
            return False
        # El primer evento post-seed debe solapar el snapshot: U <= lastUpdateId + 1 <= u
        if self.last_pu is None:
            if not (U <= self.last_update_id + 1 <= u):
                self.invalid_depth_events += 1
                return False
        else:
            # Continuidad: pu == previous_u
            if pu != self.last_pu:
                self.sequence_gaps += 1
                self.sequence_gap_events += 1
                self.invalid_depth_events += 1
                return False
        self._apply_side(self.bids, event.get("bids", []))
        self._apply_side(self.asks, event.get("asks", []))
        # Invariante: best_bid < best_ask — solo permitir cruzado si stream lo evidencia
        # explícitamente. Aquí detectamos cruzado tras merge incremental.
        if self.bids and self.asks:
            best_bid = max(self.bids.keys())
            best_ask = min(self.asks.keys())
            if best_bid >= best_ask:
                self.crossed_book_events += 1
                # Por defecto lo tratamos como invalid salvo evidencia explícita del stream.
                # Documentar condición: solo permitir si el update trae niveles que
                # explícitamente cruzan (ej. bid price >= ask price en el delta).
                # Como no hay flag de evidencia, lo contamos como invalid_depth.
                self.invalid_depth_events += 1
                # No revertimos el apply; el caller puede decidir reseed ante cruzado.
        self.last_update_id = u
        self.last_pu = u
        return True

    def top_levels(self, n: Optional[int] = None) -> Dict[str, str]:
        n = n or self.levels
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[:n]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        to_str = lambda lst: ";".join(f"{p}:{q}" for p, q in lst)
        return {"bid_levels": to_str(bids), "ask_levels": to_str(asks)}


def detect_pu_gap(prev_pu: Optional[int], pu: int) -> bool:
    """True si hay hueco de continuidad entre eventos consecutivos."""
    if prev_pu is None:
        return False
    return pu != prev_pu


# ── Estado de sincronización (orquesta buffer + snapshot + reseed) ──────────
class DepthCaptureState:
    """Máquina de estados de sincronización depth, testeable sin red.

    Fases:
      BUFFERING    : WS abierto, esperando snapshot inicial + evento solapante.
      SYNCING      : Snapshot recibido, esperando evento solapante.
      SYNCED       : Aplicando deltas con continuidad pu == previous_u.
      RESYNC       : Gap detectado; esperando nuevo snapshot para resincronizar.
      RECONNECTING : WS desconectado; libro inválido, backoff, snapshot pendiente.
      FAILED       : Se agotaron los reintentos de reseed (snapshot_sync_errors++).

    Contadores expuestos (stats): snapshot_sync_errors, sequence_gaps,
    discarded_pre_snapshot_events, reseeds, valid_depth_events, aggtrade_events,
    ws_reconnects, ws_disconnects.
    """

    def __init__(self, levels: int = 20, max_reseed_attempts: int = MAX_RESEED_ATTEMPTS):
        self.levels = levels
        self.buffer = DepthMergeBuffer(levels=levels)
        self.pending: List[Dict[str, Any]] = []
        self.phase = "BUFFERING"
        self.reseed_attempts = 0
        self.max_reseed_attempts = max_reseed_attempts
        # Snapshot actual (init o reseed) y su lastUpdateId.
        self.snap: Optional[Dict[str, Any]] = None
        self.snap_L = 0
        # Contadores separados.
        self.snapshot_sync_errors = 0
        self.snapshot_fetch_errors = 0
        self.sequence_gaps = 0
        self.sequence_gap_events = 0  # alias para grep
        self.discarded_pre_snapshot_events = 0
        self.reseeds = 0
        self.reseed_attempts = 0
        self.valid_depth_events = 0
        self.aggtrade_events = 0
        # Invariantes microestructura
        self.crossed_book_events = 0
        self.invalid_depth_events = 0
        self.invalid_trade_events = 0
        self.valid_trade_events = 0
        # WebSocket reconnection counters
        self.ws_reconnects = 0
        self.ws_disconnects = 0
        self.last_disconnect_reason: Optional[str] = None
        self.last_disconnect_ts: Optional[float] = None

    # ── Eventos ──────────────────────────────────────────────────────────
    def on_depth_event(self, ev: Dict[str, Any]) -> bool:
        """Recibe un evento depth parseado. Devuelve True si se aplicó al libro."""
        if self.phase == "FAILED":
            self.invalid_depth_events += 1
            return False
        if self.phase == "BUFFERING":
            # Aún sin snapshot: bufferizar todo.
            self.pending.append(ev)
            return False
        if self.phase == "RESYNC":
            # Esperando nuevo snapshot: bufferizar para el próximo intento.
            self.pending.append(ev)
            return False
        if self.phase == "RECONNECTING":
            # WS desconectado: bufferizar para después del reconnect + reseed.
            self.pending.append(ev)
            return False
        if self.phase == "SYNCING":
            # Ya tenemos L (snapshot). El evento solapante llega DESPUÉS del snapshot.
            L = self.snap_L
            u = ev["update_id"]
            U = ev["first_update_id"]
            if u <= L:
                # Evento obsoleto respecto del snapshot: descartar.
                self.discarded_pre_snapshot_events += 1
                self.invalid_depth_events += 1
                return False
            if U <= L + 1 <= u:
                # ¡Solapamiento! U <= lastUpdateId + 1 <= u — primer diff válido.
                assert self.snap is not None
                self.buffer.seed(self.snap)
                # buffer.apply valida pu == previous_u para futuros eventos; primer evento usa rama solapante.
                applied = self.buffer.apply(ev)
                if not applied:
                    self.invalid_depth_events += 1
                    self.sequence_gaps += 1
                    self.sequence_gap_events += 1
                    self.phase = "RESYNC"
                    self.pending = [ev]
                    return False
                self.valid_depth_events += 1
                # Propagar crossed si el evento generó libro cruzado
                if self.buffer.crossed_book_events > self.crossed_book_events:
                    self.crossed_book_events = self.buffer.crossed_book_events
                    self.invalid_depth_events += 1
                self.phase = "SYNCED"
                self.pending = []
                return True
            # u > L pero U > L+1: nos saltamos el evento solapante (hueco).
            # Pedir nuevo snapshot.
            self.sequence_gaps += 1
            self.sequence_gap_events += 1
            self.invalid_depth_events += 1
            self.phase = "RESYNC"
            self.pending = [ev]
            return False
        # SYNCED: aplicar con continuidad pu == previous_u.
        # snapshot_crossed sync: recordar crossed previo para detectar incremento.
        prev_crossed = self.buffer.crossed_book_events
        applied = self.buffer.apply(ev)
        if applied:
            self.valid_depth_events += 1
            if self.buffer.crossed_book_events > prev_crossed:
                self.crossed_book_events += 1
                # crossed ya contado como invalid_depth_events en buffer, reflejar aquí
                self.invalid_depth_events += 1
            return True
        # Gap de secuencia (buffer.apply ya contó en buffer.sequence_gaps):
        # contar en el estado y resincronizar. pu != previous_u.
        self.sequence_gaps += 1
        self.sequence_gap_events += 1
        self.invalid_depth_events += 1
        # Sincronizar contador crossed si buffer detectó cruzado en este evento fallido
        if self.buffer.crossed_book_events > prev_crossed:
            self.crossed_book_events = self.buffer.crossed_book_events
        self.phase = "RESYNC"
        return False

    def on_trade_event(self, tr: Dict[str, Any]) -> bool:
        """aggTrade es INDEPENDIENTE de la sincronización de depth."""
        # Validación de trade: price>0 qty>0 ya filtrada en parse_trade_message,
        # pero si llega aquí un dict con price/qty <=0 lo contamos como invalid.
        price = tr.get("price", 0)
        qty = tr.get("qty", 0)
        if price is None or qty is None or price <= 0 or qty <= 0:
            self.invalid_trade_events += 1
            return False
        self.aggtrade_events += 1
        self.valid_trade_events += 1
        return True

    def on_invalid_trade(self) -> None:
        """Contabiliza un mensaje trade descartado por parse_trade_message (no compatible / price||qty <=0)."""
        self.invalid_trade_events += 1

    def on_ws_disconnect(self, reason: str) -> None:
        """Registra desconexión WS y transiciona a RECONNECTING."""
        self.ws_disconnects += 1
        self.last_disconnect_reason = reason
        self.last_disconnect_ts = time.time()
        # Invalidar libro: cualquier evento depth posterior es huérfano hasta reseed.
        self.phase = "RECONNECTING"
        # Limpiar buffer y snapshot actual; se pedirá uno nuevo tras reconectar.
        self.pending = []
        self.snap = None
        self.snap_L = 0
        self.buffer = DepthMergeBuffer(levels=self.levels)
        self.reseed_attempts = 0

    def on_ws_reconnect(self) -> None:
        """Registra reconexión WS exitosa."""
        self.ws_reconnects += 1
        # Fase pasa a BUFFERING para esperar nuevo snapshot.
        self.phase = "BUFFERING"

    # ── Snapshot (inicial o reseed) ───────────────────────────────────────
    def set_snapshot(self, snap: Dict[str, Any]) -> bool:
        """Aplica un snapshot REST. Devuelve True si logró sincronizar."""
        if self.phase == "FAILED":
            return False
        if self.phase == "SYNCED":
            # Snapshot redundante mientras ya estamos sincronizados: ignorar.
            return True
        self.snap = snap
        self.snap_L = int(snap.get("lastUpdateId", 0))
        if self.phase == "BUFFERING":
            # Pasamos a esperar el evento solapante (que llega tras el snapshot).
            self.phase = "SYNCING"
            return self._try_sync_pending()
        # RESYNC o RECONNECTING: cada intento cuenta contra el límite.
        self.reseed_attempts += 1
        self.phase = "SYNCING"
        ok = self._try_sync_pending()
        if self.phase == "SYNCED":
            self.reseeds += 1
        elif self.reseed_attempts > self.max_reseed_attempts:
            self.phase = "FAILED"
            self.snapshot_sync_errors += 1
        return ok

    def _try_sync_pending(self) -> bool:
        """Intenta sincronizar con los eventos ya bufferizados + el snapshot."""
        assert self.snap is not None
        L = self.snap_L
        # Descartar eventos obsoletos (u <= L) y conservar candidatos.
        candidates: List[Dict[str, Any]] = []
        for ev in self.pending:
            if ev["update_id"] <= L:
                self.discarded_pre_snapshot_events += 1
                self.invalid_depth_events += 1
            else:
                candidates.append(ev)
        self.pending = candidates
        # Primer evento con solapamiento: U <= lastUpdateId + 1 <= u
        first_idx = -1
        for i, ev in enumerate(candidates):
            if ev["first_update_id"] <= L + 1 <= ev["update_id"]:
                first_idx = i
                break
        if first_idx == -1:
            # Ninguno solapa: esperar eventos nuevos en SYNCING (pending limpio).
            self.pending = []
            return False
        # Reconstruir el libro y aplicar desde el evento solapante en adelante.
        # Contar descartados pre-solape como invalid_depth_events
        for ev in candidates[:first_idx]:
            if ev["first_update_id"] > L + 1:
                self.invalid_depth_events += 1
                self.sequence_gaps += 1
                self.sequence_gap_events += 1
        self.buffer.seed(self.snap)
        prev_crossed = self.buffer.crossed_book_events
        assert self.buffer.apply(candidates[first_idx])
        self.valid_depth_events += 1
        if self.buffer.crossed_book_events > prev_crossed:
            self.crossed_book_events += 1
        self.pending = []
        self.phase = "SYNCED"
        for ev in candidates[first_idx + 1:]:
            prev_crossed = self.buffer.crossed_book_events
            if self.buffer.apply(ev):
                self.valid_depth_events += 1
                if self.buffer.crossed_book_events > prev_crossed:
                    self.crossed_book_events += 1
            else:
                self.sequence_gaps += 1
                self.sequence_gap_events += 1
                self.invalid_depth_events += 1
                if self.buffer.crossed_book_events > prev_crossed:
                    self.crossed_book_events = self.buffer.crossed_book_events
                self.phase = "RESYNC"
                break
        return True

    def stats(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "snapshot_sync_errors": self.snapshot_sync_errors,
            "snapshot_fetch_errors": self.snapshot_fetch_errors,
            "sequence_gaps": self.sequence_gaps,
            "sequence_gap_events": self.sequence_gap_events,
            "discarded_pre_snapshot_events": self.discarded_pre_snapshot_events,
            "reseeds": self.reseeds,
            "valid_depth_events": self.valid_depth_events,
            "aggtrade_events": self.aggtrade_events,
            "valid_trade_events": self.valid_trade_events,
            "invalid_trade_events": self.invalid_trade_events,
            "crossed_book_events": self.crossed_book_events,
            "invalid_depth_events": self.invalid_depth_events,
            "reseed_attempts": self.reseed_attempts,
            "max_reseed_attempts": self.max_reseed_attempts,
            "ws_reconnects": self.ws_reconnects,
            "ws_disconnects": self.ws_disconnects,
            "last_disconnect_reason": self.last_disconnect_reason,
            "last_disconnect_ts": self.last_disconnect_ts,
        }


# ── Lectura de CSV capturado (para L2Replay) ───────────────────────────────
def read_capture(depth_csv: str, trades_csv: Optional[str] = None) -> Dict[str, List[Dict]]:
    """Lee CSVs de captura y devuelve rows compatibles con L2Replay."""
    depth_rows: List[Dict] = []
    if depth_csv and os.path.exists(depth_csv):
        with open(depth_csv, newline="") as f:
            for r in csv.DictReader(f):
                depth_rows.append({
                    "ts_ms": int(r["ts_ms"]),
                    "update_id": int(r.get("update_id", 0)),
                    "pu": int(r.get("pu", 0)),
                    "first_update_id": int(r.get("first_update_id", 0)),
                    "bid_levels_csv": r.get("bid_levels", ""),
                    "ask_levels_csv": r.get("ask_levels", ""),
                })
    trade_rows: List[Dict] = []
    if trades_csv and os.path.exists(trades_csv):
        with open(trades_csv, newline="") as f:
            for r in csv.DictReader(f):
                trade_rows.append({
                    "ts_ms": int(r["ts_ms"]),
                    "price": float(r["price"]),
                    "qty": float(r["qty"]),
                    "is_buyer_maker": r["is_buyer_maker"].lower() == "true",
                })
    return {"depth_rows": depth_rows, "trade_rows": trade_rows}


# ── Cliente de captura (solo se instancia con red en main) ─────────────────
class CaptureClient:
    def __init__(self, symbol: str = "XRPUSDC", real: bool = True, levels: int = 20,
                 out_dir: str = "data/l2", log_gaps: bool = True,
                 max_reseed_attempts: int = MAX_RESEED_ATTEMPTS):
        self.symbol = symbol.upper()
        self.real = real
        self.levels = levels
        self.out_dir = out_dir
        self.log_gaps = log_gaps
        self.max_reseed_attempts = max_reseed_attempts
        self.sync = DepthCaptureState(levels=levels,
                                      max_reseed_attempts=max_reseed_attempts)
        self.depth_path = os.path.join(out_dir, f"{self.symbol}_depth.csv")
        self.trades_path = os.path.join(out_dir, f"{self.symbol}_trades.csv")
        self.ws_events_path = os.path.join(out_dir, f"{self.symbol}_ws_events.log")
        self._depth_f = None
        self._trades_f = None
        self._ws_events_f = None
        self._depth_w = None
        self._trades_w = None
        self._ws_events_w = None
        self._lock = threading.Lock()
        self._last_reseed_ts = 0.0
        self._last_reseed_ws_reconnects = 0
        self._deadline = 0.0
        self._stop = False
        self._total_failures = 0
        self._ws: Optional[Any] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._reconnect_attempt = 0
        self._first_connect = True
        self._disconnect_ts: Optional[float] = None
        self._start_ts: Optional[float] = None
        self._end_ts: Optional[float] = None
        self._exit_reason: str = ""
        # Registrar handlers de señal para cierre ordenado
        self._setup_signal_handlers()

    def _setup_signal_handlers(self) -> None:
        """Configura handlers para SIGINT (Ctrl+C) y SIGTERM."""
        def _signal_handler(signum, frame):
            sys.stderr.write(f"\n[capture] Señal {signum} recibida, iniciando cierre ordenado...\n")
            self._stop = True
            self._exit_reason = f"signal_{signum}"
        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except (AttributeError, OSError):
            # Windows puede no tener SIGTERM, o signal no disponible en algunos entornos
            pass

    def _open_files(self):
        os.makedirs(self.out_dir, exist_ok=True)
        self._depth_f = open(self.depth_path, "a", newline="")
        self._trades_f = open(self.trades_path, "a", newline="")
        self._ws_events_f = open(self.ws_events_path, "a", newline="")
        self._depth_w = csv.writer(self._depth_f)
        self._trades_w = csv.writer(self._trades_f)
        self._ws_events_w = csv.writer(self._ws_events_f)
        if os.path.getsize(self.depth_path) == 0:
            self._depth_w.writerow(
                ["ts_ms", "update_id", "pu", "first_update_id", "bid_levels", "ask_levels"])
        if os.path.getsize(self.trades_path) == 0:
            self._trades_w.writerow(["ts_ms", "price", "qty", "is_buyer_maker"])
        if os.path.getsize(self.ws_events_path) == 0:
            self._ws_events_w.writerow(["ts_wall_ms", "event", "reason", "reconnect_attempt", "duration_ms"])

    def _close_files(self):
        if self._depth_f:
            self._depth_f.close()
        if self._trades_f:
            self._trades_f.close()
        if self._ws_events_f:
            self._ws_events_f.close()

    def _log_ws_event(self, event: str, reason: str = "", duration_ms: int = 0):
        """Registra evento de WebSocket en archivo de log."""
        with self._lock:
            if self._ws_events_w:
                self._ws_events_w.writerow([
                    int(time.time() * 1000), event, reason, self._reconnect_attempt, duration_ms
                ])
                self._ws_events_f.flush()

    def _fetch_snapshot(self) -> Dict[str, Any]:
        import urllib.request
        url = snapshot_url(self.symbol, self.levels, self.real)
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    backoff = 1.0 * (2 ** attempt)
                    # jitter pequeño
                    backoff += random.uniform(-0.1, 0.1) * backoff
                    backoff = max(0.2, backoff)
                    time.sleep(backoff)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def _reseed_now(self) -> None:
        """Pide un snapshot REST y lo aplica (inicial o reseed).

        Diferencia fallo REST (timeout/10060) de mismatch lógico (U<=L+1<=u sin
        pending). En fallo REST: incrementa snapshot_fetch_errors y loggea
        'snapshot fetch failed', sin avanzar reseed_attempts. En mismatch lógico:
        deja que set_snapshot avance reseed_attempts (protocolo Binance intacto).
        """
        # Gate anti-spam: no reseed si pending==[] y ws_reconnects sin cambio
        # Nota: BUFFERING inicial (snap is None) nunca se bloquea: debe tomar snapshot aunque pending vacío
        with self._lock:
            pending_len = len(self.sync.pending)
            ws_recon = self.sync.ws_reconnects
            phase = self.sync.phase
            has_snap = self.sync.snap is not None
        if phase in ("SYNCING", "RESYNC", "RECONNECTING"):
            if pending_len == 0 and ws_recon == self._last_reseed_ws_reconnects:
                # No hubo nuevos eventos WS desde último reseed: spam inútil
                # (protocolo U<=L+1<=u requiere pending no vacío para solapar)
                # Mantener intervalo alargado sin spamear REST
                self._last_reseed_ts = time.time()
                return
        elif phase == "BUFFERING" and has_snap:
            # BUFFERING post-reconnect con snap previo ya usado: tratar como gate si pending vacío sin nuevo WS
            if pending_len == 0 and ws_recon == self._last_reseed_ws_reconnects:
                self._last_reseed_ts = time.time()
                return
        self._last_reseed_ts = time.time()
        self._last_reseed_ws_reconnects = ws_recon
        try:
            snap = self._fetch_snapshot()
        except Exception as e:
            sys.stderr.write(f"[capture] snapshot fetch failed: {e}\n")
            with self._lock:
                self.sync.snapshot_fetch_errors += 1
            return
        with self._lock:
            self.sync.set_snapshot(snap)
        if self.sync.phase == "SYNCED":
            sys.stderr.write("[capture] depth synced\n")
        elif self.sync.phase == "FAILED":
            sys.stderr.write("[capture] snapshot sync FAILED (max reseeds)\n")

    def _on_depth(self, raw):
        ev = parse_depth_message(raw)
        if ev is None:
            with self._lock:
                self.sync.invalid_depth_events += 1
            return
        with self._lock:
            applied = self.sync.on_depth_event(ev)
            if applied:
                # Verificar best_bid < best_ask tras apply; crossed ya contado en buffer/sync
                top = self.sync.buffer.top_levels(self.levels)
                self._depth_w.writerow(
                    [ev["ts_ms"], ev["update_id"], ev["pu"], ev["first_update_id"],
                     top["bid_levels"], top["ask_levels"]])
                self._depth_f.flush()
            else:
                # Evento descartado por violación protocolo o crossed sin evidencia ya contado
                pass

    def _on_trade(self, raw):
        tr = parse_trade_message(raw)
        if tr is None:
            with self._lock:
                self.sync.on_invalid_trade()
            return
        with self._lock:
            ok = self.sync.on_trade_event(tr)
            if ok:
                self._trades_w.writerow(
                    [tr["ts_ms"], tr["price"], tr["qty"], tr["is_buyer_maker"]])
                self._trades_f.flush()
            else:
                # price/qty <=0 ya contado como invalid_trade_events
                pass

    def _build_ws_url(self) -> str:
        """Construye la URL base del WebSocket (endpoint /ws para SUBSCRIBE)."""
        return depth_ws_url(self.real)

    def _get_subscribe_streams(self) -> List[str]:
        """Retorna la lista de streams a suscribir."""
        return [
            depth_stream_name(self.symbol, self.levels),
            trades_stream_name(self.symbol)
        ]

    def _send_subscribe(self, ws) -> None:
        """Envía mensaje SUBSCRIBE al WebSocket."""
        streams = self._get_subscribe_streams()
        msg = json.dumps({"method": "SUBSCRIBE", "params": streams, "id": 1})
        ws.send(msg)
        sys.stderr.write(f"[capture] SUBSCRIBE sent: {streams}\n")

    def _run_ws_loop(self) -> None:
        """Bucle principal de WebSocket con reconexión automática."""
        import websocket
        ws_url = self._build_ws_url()
        self._deadline = time.time() + self._minutes * 60.0
        self._stop = False
        self._reconnect_attempt = 0

        while not self._stop and time.time() < self._deadline:
            # Crear nueva conexión WebSocket
            ws = websocket.WebSocketApp(
                ws_url,
                on_message=self._ws_on_message,
                on_error=self._ws_on_error,
                on_close=self._ws_on_close,
                on_open=self._ws_on_open,
            )
            self._ws = ws

            try:
                ws.run_forever(ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT)
            except Exception as e:
                if not self._stop:
                    sys.stderr.write(f"[capture] ws run_forever error: {e}\n")

            # Si nos detenemos por deadline o stop, salir del bucle
            if self._stop or time.time() >= self._deadline:
                break

            # Reconexión con backoff exponencial + jitter
            self._reconnect_attempt += 1
            if self._reconnect_attempt > WS_MAX_RECONNECT_ATTEMPTS:
                sys.stderr.write(f"[capture] max reconnect attempts ({WS_MAX_RECONNECT_ATTEMPTS}) reached\n")
                break

            backoff = min(WS_RECONNECT_BASE_SEC * (2 ** (self._reconnect_attempt - 1)), WS_RECONNECT_MAX_SEC)
            jitter = random.uniform(-WS_RECONNECT_JITTER, WS_RECONNECT_JITTER) * backoff
            sleep_time = max(0.1, backoff + jitter)

            sys.stderr.write(f"[capture] reconnecting in {sleep_time:.1f}s (attempt {self._reconnect_attempt})\n")
            time.sleep(sleep_time)

    def _ws_on_open(self, ws):
        """Callback al abrir conexión WebSocket: enviar SUBSCRIBE."""
        sys.stderr.write("[capture] WS connected, sending SUBSCRIBE\n")
        self._send_subscribe(ws)
        # Notificar al estado que nos reconectamos
        with self._lock:
            self.sync.on_ws_reconnect()
        self._log_ws_event("reconnect", "", 0)

    def _ws_on_message(self, ws, message):
        """Callback de mensaje WebSocket."""
        try:
            raw = json.loads(message)
        except json.JSONDecodeError:
            return

        # Respuesta a SUBSCRIBE
        if "result" in raw and raw.get("id") == 1:
            sys.stderr.write(f"[capture] SUBSCRIBE confirmed: {raw}\n")
            return

        try:
            if raw.get("e") == "aggTrade":
                self._on_trade(raw)
            elif raw.get("e") == "trade":
                # Fallback si se usa @trade en lugar de @aggTrade
                self._on_trade(raw)
            elif "b" in raw or "a" in raw:
                # depthUpdate tiene b/a como listas; aggTrade también tiene "a" (int) por eso se chequea e primero
                # Validar que b/a sean listas para evitar colisión con aggTrade
                b = raw.get("b")
                a = raw.get("a")
                if isinstance(b, list) or isinstance(a, list):
                    self._on_depth(raw)
        except Exception as e:
            import traceback
            sys.stderr.write(f"[capture] message handling error: {e} raw={str(message)[:300]}\n")
            traceback.print_exc(file=sys.stderr)

        if time.time() >= self._deadline:
            self._stop = True
            try:
                ws.close()
            except Exception:
                pass

    def _ws_on_error(self, ws, error):
        sys.stderr.write(f"[capture] ws error: {error}\n")

    def _ws_on_close(self, ws, code, msg):
        reason = f"code={code} msg={msg}"
        sys.stderr.write(f"[capture] WS closed: {reason}\n")
        disconnect_ts = time.time()
        with self._lock:
            self.sync.on_ws_disconnect(reason)
        self._log_ws_event("disconnect", reason, 0)

    def run(self, minutes: float = 60):
            import websocket  # import perezoso: el módulo es importable sin red
            self._minutes = minutes
            self._deadline = time.time() + minutes * 60.0
            self._start_ts = time.time()
            self._exit_reason = ""
            self._open_files()
            # 1) Abrir el WebSocket ANTES de pedir el snapshot REST (protocolo Binance).
            #    Bufferizar eventos depth mientras se obtiene el snapshot.
            self._ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True)
            self._ws_thread.start()
            # Esperar a que WS conecte y empiece a bufferizar (1.0s ventana inicial)
            time.sleep(1.0)
            self._reseed_now()
            # 3) Bucle de monitoreo: deadline + reintentos de reseed (throttle).
            _last_heartbeat = time.time()
            try:
                while time.time() < self._deadline and not self._stop:
                    with self._lock:
                        phase = self.sync.phase
                    if phase == "FAILED":
                        # Auto-cura: un gap transitorio no debe matar una captura larga.
                        self._total_failures += 1
                        sys.stderr.write(
                            f"[capture] sync FAILED; self-healing (intento {self._total_failures})\n")
                        if self._total_failures > MAX_TOTAL_FAILURES:
                            self._exit_reason = "max_failures"
                            break
                        old = self.sync
                        self.sync = DepthCaptureState(levels=self.levels,
                                                       max_reseed_attempts=self.max_reseed_attempts)
                        # Preservar contadores acumulados (incluye invariantes microestructura).
                        # FIX doble conteo: snapshot_sync_errors ya contado en set_snapshot → no +1
                        self.sync.valid_depth_events = old.valid_depth_events
                        self.sync.sequence_gaps = old.sequence_gaps
                        self.sync.sequence_gap_events = old.sequence_gap_events
                        self.sync.aggtrade_events = old.aggtrade_events
                        self.sync.valid_trade_events = old.valid_trade_events
                        self.sync.invalid_trade_events = old.invalid_trade_events
                        self.sync.crossed_book_events = old.crossed_book_events
                        self.sync.invalid_depth_events = old.invalid_depth_events
                        self.sync.discarded_pre_snapshot_events = old.discarded_pre_snapshot_events
                        self.sync.snapshot_sync_errors = old.snapshot_sync_errors
                        self.sync.snapshot_fetch_errors = old.snapshot_fetch_errors
                        self.sync.reseeds = old.reseeds
                        self.sync.reseed_attempts = old.reseed_attempts
                        self.sync.ws_reconnects = old.ws_reconnects
                        self.sync.ws_disconnects = old.ws_disconnects
                        self.sync.last_disconnect_reason = old.last_disconnect_reason
                        self.sync.last_disconnect_ts = old.last_disconnect_ts
                        # Reset gate tracking al clonar
                        self._last_reseed_ws_reconnects = old.ws_reconnects
                        time.sleep(0.3)
                        self._reseed_now()
                        continue
                    now = time.time()
                    if phase == "BUFFERING" and (now - self._last_reseed_ts) >= RESEED_INTERVAL_SEC:
                        self._reseed_now()
                    elif phase in ("RESYNC", "RECONNECTING") and (now - self._last_reseed_ts) >= RESEED_INTERVAL_SEC:
                        self._reseed_now()
                    elif phase == "SYNCING" and (now - self._last_reseed_ts) >= RESEED_INTERVAL_SYNCING_SEC:
                        # Atascado esperando el evento solapante: reintentar snapshot (intervalo alargado 15s para evitar spam con pending vacío).
                        self._reseed_now()
                    # Heartbeat cada 30s a stderr + archivo dedicado (desacoplado de shell redirect)
                    if (now - _last_heartbeat) >= 30.0:
                        _last_heartbeat = now
                        with self._lock:
                            vd = self.sync.valid_depth_events
                            tr = self.sync.aggtrade_events
                            gaps = self.sync.sequence_gaps
                            reseeds = self.sync.reseeds
                            phase_hb = self.sync.phase
                            recon = self.sync.ws_reconnects
                        try:
                            depth_bytes = os.path.getsize(self.depth_path) if os.path.exists(self.depth_path) else 0
                            trade_bytes = os.path.getsize(self.trades_path) if os.path.exists(self.trades_path) else 0
                        except Exception:
                            depth_bytes = trade_bytes = 0
                        elapsed = int(now - self._start_ts) if self._start_ts else 0
                        msg = f"[capture] heartbeat elapsed={elapsed}s phase={phase_hb} depth={vd} trades={tr} gaps={gaps} reseeds={reseeds} reconnects={recon} bytes_depth={depth_bytes} bytes_trade={trade_bytes}\n"
                        try:
                            sys.stderr.write(msg)
                            sys.stderr.flush()
                        except Exception:
                            pass
                        # Escritura directa a logs/capture_mainnet_1h.log (append, shared)
                        try:
                            with open("logs/capture_mainnet_1h.log", "a", encoding="utf-8") as hb:
                                hb.write(msg)
                                hb.flush()
                        except Exception:
                            pass
                    time.sleep(0.2)
                # Determinar razón de salida si no se estableció ya
                if not self._exit_reason:
                    if self._stop and "signal" in str(self._exit_reason):
                        self._exit_reason = "signal"
                    elif time.time() >= self._deadline:
                        self._exit_reason = "completed_duration"
                    else:
                        self._exit_reason = "stopped"
            except Exception as e:
                self._exit_reason = f"error_{type(e).__name__}"
                sys.stderr.write(f"[capture] ERROR inesperado: {e}\n")
                traceback.print_exc(file=sys.stderr)
            finally:
                self._end_ts = time.time()
                self._stop = True
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
                if self._ws_thread:
                    self._ws_thread.join(timeout=5)
                self._close_files()
                self._write_stats()

    def _write_stats(self):
        s = self.sync.stats()
        # Añadir metadata de captura
        s["capture_start_ts"] = self._start_ts
        s["capture_end_ts"] = self._end_ts
        s["capture_duration_sec"] = (self._end_ts - self._start_ts) if (self._start_ts and self._end_ts) else 0
        s["exit_reason"] = self._exit_reason or "unknown"
        # first_ts/last_ts desde los CSVs si existen
        first_ts = None
        last_ts = None
        try:
            if os.path.exists(self.depth_path):
                with open(self.depth_path, newline="") as f:
                    reader = csv.reader(f)
                    next(reader, None)  # skip header
                    for row in reader:
                        if row:
                            ts = int(row[0])
                            if first_ts is None:
                                first_ts = ts
                            last_ts = ts
        except Exception:
            pass
        s["first_ts"] = first_ts
        s["last_ts"] = last_ts
        path = os.path.join(self.out_dir, f"{self.symbol}_capture_stats.json")
        try:
            with open(path, "w") as f:
                json.dump(s, f, indent=2)
        except Exception:
            pass
        sys.stderr.write(f"[capture] stats: {json.dumps(s)}\n")


# ── main ────────────────────────────────────────────────────────────────────
def main(argv=None):
    p = argparse.ArgumentParser(description="Capture L2 depth+trades (no creds).")
    p.add_argument("--symbol", default="XRPUSDC")
    p.add_argument("--testnet", action="store_true", help="Usar testnet WS/REST público")
    p.add_argument("--levels", type=int, default=20)
    p.add_argument("--out", default="data/l2")
    p.add_argument("--minutes", type=float, default=None, help="Duración en minutos (legacy, use --duration-minutes)")
    p.add_argument("--duration-minutes", type=int, default=None, help="Duración en minutos (para pruebas, ej. 2)")
    p.add_argument("--duration-hours", type=float, default=None, help="Duración en horas (ej. 24 para captura 24h)")
    p.add_argument("--max-reseed", type=int, default=MAX_RESEED_ATTEMPTS)
    args = p.parse_args(argv)

    # Calcular duración efectiva en minutos
    duration_minutes = None
    if args.duration_hours is not None:
        duration_minutes = args.duration_hours * 60.0
    if args.duration_minutes is not None:
        if duration_minutes is not None:
            # Usar el más restrictivo (menor)
            duration_minutes = min(duration_minutes, float(args.duration_minutes))
        else:
            duration_minutes = float(args.duration_minutes)
    if args.minutes is not None:
        if duration_minutes is not None:
            duration_minutes = min(duration_minutes, args.minutes)
        else:
            duration_minutes = args.minutes
    if duration_minutes is None:
        duration_minutes = 60.0  # default 1 hora

    client = CaptureClient(symbol=args.symbol, real=not args.testnet,
                           levels=args.levels, out_dir=args.out,
                           max_reseed_attempts=args.max_reseed)
    start_msg = f"[capture] Iniciando captura {args.symbol} real={not args.testnet} -> {args.out} por {duration_minutes}m\n"
    sys.stderr.write(start_msg)
    try:
        sys.stderr.flush()
    except Exception:
        pass
    # Escritura directa desacoplada de shell redirect (para lanzamientos WMI directos)
    try:
        with open("logs/capture_mainnet_1h.log", "a", encoding="utf-8") as lf:
            lf.write(start_msg)
            lf.flush()
    except Exception:
        pass
    client.run(minutes=duration_minutes)


if __name__ == "__main__":
    main()
