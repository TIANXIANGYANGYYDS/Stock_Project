"""将已有账户按首次真实模拟买入激活；保留交易路径并原子发布新统计口径。"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

from app.core.config import get_settings
from app.quant.core.execution import money
from app.quant.runtime.daily_flow import IndependentAccount, independent_account_summary
from app.quant.strategies.provisional_daily_macd_3m import STRATEGY_ID

CN_TZ = timezone(timedelta(hours=8))
COLLECTION = 'quant_daily_results'


def rebase_documents(documents: list[dict], *, rebased_at: str) -> list[dict]:
    """必须提供同一起点的完整连续账本；首次买入时点只从成交记录恢复。"""
    if not documents:
        raise ValueError('没有可转换的账户记录')
    ordered = sorted(documents, key=lambda d: d['trade_date'])
    if ordered[0]['trade_date'] != ordered[0]['recording']['start_date']:
        raise ValueError('必须从账户起点恢复，不能只取最近几天')
    first_buys: dict[str, str] = {}
    previous = None
    result = []
    for original in ordered:
        doc = copy.deepcopy(original)
        day = doc['trade_date']
        state = doc['_runtime_state']
        opening = state['opening_flow']
        if previous and doc['selection_date'] != previous['trade_date']:
            raise ValueError('账户交易日链存在缺口')
        if not previous and opening.get('holdings'):
            raise ValueError('起点已有持仓，缺少完整买入记录')
        for account in opening['accounts']:
            account['first_buy_at'] = first_buys.get(account['code'])
        opening_assets = money(
            sum(a['cash'] for a in opening['accounts'] if a['first_buy_at'])
            + sum(money(h['shares'] * h['mark_price']) for h in opening['holdings'])
        )
        if previous:
            if opening['accounts'] != previous['_runtime_state']['accounts']:
                raise ValueError('开盘账户没有完整承接上一日账户')
            if opening_assets != previous['summary']['total_assets']:
                raise ValueError('开盘资产没有承接上一日资产')
        opening['opening_total_assets'] = opening_assets
        for event in sorted(doc['intraday_trading']['items'], key=lambda e: e['execution_at']):
            if event.get('status') == 'filled' and event['action'] == 'buy':
                if event['execution_at'][:10] != day:
                    raise ValueError('成交日期不属于当前快照')
                first_buys.setdefault(event['code'], event['execution_at'])
        for account in state['accounts']:
            account['first_buy_at'] = first_buys.get(account['code'])
        summary = independent_account_summary(
            accounts=[IndependentAccount(**a) for a in state['accounts']],
            holding_items=doc['holding_pool']['items'],
            opening_total_assets=opening_assets, trade_date=day,
        )
        for key in ['total_pnl', 'realized_pnl', 'unrealized_pnl', 'account_day_pnl', 'market_value']:
            if money(summary[key]) != money(original['summary'][key]):
                raise ValueError(f'{day}: 统计转换改变了{key}')
        if summary['account_count'] != len(first_buys):
            raise ValueError('首次买入记录与账户数量不一致')
        doc['summary'].update(summary)
        doc['recording']['accounting_rebased_at'] = rebased_at
        doc['runtime']['version'] = int(doc['runtime'].get('version', 0)) + 1
        for key in ['signals', 'intraday_trading', 'holding_pool', 'closed_trades', 'exit_decisions']:
            if doc[key] != original[key]:
                raise ValueError(f'{day}: 不允许改动交易路径{key}')
        # 仅加参与统计时点，初始资金、现金和累计已实现盈亏必须逐账户原样保留。
        for location in ['accounts', 'opening_flow']:
            before = original['_runtime_state'][location]
            after = state[location]
            if location == 'opening_flow':
                before, after = before['accounts'], after['accounts']
            for a, b in zip(before, after):
                if {k: v for k, v in a.items() if k != 'first_buy_at'} != {k: v for k, v in b.items() if k != 'first_buy_at'}:
                    raise ValueError('账户经济数据发生改变')
        result.append(doc)
        previous = doc
    return result


def digest(documents: list[dict]) -> str:
    return hashlib.sha256(json_util.dumps(documents, sort_keys=True).encode()).hexdigest()


def run(args) -> dict:
    settings = get_settings()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    rebased_at = datetime.now(CN_TZ).isoformat()
    run_id = datetime.now(CN_TZ).strftime('%Y%m%d_%H%M%S_%f')
    with MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10000) as client:
        db = client[settings.mongo_db_name]
        source = db[COLLECTION]
        all_docs = list(source.find().sort([('strategy_id', 1), ('trade_date', 1)]))
        selected = [d for d in all_docs if d['strategy_id'] == STRATEGY_ID]
        updated = rebase_documents(selected, rebased_at=rebased_at)
        for filename, docs in [('before.json.gz', all_docs), ('after.json.gz', updated)]:
            with gzip.open(output / filename, 'wt', encoding='utf-8') as stream:
                stream.write(json_util.dumps(docs, ensure_ascii=False))
        report = {'applied': False, 'trade_days': len(updated), 'source_sha256': digest(all_docs),
                  'accounting_rebased_at': rebased_at, 'latest_summary': updated[-1]['summary']}
        if args.apply:
            backup_name = COLLECTION + '_before_active_scope_' + run_id
            stage_name = COLLECTION + '_active_scope_' + run_id
            backup, stage = db[backup_name], db[stage_name]
            backup.insert_many(copy.deepcopy(all_docs))
            replacements = {d['_id']: d for d in updated}
            staged = [replacements.get(d['_id'], d) for d in all_docs]
            stage.insert_many(copy.deepcopy(staged))
            for index_name, spec in source.index_information().items():
                if index_name == '_id_':
                    continue
                stage.create_index(spec['key'], name=index_name,
                                   **{k: spec[k] for k in ('unique', 'sparse', 'expireAfterSeconds', 'partialFilterExpression', 'collation') if k in spec})
            if digest(list(backup.find().sort([('strategy_id', 1), ('trade_date', 1)]))) != digest(all_docs):
                raise RuntimeError('备份核验失败')
            if digest(list(stage.find().sort([('strategy_id', 1), ('trade_date', 1)]))) != digest(staged):
                raise RuntimeError('待发布数据核验失败')
            current = list(source.find().sort([('strategy_id', 1), ('trade_date', 1)]))
            if digest(current) != digest(all_docs):
                raise RuntimeError('正式记录在核验期间发生变化，禁止覆盖')
            stage.rename(COLLECTION, dropTarget=True)
            if digest(list(db[COLLECTION].find().sort([('strategy_id', 1), ('trade_date', 1)]))) != digest(staged):
                raise RuntimeError('发布后的数据核验失败')
            report.update(applied=True, backup_collection=backup_name)
        (output / 'migration_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    run(parser.parse_args())
