# test_offline_coordinator.py — Pruebas sintéticas F1.1: coordinador e interfaz unificados.
# Alcance: compatibilidad de eventos/comandos con el motor real
# (ExecutionReconstructor), conservando todos los eventos y su orden,
# incluidos varios libros con el mismo timestamp. Sin simulaciones de mercado.

import unittest

from strategy.offline_coordinator import (
    Command,
    CoordinatorConfig,
    MarketEvent,
    OfflineCoordinator,
    create_coordinator_from_capture,
    flatten_command,
    flatten_event,
)


def _book(ts_ms, bid, ask, update_id, pu):
    return MarketEvent(ts_ms, 'book', {
        'bids': [[bid, 10]], 'asks': [[ask, 100]],
        'update_id': update_id, 'pu': pu,
    })


def _trade(ts_ms, trade_id, price, qty, buyer_maker):
    return MarketEvent(ts_ms, 'trade', {
        'trade_id': trade_id, 'price_ticks': price,
        'qty_lots': qty, 'is_buyer_maker': buyer_maker,
    })


def _submit(ts_ms, order_id, side, price, qty):
    return Command(ts_ms, 'submit', {
        'order_id': order_id, 'side': side,
        'price_ticks': price, 'qty_lots': qty,
    })


def _test_config(**kw):
    kw.setdefault('cycle_interval_ms', 100)
    kw.setdefault('max_gap_ms', 60000)
    kw.setdefault('max_book_age_ms', 60000)
    kw.setdefault('volatility_window_sec', 0)
    kw.setdefault('alpha_window_sec', 0)
    kw.setdefault('momentum_window_sec', 0)
    return CoordinatorConfig(**kw)


class TestFlattenBoundary(unittest.TestCase):
    """La frontera coordinador→motor habla el protocolo dict del motor."""

    def test_flatten_event_is_engine_dict(self):
        d = flatten_event(_book(1000, 10000, 10001, 1, 0))
        self.assertEqual(d['ts_ms'], 1000)
        self.assertEqual(d['kind'], 'book')
        self.assertEqual(d['bids'], [[10000, 10]])
        self.assertEqual(d['update_id'], 1)
        # Protocolo dict: el motor usa .get() y ['kind']
        self.assertEqual(d.get('ts_ms'), 1000)

    def test_flatten_command_is_engine_dict(self):
        d = flatten_command(_submit(1000, 'o1', 'BUY', 10000, 5))
        self.assertEqual(
            d, {'ts_ms': 1000, 'kind': 'submit', 'order_id': 'o1',
                'side': 'BUY', 'price_ticks': 10000, 'qty_lots': 5})

    def test_flatten_event_rejects_reserved_keys_in_data(self):
        """data con 'kind' (p. ej. submit camuflado) se rechaza, no se resuelve."""
        bad = MarketEvent(1000, 'book', {'kind': 'submit', 'bids': [[10000, 10]],
                                         'asks': [[10001, 100]],
                                         'update_id': 1, 'pu': 0})
        with self.assertRaises(ValueError):
            flatten_event(bad)
        bad_ts = MarketEvent(1000, 'book', {'ts_ms': 2000, 'bids': [[10000, 10]],
                                            'asks': [[10001, 100]],
                                            'update_id': 1, 'pu': 0})
        with self.assertRaises(ValueError):
            flatten_event(bad_ts)

    def test_flatten_command_rejects_reserved_keys_in_data(self):
        """Un 'cancel' con kind=submit en data se rechaza explícitamente."""
        bad = Command(1000, 'cancel', {'kind': 'submit', 'order_id': 'o1'})
        with self.assertRaises(ValueError):
            flatten_command(bad)

    def test_coordinator_rejects_before_touching_engine(self):
        """El rechazo ocurre en la frontera: el motor queda intacto."""
        coord = OfflineCoordinator(_test_config())
        coord.load_events([
            MarketEvent(1000, 'book', {'kind': 'trade', 'bids': [[10000, 10]],
                                       'asks': [[10001, 100]],
                                       'update_id': 1, 'pu': 0}),
        ])
        with self.assertRaises(ValueError):
            coord.advance_to_next_timestamp()
        self.assertEqual(coord.engine.books, [])
        self.assertEqual(coord.engine.fills, [])


