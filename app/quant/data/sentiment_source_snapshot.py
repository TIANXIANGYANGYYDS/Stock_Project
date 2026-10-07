"""策略二供应商原始快照；只写显式研究目录，不认证历史可见时间。"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import signal
import socket
import time
from typing import Callable

RAW_FIELDS = 'date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST,pctChg'
BULK_FIELDS = tuple('date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST'.split(','))
METHOD_FIELDS = {
    'query_daily_history_k_AStock': BULK_FIELDS,
    'query_stock_basic': ('code', 'code_name', 'ipoDate', 'outDate', 'type', 'status'),
    'query_trade_dates': ('calendar_date', 'is_trading_day'),
    'query_all_stock': ('code', 'tradeStatus', 'code_name'),
    'query_history_k_data_plus': tuple(RAW_FIELDS.split(',')),
    'query_stock_industry': ('updateDate', 'code', 'code_name', 'industry', 'industryClassification'),
    'query_adjust_factor': ('code', 'dividOperateDate', 'foreAdjustFactor', 'backAdjustFactor', 'adjustFactor'),
    'query_dividend_data': ('code', 'dividPreNoticeDate', 'dividAgmPumDate', 'dividPlanAnnounceDate',
        'dividPlanDate', 'dividRegistDate', 'dividOperateDate', 'dividPayDate', 'dividStockMarketDate',
        'dividCashPsBeforeTax', 'dividCashPsAfterTax', 'dividStocksPs', 'dividCashStock', 'dividReserveToStockPs'),
}
SYMBOL = re.compile(r'(?:sh|sz)\.\d{6}')


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_file(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def mainboard_candidate(symbol: str) -> bool:
    return bool(SYMBOL.fullmatch(symbol)) and (
        symbol.startswith(('sh.600', 'sh.601', 'sh.603', 'sh.605'))
        or symbol.startswith(('sz.000', 'sz.001', 'sz.002', 'sz.003'))
    )


def request_id(request: dict) -> str:
    return sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]


def validate_request(request: dict) -> None:
    if set(request) != {'method', 'params'} or request['method'] not in METHOD_FIELDS:
        raise ValueError('不支持的只读数据请求')
    method, params = request['method'], request['params']
    expected = {
        'query_stock_basic': set(),
        'query_daily_history_k_AStock': {'date'},
        'query_trade_dates': {'start_date', 'end_date'},
        'query_all_stock': {'day'},
        'query_history_k_data_plus': {'code', 'fields', 'start_date', 'end_date', 'frequency', 'adjustflag'},
        'query_stock_industry': {'code', 'date'},
        'query_adjust_factor': {'code', 'start_date', 'end_date'},
        'query_dividend_data': {'code', 'year', 'yearType'},
    }[method]
    if set(params) != expected:
        raise ValueError('请求参数不符合快照契约')
    for key in ('start_date', 'end_date', 'day', 'date'):
        if key in params and date.fromisoformat(params[key]).isoformat() != params[key]:
            raise ValueError('日期必须为ISO格式')
    if 'start_date' in params and params['start_date'] > params['end_date']:
        raise ValueError('请求起止日期逆序')
    if method == 'query_history_k_data_plus' and (
        not mainboard_candidate(params['code']) or params['fields'] != RAW_FIELDS
        or params['adjustflag'] != '3' or params['frequency'] != 'd'
    ):
        raise ValueError('仅采集主板候选日线原始价adjustflag=3')
    if method in ('query_stock_industry', 'query_adjust_factor', 'query_dividend_data'):
        if not (method == 'query_stock_industry' and params['code'] == '') and not mainboard_candidate(params['code']):
            raise ValueError('行业或公司行动请求代码不是主板候选')
    if method == 'query_dividend_data' and (params['yearType'] != 'operate' or not isinstance(params['year'], str)
            or len(params['year']) != 4 or not params['year'].isdigit()):
        raise ValueError('分红必须按明确的除权年份查询')


def collect_result(result, request: dict) -> tuple[list[str], list[list[str]]]:
    """SDK分页/字段/重复验证；传输层另须报告静默断页。"""
    if result.error_code != '0':
        raise RuntimeError('provider_query_error:' + str(result.error_code))
    fields = list(result.fields)
    if len(set(fields)) != len(fields) or set(fields) != set(METHOD_FIELDS[request['method']]):
        raise ValueError('供应商字段发生变化')
    rows, seen = [], set()
    method, params = request['method'], request['params']
    while result.next():
        if result.error_code != '0':
            raise RuntimeError('provider_cursor_error:' + str(result.error_code))
        row = result.get_row_data()
        if len(row) != len(fields) or not all(isinstance(value, str) for value in row):
            raise ValueError('供应商数据列不完整')
        item = dict(zip(fields, row))
        key = item['code'] if method == 'query_daily_history_k_AStock' else (item.get('date') or item.get('calendar_date') or item['code'])
        if method in ('query_adjust_factor', 'query_dividend_data'):
            key = (item['code'], item['dividOperateDate'], item.get('dividPlanDate'))
        if key in seen:
            raise ValueError('供应商返回重复记录')
        seen.add(key)
        if method in ('query_history_k_data_plus', 'query_daily_history_k_AStock'):
            if item['adjustflag'] != '3' or (method == 'query_history_k_data_plus' and item['code'] != params['code']):
                raise ValueError('原始价代码或复权口径不符')
            day = date.fromisoformat(item['date']).isoformat()
            start = params.get('start_date', params.get('date'))
            end = params.get('end_date', params.get('date'))
            if not start <= day <= end:
                raise ValueError('行情越出请求区间')
            if item['tradestatus'] not in ('0', '1', '') or item['isST'] not in ('0', '1', ''):
                raise ValueError('未知的供应商状态编码')
        elif method == 'query_trade_dates':
            day = date.fromisoformat(item['calendar_date']).isoformat()
            if not params['start_date'] <= day <= params['end_date'] or item['is_trading_day'] not in ('0', '1'):
                raise ValueError('供应商日历范围或状态异常')
        elif method == 'query_all_stock' and item['tradeStatus'] not in ('0', '1'):
            raise ValueError('供应商名单状态编码异常')
        elif method == 'query_stock_industry':
            updated = date.fromisoformat(item['updateDate']).isoformat()
            if updated > params['date'] or (params['code'] and item['code'] != params['code']):
                raise ValueError('历史行业返回未来版本或错误证券')
        elif method in ('query_adjust_factor', 'query_dividend_data'):
            operated = date.fromisoformat(item['dividOperateDate']).isoformat()
            if item['code'] != params['code']:
                raise ValueError('公司行动证券不符')
            if method == 'query_adjust_factor' and not params['start_date'] <= operated <= params['end_date']:
                raise ValueError('复权事件超出日期范围')
            if method == 'query_dividend_data' and operated[:4] != params['year']:
                raise ValueError('分红事件不属于请求除权年份')
        rows.append(row)
        if len(rows) > 100_000:
            raise ValueError('单次快照超过行数上限')
    if result.error_code != '0':
        raise RuntimeError('provider_cursor_error:' + str(result.error_code))
    if method == 'query_trade_dates':
        expected = (date.fromisoformat(params['end_date']) - date.fromisoformat(params['start_date'])).days + 1
        if len(rows) != expected:
            raise ValueError('供应商日历缺少自然日')
    if method not in ('query_history_k_data_plus', 'query_adjust_factor', 'query_dividend_data') and not rows:
        if method == 'query_stock_industry' and params['code']:
            return fields, rows
        raise ValueError('参考数据返回空结果')
    return fields, rows


@contextmanager
def request_deadline(seconds: int):
    """Linux独立采集CLI的硬超时，处理SDK遇EOF仍循环的情况。"""
    def expired(signum, frame):
        raise TimeoutError('provider_request_deadline')
    old_handler = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, old_handler)


class BaoStockSource:
    """延迟加载SDK，仅在独立CLI显式进入会话时联网。"""
    def __init__(self, timeout_seconds: int = 30):
        if timeout_seconds <= 0:
            raise ValueError('timeout_seconds必须为正')
        self.timeout_seconds = timeout_seconds

    def __enter__(self):
        import baostock as bs
        self.bs = bs
        self.old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(min(30, self.timeout_seconds))
        try:
            with request_deadline(self.timeout_seconds), redirect_stdout(io.StringIO()):
                result = bs.login()
            if result.error_code != '0':
                raise RuntimeError('provider_login_error:' + str(result.error_code))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        import baostock.common.context as context
        active = getattr(context, 'default_socket', None)
        if active is not None:
            active.close()
            context.default_socket = None
        socket.setdefaulttimeout(self.old_timeout)

    def fetch_with_retries(self, request: dict, attempts: int = 3) -> tuple[list[str], list[list[str]]]:
        """只重试网络中断；数据验证错误和供应商拒绝请求立即失败。"""
        if attempts < 1:
            raise ValueError('attempts必须为正')
        for attempt in range(attempts):
            try:
                if attempt:
                    self.__enter__()
                return self.fetch(request)
            except (OSError, RuntimeError) as exc:
                retryable = isinstance(exc, OSError) or str(exc) in (
                    'provider_transport_incomplete', 'provider_request_deadline',
                )
                if not retryable or attempt + 1 == attempts:
                    raise
                self.__exit__(None, None, None)
                time.sleep(attempt + 1)
        raise AssertionError('unreachable')

    def fetch(self, request: dict) -> tuple[list[str], list[list[str]]]:
        import baostock.util.socketutil as transport
        validate_request(request)
        original = transport.send_msg
        failures = []
        def checked_send(message):
            reply = original(message)
            if reply is None or not reply.strip():
                failures.append('empty_transport_reply')
                # ResultData.next() can otherwise hide a failed final page.
                raise RuntimeError('provider_transport_incomplete')
            return reply
        transport.send_msg = checked_send
        try:
            with request_deadline(self.timeout_seconds), redirect_stdout(io.StringIO()):
                result = getattr(self.bs, request['method'])(**request['params'])
                fields, rows = collect_result(result, request)
            if failures:
                raise RuntimeError('provider_transport_incomplete')
            return fields, rows
        finally:
            transport.send_msg = original


def load_snapshot(output: Path, request: dict) -> dict:
    job = output / 'requests' / request_id(request)
    receipt = json.loads((job / 'receipt.json').read_text(encoding='utf-8'))
    path = job / 'data.json'
    if receipt.get('complete') is not True or digest_file(path) != receipt['sha256']:
        raise ValueError('快照校验失败，拒绝续跑跳过')
    snapshot = json.loads(path.read_text(encoding='utf-8'))
    if snapshot['request'] != request or len(snapshot['rows']) != receipt['rows']:
        raise ValueError('快照与计划不一致')
    return snapshot


def download_plan(requests: list[dict], output: Path, fetch: Callable, *, interval: float = .25,
                  progress: Callable | None = None) -> dict:
    """按不可变请求计划续跑；成功请求有哈希收据，失败即停止。"""
    if not requests or len(requests) > 20_000 or interval < 0:
        raise ValueError('空计划、过大计划或非法间隔')
    for request in requests:
        validate_request(request)
    if len({request_id(item) for item in requests}) != len(requests):
        raise ValueError('请求计划重复')
    plan = {'schema_version': 1, 'requests': requests}
    plan_path = output / 'plan.json'
    if output.exists():
        if not plan_path.is_file() or json.loads(plan_path.read_text(encoding='utf-8')) != plan:
            raise ValueError('输出目录的请求计划不一致')
    else:
        output.mkdir(parents=True)
        write_json(plan_path, plan)
    manifest = {'complete': False, 'status': 'running', 'started_at': timestamp(),
                'plan_sha256': digest_file(plan_path), 'source_code_sha256': digest_file(Path(__file__)),
                'dataset_accepted_for_sentiment': False, 'historical_known_at_verified': False,
                'jobs_total': len(requests), 'jobs_completed': 0, 'rows': 0, 'empty_bar_jobs': 0}
    write_json(output / 'manifest.json', manifest)
    try:
        for index, request in enumerate(requests, 1):
            job = output / 'requests' / request_id(request)
            if (job / 'receipt.json').exists():
                snapshot = load_snapshot(output, request)
            else:
                fields, rows = fetch(request)
                snapshot = {'schema_version': 1, 'provider': 'baostock', 'request': request,
                            'observed_at': timestamp(), 'known_at': None, 'fields': fields, 'rows': rows}
                job.mkdir(parents=True, exist_ok=True)
                write_json(job / 'data.json', snapshot)
                write_json(job / 'receipt.json', {'complete': True, 'sha256': digest_file(job / 'data.json'), 'rows': len(rows)})
                if interval:
                    time.sleep(interval)
            manifest['jobs_completed'] = index
            manifest['rows'] += len(snapshot['rows'])
            manifest['empty_bar_jobs'] += int(not snapshot['rows'])
            if index % 50 == 0 or index == len(requests):
                write_json(output / 'manifest.json', manifest)
                if progress:
                    progress({key: manifest[key] for key in ('jobs_completed', 'jobs_total', 'rows', 'empty_bar_jobs')})
        if manifest['plan_sha256'] != digest_file(plan_path):
            raise ValueError('请求计划在采集中变更')
        manifest.update(complete=True, status='completed', completed_at=timestamp())
        write_json(output / 'manifest.json', manifest)
        return manifest
    except BaseException as exc:
        manifest.update(status='failed', error_type=type(exc).__name__, failed_request=request)
        write_json(output / 'manifest.json', manifest)
        raise
