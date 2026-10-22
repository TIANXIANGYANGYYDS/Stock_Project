"""Behavioral contracts for the accepted V4.2 baseline market engine."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from app.services.short_sentiment.market_environment import MarketEnvironment, STATES, STATE_RANK, structure_support, vote


def direction(**overrides):
    return {'id':'small', 'name':'测试方向', 'quality':True, 'N':100, 'B':80, 'M':2,
            'U':30, 'D5':0, 'C':13, 'credit':13, 'relative_M':1.2, 'enrichment':2.5,
            'limit_up':3, 'previous_strong_quality':False, 'previous_strong_M':None,
            'previous_strong_failure_upper':None, **overrides}


def window(at='2026-09-01T10:00', **overrides):
    end = datetime.fromisoformat(at)
    fact = dict(quality=True, structure_quality=True, B=60, M=.7, U=9,
                D5=1, D3=2, N=5000, K=450, active=1, directions=[],
                delta15_B=0, delta15_M=0, delta15_D3=0)
    fact.update(overrides)
    return [dict(deepcopy(fact), at=(end-timedelta(minutes=i)).isoformat(timespec='minutes'))
            for i in range(4, -1, -1)]


def seeded(state='微热', **attributes):
    m = MarketEnvironment()
    m.state = state
    m.state_since = '2026-08-31T14:00'
    m.last_supported_at = '2026-08-31T14:55'
    m.age = 20
    for key, value in attributes.items():
        setattr(m, key, value)
    return m


def feed(m, count, at='2026-09-01T10:00', **facts):
    end = datetime.fromisoformat(at)
    return [m.step(window((end+timedelta(minutes=5*i)).isoformat(timespec='minutes'), **facts))
            for i in range(count)]


def test_window_requires_known_points_and_last_point_confirmation():
    assert vote([True, False, True, True, True]) is True
    assert vote([True, True, True, True, False]) is False
    assert vote([None, True, True, True, True]) is None


def test_state_order_is_best_to_worst_and_ice_is_the_minimum():
    assert STATES == ('沸点','过热','微热','微冷','过冷','冰点')
    assert [STATE_RANK[s] for s in STATES] == [5,4,3,2,1,0]


@pytest.mark.parametrize('override', [{}, {'delta15_M':None}, {'delta15_M':-.3}, {'M':-1.2}])
def test_quiet_or_stable_weak_prices_are_not_ice(override):
    m = seeded('微冷')
    facts = dict(B=30,M=-.7,U=2,D5=1,D3=3)
    facts.update(override)
    assert feed(m,4,**facts)[-1]['state'] == '微冷'
    assert not m.events


@pytest.mark.parametrize('start', ['沸点','过热','微热','微冷','过冷'])
def test_extreme_breadth_and_losses_move_every_higher_state_down_to_ice(start):
    m = seeded(start,age=0)
    rows = feed(m,2,B=8,M=-2.1,U=1,D3=30,D5=7,delta15_M=None)
    assert rows[-1]['state'] == '冰点'
    assert m.events[-1]['direction'] == '恶化'
    assert m.events[-1]['required_windows'] == 2
    # In particular, cold -> ice must never require cold recovery first.
    assert rows[-1]['entry']['恢复'] is False


@pytest.mark.parametrize('b',[5,10,15,30])
@pytest.mark.parametrize('median',[-3,-2,-1.5,-.7])
@pytest.mark.parametrize('tails',[(10,2),(25,6),(30,12)])
def test_ice_evidence_is_more_severe_than_cold_evidence(b,median,tails):
    entry,_ = seeded().guards(window(B=b,M=median,D3=tails[0],D5=tails[1])[-1])
    if entry['冰点'] or entry['急剧冰点']:
        assert entry['过冷_深跌'] or entry['过冷_普跌']
        assert entry['微冷_下行']


def test_rapid_ice_can_bypass_dwell_in_a_single_complete_window():
    m = seeded('过冷',age=0)
    assert feed(m,1,B=4,M=-3.2,D3=40,D5=20)[0]['state'] == '冰点'
    assert m.events[-1]['condition'] == '急剧冰点'


def test_ice_hold_prevents_improvement_just_from_losing_entry_threshold():
    m = seeded('冰点')
    rows = feed(m,4,B=12,M=-1.8,D3=23,D5=7)
    assert all(r['state']=='冰点' and r['support'] is True for r in rows)
    assert not m.events


def test_ice_recovery_to_cold_is_an_improvement_with_its_own_gate():
    m = seeded('冰点')
    rows = feed(m,2,B=20,M=-1.6,D3=18,D5=6)
    assert [r['state'] for r in rows] == ['冰点','过冷']
    assert m.events[-1]['direction'] == '改善'
    assert m.events[-1]['decision_runs']['冰点恢复'] == 2
    assert m.events[-1]['decision_runs']['恢复'] == 0


def test_ice_cannot_skip_cold_recovery_to_call_weak_prices_microcold():
    m = seeded('冰点')
    assert feed(m,4,B=20,M=-.7,D3=3,D5=1)[-1]['state'] == '冰点'
    assert m.runs['冰点恢复'] == 4 and m.runs['恢复'] == 0
    assert feed(m,2,at='2026-09-01T10:20',B=40,M=-.8,U=4)[-1]['state'] == '微冷'
    assert m.events[-1]['direction'] == '改善'


@pytest.mark.parametrize('start,facts,target',[
    ('冰点',dict(B=20,M=-1.6,D3=18,D5=6),'过冷'),
    ('过冷',dict(B=40,M=-.8,U=4),'微冷')])
def test_improvement_to_a_negative_stage_still_obeys_twenty_minute_dwell(start,facts,target):
    m = seeded(start,age=0)
    rows = feed(m,4,**facts)
    assert [r['state'] for r in rows] == [start,start,start,target]
    assert m.events[-1]['direction'] == '改善'


def test_gentle_cooling_has_memory_and_requires_three_windows():
    m = seeded()
    rows = feed(m,3,B=48,M=-.1,U=4)
    assert [r['state'] for r in rows] == ['微热','微热','微冷']
    assert m.events[-1]['condition'] == '微冷_收缩'
    cold = seeded('微冷')
    assert cold.guards(window(B=48,M=-.1)[-1])[0]['微冷_收缩'] is False


def test_widespread_moderate_losses_trigger_cold_without_many_deep_losses():
    m = seeded(age=0)
    rows = feed(m,2,B=10,M=-1.7,D3=20,D5=3)
    assert [r['state'] for r in rows] == ['微热','过冷']
    assert m.events[-1]['condition'] == '过冷_普跌'
    assert m.events[-1]['prior_age_windows'] == 2  # adverse transition bypasses dwell


def test_extreme_selloff_fast_path_and_cold_recovery_guard():
    m = seeded(age=0)
    assert feed(m,1,B=10,M=-2,D3=30,D5=12)[-1]['state'] == '过冷'
    # Stable, weak prices can meet ice entry without meeting breadth recovery.
    assert feed(m,5,at='2026-09-01T10:05',B=20,M=-.7,U=2,D5=1)[-1]['state'] == '过冷'
    rows = feed(m,2,at='2026-09-01T10:30')
    assert [r['state'] for r in rows] == ['过冷','微热']


def test_known_weakness_can_bridge_but_unknown_retains_confirmed_phase():
    m = seeded()
    rows = feed(m,3,B=52,M=-.4,U=3,D5=2)
    assert rows[-1]['state'] == '微冷' and rows[-1]['support'] is True
    assert m.events[-1]['condition'] == '微冷_偏热退出承接'
    since, supported = m.state_since, m.last_supported_at
    unknown = feed(m,1,at='2026-09-01T10:15',quality=False)[0]
    assert unknown['state_since'] == since and unknown['last_supported_at'] == supported
    assert unknown['status'] == '数据不足，阶段沿用'
    assert not unknown['candidate_windows']


@pytest.mark.parametrize('next_at', ['2026-09-01T13:05','2026-09-02T09:35','2026-09-01T10:10'])
def test_session_or_missing_window_resets_confirmation_but_preserves_state(next_at):
    m = seeded()
    facts = dict(B=40,M=-.8,U=4)
    first = feed(m,1,**facts)[0]
    row = feed(m,1,at=next_at,**facts)[0]
    assert row['state'] == '微热' and row['state_since'] == first['state_since']
    assert row['candidate_windows']['微冷_下行'] == 1
    assert row['age_windows'] == first['age_windows']+1


@pytest.mark.parametrize('at', ['2026-09-01T10:00','2026-09-01T09:55',
                               '2026-09-01T10:06','2026-09-01T12:00','2026-09-01T15:00'])
def test_bad_or_duplicate_boundary_rejected_before_state_mutation(at):
    m = seeded()
    feed(m,1)
    before = deepcopy(m.__dict__)
    with pytest.raises(ValueError):
        m.step(window(at))
    assert m.__dict__ == before


def test_noncontiguous_minutes_rejected():
    points = window()
    points[0]['at'] = '2026-09-01T09:54'
    m = seeded()
    before = deepcopy(m.__dict__)
    with pytest.raises(ValueError):
        m.step(points)
    assert m.__dict__ == before


def test_leader_requires_relative_strength_and_competitor_dominance():
    m = seeded()
    large = direction(id='large',N=1500,credit=40,C=40,enrichment=1.1,relative_M=.2)
    assert m.leader_candidate_at(window(K=100,directions=[large,direction()])[-1]) == 'small'
    rival = direction(id='rival',credit=12,C=12)
    assert m.leader_candidate_at(window(K=100,directions=[direction(),rival])[-1]) is False
    assert m.leader_candidate_at(window(K=100,directions=[direction(N=19)])[-1]) is False


def test_leader_needs_six_windows_then_two_new_market_windows():
    m = seeded()
    rows = feed(m,8,directions=[direction()])
    assert all(r['structure']['current'] is None for r in rows[:5])
    assert rows[5]['structure']['current']['since'] == rows[5]['at']
    assert rows[5]['entry']['沸点'] is False
    assert [r['state'] for r in rows] == ['微热']*7+['沸点']
    assert m.events[-1]['at'] == '2026-09-01T10:35'


def test_missing_fixed_group_is_unknown_and_new_limit_ups_can_still_support():
    assert structure_support(direction(limit_up=0)) is None
    assert structure_support(direction()) is True
    assert structure_support(direction(limit_up=0,previous_strong_quality=True,
        previous_strong_M=.1,previous_strong_failure_upper=21)) is False


def test_missing_leader_evidence_does_not_falsely_retire_then_two_failures_retire():
    m = seeded()
    feed(m,6,directions=[direction()])
    identity = deepcopy(m.leader)
    rows = feed(m,2,at='2026-09-01T10:30',structure_quality=False)
    assert m.leader == identity and rows[-1]['structure']['support'] is None
    feed(m,1,at='2026-09-01T10:40',directions=[direction(M=-1)])
    assert m.leader is not None
    feed(m,1,at='2026-09-01T10:45',directions=[direction(M=-1)])
    assert m.leader is None and m.retired_leaders[-1]['id'] == identity['id']


def test_held_boil_does_not_downgrade_when_only_entry_threshold_is_lost():
    m = seeded('沸点',leader={'id':'small','name':'测试方向','since':'2026-08-31T14:00'})
    rows = feed(m,6,U=7,directions=[direction()])
    assert all(r['state'] == '沸点' and r['support'] is True for r in rows)
    assert any('保持' in b for c in rows[-1]['candidates'] for b in c['blocked'])
    rows = feed(m,2,at='2026-09-01T10:30',U=4,directions=[direction()])
    assert [r['state'] for r in rows] == ['沸点','微热']


def test_unknown_boil_support_cannot_count_as_downgrade_failure():
    m = seeded('沸点',leader={'id':'small','name':'测试方向','since':'2026-08-31T14:00'})
    assert feed(m,3,structure_quality=False)[-1]['state'] == '沸点'


def test_retired_leader_price_weakness_can_trigger_cooling_then_expires_in_trade_days():
    m = seeded()
    m.retired_leaders = [{'id':'small','retired_day':'2026-08-31'}]
    m.trade_days.append('2026-08-31')
    rows = feed(m,2,B=52,M=-.1,directions=[direction(B=40,M=-1,D5=12)])
    assert rows[-1]['state'] == '微冷'
    assert m.events[-1]['condition'] == '微冷_主线转弱'
    for day in ('02','03','04'):
        feed(m,1,at=f'2026-09-{day}T09:35')
    assert m.retired_leaders
    feed(m,1,at='2026-09-07T09:35')
    assert not m.retired_leaders


def test_streaming_prefix_is_immutable_when_future_is_processed():
    m = seeded()
    rows = feed(m,6,directions=[direction()])
    before = deepcopy(rows)
    feed(m,4,at='2026-09-01T10:30',B=10,M=-2,D3=30,D5=12)
    assert rows == before




def test_ice_improvement_does_not_need_a_new_worsening_into_cold_entry():
    m = seeded('冰点')
    a = feed(m, 2, B=12, M=-1.3, D3=13, D5=3)
    assert [w['state'] for w in a] == ['冰点', '过冷']
    assert a[-1]['entry']['过冷_深跌'] is False
    assert a[-1]['entry']['过冷_普跌'] is False
    assert a[-1]['support'] is True
    assert m.events[-1]['condition'] == '过冷_冰点恢复承接'


def test_cold_recovery_can_stop_between_severe_cold_and_broad_warmth():
    m = seeded('过冷')
    a = feed(m, 2, B=48, M=-.1, D3=2, D5=1)
    assert [w['state'] for w in a] == ['过冷', '微冷']
    assert m.events[-1]['condition'] == '微冷_冷态恢复承接'


def test_lost_leader_downgrades_boiling_when_lower_entry_is_not_met():
    m = seeded('沸点')
    a = feed(m, 2, B=50, M=.1, U=4, D5=1)
    assert [w['state'] for w in a] == ['沸点', '微热']
    assert a[-1]['entry']['微热'] is False and a[-1]['support'] is True


def test_bridge_cannot_treat_unknown_leader_support_as_failure():
    m = seeded('沸点', leader={'id':'missing','name':'old','since':'2026-08-31T14:00'})
    a = feed(m, 6, B=50, M=.1, U=6, structure_quality=False)
    assert all(w['state'] == '沸点' and w['support'] is None for w in a)


def test_unknown_price_window_resets_recovery_confirmation():
    m = seeded('过冷')
    feed(m, 1, B=48, M=-.1, D5=1)
    feed(m, 1, at='2026-09-01T10:05', quality=False)
    assert feed(m, 1, at='2026-09-01T10:10', B=48, M=-.1, D5=1)[0]['state'] == '过冷'
    assert feed(m, 1, at='2026-09-01T10:15', B=48, M=-.1, D5=1)[0]['state'] == '微冷'


def test_bridge_recovery_obeys_minimum_observed_dwell():
    m = seeded('冰点', age=0)
    assert [w['state'] for w in feed(m, 4, B=12, M=-1.3, D3=13, D5=3)] == ['冰点']*3+['过冷']


def test_higher_risk_still_takes_priority_over_bridges():
    m = seeded('沸点')
    assert feed(m, 1, B=4, M=-3.2, D3=40, D5=20)[0]['state'] == '冰点'
    assert m.events[-1]['condition'] == '急剧冰点'
