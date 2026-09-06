"""Paired exit diagnostics, separate from independently compounded accounts."""
from __future__ import annotations

from datetime import datetime
from statistics import mean, median
from typing import Any

from app.quant.research.evaluation import expected_shortfall


def avg(values):
    return mean(values) if values else None


def med(values):
    return median(values) if values else None


def paired_exit(reference, candidate, *, reference_result, candidate_result, minutes, market_dates):
    entry = reference['entry_execution_at']
    both = reference['outcome'] == candidate['outcome'] == 'closed'
    ref_sell = next((s for s in reference_result['signal_rows'] if s['action']=='sell'
                    and s['signal_at']>=entry), None)
    decisions = candidate_result.get('exit_decision_rows', [])
    deferred = next((r for r in decisions if r['action']=='defer'), None)
    submit = next((r for r in decisions if r['action']=='submit'), None)
    cand_end = candidate.get('exit_execution_at')
    end_at = cand_end or candidate['mark_date']+'T23:59:59+08:00'
    ref_end = reference.get('exit_execution_at')
    # A diagnostic may overlap later E0 entries; never put it in the real ledger.
    later_entries = [e for e in reference_result['event_rows'] if e['action']=='buy' and entry<e['execution_at']<=end_at]
    origin = deferred['at'] if deferred else None
    new_adverse = None
    after_fill_adverse = None
    if origin:
        anchor = next(s['signal_price'] for s in candidate_result['signal_rows'] if s['signal_at']==origin)
        lows = [b.low for items in minutes.values() for b in items if origin<b.trade_date<end_at]
        if cand_end:
            lows.append(candidate['exit_execution_price'])
        new_adverse = min(0.,min(lows)/anchor-1) if lows else 0.
        if ref_end and end_at>ref_end:
            lows2 = [b.low for items in minutes.values() for b in items if ref_end<b.trade_date<end_at]
            if cand_end:lows2.append(candidate['exit_execution_price'])
            after_fill_adverse=min(0.,min(lows2)/reference['exit_execution_price']-1) if lows2 else 0.
    delay_days = len([d for d in market_dates if origin[:10]<d<=end_at[:10]]) if origin else None
    signal_delta = ((datetime.fromisoformat(submit['at'])-datetime.fromisoformat(ref_sell['signal_at'])).total_seconds()/60
                    if submit and ref_sell else None)
    fill_delta = ((datetime.fromisoformat(cand_end)-datetime.fromisoformat(ref_end)).total_seconds()/60 if both else None)
    return {
        'code':reference['code'],'name':reference['name'],'entry_signal_at':reference['entry_signal_at'],
        'entry_execution_at':entry,'entry_execution_price':reference['entry_execution_price'],
        'shares':reference['shares'],'entry_notional':reference['entry_notional'],'buy_commission':reference['buy_commission'],
        'reference_outcome':reference['outcome'],'candidate_outcome':candidate['outcome'],
        'reference_exit_signal_at':ref_sell['signal_at'] if ref_sell else None,
        'candidate_exit_signal_at':submit['at'] if submit else None,
        'reference_exit_execution_at':ref_end,'candidate_exit_execution_at':cand_end,
        'reference_net_return':reference['net_return'],'candidate_net_return':candidate['net_return'],
        'both_naturally_closed':both,'closed_return_delta':candidate['net_return']-reference['net_return'] if both else None,
        'reference_asof_return':reference['asof_return'],'candidate_asof_return':candidate['asof_return'],
        'asof_return_delta':candidate['asof_return']-reference['asof_return'],
        'reference_mark_date':reference.get('mark_date'),'candidate_mark_date':candidate.get('mark_date'),
        'reference_mark_price':reference.get('mark_price'),'candidate_mark_price':candidate.get('mark_price'),
        'candidate_mae_return':candidate['mae_return'],'candidate_holding_trading_days':candidate['holding_trading_days'],
        'mechanism':'deferred' if deferred else 'early' if submit and submit['reason']=='early_protection' else 'original_or_untriggered',
        'deferred_from':origin,'delay_trading_days':delay_days,
        'delay_unfinished':bool(origin and not cand_end),
        'post_original_signal_mae':new_adverse,'post_original_fill_mae':after_fill_adverse,
        'signal_time_delta_minutes':signal_delta,'execution_time_delta_minutes':fill_delta,
        'actually_earlier_fill':fill_delta<0 if both else None,
        'actually_later_fill':fill_delta>0 if both else None,
        'profitable_reference_to_loss_closed':reference['net_return']>0 and candidate['net_return']<0 if both else None,
        'profitable_reference_to_loss_asof':reference['asof_return']>0 and candidate['asof_return']<0,
        'overlapping_later_reference_entries':len(later_entries),
    }


