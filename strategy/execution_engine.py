# execution_engine.py
"""Execution Engine para el bot de market making de Binance Futures (XRPUSDC).

Única interfaz entre la estrategia y las órdenes (§9-§10). NO duplica funciones
de API_binance_futuros.py (§1): delega toda la comunicación con Binance en ese
módulo. Este motor se encarga de:

- Generar client_order_id únicos POR LADO (MM-<side>-<ts>-<counter>), nunca
  compartidos entre bid y ask (§0.6 — órdenes fantasma).
- Validar precio/cantidad contra los filtros reales del símbolo (§3, §0.6).
- Maker check §10: en modo real, rechazar órdenes que cruzarían el spread.
- Cancelar / reemplazar / sincronizar órdenes con frecuencia controlada (§0.6).
- Dry-run (Nivel 0, §0.4): no envía órdenes reales; simula en memoria.

Thread-safety: todo acceso a open_orders/bid_orders/ask_orders/fills está bajo
self._lock (§0.6 — race conditions entre tareas concurrentes).
"""

import os
import sys
import time
import json
import threading
import logging
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)

try:
    from strategy import config
except ImportError:
    import config

# API_binance_futuros es la única interfaz con Binance (§1). Si no está
# disponible (offline), api = None y el motor queda en modo simulación.
try:
    import API_binance_futuros as api
except Exception:
    api = None

try:
    from binance.exceptions import BinanceAPIException
except ImportError:
    # Sin binance instalado: api será None y nunca habrá instancias reales que
    # chequear. El alias a RuntimeError mantiene un único nombre para el tipo.
    BinanceAPIException = RuntimeError


logger = logging.getLogger("ExecutionEngine")


def _round_to_step(value, step, mode=ROUND_HALF_UP):
    """Redondea value al múltiplo más cercano de step (Decimal, sin artefactos float)."""
    if not step or step <= 0:
        return float(value)
    d = Decimal(str(value)) / Decimal(str(step))
    d = d.quantize(Decimal("1"), rounding=mode)
    return float(d * Decimal(str(step)))