class TestCoordinatorEngineCompatibility(unittest.TestCase):
    """Eventos y comandos del coordinador contra el motor real."""

    def test_multiple_books_same_ts_preserved_in_order(self):
        """Varios libros con el mismo ts: se conservan todos y en orden."""
        coord = OfflineCoordinator(_test_config())
        coord.load_events([
            _book(1000, 10000, 10001, 1, 0),
            _book(1000, 10002, 10003, 2, 1),
            _trade(1000, 't1', 10002, 5, True),
        ])
        coord.advance_to_next_timestamp()
        mids = [b['mid_ticks'] for b in coord.engine.books]
        self.assertEqual(mids, [10000.5, 10002.5])

    def test_submit_fill_finish_end_to_end(self):
        """Libro → submit (Command) → arrival → trade fill → finish."""
        coord = OfflineCoordinator(_test_config())
        coord.load_events([
            _book(1000, 10000, 10001, 1, 0),
            _trade(1100, 't1', 10000, 25, True),
        ])
        coord.advance_to_next_timestamp()  # ts=1000: libro
        # BUY 10: qa = visible(10); trade 25 > 10 -> fill 10
        coord.apply_policy_commands(1000, [_submit(1000, 'o1', 'BUY', 10000, 10)])
        coord.advance_to_next_timestamp()  # ts=1100: arrival(1040) + trade
        fills = [f for f in coord.engine.fills if f.order_id == 'o1']
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].qty, 10.0)
        self.assertEqual(coord.engine.inventory_lots, 10)
        result = coord.finish()
        self.assertEqual(result.inventory_lots, 10)

    def test_run_full_with_policy_fn(self):
        """run_full: la política recibe snapshots y sus Command se ejecutan."""
        coord = OfflineCoordinator(_test_config())
        coord.load_events([
            _book(1000, 10000, 10001, 1, 0),
            _book(1100, 10000, 10001, 2, 1),
            _book(1200, 10000, 10001, 3, 2),
            _trade(1300, 't1', 10000, 25, True),
        ])
        seen = []

        def policy(ts_ms, snapshot):
            seen.append(ts_ms)
            if ts_ms == 1200:
                return [_submit(1200, 'o1', 'BUY', 10000, 10)]
            return []

        result = coord.run_full(policy)
        self.assertIn(1200, seen)
        self.assertEqual(result.inventory_lots, 10)
        self.assertEqual(len(result.fills), 1)

    def test_factory_from_capture_rows(self):
        """create_coordinator_from_capture: filas crudas → motor operativo."""
        coord = create_coordinator_from_capture(
            depth_rows=[
                {'ts_ms': 1000, 'bids': [[10000, 10]], 'asks': [[10001, 100]],
                 'update_id': 1, 'pu': 0},
                {'ts_ms': 1100, 'bids': [[10000, 10]], 'asks': [[10001, 100]],
                 'update_id': 2, 'pu': 1},
            ],
            trade_rows=[
                {'ts_ms': 1100, 'trade_id': 't1', 'price_ticks': 10000,
                 'qty_lots': 25, 'is_buyer_maker': True},
            ],
            command_rows=[
                {'ts_ms': 1000, 'kind': 'submit', 'order_id': 'o1',
                 'side': 'BUY', 'price_ticks': 10000, 'qty_lots': 10},
            ],
            coordinator_config=_test_config(),
        )
        coord.advance_to_next_timestamp()
        coord.apply_policy_commands(
            1000, coord._get_commands_for_ts(1000))
        coord.advance_to_next_timestamp()
        fills = [f for f in coord.engine.fills if f.order_id == 'o1']
        self.assertEqual(len(fills), 1)
        self.assertEqual(coord.engine.inventory_lots, 10)


if __name__ == '__main__':
    unittest.main()