def paired_exit_metrics(rows):
    closed=[r for r in rows if r['both_naturally_closed']]
    gains=[r['closed_return_delta'] for r in closed]
    asof=[r['asof_return_delta'] for r in rows]
    delays=[r for r in rows if r['mechanism']=='deferred']
    profitable=[r for r in closed if r['reference_net_return']>0]
    delayed_profitable=[r for r in delays if r['both_naturally_closed'] and r['reference_net_return']>0]
    def rate(group,predicate):return sum(bool(predicate(r)) for r in group)/len(group) if group else None
    return {'pair_count':len(rows),'both_closed_count':len(closed),'both_closed_coverage':len(closed)/len(rows) if rows else None,
        'reference_open_count':sum(r['reference_outcome']=='open' for r in rows),
        'candidate_open_count':sum(r['candidate_outcome']=='open' for r in rows),
        'closed_mean_delta':avg(gains),'closed_median_delta':med(gains),
        'asof_mean_delta':avg(asof),'asof_median_delta':med(asof),
        'closed_benefit_rate':rate(closed,lambda r:r['closed_return_delta']>1e-12),
        'closed_harm_rate':rate(closed,lambda r:r['closed_return_delta']<-1e-12),
        'closed_mean_benefit':avg([v for v in gains if v>1e-12]),
        'closed_mean_harm':avg([v for v in gains if v<-1e-12]),
        'asof_benefit_rate':rate(rows,lambda r:r['asof_return_delta']>1e-12),
        'asof_harm_rate':rate(rows,lambda r:r['asof_return_delta']<-1e-12),
        'asof_mean_benefit':avg([v for v in asof if v>1e-12]),
        'asof_mean_harm':avg([v for v in asof if v<-1e-12]),
        'profitable_reference_closed_count':len(profitable),
        'profit_to_loss_closed_rate':rate(profitable,lambda r:r['candidate_net_return']<0),
        'delayed_profitable_reference_closed_count':len(delayed_profitable),
        'delayed_profit_to_loss_closed_rate':rate(delayed_profitable,lambda r:r['candidate_net_return']<0),
        'delay_count':len(delays),'delay_unfinished_count':sum(r['delay_unfinished'] for r in delays),
        'mean_delay_trading_days':avg([r['delay_trading_days'] for r in delays]),
        'median_delay_trading_days':med([r['delay_trading_days'] for r in delays]),
        'max_delay_trading_days':max([r['delay_trading_days'] for r in delays],default=None),
        'mean_post_original_signal_mae':avg([r['post_original_signal_mae'] for r in delays]),
        'worst_post_original_signal_mae':min([r['post_original_signal_mae'] for r in delays],default=None),
        'mean_post_original_fill_mae':avg([r['post_original_fill_mae'] for r in delays if r['post_original_fill_mae'] is not None]),
        'overlapping_diagnostic_count':sum(r['overlapping_later_reference_entries']>0 for r in rows),
        'actually_earlier_fill_rate_closed':rate(closed,lambda r:r['actually_earlier_fill']),
        'actually_later_fill_rate_closed':rate(closed,lambda r:r['actually_later_fill']),
    }


def quality(rows):
    closed=[r for r in rows if r['outcome']=='closed'];opened=[r for r in rows if r['outcome']=='open']
    values=[r['net_return'] for r in closed];all_values=[r['asof_return'] for r in rows]
    return {'entry_count':len(rows),'closed_count':len(closed),'open_count':len(opened),
        'closed_mean_net_return':avg(values),'closed_median_net_return':med(values),
        'closed_win_rate':avg([v>0 for v in values]),'closed_loss_over10_rate':avg([v<-.1 for v in values]),
        'closed_es95':expected_shortfall(values,.95),'closed_mean_mae':avg([r['mae_return'] for r in closed]),
        'closed_mean_holding_days':avg([r['holding_trading_days'] for r in closed]),
        'all_asof_mean_return':avg(all_values),'all_asof_median_return':med(all_values),
        'all_asof_loss_over10_rate':avg([v<-.1 for v in all_values]),'all_asof_es95':expected_shortfall(all_values,.95),
        'all_mean_mae':avg([r['mae_return'] for r in rows]),'all_mean_holding_days':avg([r['holding_trading_days'] for r in rows]),
        'open_mean_marked_return':avg([r['asof_return'] for r in opened])}
