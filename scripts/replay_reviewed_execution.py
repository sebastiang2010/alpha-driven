"""Offline quote-journal replay, gated by an independent review attestation.

This does not generate A-S quotes, read secrets, or connect to an exchange.
Read md/RECONSTRUCTION_REVIEW_REQUEST.md before considering a dataset run.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from decimal import Decimal
import hashlib
import heapq
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.chronological_execution import Event, PRIORITY, Replay, Settings, integer

REVIEW_FILES = (
    'research/chronological_execution.py', 'scripts/replay_reviewed_execution.py',
    'strategy/execution_reconstruction.py', 'tests/test_chronological_execution.py',
)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_review(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('decision') != 'APPROVED' or data.get('blocking_findings') != []:
        raise ValueError('Independent review pending or blocking findings unresolved')
    if not data.get('reviewer') or not data.get('review_report'):
        raise ValueError('Reviewer and report required')
    for relative in REVIEW_FILES:
        if data.get('sha256', {}).get(relative) != digest(ROOT / relative):
            raise ValueError(f'Review hash missing or stale: {relative}')
    return data


def units(value, quantum):
    value, quantum = Decimal(str(value)), Decimal(str(quantum))
    if not value.is_finite() or not quantum.is_finite() or value <= 0 or quantum <= 0:
        raise ValueError('Invalid price/quantity/quantum')
    n = value / quantum
    if n != n.to_integral_value():
        raise ValueError('Off-grid price/quantity; no silent rounding')
    n = int(n)
    integer(n, positive=True)
    return n


def checked(events):
    last = (-1, -1)
    for e in events:
        key = e.ts_ms, PRIORITY[e.kind]
        if key < last:
            raise ValueError('Unsorted source stream; do not silently reorder')
        last = key
        yield e


def books(path, tick, step):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        for r in csv.DictReader(stream):
            def levels(col):
                return tuple((units(p, tick), units(q, step)) for p, q in (x.split(':') for x in r[col].split(';')))
            yield Event(int(r['ts_ms']), 'book', dict(bids=levels('bid_levels'), asks=levels('ask_levels'),
                        update_id=int(r['update_id']), pu=int(r['pu'])))


def trades(path, tick, step):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        for r in csv.DictReader(stream):
            value = r['is_buyer_maker'].lower()
            if value not in ('true', 'false') or r.get('aggressor_source') != 'OBSERVED':
                raise ValueError('Missing observed aggressor side')
            yield Event(int(r['ts_ms']), 'trade', dict(price_ticks=units(r['price'], tick),
                        qty_lots=units(r['qty'], step), trade_id=r['trade_id'], is_buyer_maker=value == 'true'))


def commands(path, tick, step):
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            r = json.loads(line)
            kind = r['kind']
            if kind not in ('submit', 'cancel'):
                raise ValueError('Unknown command')
            data = {'order_id': r['order_id']}
            if kind == 'submit':
                data.update(side=r['side'], price_ticks=units(r['price'], tick), qty_lots=units(r['qty'], step))
            yield Event(r['ts_ms'], kind, data)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ('depth', 'trades', 'commands', 'settings', 'review', 'out'):
        ap.add_argument('--'+name, type=Path, required=True)
    args = ap.parse_args()
    review = check_review(args.review)  # MUST precede loading any dataset.
    cfg = json.loads(args.settings.read_text(encoding='utf-8'))
    if not cfg.get('symbol') or not cfg.get('specs_source') or not cfg.get('quote_source'):
        raise ValueError('Explicit symbol, instrument-spec provenance and causal quote source required')
    tick, step = cfg['tick_size'], cfg['qty_step']
    units(tick, tick)
    units(step, step)
    replay = Replay(Settings(**cfg['execution']))
    sources = [args.depth, args.trades, args.commands, args.settings]
    if args.depth.name != f"{cfg['symbol']}_depth.csv" or args.trades.name != f"{cfg['symbol']}_trades.csv":
        raise ValueError('Capture file names do not match configured instrument')
    inputs = {str(p.resolve()): digest(p) for p in sources}
    if args.out.exists():
        raise FileExistsError('Output must be a new directory')
    replay.run(heapq.merge(checked(books(args.depth, tick, step)), checked(trades(args.trades, tick, step)),
                          checked(commands(args.commands, tick, step)), key=lambda e: (e.ts_ms, PRIORITY[e.kind])))
    if not replay.books:
        raise ValueError('No books')
    # Detect inputs changed during replay (e.g. a live capture); no valid report.
    if any(digest(Path(p)) != h for p, h in inputs.items()):
        raise ValueError('Inputs changed during analysis')
    args.out.mkdir(parents=True, exist_ok=False)
    for name, rows in [('fills', replay.fills), ('events', replay.journal),
                       ('orders', list(replay.orders.values())), ('markouts', replay.markouts())]:
        with (args.out / f'{name}.jsonl').open('w', encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row, allow_nan=False)+'\n')
    gross_ticks_lots = Decimal(replay.cash) + Decimal(replay.inventory)*Decimal(str(replay.books[-1]['mid_ticks']))
    result = dict(mode='offline_exact_price_reconstruction', inputs=inputs, settings=cfg, review=review,
                  execution=asdict(replay.settings), fill_events=len(replay.fills),
                  inventory_lots=replay.inventory,
                  gross_equity_quote_currency=str(gross_ticks_lots*Decimal(str(tick))*Decimal(str(step))),
                  equity_mark_ts_ms=replay.books[-1]['ts_ms'],
                  net_pnl=None, note='Gross mark-to-mid only; no fees/funding/exit costs; no A-S quote generation.')
    (args.out / 'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.out), 'fill_events': len(replay.fills)}))


if __name__ == '__main__':
    main()
