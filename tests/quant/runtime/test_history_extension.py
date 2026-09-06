from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta

from app.quant.cli.extend_live_history import HistoricalReplayService
from app.quant.runtime.live import LiveThreeMinuteBar, three_minute_bar_ends
from app.services.quant_live_service import QuantLiveService


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        async def iterate():
            for row in self.rows:
                yield deepcopy(row)
        return iterate()


class Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection=None):
        def matches(row):
            for key, value in query.items():
                if isinstance(value, dict) and '$in' in value:
                    if row.get(key) not in value['$in']:
                        return False
                elif row.get(key) != value:
                    return False
            return True
        return Cursor([r for r in self.rows if matches(r)])

    async def find_one(self, *args, **kwargs):
        return {'trade_date': '2026-08-19'}


def test_backfill_uses_a_whole_verified_history_day_and_preserves_raw_inputs(monkeypatch):
    day = '2026-08-20'
    raw = [LiveThreeMinuteBar(
        start_at=(datetime.fromisoformat(t)-timedelta(minutes=3)).isoformat(), end_at=t,
        open=10., high=10.1, low=9.9, close=10., previous_close=None)
        for t in three_minute_bar_ends(day)[:20]]

    async def load(self, **kwargs):
        return {'000001': tuple(raw)}

    monkeypatch.setattr(QuantLiveService, '_load_three_minute_bars', load)
    history = [{'code': '000001', 'trade_date': day, 'timestamp': t, 'open': 10., 'high': 10.1,
                'low': 9.9, 'close': 10., 'interval': '3m', 'adjust': 'qfq'}
               for t in three_minute_bar_ends(day)]
    service = HistoricalReplayService.__new__(HistoricalReplayService)
    service.database = {'stock_history_3m_bars_ths_forward_stage': Collection(history)}
    service.daily_collection = Collection([
        {'code': '000001', 'trade_date': '2026-08-19', 'adjust': 'qfq', 'close': 10.},
        {'code': '000001', 'trade_date': day, 'adjust': 'qfq', 'close': 10., 'change_amount': 0., 'pct_chg': 0.},
    ])
    service.minute_collection = Collection([])
    result = asyncio.run(service._load_three_minute_bars(trade_date=day, codes=['000001']))
    assert len(result['000001']) == 80
    assert all(bar.previous_close == 10. for bar in result['000001'])
    assert all(bar.previous_close is None for bar in raw)
    assert result['000001'][0].start_at == day+'T09:30:00+08:00'
    assert result['000001'][40].start_at == day+'T13:00:00+08:00'
    audit = service.reference_audit['000001']
    assert audit['bar_source'] == 'validated_historical_3m_from_1m'
    assert audit['method'] == 'validated_daily_change_reference'
