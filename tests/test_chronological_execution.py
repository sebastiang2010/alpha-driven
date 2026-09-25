"""Only synthetic books/trades; never open project capture files or use a network."""
import json

import pytest

from research.chronological_execution import CoverageError, Event, PRIORITY, Replay, Settings
from scripts import replay_reviewed_execution as cli


def book(t=0, bid=100, ask=102, qty=10, u=1, pu=0):
    return Event(t, 'book', dict(bids=((bid, qty),), asks=((ask, qty),), update_id=u, pu=pu))


def submit(t=0, oid='b', side='BUY', price=100, qty=5):
    return Event(t, 'submit', dict(order_id=oid, side=side, price_ticks=price, qty_lots=qty))


def trade(t, qty, price=100, maker=True, tid=None):
    return Event(t, 'trade', dict(trade_id=tid or str(t), price_ticks=price, qty_lots=qty, is_buyer_maker=maker))


def cancel(t, oid='b'):
    return Event(t, 'cancel', {'order_id': oid})


def run(*events, cap=100):
    return Replay(Settings(10, 5, 1000, 2000, cap)).run(sorted(events, key=lambda e: (e.ts_ms, PRIORITY[e.kind])))


def test_public_volume_excludes_shadow_order_and_fill_is_partial():
    r = run(book(), submit(), trade(11, 12))
    assert r.orders['b']['initial_queue_lots'] == 10
    assert [f['qty_lots'] for f in r.fills] == [2]
    assert r.orders['b']['remaining_lots'] == 3
    assert r.inventory == 2 and r.cash == -200


def test_trades_before_or_at_arrival_cannot_fill():
    r = run(book(), submit(), trade(9, 99), trade(10, 99), trade(11, 11))
    assert [(f['ts_ms'], f['qty_lots']) for f in r.fills] == [(11, 1)]


def test_cancel_latency_tie_and_repeat_do_not_extend_window():
    r = run(book(), submit(price=101), cancel(11), cancel(12),
            trade(15, 1, 101), trade(16, 1, 101), trade(17, 99, 101))
    assert [f['ts_ms'] for f in r.fills] == [15, 16]
    assert r.orders['b']['status'] == 'cancelled'
    assert r.orders['b']['remaining_lots'] == 3


def test_cancel_pending_order_before_arrival():
    r = run(book(), submit(), cancel(1), trade(11, 100))
    assert not r.fills
    assert r.orders['b']['status'] == 'cancelled'


def test_cancel_tying_last_trade_is_processed_before_end_censoring():
    r = run(book(), submit(price=101), cancel(11), trade(16, 1, 101))
    assert len(r.fills) == 1
    assert r.orders['b']['status'] == 'cancelled'


def test_cancel_effective_at_arrival_prevents_activation():
    r = run(book(), submit(), cancel(5), trade(11, 100))
    assert not r.fills and r.orders['b']['status'] == 'cancelled'


def test_post_only_rechecked_at_arrival():
    r = run(book(), submit(), book(5, 99, 100, u=2, pu=1), trade(11, 100))
    assert not r.fills
    assert r.orders['b']['status'] == 'rejected_post_only'


def test_order_outside_observed_depth_is_not_zero_queue():
    r = run(book(), submit(price=99), trade(11, 100, 99))
    assert not r.fills and r.orders['b']['status'] == 'rejected_unknown_depth'


def test_wrong_aggressor_does_not_consume_queue():
    r = run(book(), submit(), trade(11, 50, maker=False), trade(12, 11))
    assert [(f['ts_ms'], f['qty_lots']) for f in r.fills] == [(12, 1)]


def test_sell_side_and_actual_trade_timestamp():
    r = run(book(), submit(side='SELL', price=102), trade(11, 30, 102, True), trade(12, 12, 102, False))
    assert len(r.fills) == 1 and r.fills[0]['ts_ms'] == 12
    assert r.inventory == -2 and r.cash == 204


def test_queue_not_decremented_twice_by_depth_and_trade():
    r = run(book(), submit(), trade(11, 6), book(12, qty=4, u=2, pu=1), trade(13, 5))
    assert sum(f['qty_lots'] for f in r.fills) == 1