class ExecutionEngine:
    """Gestión de órdenes maker: place / cancel / replace / reconcile / fills."""

    def __init__(self, symbol: str, real: bool = False, dry_run: bool = True):
        self.symbol = symbol
        self.real = real
        self.dry_run = dry_run

        self._lock = threading.Lock()
        self._log_lock = threading.Lock()

        # Estado de órdenes. open_orders es el índice global; bid_orders y
        # ask_orders son las vistas por lado (IDs únicos por lado §0.6).
        self.open_orders: dict = {}
        self.bid_orders: dict = {}
        self.ask_orders: dict = {}

        # Fills registrados (en memoria) con timestamp.
        self.fills: list = []

        # Filtros reales del símbolo extraídos dinámicamente (§3).
        self.symbol_info = None

        self._order_counter = 0
        self._last_sync_ts = 0.0

        today = datetime.now().strftime("%Y%m%d")
        orders_dir = config.LOG_ORDERS
        fills_dir = config.LOG_FILLS
        os.makedirs(str(orders_dir), exist_ok=True)
        os.makedirs(str(fills_dir), exist_ok=True)
        self._orders_log_path = str(orders_dir / f"orders_{today}.jsonl")
        self._fills_log_path = str(fills_dir / f"fills_{today}.jsonl")

        # IDs de fills ya registrados hoy (idempotencia §0.6): si el proceso
        # reinicia, refresh_open_orders no debe volver a registrar un fill que
        # ya quedó en el log del día (evita doble conteo en fill_count/PnL).
        self._recorded_fill_ids: set = self._load_recorded_fill_ids()

        self.init_symbol_info()

    # ── Helpers internos de estado (asumen self._lock tomado) ────────────
    def _add_order(self, client_order_id, info):
        """Registra una orden en open_orders + el dict de su lado."""
        self.open_orders[client_order_id] = info
        if info["side"] == "BUY":
            self.bid_orders[client_order_id] = info
        else:
            self.ask_orders[client_order_id] = info

    def _remove_order(self, client_order_id):
        """Purgar la orden de todos los dicts (sin orden fantasma §0.6)."""
        info = self.open_orders.pop(client_order_id, None)
        if info is not None:
            if info["side"] == "BUY":
                self.bid_orders.pop(client_order_id, None)
            else:
                self.ask_orders.pop(client_order_id, None)

    def _gen_client_order_id(self, side):
        """ID único por lado: MM-<side>-<ts_ms>-<counter>. Counter global anti-colisión."""
        self._order_counter += 1
        return f"MM-{side}-{int(time.time() * 1000)}-{self._order_counter}"

    # ── Logging a JSONL (§15) ────────────────────────────────────────────
    def _log_event_jsonl(self, filepath, event):
        with self._log_lock:
            try:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, default=str) + "\n")
            except Exception as e:
                logger.warning("No se pudo escribir log JSONL %s: %s", filepath, e)

    def _log_order_event(self, event_type, client_order_id, side, price, qty,
                         reason, server_order_id=None):
        self._log_event_jsonl(self._orders_log_path, {
            "ts": time.time(),
            "event": event_type,
            "client_order_id": client_order_id,
            "side": side,
            "price": price,
            "qty": qty,
            "reason": reason,
            "server_order_id": server_order_id,
            "dry_run": self.dry_run,
        })

    def _log_fill_event(self, fill):
        self._log_event_jsonl(self._fills_log_path, fill)

    def _load_recorded_fill_ids(self) -> set:
        """Carga los client_order_id de fills ya registrados HOY (idempotencia).

        §0.6/§15: el fills log es el audit trail del día. Si el proceso
        reinicia, refresh_open_orders volvería a ver como "desaparecida" una
        orden cuyo fill ya quedó registrado; este set evita duplicar el fill.
        """
        ids = set()
        try:
            if os.path.exists(self._fills_log_path):
                with open(self._fills_log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            oid = json.loads(line).get("client_order_id")
                        except (ValueError, TypeError):
                            continue
                        if oid:
                            ids.add(oid)
        except Exception as e:
            logger.warning("ExecutionEngine: no se pudieron cargar fills previos "
                           "(idempotencia degradada): %s", e)
        if ids:
            logger.info("ExecutionEngine: %d fills previos cargados (idempotencia)", len(ids))
        return ids

    # ── Filtros del símbolo (§3) ─────────────────────────────────────────
    def init_client(self, real: bool = False):
        """Inicializa el cliente API de Binance Futures (§1).

        Es la ÚNICA vía por la que el orquestador inicializa el cliente:
        API_binance_futuros es la única interfaz con Binance (§1). Seguridad
        §0.1/§0.5: `real=True` (mainnet) requiere autorización humana; en
        Nivel 0/dry-run solo se conecta testnet (real=False) y jamás se
        envían órdenes reales (§0.1/§21).

        Si la API no está disponible (offline), degrada con gracia: el motor
        queda en modo simulación sin romper el run.
        """
        if api is None:
            logger.warning("ExecutionEngine: API no disponible (offline). "
                           "Modo simulación sin cliente.")
            return False
        try:
            api.init_client(real=real)
            logger.info("ExecutionEngine: cliente API inicializado (real=%s, dry_run=%s)",
                        real, self.dry_run)
            return True
        except Exception as e:
            logger.error("ExecutionEngine: error en init_client(real=%s): %s", real, e)
            return False

    def init_symbol_info(self):
        """Consulta api.get_symbol y extrae los filtros reales. Nunca asume valores."""
        if api is None:
            logger.warning("ExecutionEngine: API no disponible (offline). "
                           "Filtros del símbolo no cargados.")
            return None
        try:
            info = api.get_symbol(self.symbol)
        except Exception as e:
            logger.error("ExecutionEngine: error en get_symbol(%s): %s", self.symbol, e)
            return None
        if isinstance(info, BinanceAPIException):
            logger.error("ExecutionEngine: get_symbol devolvió BinanceAPIException: %s", info)
            return None
        if not info:
            logger.error("ExecutionEngine: símbolo %s no encontrado en exchange_info", self.symbol)
            return None

        price_precision = info.get("pricePrecision")
        quantity_precision = info.get("quantityPrecision")
        tick_size = step_size = min_qty = min_notional = None
        for f in info.get("filters", []):
            ftype = f.get("filterType")
            if ftype == "PRICE_FILTER":
                tick_size = float(f.get("tickSize"))
            elif ftype == "LOT_SIZE":
                step_size = float(f.get("stepSize"))
                min_qty = float(f.get("minQty"))
            elif ftype == "MIN_NOTIONAL":
                min_notional = float(f.get("notional"))

        self.symbol_info = {
            "status": info.get("status"),
            "price_precision": price_precision,
            "quantity_precision": quantity_precision,
            "tick_size": tick_size,
            "step_size": step_size,
            "min_qty": min_qty,
            "min_notional": min_notional,
        }
        logger.info("ExecutionEngine: filtros de %s cargados: %s", self.symbol, self.symbol_info)
        return self.symbol_info

    # ── Validación de parámetros (§3, §0.6) ──────────────────────────────
    def validate_order_params(self, price: float, qty: float):
        """Redondea price al tick y qty al step; valida minQty/minNotional y price > 0.

        Returns: (ok, reason, rounded_price, rounded_qty). Los dos últimos son
        los valores ya redondeados para usar en la orden.
        """
        rounded_price = float(price)
        rounded_qty = float(qty)
        info = self.symbol_info

        if info is None:
            if price <= 0 or qty <= 0:
                return (False, "precio o cantidad no positivos (sin filtros del símbolo)",
                        rounded_price, rounded_qty)
            return (True, "", rounded_price, rounded_qty)

        if info.get("tick_size"):
            rounded_price = _round_to_step(price, info["tick_size"], ROUND_HALF_UP)
        if info.get("step_size"):
            rounded_qty = _round_to_step(qty, info["step_size"], ROUND_DOWN)

        if rounded_price <= 0:
            return (False, f"precio inválido tras redondeo: {rounded_price}",
                    rounded_price, rounded_qty)
        if rounded_qty <= 0:
            return (False, f"cantidad inválida tras redondeo: {rounded_qty}",
                    rounded_price, rounded_qty)

        if info.get("min_qty") is not None and rounded_qty < info["min_qty"] - 1e-12:
            return (False,
                    f"qty {rounded_qty} < minQty {info['min_qty']}",
                    rounded_price, rounded_qty)

        if info.get("min_notional") is not None:
            notional = rounded_price * rounded_qty
            if notional < info["min_notional"] - 1e-12:
                return (False,
                        f"notional {notional} < minNotional {info['min_notional']}",
                        rounded_price, rounded_qty)

        return (True, "", rounded_price, rounded_qty)

    # ── Maker check §10 ──────────────────────────────────────────────────
    def _maker_check_ok(self, side, price):
        """¿Esta orden límite NO cruzaría el spread (se mantiene maker)?"""
        if api is None:
            return False
        book = api.get_order_book_top(self.symbol)
        best_bid = book.get("best_bid")
        best_ask = book.get("best_ask")
        if best_bid is None or best_ask is None:
            logger.warning("ExecutionEngine: sin libro de órdenes para maker check. "
                           "Orden %s @ %.8g rechazada por seguridad.", side, price)
            return False
        # §10: una orden maker NO cruza el spread. Un BUY es maker si queda
        # estrictamente bajo el mejor ask (si price >= best_ask sería taker);
        # un SELL es maker si queda estrictamente sobre el mejor bid.
        if side == "BUY":
            return price < best_ask - 1e-12
        return price > best_bid + 1e-12

    # ── Envío de órdenes ─────────────────────────────────────────────────
    def place_maker_order(self, side: str, qty: float, price: float,
                          reduce_only: bool = False):
        """Envía una orden límite maker (GTX via API). Returns (order_id, ok, reason).

        dry_run=True (Nivel 0): registra la orden en memoria con estado
        'SIMULATED' y NO llama a la API. En modo real, ejecuta el maker check
        §10: si la orden cruzaría el spread, NO se ejecuta y se registra el motivo.
        """
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            return (None, False, f"side inválido: {side}")

        ok, reason, rounded_price, rounded_qty = self.validate_order_params(price, qty)
        if not ok:
            return (None, False, reason)
        price, qty = rounded_price, rounded_qty

        oid = self._gen_client_order_id(side)

        if self.dry_run:
            with self._lock:
                self._add_order(oid, {
                    "client_order_id": oid,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "status": "SIMULATED",
                    "reduce_only": reduce_only,
                    "created_ts": time.time(),
                    "server_order_id": None,
                })
            self._log_order_event("placed", oid, side, price, qty, "SIMULATED (dry-run)")
            return (oid, True, "SIMULATED (dry-run)")

        if not self._maker_check_ok(side, price):
            reason = (f"maker check §10 rechazó: orden {side} @ {price} cruzaría el spread")
            logger.warning("ExecutionEngine: %s", reason)
            self._log_order_event("rejected", oid, side, price, qty, reason)
            return (None, False, reason)

        if api is None:
            reason = "API no disponible (offline) para órdenes reales"
            self._log_order_event("error", oid, side, price, qty, reason)
            return (None, False, reason)

        func = api.buy_limit if side == "BUY" else api.sell_limit
        result = func(self.symbol, qty, price,
                      position_side="BOTH", reduce_only=reduce_only,
                      newClientOrderId=oid)
        if isinstance(result, BinanceAPIException):
            reason = str(result)
            logger.error("ExecutionEngine: error colocando orden %s: %s", oid, reason)
            self._log_order_event("error", oid, side, price, qty, reason)
            return (None, False, reason)

        server_id = result.get("orderId") if isinstance(result, dict) else None
        with self._lock:
            self._add_order(oid, {
                "client_order_id": oid,
                "side": side,
                "qty": qty,
                "price": price,
                "status": result.get("status", "NEW"),
                "reduce_only": reduce_only,
                "created_ts": time.time(),
                "server_order_id": server_id,
            })
        self._log_order_event("placed", oid, side, price, qty, "NEW", server_id)
        return (oid, True, "ok")

    # ── Cancelación ──────────────────────────────────────────────────────
    def cancel_order_by_id(self, order_id):
        """Cancela la orden (si existe) y la purga de los dicts. No-op si no existe.

        §0.6 (órdenes fantasma): en modo real se cancela PRIMERO en la API y
        solo se purga el estado local si el cancel tuvo éxito. Si la API
        falla, la orden queda en open_orders (se reintentará o la limpiará
        refresh_open_orders). En dry-run no hay API: se purga directamente.
        """
        with self._lock:
            info = self.open_orders.get(order_id)
            if info is None:
                return False
            side, price, qty = info["side"], info["price"], info["qty"]

        if not self.dry_run and api is not None:
            result = api.cancel_order(self.symbol, orig_client_id=order_id)
            if isinstance(result, BinanceAPIException):
                logger.warning(
                    "ExecutionEngine: error cancelando %s en la API. "
                    "NO se purga localmente (evita orden fantasma §0.6): %s",
                    order_id, result,
                )
                return False

        with self._lock:
            self._remove_order(order_id)
        self._log_order_event("canceled", order_id, side, price, qty,
                              "canceled (purgado §0.6)")
        return True

    def cancel_all_orders(self):
        """Cancela todas las órdenes abiertas y limpia los dicts. Devuelve cuántas canceló.

        Solo cuenta las cancelaciones exitosas (en real, las que la API
        confirmó; en dry-run, todas las que existían).
        """
        with self._lock:
            ids = list(self.open_orders.keys())
        canceled = 0
        for oid in ids:
            if self.cancel_order_by_id(oid):
                canceled += 1
        return canceled

    # ── Sincronización con el exchange (§0.6 frecuencia) ─────────────────
    def refresh_open_orders(self):
        """Sincroniza open_orders con la realidad del exchange.

        Control de frecuencia §0.6: solo sincroniza si pasó
        STATE_SYNC_INTERVAL_SEC desde la última vez. Devuelve True si
        sincronizó, False en caso contrario (o en dry-run).
        """
        now = time.time()
        if now - self._last_sync_ts < config.STATE_SYNC_INTERVAL_SEC:
            return False
        self._last_sync_ts = now

        if self.dry_run or api is None:
            return False

        result = api.get_open_orders(self.symbol)
        if isinstance(result, BinanceAPIException):
            logger.error("ExecutionEngine: get_open_orders falló: %s", result)
            return False

        live_ids = {o.get("clientOrderId") for o in result if o.get("clientOrderId")}
        removed = []
        with self._lock:
            for oid in list(self.open_orders.keys()):
                if oid not in live_ids:
                    info = self.open_orders[oid]
                    removed.append((oid, info))
                    self._remove_order(oid)

        for oid, info in removed:
            logger.info("ExecutionEngine: %s ya no existe en el exchange; purgada.", oid)
            self._log_order_event("sync_removed", oid, info["side"],
                                  info["price"], info["qty"],
                                  "no existe en exchange (refresh)")
            # En real, una maker GTX solo sale del book llenándose: registrar
            # el fill (idempotente ante reinicios §0.6, audit trail §15).
            self._detect_real_fill(oid, info)
        return True

    def _detect_real_fill(self, order_id, info):
        """Registra como fill una orden que desapareció del exchange (modo real).

        refresh_open_orders ya purgó la orden localmente; aquí queda constancia
        en self.fills y en el fills log (§15) con source="refresh". El fill se
        registra al PRECIO DE LA ORDEN: una maker GTX se llena a precio límite
        y el PnL exacto se reconcilia por posición (§18), no por este registro.

        Idempotencia (§0.6): si el fill ya estaba en el log del día (proceso
        anterior), se ignora. NO hace llamadas API extra (rate-limit §0.6).
        """
        with self._lock:
            if order_id in self._recorded_fill_ids:
                return
            self._recorded_fill_ids.add(order_id)
            # La orden ya fue removida por refresh_open_orders; el remove
            # interno es un no-op seguro (pop de key inexistente).
            self._register_fill_locked(order_id, info, info["price"], info["qty"],
                                       source="refresh")

    # ── Reemplazo ────────────────────────────────────────────────────────
    def replace_order(self, old_id, side, new_qty, new_price):
        """Cancela old_id y coloca una orden nueva. Devuelve (new_id, ok, reason)."""
        self.cancel_order_by_id(old_id)
        return self.place_maker_order(side, new_qty, new_price)

    # ── Consultas de estado ──────────────────────────────────────────────
    def get_order_lifetime(self, order_id) -> float:
        """Segundos desde la creación de la orden (0.0 si no existe)."""
        with self._lock:
            info = self.open_orders.get(order_id)
            if info is None:
                return 0.0
            return time.time() - info["created_ts"]

    def get_open_order_count(self) -> int:
        with self._lock:
            return len(self.open_orders)

    def get_orders(self) -> dict:
        """Copia segura de las órdenes abiertas (para el market_maker)."""
        with self._lock:
            return {oid: dict(info) for oid, info in self.open_orders.items()}

    def get_order(self, order_id):
        with self._lock:
            info = self.open_orders.get(order_id)
            return dict(info) if info else None

    # ── Posición e inventario ────────────────────────────────────────────
    def reconcile_position(self):
        """Devuelve el dict de posición de api.get_open_position_for_symbol.

        Semántica de retorno:
        - dict → posición reconciliada (válida). Si la API confirmó que NO
          hay posición abierta, se devuelve un dict normalizado de posición
          vacía: {"symbol", "positionAmt": "0", "entryPrice": "0",
          "unrealizedProfit": "0"} — permite distinguir "cuenta limpia"
          (válido) de "error" (None).
        - None → error: la API falló (BinanceAPIException) o el módulo no
          está disponible (api is None).
        """
        if api is None:
            return None
        result = api.get_open_position_for_symbol(self.symbol)
        if isinstance(result, BinanceAPIException):
            logger.error("ExecutionEngine: get_open_position_for_symbol falló: %s", result)
            return None
        if result is None:
            # API OK pero sin posición abierta: dict normalizado de posición 0
            # (el caller distingue "válido" de "error" que devuelve None).
            return {
                "symbol": self.symbol,
                "positionAmt": "0",
                "entryPrice": "0",
                "unrealizedProfit": "0",
            }
        return result

    # ── Leverage ─────────────────────────────────────────────────────────
    def set_leverage(self, leverage: int):
        """Aplica leverage al símbolo vía api.mod_leverage (con try/except)."""
        if api is None:
            return None
        try:
            result = api.mod_leverage(self.symbol, leverage)
        except Exception as e:
            logger.error("ExecutionEngine: set_leverage(%s) lanzó excepción: %s", leverage, e)
            return None
        if isinstance(result, BinanceAPIException):
            logger.error("ExecutionEngine: set_leverage(%s) devolvió error: %s", leverage, result)
            return None
        logger.info("ExecutionEngine: leverage de %s seteado a %s", self.symbol, leverage)
        return result

    # ── Fills ────────────────────────────────────────────────────────────
    def process_fills_from_trades(self, trades: list):
        """Marca órdenes como filled cuando un trade coincide con su precio/qty.

        Para dry-run/testing. En producción el market_maker usa los trades del
        WS y llama a este método (o a simulate_fill) para sus propias órdenes.
        Devuelve la lista de client_order_id llenadas.
        """
        tick = (self.symbol_info or {}).get("tick_size") or 1e-9
        tol = tick / 2.0
        filled = []
        with self._lock:
            for oid, info in list(self.open_orders.items()):
                for t in trades:
                    tprice = float(t.get("price"))
                    tqty = float(t.get("qty"))
                    is_buyer_maker = bool(t.get("is_buyer_maker", False))
                    if abs(tprice - info["price"]) > tol:
                        continue
                    # Semántica Binance de is_buyer_maker (ver market_state.update_trade):
                    #   True  → el buyer era el maker, el SELLER agredió → llena nuestro BUY resting
                    #   False → el BUYER agredió → llena nuestro SELL resting
                    if info["side"] == "BUY" and not is_buyer_maker:
                        continue
                    if info["side"] == "SELL" and is_buyer_maker:
                        continue
                    self._register_fill_locked(oid, info, tprice, tqty)
                    filled.append(oid)
                    break
        return filled

    def simulate_fill(self, order_id):
        """Marca la orden order_id como filled, la saca de open_orders y la
        agrega a self.fills. Devuelve True si la orden existía."""
        with self._lock:
            info = self.open_orders.get(order_id)
            if info is None:
                return False
            self._register_fill_locked(order_id, info, info["price"], info["qty"],
                                       source="simulated")
        return True

    def _register_fill_locked(self, order_id, info, fill_price, fill_qty,
                              source="trades"):
        """Registra un fill y purga la orden (asume self._lock tomado).

        source indica el origen del fill para el audit trail (§15):
        "trades" (WS/trades), "simulated" (dry-run) o "refresh" (detectado
        por refresh_open_orders en modo real).
        """
        fill = {
            "client_order_id": order_id,
            "side": info["side"],
            "order_price": info["price"],
            "order_qty": info["qty"],
            "fill_price": fill_price,
            "fill_qty": fill_qty,
            "ts": time.time(),
            "status": "FILLED",
            "simulated": self.dry_run,
            "source": source,
        }
        self.fills.append(fill)
        self._remove_order(order_id)
        self._log_fill_event(fill)
        logger.info("ExecutionEngine: fill de %s %s @ %.8g x %.6g",
                    fill["side"], order_id, fill_price, fill_qty)
