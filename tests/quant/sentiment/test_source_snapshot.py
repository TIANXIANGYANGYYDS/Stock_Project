from __future__ import annotations

import json

import pytest

from app.quant.data.sentiment_source_snapshot import (
    METHOD_FIELDS, RAW_FIELDS, collect_result, download_plan, load_snapshot,
    mainboard_candidate, request_id, validate_request,
)


def bar_request():
    return {'method': 'query_history_k_data_plus', 'params': {
        'code': 'sh.600001', 'fields': RAW_FIELDS, 'start_date': '2026-09-01',
        'end_date': '2026-09-04', 'frequency': 'd', 'adjustflag': '3',
    }}


class Result:
    def __init__(self, fields, rows, *, final_error='0'):
        self.fields, self.rows = fields, list(rows)
        self.error_code = '0'
        self.final_error = final_error

    def next(self):
        if not self.rows:
            self.error_code = self.final_error
        return bool(self.rows)

    def get_row_data(self):
        return self.rows.pop(0)


def bar_result(**changes):
    values = dict(date='2026-09-01', code='sh.600001', open='10', high='11', low='9', close='10',
                  preclose='10', volume='100', amount='1000', adjustflag='3', tradestatus='1', isST='0', pctChg='0')
    values.update(changes)
    fields = list(METHOD_FIELDS['query_history_k_data_plus'])
    return Result(fields, [[values[key] for key in fields]])


@pytest.mark.parametrize('symbol,expected', [('sh.600001',True),('sz.000001',True),('sz.003001',True),
                                           ('sh.000001',False),('sh.688001',False),('sz.300001',False),('bad',False)])
def test_mainboard_prefix_does_not_select_indices_or_other_boards(symbol, expected):
    assert mainboard_candidate(symbol) is expected


def test_raw_only_request_and_response():
    request = bar_request()
    validate_request(request)
    fields, rows = collect_result(bar_result(), request)
    assert rows[0][fields.index('adjustflag')] == '3'
    request['params']['adjustflag'] = '2'
    with pytest.raises(ValueError, match='adjustflag=3'):
        validate_request(request)
    with pytest.raises(ValueError, match='复权'):
        collect_result(bar_result(adjustflag='2'), bar_request())


@pytest.mark.parametrize('changes', [{'code':'sh.600002'},{'date':'2026-09-07'},{'isST':'false'}])
def test_provider_wrong_symbol_date_and_status_are_rejected(changes):
    with pytest.raises(ValueError):
        collect_result(bar_result(**changes), bar_request())


def test_duplicate_partial_page_and_missing_calendar_days_fail():
    result = bar_result()
    result.rows *= 2
    with pytest.raises(ValueError, match='重复'):
        collect_result(result, bar_request())
    result = bar_result()
    result.final_error = 'network_error'
    with pytest.raises(RuntimeError, match='cursor_error'):
        collect_result(result, bar_request())
    request = {'method':'query_trade_dates','params':{'start_date':'2026-09-01','end_date':'2026-09-02'}}
    with pytest.raises(ValueError, match='自然日'):
        collect_result(Result(['calendar_date','is_trading_day'],[['2026-09-01','1']]), request)


def test_resume_verifies_receipt_and_does_not_redownload_completed_request(tmp_path):
    calls = []
    request = bar_request()
    other = bar_request()
    other['params']['code'] = 'sh.600002'
    def fetch(item):
        calls.append(item)
        if item == other:
            raise TimeoutError('test interrupted transport')
        return collect_result(bar_result(), item)
    output = tmp_path / 'download'
    with pytest.raises(TimeoutError):
        download_plan([request, other], output, fetch, interval=0)
    assert json.loads((output/'manifest.json').read_text())['complete'] is False
    assert load_snapshot(output, request)['known_at'] is None
    calls.clear()
    def resumed(item):
        calls.append(item)
        return collect_result(bar_result(code=item['params']['code']), item)
    report = download_plan([request, other], output, resumed, interval=0)
    assert calls == [other]
    assert report['complete'] is True and report['rows'] == 2
    assert report['dataset_accepted_for_sentiment'] is False
    path = output/'requests'/request_id(request)/'data.json'
    path.write_text(path.read_text().replace('"10"','"12"'))
    with pytest.raises(ValueError, match='校验失败'):
        download_plan([request, other], output, resumed, interval=0)


def test_changed_plan_and_empty_reference_not_accepted(tmp_path):
    request = bar_request()
    output = tmp_path/'download'
    download_plan([request],output,lambda item: collect_result(bar_result(),item),interval=0)
    other = bar_request()
    other['params']['end_date'] = '2026-09-03'
    with pytest.raises(ValueError, match='计划不一致'):
        download_plan([other],output,lambda item: None,interval=0)
    with pytest.raises(ValueError, match='空结果'):
        collect_result(Result(METHOD_FIELDS['query_stock_basic'],[]),{'method':'query_stock_basic','params':{}})


def test_empty_bars_preserved_as_empty_not_invented_suspension(tmp_path):
    request = bar_request()
    report = download_plan([request],tmp_path/'download',lambda item: (list(RAW_FIELDS.split(',')),[]),interval=0)
    assert report['empty_bar_jobs'] == 1
    assert load_snapshot(tmp_path/'download',request)['rows'] == []