def test_new_depth_volume_does_not_jump_ahead():
    r = run(book(), submit(), book(11, qty=100, u=2, pu=1), trade(12, 11))
    assert sum(f['qty_lots'] for f in r.fills) == 1


def test_fifo_partials_conserve_total_volume_and_remaining_size():
    r = run(book(), submit(), trade(11, 12), trade(12, 2), trade(13, 100), trade(14, 100))
    assert [f['qty_lots'] for f in r.fills] == [2, 2, 1]
    assert r.orders['b']['status'] == 'filled'
    assert r.inventory == 5


def test_one_side_slot_prevents_reusing_trade_volume():
    r = run(book(), submit(), submit(1, oid='b2'), trade(11, 100))
    assert r.orders['b2']['status'] == 'rejected_side_busy'
    assert sum(f['qty_lots'] for f in r.fills) == 5


def test_pending_orders_reserve_exposure_without_opposite_side_netting():
    r = run(book(), submit(), submit(1, oid='s', side='SELL', price=102),
            trade(11, 100), submit(12, oid='b2'), cap=5)
    assert r.orders['b2']['status'] == 'rejected_position_cap'


def test_no_cancel_reset_of_queue_during_replace():
    r = run(book(), submit(), cancel(11), submit(12, oid='early'),
            submit(17, oid='later'), trade(28, 11))
    assert r.orders['early']['status'] == 'rejected_side_busy'
    assert r.fills[0]['order_id'] == 'later'
    assert r.fills[0]['qty_lots'] == 1


def test_trade_through_is_counted_as_unmodelled_not_a_free_fill():
    r = run(book(), submit(), trade(11, 100, 99))
    assert not r.fills
    assert any(e['event'] == 'unmodelled_trade_through' for e in r.journal)


@pytest.mark.parametrize('end', [book(20, u=3, pu=99), book(2001, u=2, pu=1)])
def test_sequence_or_time_gaps_abort_instead_of_resetting_inventory(end):
    with pytest.raises(CoverageError):
        run(book(), submit(), trade(11, 11), end)


def test_stale_book_never_fills_and_invalid_run_cannot_report_markouts():
    r = Replay(Settings(10, 5, 1000, 2000, 100))
    with pytest.raises(CoverageError):
        r.run([book(), submit(), trade(1001, 100)])
    with pytest.raises(ValueError, match='completed'):
        r.markouts()


@pytest.mark.parametrize('events', [
    [book(), submit(), trade(11, 12, tid='dup'), trade(12, 12, tid='dup')],
    [book(), submit(), submit(1)],
    [book(), cancel(1, 'unknown')],
])
def test_invalid_ids_fail_loudly(events):
    with pytest.raises(ValueError):
        run(*events)


def test_bad_time_order_is_not_silently_sorted_in_engine():
    with pytest.raises(ValueError, match='Out-of-order'):
        Replay(Settings(10, 5, 1000, 2000, 100)).run([book(10), trade(9, 1)])


def test_end_is_censored_and_does_not_advance_unobserved_timers():
    r = run(book(), submit())
    assert not r.fills and r.orders['b']['status'] == 'pending'
    assert r.journal[-1]['event'] == 'end_censored'


def test_markouts_anchor_to_fill_and_decompose_once():
    r = run(book(), submit(), trade(11, 11), book(1000, 105, 107, u=2, pu=1),
            book(1011, 109, 111, u=3, pu=2))
    rows = r.markouts((1000, 5000), tolerance_ms=0)
    assert rows[0]['future_ts_ms'] == 1011
    assert rows[0]['markout_bps'] == 1000
    assert rows[0]['markout_bps'] == rows[0]['spread_bps'] - rows[0]['adverse_move_bps']
    assert rows[1]['markout_bps'] is None and rows[1]['reason'] == 'end_of_data'


def test_future_prices_do_not_change_earlier_fills():
    prefix = [book(), submit(), trade(11, 12)]
    a = run(*prefix, book(100, 95, 97, u=2, pu=1))
    b = run(*prefix, book(100, 105, 107, u=2, pu=1))
    assert a.fills == b.fills


