import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.crawlers.fund_flow_crawler import FundFlowCrawler, parse_fund_flow
from app.crawlers.realtime_market_crawler import CN_TZ
from app.services.fund_flow_service import in_fund_flow_session, closed_fund_flow_day
from app.scheduler.fund_flow_jobs import register_fund_flow_jobs
from apscheduler.schedulers.asyncio import AsyncIOScheduler

NOW = datetime(2026, 11, 6, 10, 0, 10, tzinfo=CN_TZ)


def payload(code='600519'):
    # Real 2026-11-06 values; timestamp controlled for temporal tests.
    return {'rc': 0, 'data': {'total': 1, 'diff': [{
        'f12': code, 'f14': '贵州茅台', 'f2': 1453.0, 'f3': 0.07,
        'f62': -318894480.0, 'f184': -5.14, 'f66': -185607584.0, 'f69': -2.99,
        'f72': -133286896.0, 'f75': -2.15, 'f78': 319003136.0, 'f81': 5.15,
        'f84': -108674.0, 'f87': 0.0, 'f124': int(NOW.timestamp()),
    }]}}


def parse(data, now=NOW):
    return parse_fund_flow(data, {'600519'}, day='2026-11-06', observed_at=now)


def test_flow_preserves_yuan_sign_zero_and_distinguishes_quote_clock():
    data = payload()
    row = parse(data)[0]
    assert row['flows']['main'] == {'net_amount_yuan': -318894480, 'net_ratio_pct': -5.14}
    assert row['flows']['small']['net_ratio_pct'] == 0
    assert row['quote_time'] == NOW and row['fund_flow_time'] is None
    assert row['usable'] and row['raw'] == data['data']['diff'][0]


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(rc=1),
    lambda p: p['data'].update(diff=[]),
    lambda p: p['data']['diff'].append(deepcopy(p['data']['diff'][0])),
    lambda p: p['data']['diff'][0].update(f12='000001'),
    lambda p: p['data']['diff'][0].pop('f62'),
    lambda p: p['data']['diff'][0].update(f62='nan'),
    lambda p: p['data']['diff'][0].update(f184=101),
    lambda p: p['data']['diff'][0].update(f62=True),
])
def test_malformed_provider_payload_is_not_accepted(mutate):
    data = payload(); mutate(data)
    with pytest.raises(ValueError): parse(data)


@pytest.mark.parametrize('changes,issue', [
    ({'f62': '-'}, 'missing_metrics'),
    ({'f124': 0}, 'missing_quote_time'),
    ({'f124': int((NOW-timedelta(days=1)).timestamp())}, 'quote_date_mismatch'),
    ({'f124': int((NOW+timedelta(minutes=1)).timestamp())}, 'future_quote_time'),
    ({'f124': int((NOW-timedelta(minutes=6)).timestamp())}, 'stale_quote_time'),
])
def test_unavailable_observations_are_flagged_without_filling_zero(changes, issue):
    data = payload(); data['data']['diff'][0].update(changes)
    row = parse(data)[0]
    assert not row['usable'] and issue in row['quality_issues']
    if issue == 'missing_metrics': assert row['flows']['main']['net_amount_yuan'] is None


def test_post_close_keeps_actual_quote_time():
    row = parse(payload(), now=NOW.replace(hour=23))[0]
    assert row['quote_time'] == NOW and row['observed_at'].hour == 23
    assert row['usable']  # This is a receipt after close, not a backdated 15:00 observation.


@pytest.mark.parametrize('clock,expected', [
    ('09:29:59', False), ('09:30:00', True), ('11:30:05', True),
    ('11:30:06', False), ('12:00:00', False), ('13:00:00', True),
    ('15:00:05', True), ('15:00:06', False), ('23:50:00', False),
])
def test_fund_flow_sampling_session(clock, expected):
    assert in_fund_flow_session(datetime.fromisoformat('2026-11-06T'+clock+'+08:00')) is expected


def test_daily_target_handles_close_weekend_and_restart():
    assert closed_fund_flow_day(NOW) == '2026-11-05'
    assert closed_fund_flow_day(NOW.replace(hour=15, minute=4)) == '2026-11-05'
    assert closed_fund_flow_day(NOW.replace(hour=15, minute=5)) == '2026-11-06'
    assert closed_fund_flow_day(NOW.replace(day=7)) == '2026-11-06'


