"""Closed partial ledgers recover as a verified chain, never as isolated days."""
import asyncio
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

import app.quant.cli.recover_partial_days as recovery
from app.repositories.quant_daily_result_repository import QuantDailyResultRepository
from tests.api.conftest import FakeCollection, FakeCursor, matches, project


class Cursor(FakeCursor):
    def sort(self, field, direction=None):
        return super().sort([(field, direction)] if direction is not None else field)

    def __aiter__(self):
        self.iterator = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration


class Collection(FakeCollection):
    def __init__(self, database, name, rows=None):
        super().__init__(deepcopy(rows))
        self.database, self.name = database, name

    def find(self, filters=None, projection=None):
        return Cursor([project(r, projection) for r in self.rows if matches(r, filters or {})])

    async def distinct(self, field, filters):
        return sorted({r[field] for r in self.rows if matches(r, filters)})

    async def insert_one(self, row):
        self.rows.append(deepcopy(row))

    async def replace_one(self, filters, row, upsert=False):
        for index, old in enumerate(self.rows):
            if matches(old, filters):
                self.rows[index] = {**deepcopy(row), '_id': old['_id']}
                return
        self.rows.append({'_id': row['trade_date'], **deepcopy(row)})

    async def delete_many(self, filters):
        self.rows = [r for r in self.rows if not matches(r, filters)]

    async def rename(self, target, dropTarget):
        assert dropTarget
        self.database.published += 1
        self.database.collections[target] = self


class Database:
    def __init__(self, rows):
        self.published = 0
        self.collections = {}
        self.collections[recovery.COLLECTION] = Collection(self, recovery.COLLECTION, rows)

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = Collection(self, name)
        return self.collections[name]


def document(day, *, partial=True, balance=10, strategy=recovery.STRATEGY_ID):
    return {'_id': day + strategy, 'trade_date': day, 'strategy_id': strategy, 'status': 'closed',
            'runtime': {'version': 100, 'incomplete_code_count': int(partial),
                        'incomplete_codes': ['000001'] if partial else []},
            '_runtime_state': {'snapshot_key': 'old', 'opening_flow': {'balance': balance - 1}},
            'balance': balance, 'recording': {'mode': 'live'}}


class Service:
    bars = {}
    processed = []

    def __init__(self, database):
        self.results = QuantDailyResultRepository(database)

    async def _load_three_minute_bars(self, *, trade_date, codes):
        return self.bars.get(trade_date, {})

    async def _require_daily_sync_ready(self, day): pass
    async def ensure_indexes(self): pass

    async def process(self, *, now):
        day = now.date().isoformat()
        self.processed.append(day)
        previous = await self.results.latest_before(day)
        balance = previous['balance'] if previous else 0
        current = await self.results.get(day)
        if current is None:
            current = document(day)
            current['_runtime_state']['opening_flow']['balance'] = balance
        assert current['_runtime_state']['opening_flow']['balance'] == balance
        current['balance'] = balance + 100
        current['status'] = 'closed'
        current['runtime'].update(incomplete_codes=[], incomplete_code_count=0, version=2)
        return current


@pytest.fixture
def setup_recovery(monkeypatch):
    monkeypatch.setattr(recovery, 'QuantLiveService', Service)
    monkeypatch.setattr(Service, 'processed', [])
    monkeypatch.setattr(Service, 'bars', {'2026-11-03': {'000001': [SimpleNamespace(previous_close=10)] * 80}})
    return Database([document('2026-11-02', partial=False), document('2026-11-03', balance=11),
                     document('2026-11-04', partial=False, balance=12),
                     document('2026-11-04', strategy='unrelated')])


@pytest.mark.parametrize('apply', [False, True])
def test_recovery_rebuilds_later_complete_day_and_publishes_only_validated_chain(
        setup_recovery, monkeypatch, tmp_path, apply):
    database = setup_recovery
    source_before = deepcopy(database[recovery.COLLECTION].rows)
    validated = []

    async def validate(service, current, previous):
        assert database[recovery.COLLECTION].rows == source_before
        assert current['_runtime_state']['opening_flow']['balance'] == previous['balance']
        assert current['runtime']['version'] == 101
        assert current['recording']['mode'] == 'historical_replay'
        validated.append(current['trade_date'])
        return {'incomplete_codes': []}

    monkeypatch.setattr(recovery, 'validate_replay', validate)
    output = tmp_path / 'audit'
    result = asyncio.run(recovery.recover(database, start='2026-11-03', end='2026-11-04',
                                          output=output, apply=apply))
    assert validated == Service.processed == ['2026-11-03', '2026-11-04']
    assert result['applied'] == apply
    assert database.published == int(apply)
    assert (output / '2026-11-03_before.json.gz').exists()
    assert (output / '2026-11-04_after.json.gz').exists()
    rows = database[recovery.COLLECTION].rows
    if apply:
        assert next(r for r in rows if r['trade_date'] == '2026-11-03')['balance'] == 110
        assert next(r for r in rows if r['trade_date'] == '2026-11-04' and
                    r['strategy_id'] == recovery.STRATEGY_ID)['balance'] == 210
        assert rows[0] == source_before[0]
        assert next(r for r in rows if r['strategy_id'] == 'unrelated') == source_before[-1]
        again = asyncio.run(recovery.recover(database, start='2026-11-03', end='2026-11-04',
                                             output=tmp_path / 'again', apply=True))
        assert again['status'] == 'unchanged' and database.published == 1
    else:
        assert rows == source_before


