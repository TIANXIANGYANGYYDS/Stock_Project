from __future__ import annotations

import copy

from app.quant.cli.rebase_account_scope import rebase_documents


def legacy_account(code, cash=100000, realized=0):
    return dict(code=code, name=code, initial_cash=100000, cash=cash, realized_pnl=realized)


def test_rebase_uses_past_buys_only_and_keeps_closed_accounts_and_execution_history():
    cold = [legacy_account('000001'), legacy_account('000002')]
    held = [legacy_account('000001', 90000), legacy_account('000002')]
    cleared = [legacy_account('000001', 100500, 500), legacy_account('000002')]
    both = [legacy_account('000001', 100500, 500), legacy_account('000002', 90000)]
    docs = []
    for day, previous, opening, ending, opening_holdings, holdings, event, pnl, realized, unrealized, day_pnl in [
        ('2026-08-20','2026-08-19',cold,held,[],[{'code':'000001','market_value':10500,'unrealized_pnl':500}],
         {'code':'000001','action':'buy'},500,0,500,500),
        ('2026-08-21','2026-08-20',held,cleared,[{'code':'000001','shares':1000,'mark_price':10.5}],[],
         {'code':'000001','action':'sell'},500,500,0,0),
        ('2026-08-24','2026-08-21',cleared,both,[],[{'code':'000002','market_value':9000,'unrealized_pnl':-1000}],
         {'code':'000002','action':'buy'},-500,500,-1000,-1000),
    ]:
        docs.append({'trade_date':day,'selection_date':previous,'recording':{'start_date':'2026-08-20'},
            'runtime':{'version':1}, '_runtime_state':{'accounts':ending,'opening_flow':{
                'accounts':opening,'holdings':opening_holdings,'opening_total_assets':200000}},
            'intraday_trading':{'items':[{**event,'status':'filled','execution_at':day+'T10:00:00+08:00'}]},
            'holding_pool':{'items':holdings}, 'signals':{},'closed_trades':{},'exit_decisions':{},
            'summary':{'total_pnl':pnl,'realized_pnl':realized,'unrealized_pnl':unrealized,
                       'account_day_pnl':day_pnl,'market_value':sum(h['market_value'] for h in holdings)}})
    saved = copy.deepcopy(docs)
    result = rebase_documents(docs, rebased_at='2026-09-06T20:00:00+08:00')
    assert docs == saved
    assert [d['summary']['account_count'] for d in result] == [1, 1, 2]
    assert [d['summary']['capital_inflow'] for d in result] == [100000, 0, 100000]
    assert [d['summary']['opening_total_assets'] for d in result] == [0, 100500, 100500]
    assert result[0]['_runtime_state']['opening_flow']['accounts'][0]['first_buy_at'] is None
    assert result[0]['_runtime_state']['accounts'][1]['first_buy_at'] is None
    assert result[1]['_runtime_state']['accounts'][0]['first_buy_at'].startswith('2026-08-20')
    assert result[2]['summary']['account_day_pnl'] == -1000
    assert result[2]['summary']['account_day_return'] == -1000/200500
    assert [d['intraday_trading'] for d in result] == [d['intraday_trading'] for d in saved]
