"""基础行情与参考数据的原始快照采集 CLI；只向指定目录保存来源。"""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
from pathlib import Path

from app.quant.data.sentiment_source_snapshot import (
    BaoStockSource, RAW_FIELDS, download_plan, validate_request, write_json,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='采集只读供应商原始快照，支持相同计划断点续跑')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--interval', type=float, default=.25)
    sub = parser.add_subparsers(dest='mode', required=True)
    reference = sub.add_parser('reference')
    reference.add_argument('--start-date', required=True)
    reference.add_argument('--end-date', required=True)
    bars = sub.add_parser('bars')
    bars.add_argument('--symbols', type=Path, required=True, help='JSON字符串数组，如["sh.600000"]，不是已认证股票池')
    bars.add_argument('--start-date', required=True)
    bars.add_argument('--end-date', required=True)
    daily = sub.add_parser('daily-bars')
    daily.add_argument('--dates', type=Path, required=True, help='按日采集全A股原始行情，后续再按主板历史名单筛选')
    members = sub.add_parser('membership')
    members.add_argument('--dates', type=Path, required=True, help='需要快照的交易日期JSON数组')
    args = parser.parse_args(argv)
    if args.mode == 'reference':
        requests = [
            {'method': 'query_stock_basic', 'params': {}},
            {'method': 'query_trade_dates', 'params': {'start_date': args.start_date, 'end_date': args.end_date}},
        ]
    elif args.mode == 'bars':
        symbols = json.loads(args.symbols.read_text(encoding='utf-8'))
        if not isinstance(symbols, list) or not all(isinstance(symbol, str) for symbol in symbols):
            raise ValueError('symbols必须为字符串数组')
        requests = [{'method': 'query_history_k_data_plus', 'params': {
            'code': symbol, 'fields': RAW_FIELDS, 'start_date': args.start_date,
            'end_date': args.end_date, 'frequency': 'd', 'adjustflag': '3',
        }} for symbol in symbols]
    else:
        dates = json.loads(args.dates.read_text(encoding='utf-8'))
        if not isinstance(dates, list) or not all(isinstance(day, str) for day in dates):
            raise ValueError('dates必须为字符串数组')
        method, key = ('query_daily_history_k_AStock', 'date') if args.mode == 'daily-bars' else ('query_all_stock', 'day')
        requests = [{'method': method, 'params': {key: day}} for day in dates]
    for request in requests:
        validate_request(request)
    if args.interval < 0 or not requests:
        raise ValueError('采集间隔或空请求计划错误')
    with BaoStockSource(args.timeout) as source:
        report = download_plan(requests, args.output, source.fetch_with_retries, interval=args.interval,
                               progress=lambda row: print(json.dumps(row), flush=True))
    write_json(args.output / 'sdk.json', {'name': 'baostock', 'version': version('baostock')})
    print(json.dumps({'complete': report['complete'], 'jobs': report['jobs_completed'], 'rows': report['rows']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