def test_markout_late_observation_is_missing_not_interpolated():
    r = run(book(), submit(), trade(11, 11), book(1500, u=2, pu=1))
    assert r.markouts((1000,), tolerance_ms=100)[0]['reason'] == 'late_book'


def test_symmetric_accounting_without_double_spread_credit():
    r = run(book(), submit(), submit(1, oid='s', side='SELL', price=102),
            trade(11, 15), trade(12, 15, 102, False))
    assert r.inventory == 0 and r.cash == 10


@pytest.mark.parametrize('value,quantum', [('NaN', '.1'), ('-1', '.1'), ('1.01', '.1'), ('1', '0')])
def test_invalid_units_rejected(value, quantum):
    with pytest.raises(ValueError):
        cli.units(value, quantum)


def test_decimal_grid_and_large_exchange_update_ids():
    assert cli.units('1.5367', '0.0001') == 15367
    r = run(book(u=11648678078321, pu=11648678060265))
    assert r.book['update_id'] == 11648678078321


def test_review_gate_precedes_any_capture_read(tmp_path, monkeypatch):
    review = tmp_path / 'review.json'
    review.write_text(json.dumps({'decision': 'PENDING'}))
    argv = ['replay']
    for key in ('depth', 'trades', 'commands', 'settings', 'out'):
        argv += ['--'+key, str(tmp_path / ('missing_'+key))]
    argv += ['--review', str(review)]
    monkeypatch.setattr('sys.argv', argv)
    with pytest.raises(ValueError, match='Independent review pending'):
        cli.main()
    assert not (tmp_path / 'missing_out').exists()


def test_review_rejects_stale_code_hash(tmp_path):
    p = tmp_path / 'review.json'
    p.write_text(json.dumps({'decision': 'APPROVED', 'blocking_findings': [],
                             'reviewer': 'synthetic-test', 'review_report': 'synthetic', 'sha256': {}}))
    with pytest.raises(ValueError, match='hash missing or stale'):
        cli.check_review(p)


@pytest.mark.parametrize('delay', [0, -1, 1.5, True])
def test_invalid_latency_is_rejected(delay):
    with pytest.raises(ValueError):
        Settings(delay, 5, 1000, 2000, 100)


def test_cli_entire_pipeline_on_synthetic_files_only(tmp_path, monkeypatch):
    depth = tmp_path / 'TEST_depth.csv'
    depth.write_text('ts_ms,update_id,pu,bid_levels,ask_levels\n0,1,0,1.00:10,1.02:10\n100,2,1,1.02:10,1.04:10\n')
    trades = tmp_path / 'TEST_trades.csv'
    trades.write_text('ts_ms,price,qty,is_buyer_maker,aggressor_source,trade_id\n11,1.00,12,True,OBSERVED,1\n')
    commands = tmp_path / 'commands.jsonl'
    commands.write_text(json.dumps(dict(ts_ms=0, kind='submit', order_id='b', side='BUY', price='1.00', qty='5'))+'\n')
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps(dict(symbol='TEST', specs_source='synthetic fixture', quote_source='synthetic fixture',
        tick_size='.01', qty_step='1', execution=dict(place_ms=10, cancel_ms=5, max_book_age_ms=1000, max_gap_ms=2000, max_position_lots=100))))
    review = tmp_path / 'synthetic_review.json'
    # Test fixture only. Does not approve the implementation or touch the real gate.
    review.write_text(json.dumps(dict(decision='APPROVED', reviewer='synthetic fixture', review_report='synthetic fixture',
        blocking_findings=[], sha256={p: cli.digest(cli.ROOT / p) for p in cli.REVIEW_FILES})))
    out = tmp_path / 'result'
    argv = ['replay']
    for key, path in dict(depth=depth, trades=trades, commands=commands, settings=settings, review=review, out=out).items():
        argv += ['--'+key, str(path)]
    monkeypatch.setattr('sys.argv', argv)
    cli.main()
    result = json.loads((out / 'summary.json').read_text())
    assert result['fill_events'] == 1 and result['inventory_lots'] == 2
    assert result['gross_equity_quote_currency'] == '0.060'
    assert result['net_pnl'] is None
    assert json.loads((out / 'fills.jsonl').read_text())['ts_ms'] == 11