@pytest.mark.parametrize('failure', ['validation', 'concurrent_write'])
def test_recovery_failure_never_publishes(setup_recovery, monkeypatch, tmp_path, failure):
    database = setup_recovery

    async def validate(service, current, previous):
        if failure == 'validation':
            raise AssertionError('inconsistent ledger')
        database[recovery.COLLECTION].rows[0]['balance'] = 999
        return {'incomplete_codes': []}

    monkeypatch.setattr(recovery, 'validate_replay', validate)
    with pytest.raises((AssertionError, RuntimeError), match='inconsistent ledger|changed during recovery'):
        asyncio.run(recovery.recover(database, start='2026-11-03', end='2026-11-04',
                                     output=tmp_path / 'failed', apply=True))
    assert database.published == 0
    assert database[recovery.COLLECTION].rows[1]['balance'] == 11


@pytest.mark.parametrize('bars', [[], [SimpleNamespace(previous_close=10)] * 40,
                                [SimpleNamespace(previous_close=None)] * 80])
def test_still_missing_or_suspended_sources_do_not_trigger_recovery(setup_recovery, monkeypatch, tmp_path, bars):
    monkeypatch.setattr(Service, 'bars', {'2026-11-03': {'000001': bars}})
    result = asyncio.run(recovery.recover(setup_recovery, start='2026-11-03', end='2026-11-04',
                                          output=tmp_path / 'unused', apply=True))
    assert result['status'] == 'unchanged'
    assert not Service.processed and not (tmp_path / 'unused').exists()


def test_open_following_ledger_defers_history_replacement(setup_recovery, tmp_path):
    setup_recovery[recovery.COLLECTION].rows[2]['status'] = 'monitoring'
    result = asyncio.run(recovery.recover(setup_recovery, start='2026-11-03', end='2026-11-04',
                                          output=tmp_path / 'unused', apply=True))
    assert result['reason'] == 'newer_or_open_ledger_exists'
    assert not Service.processed


def test_recovery_can_start_at_validated_inception(setup_recovery, monkeypatch, tmp_path):
    first = document('2026-08-20', balance=1)
    first['recording']['start_date'] = '2026-08-20'
    first['strategy'] = {'recording_start_date': '2026-08-20'}
    accounts = [{'code': '000001', 'cash': 100000, 'initial_cash': 100000}]
    first['_runtime_state']['opening_flow'].update(accounts=accounts, holdings=[])
    first['_runtime_state']['accounts'] = accounts
    first['holding_pool'] = {'items': []}
    first['summary'] = dict(account_count=0, initial_capital=0, cash_balance=0,
                            market_value=0, total_assets=0)
    database = Database([first, document('2026-08-21', balance=2)])
    monkeypatch.setattr(Service, 'bars', {
        '2026-08-20': {'000001': [SimpleNamespace(previous_close=10)] * 80}})
    seen_previous = []

    async def validate(service, current, previous):
        seen_previous.append(previous['trade_date'] if previous else None)
        return {'incomplete_codes': []}

    monkeypatch.setattr(recovery, 'validate_replay', validate)
    result = asyncio.run(recovery.recover(database, start='2026-08-20', end='2026-08-21',
                                          output=tmp_path / 'inception', apply=True))
    assert result['applied']
    assert seen_previous == [None, '2026-08-20']


def test_missing_preceding_ledger_is_rejected_outside_inception(setup_recovery, tmp_path):
    database = Database([document('2026-11-03')])
    with pytest.raises(AssertionError):
        asyncio.run(recovery.recover(database, start='2026-11-03', end='2026-11-03',
                                    output=tmp_path / 'invalid', apply=True))
    assert database.published == 0
    assert not (tmp_path / 'invalid').exists()