def test_historical_industry_date_and_symbol_are_checked():
    request = dict(method='query_stock_industry', params=dict(code='', date='2026-08-03'))
    validate_request(request)
    fields = METHOD_FIELDS[request['method']]
    records = [['2026-08-03', 'sh.600000', '浦发银行', 'J66货币金融服务', '证监会行业分类']]
    assert len(collect_result(Result(fields, records), request)[1]) == 1
    with pytest.raises(ValueError, match='未来'):
        collect_result(Result(fields, [['2026-08-04', *records[0][1:]]]), request)
    with pytest.raises(ValueError, match='错误证券'):
        collect_result(Result(fields, records), dict(method=request['method'], params=dict(code='sh.600001', date='2026-08-03')))
    with pytest.raises(ValueError, match='空结果'):
        collect_result(Result(fields, []), request)
    specific = dict(method=request['method'], params=dict(code='sz.001232', date='2025-05-26'))
    assert collect_result(Result(fields, []), specific)[1] == []


def test_adjustment_events_allow_same_stock_different_dates_and_empty_result():
    request = dict(method='query_adjust_factor', params=dict(code='sh.600000', start_date='2025-01-01', end_date='2026-09-08'))
    validate_request(request)
    fields = METHOD_FIELDS[request['method']]
    rows = [['sh.600000', day, '1', '12.3', '1.05'] for day in ('2025-07-16', '2026-07-16')]
    assert len(collect_result(Result(fields, rows), request)[1]) == 2
    assert collect_result(Result(fields, []), request)[1] == []
    with pytest.raises(ValueError, match='重复'):
        collect_result(Result(fields, rows+rows), request)
    with pytest.raises(ValueError, match='超出'):
        collect_result(Result(fields, [['sh.600000', '2026-09-09', '1', '12', '1']]), request)


def test_dividends_preserve_ambiguous_tax_text_and_reject_wrong_year():
    request = dict(method='query_dividend_data', params=dict(code='sh.600000', year='2026', yearType='operate'))
    validate_request(request)
    fields = METHOD_FIELDS[request['method']]
    record = {key: '' for key in fields}
    record.update(code='sh.600000', dividOperateDate='2026-07-16', dividCashPsAfterTax='0.378或0.42')
    _, rows = collect_result(Result(fields, [[record[key] for key in fields]]), request)
    assert rows[0][fields.index('dividCashPsAfterTax')] == '0.378或0.42'
    record['dividOperateDate'] = '2025-07-16'
    with pytest.raises(ValueError, match='年份'):
        collect_result(Result(fields, [[record[key] for key in fields]]), request)
    for year, year_type in [('26', 'operate'), ('2026', 'report'), (2026, 'operate')]:
        with pytest.raises(ValueError):
            validate_request(dict(method=request['method'], params=dict(code='sh.600000', year=year, yearType=year_type)))


def test_import_does_not_load_database_sdk_or_open_network():
    import subprocess
    import sys
    script = '''
import socket, sys
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
from app.quant.cli import download_sentiment_sources
assert 'baostock' not in sys.modules
assert 'pymongo' not in sys.modules
assert 'app.core.config' not in sys.modules
'''
    subprocess.run([sys.executable,'-c',script],check=True)


def test_bulk_raw_rows_use_symbol_key_and_verify_date_basis():
    from app.quant.data.sentiment_source_snapshot import BULK_FIELDS
    request={'method':'query_daily_history_k_AStock','params':{'date':'2026-09-04'}}
    validate_request(request)
    first={key:'' for key in BULK_FIELDS}
    first.update(date='2026-09-04',code='sh.600001',adjustflag='3',tradestatus='1',isST='0')
    second=dict(first,code='sh.600002')
    rows=[[row[key] for key in BULK_FIELDS] for row in (first,second)]
    assert len(collect_result(Result(BULK_FIELDS,rows),request)[1])==2
    second['date']='2026-09-03'
    with pytest.raises(ValueError,match='区间'):
        collect_result(Result(BULK_FIELDS,[[second[key] for key in BULK_FIELDS]]),request)
    with pytest.raises(ValueError,match='重复'):
        collect_result(Result(BULK_FIELDS,[rows[0],rows[0]]),request)


def test_transport_retry_discards_partial_attempt_but_not_validation_errors(monkeypatch):
    from app.quant.data.sentiment_source_snapshot import BaoStockSource
    source=BaoStockSource()
    calls=[]
    def fetch(request):
        calls.append(request)
        if len(calls)==1:raise RuntimeError('provider_transport_incomplete')
        return ['date'],[['2026-09-04']]
    monkeypatch.setattr(source,'fetch',fetch)
    monkeypatch.setattr(source,'__enter__',lambda: source)
    monkeypatch.setattr(source,'__exit__',lambda *args: None)
    monkeypatch.setattr('app.quant.data.sentiment_source_snapshot.time.sleep',lambda seconds: None)
    assert source.fetch_with_retries(bar_request())[1]==[['2026-09-04']]
    assert len(calls)==2
    def invalid(request):raise ValueError('invalid data')
    monkeypatch.setattr(source,'fetch',invalid)
    with pytest.raises(ValueError,match='invalid data'):
        source.fetch_with_retries(bar_request())


def test_sdk_silent_empty_transport_is_not_a_completed_page(monkeypatch):
    from types import SimpleNamespace
    import baostock.util.socketutil as transport
    from app.quant.data.sentiment_source_snapshot import BaoStockSource
    original=lambda message: None
    monkeypatch.setattr(transport,'send_msg',original)
    def query(**params):
        # SDK can otherwise return an empty/short result after this reply.
        transport.send_msg('synthetic request')
        return bar_result()
    source=BaoStockSource()
    source.bs=SimpleNamespace(query_history_k_data_plus=query)
    with pytest.raises(RuntimeError,match='transport_incomplete'):
        source.fetch(bar_request())
    assert transport.send_msg is original