def test_batch_missing_symbols_is_explicit_partial_and_all_stale_fails():
    async def run():
        crawler = FundFlowCrawler(); await crawler.close()
        crawler.proxy = None
        class Client:
            async def get(self, url, **kwargs):
                assert kwargs['params']['fltt'] == '2'
                assert kwargs['params']['secids'] == '1.600519,0.000001,0.920002'
                return SimpleNamespace(raise_for_status=lambda: None, json=payload)
        crawler.client = Client()
        offset = (NOW-datetime.now(CN_TZ)).total_seconds()
        result = await crawler.fetch('2026-11-06', codes=['600519','000001','920002'], clock_offset=offset)
        assert result['collection_status'] == 'partial' and not result['complete']
        assert result['missing_codes'] == ['000001', '920002']
        assert result['usable_count'] == 1 and result['source_time'] is None
        with pytest.raises(ValueError, match='no_usable'):
            await crawler.fetch('2026-11-09', codes=['600519','000001','920002'], clock_offset=offset)
    asyncio.run(run())


def test_fund_flow_jobs_have_seconds_daily_recovery_and_raw_cleanup():
    scheduler = AsyncIOScheduler(); register_fund_flow_jobs(scheduler)
    jobs = {j.id: j for j in scheduler.get_jobs()}
    job = jobs['stock_fund_flow_5s']
    assert job.trigger.interval.total_seconds() == 5
    assert job.max_instances == 1 and job.coalesce and job.misfire_grace_time == 5
    daily = jobs['stock_fund_flow_daily']
    assert daily.trigger.get_next_fire_time(None, NOW) == NOW.replace(hour=15, minute=5, second=20)
    assert jobs['stock_fund_flow_daily_startup'].kwargs['daily']
    assert jobs['fund_flow_retention'].trigger.interval.total_seconds() == 3600
    assert 'stock_fund_flow_5m' not in jobs


def test_cycle_deadline_keeps_completed_batch_and_releases_cancelled_proxy():
    async def run():
        crawler = FundFlowCrawler(); await crawler.close()
        codes = [str(i).zfill(6) for i in range(1, 1001)]
        class Pool:
            leased = 0
            succeeded = 0
            failed = 0
            cancelled = 0
            async def get_requests_proxies(self):
                self.leased += 1
                return {'https': 'http://127.0.0.1:9000'}
            async def on_success_for(self, proxies):
                self.leased -= 1; self.succeeded += 1
            async def on_failure_for(self, proxies, error):
                self.leased -= 1; self.failed += 1
            async def release_cancelled(self, proxies):
                self.leased -= 1; self.cancelled += 1
        class Client:
            async def get(self, url, **kwargs):
                batch = [s.split('.')[1] for s in kwargs['params']['secids'].split(',')]
                if batch[0] == codes[500]:
                    await asyncio.Event().wait()
                result = {'rc': 0, 'data': {'diff': [payload(c)['data']['diff'][0] for c in batch]}}
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)
        crawler.client = Client(); crawler.proxy = Pool()
        offset = (NOW-datetime.now(CN_TZ)).total_seconds()
        result = await crawler.fetch('2026-11-06', codes=codes, clock_offset=offset,
                                     timeout_seconds=0.2, max_attempts=1)
        assert result['record_count'] == 500 and result['usable_count'] == 500
        assert result['collection_status'] == 'partial'
        assert result['missing_codes'] == codes[500:]
        assert result['batch_errors'][0]['reason'] == 'cycle_deadline'
        assert not crawler.proxy.leased and not crawler.proxy.failed
        assert crawler.proxy.succeeded == crawler.proxy.cancelled == 1
    asyncio.run(run())


def test_next_live_cycle_starts_after_completed_batches():
    async def run():
        crawler = FundFlowCrawler()
        await crawler.close()
        crawler.proxy = None
        codes = [f'{n:06d}' for n in range(1001)]
        first_codes = []
        class Client:
            async def get(self, url, **kwargs):
                batch = [s.split('.')[1] for s in kwargs['params']['secids'].split(',')]
                first_codes.append(batch[0])
                body = {'rc': 0, 'data': {'diff': [payload(code)['data']['diff'][0] for code in batch]}}
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
        crawler.client = Client()
        for _ in range(2):
            result = await crawler.fetch('2026-11-06', codes=codes, require_usable=False,
                clock_offset=(NOW-datetime.now(CN_TZ)).total_seconds(), timeout_seconds=0.10, max_attempts=1)
            assert result['record_count'] == 500 and not result['response_complete']
            await asyncio.sleep(0.25)
        assert first_codes == [codes[0], codes[500]]
    asyncio.run(run())


@pytest.mark.parametrize('status,body,reason', [
    (401, '{"status_code":-1935,"status_msg":"未登陆,请登录后再试"}', 'login_required'),
    (200, '{"status_code":-1935}', 'login_required'),
    (403, '<html>Forbidden</html>', 'access_blocked'),
    (200, '', 'empty_response'),
    (200, '{"status_code":0}', 'response_requires_schema_and_metric_verification'),
])
def test_dde_auth_or_empty_envelope_is_never_reported_as_available(status, body, reason):
    from app.manually_execute_script.probe_stock_capital_sources import classify_dde_response
    result = classify_dde_response(status, body)
    assert not result['available'] and result['reason'] == reason
