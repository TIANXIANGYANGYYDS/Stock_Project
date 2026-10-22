from copy import deepcopy
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.services.short_sentiment.theme_environment import ALLOWED, Machine, vote
from app.services.short_sentiment.theme_environment_features import advance_levels, make_day


def window(n,kind='main'):
    start=datetime.fromisoformat('2026-06-22T09:31')+timedelta(minutes=n*5)
    codes=[f'{i:06}' for i in range(30)]
    ladder={'10':[[codes[0],1],[codes[1],1],[codes[2],1]],'20':[],'30':[]}
    if kind in ('main','hold'):ladder['10'] += [[codes[3],2],[codes[4],3]]
    p={'quality':True,'N':30,'expected_N':30,'B':70.,'M':1.,'U':20. if kind=='main' else 12.,'D5':0.,
       'ladder':ladder,'limit_up':len(ladder['10']),'limit_down_codes':[],
       'groups':{'high':[],'core':[],'middle':[]},'middle_pools':{b:[] for b in ladder},
       'competition_evidence':{b:{'success':[],'blocked':[]} for b in ladder},
       'core_failure':False,'core_failure_codes':[],'previous_mid_high':False,'A30':2.,'V30':1.,'Q10':0.,
       '_expected':codes,'_values':{c:1. for c in codes}}
    if kind=='risk':p.update(B=10.,M=-2.,D5=30.)
    result=[]
    for i in range(5):
        x=deepcopy(p);x['at']=(start+timedelta(minutes=i)).isoformat(timespec='minutes');result.append(x)
    return result


def test_main_needs_three_complete_windows():
    m=Machine()
    assert m.tick(window(0),'am')['state'] is None
    assert m.tick(window(1),'am')['state'] is None
    assert m.tick(window(2),'am')['state']=='主升'


def test_valid_main_hold_never_falls_back_to_start():
    m=Machine()
    for i in range(3):m.tick(window(i),'am')
    for i in range(3,12):
        w=m.tick(window(i,'hold'),'am')
        assert w['hold']['主升'] is True
        assert w['state']=='主升' and not w['event']


def test_acute_retreat_bypasses_thirty_minute_dwell():
    m=Machine()
    for i in range(3):m.tick(window(i),'am')
    w=m.tick(window(3,'risk'),'am')
    assert w['state']=='退潮' and w['event']['path']=='急剧退潮'


def test_missing_window_resets_pending_confirmation():
    m=Machine();m.tick(window(0),'am');m.tick(window(1),'am')
    points=window(2);points[2]['quality']=False
    assert m.tick(points,'am')['state'] is None
    assert m.tick(window(3),'am')['state'] is None


def test_new_session_does_not_reset_confirmed_state():
    m=Machine()
    for i in range(3):m.tick(window(i),'am')
    assert m.tick(window(3,'hold'),'pm')['state']=='主升'


def test_rotating_stocks_cannot_manufacture_middle_ladder():
    m=Machine()
    for i in range(3):
        points=window(i)
        for j,p in enumerate(points):p['ladder']['10'][3:]=[[f'x{j}',2],[f'y{j}',3]]
        w=m.tick(points,'am')
    assert w['state']!='主升'


def test_votes_require_last_point_and_no_unknown():
    assert vote([True,True,True,True,False]) is False
    assert vote([True,True,None,True,True]) is None


def test_start_paths_include_evidence_backed_new_cycle_after_divergence():
    assert all('启动' in ALLOWED[s] for s in ('整理','退潮','分歧','主升'))
    assert '启动' not in ALLOWED['高潮']


def test_future_changes_do_not_change_prefix():
    a,b=Machine(),Machine()
    prefix=[window(i) for i in range(8)]
    one=[a.tick(deepcopy(w),'am') for w in prefix]
    two=[b.tick(deepcopy(w),'am') for w in prefix]
    b.tick(window(8,'risk'),'am')
    assert one==two


def test_confirmed_climax_immediately_has_hold_support_in_its_bucket():
    m=Machine()
    for i in range(3):
        points=window(i)
        for p in points:
            p.update(B=80.,M=2.,U=35.)
            p['ladder']['10']=[[f'{j:06}',h] for j,h in enumerate([1,1,2,3,4,5])]
            p['limit_up']=6
        w=m.tick(points,'am')
    assert w['state']=='高潮' and w['support'] is True and w['hold']['高潮'] is True




def main_machine():
    m=Machine()
    for i in range(3):m.tick(window(i),'am')
    return m


def cool(n, middle=True):
    ps=window(n)
    for p in ps:
        p.update(B=40.,M=-.2,U=4.,D5=2.)
        if not middle:
            p.update(B=20.,M=-1.)
            p['ladder']['10']=[['000000',1],['000005',5]]
            p['limit_up']=2
    return ps


def test_thirty_minutes_of_failed_main_exits_to_supported_divergence():
    m=main_machine()
    for i in range(3,8):
        w=m.tick(cool(i),'am')
        assert w['state']=='主升' and w['support'] is False
    w=m.tick(cool(8),'am')
    assert w['state']=='分歧' and w['support'] is True
    assert w['event']['path']=='持续失效退出'
    assert w['event']['exit_evidence']['failed_windows']==6


def test_broad_pressure_without_middle_tiers_is_not_divergence_forever():
    m=main_machine()
    for i in range(3,9):w=m.tick(cool(i,middle=False),'am')
    assert w['state']=='退潮' and w['support'] is True
    assert w['event']['path']=='持续失效退出'


def test_unknown_support_does_not_count_towards_expiry():
    m=main_machine()
    for i in range(3,8):m.tick(cool(i),'am')
    ps=cool(8);ps[2]['quality']=False
    w=m.tick(ps,'am')
    assert w['state']=='主升' and w['support'] is None
    assert w['exit_evidence']['failed_windows']==0
    assert '行情覆盖不足' in w['support_unknown_reasons']
    for i in range(9,14):assert m.tick(cool(i),'am')['state']=='主升'
    assert m.tick(cool(14),'am')['state']=='分歧'


def test_lunch_retains_price_anchor_but_resets_expiry_confirmation():
    m=main_machine();anchor=deepcopy(m.baseline)
    for i in range(3,8):m.tick(cool(i),'am')
    ps=cool(8)
    for j,p in enumerate(ps):p['at']=f'2026-06-22T13:{j+1:02}'
    w=m.tick(ps,'pm')
    assert m.baseline==anchor
    assert w['state']=='主升' and w['exit_evidence']['failed_windows']==1


def anchored_point(level=1.04,epoch='seed'):
    m=Machine();p=window(0)[0]
    cs=list(p['_values'])
    anchors={c:{'origin':1.,'up':.1,'down':.1,'epoch':'seed'} for c in cs}
    m.baseline={'at':'2026-06-19T14:55','values':{c:4. for c in cs},'anchors':anchors}
    p['_values']={c:0. for c in cs}  # Daily reference changed, prices need not have fallen.
    p['_levels']={c:level for c in cs}
    p['_anchors']={c:{**a,'epoch':epoch} for c,a in anchors.items()}
    return m,p


def test_daily_return_reset_does_not_create_false_contraction():
    m,p=anchored_point()
    m._enrich(p)
    assert p['contraction'] is False
    assert p['baseline_drop_B']==0 and p['baseline_drop_U']==0


@pytest.mark.parametrize('level',[.99,np.float64(.99)])
def test_actual_cross_day_price_fall_creates_comparable_contraction(level):
    m,p=anchored_point(level=level)
    m._enrich(p)
    assert p['contraction'] is True
    assert p['baseline_drop_B']==100 and p['baseline_drop_U']==100


def test_exact_breadth_boundary_is_stable_under_float_price_chaining():
    m,p=anchored_point(level=np.float64(1.03))
    m._enrich(p)
    assert p['baseline_drop_U']==0


def test_broken_daily_chain_is_unknown_instead_of_fabricated_drop():
    m,p=anchored_point(level=.99,epoch='new')
    m._enrich(p)
    assert p['contraction'] is None
    assert p['contraction_status']=='可比价格覆盖不足'


def test_split_neutral_reference_and_missing_link_epoch():
    old={'x':{'value':1.04,'epoch':'seed'}}
    same=advance_levels(['x'],{'x':{'ref':50.,'close':50.}},old,'2026-06-22')
    assert same==old  # A 2:1 split from the previous raw 100 price changes no level.
    changed=advance_levels(['x'],{'x':{'ref':50.,'close':55.}},old,'2026-06-22')
    assert changed['x']['value']==pytest.approx(1.144)
    broken=advance_levels(['x'],{},old,'2026-06-22')
    assert broken['x']['epoch']=='2026-06-22'


def test_absent_anchor_is_not_reported_as_missing_quotes():
    m=Machine();p=window(0)[0];m._enrich(p)
    assert p['contraction'] is False
    assert p['contraction_status']=='无既有强势基准，不适用'


@pytest.mark.parametrize('prior',['整理','退潮'])
def test_recovery_can_restart_with_stale_first_boards_after_six_failed_windows(prior):
    m=Machine();m.state=prior;m.since='2026-06-19T14:00';m.cycle_open=prior=='退潮';m.cycle=2;m.age=20
    m.day='2026-06-22';m.first_seen={f'{i:06}':'2026-06-22T09:00' for i in range(3)}
    for i in range(5):assert m.tick(window(i,'startup'),'am')['state']==prior
    w=m.tick(window(5,'startup'),'am')
    assert not w['fresh_first_codes']
    assert w['state']=='启动' and w['cycle_id']==3 and w['support'] is True
    assert w['event']['path']=='持续失效退出'


def quiet_window(n,missing_core=False,falling=False):
    ps=window(n,'startup')
    for p in ps:
        p.update(N=500,expected_N=500,B=55.,M=.1,U=2.,A30=.8,V30=-.05,Q10=0.,limit_up=4)
        p['_expected']=[f'{i:06}' for i in range(500)];p['_values']={c:.5 for c in p['_expected']}
        p['ladder']['10']=[[f'{i:06}',1] for i in range(4)]
        if missing_core:p['groups']['core']=['missing']
        if falling:p.update(B=20.,M=-1.,V30=-.6)
    return ps


def test_scattered_first_boards_can_consolidate_in_large_theme():
    m=Machine();m.state='退潮';m.since='2026-06-19T14:00';m.age=20;m.cycle_open=True
    for i in range(7):w=m.tick(quiet_window(i),'am')
    assert w['state']=='整理' and w['support'] is True


def test_one_isolated_middle_stock_does_not_invalidate_existing_consolidation():
    m=Machine();m.state='整理';m.since='2026-06-19T14:00';m.age=20
    for i in range(6):
        ps=quiet_window(i)
        for p in ps:
            p['ladder']['10']=[['000000',2]];p['limit_up']=1
        w=m.tick(ps,'am')
        assert w['state']=='整理' and w['support'] is True


def test_strong_first_board_spread_cannot_be_classified_as_consolidation():
    m=Machine();m.state='启动';m.since='2026-06-19T14:00';m.age=20;m.cycle_open=True
    for i in range(12):
        ps=quiet_window(i)
        for p in ps:
            p.update(B=80.,M=1.,U=15.,limit_up=5)
            p['ladder']['10']=[[f'{j:06}',1] for j in range(5)]
        w=m.tick(ps,'am')
        assert w['state']=='启动' and w['entry']['整理'] is False


@pytest.mark.parametrize('mode',['missing_core','falling'])
def test_unknown_core_or_continuing_decline_cannot_create_quiet_exit(mode):
    m=Machine();m.state='退潮';m.since='2026-06-19T14:00';m.age=20;m.cycle_open=True
    for i in range(9):
        w=m.tick(quiet_window(i,**{mode:True}),'am')
        assert w['state']!='整理'


def test_expiry_without_target_evidence_does_not_force_default_stage():
    m=main_machine()
    for i in range(3,15):
        ps=window(i,'startup')
        for p in ps:p.update(B=55.,M=.1,U=8.,A30=3.,V30=1.)
        w=m.tick(ps,'am')
    assert w['state']=='主升' and w['support'] is False
    assert w['status']=='支持持续失效，转出证据待确认'
    assert not w['exit_evidence']['ready']


def test_later_minute_prices_and_today_close_do_not_rewrite_intraday_prefix():
    codes=[f'{i:06}' for i in range(10)]
    times=[f'{9+(31+i)//60:02}:{(31+i)%60:02}' for i in range(120)]
    times += [f'{13+(1+i)//60:02}:{(1+i)%60:02}' for i in range(120)]
    z={'codes':np.array(codes),'times':np.array(times),'prices':np.full((240,10),10.),
       'volume':np.ones((240,10)),'origin':np.ones((240,10),dtype=np.int8)}
    src={c:{'ref':10.,'upper':11.,'lower':9.,'close':10.1,'board':'main'} for c in codes}
    prev=deepcopy(src);heights={c:0 for c in codes}
    one=make_day('2026-06-22',codes,z,src,prev,heights,{})
    altered=deepcopy(z);altered['prices'][35:,:]=10.9
    changed=deepcopy(src)
    for row in changed.values():row['close']=10.9
    two=make_day('2026-06-22',codes,altered,changed,prev,heights,{})
    assert one['points'][:35]==two['points'][:35]
    a,b=Machine(),Machine()
    for offset in range(0,35,5):
        assert a.tick(one['points'][offset:offset+5],'am')==b.tick(two['points'][offset:offset+5],'am')




def seeded(state):
    m=Machine();m.state=state;m.age=20;m.since='2026-06-19T14:00'
    m.last_support='2026-06-19T14:55';m.cycle_open=state!='整理';m.cycle=1
    return m


def quiet(n, **kwargs):
    ps=window(n,'startup')
    for p in ps:
        p.update(B=10.,M=-1.6,U=0.,D5=5.,A30=.8,V30=.4,Q10=0.,
                 core_failure=True,ladder={'10':[],'20':[],'30':[]},limit_up=0)
        p.update(kwargs)
    return ps


def test_upward_exit_from_quiet_entry_band_does_not_restart_old_retreat():
    m=seeded('整理');m.previous_group_negative={'core_failure'}
    for i in range(8):
        w=m.tick(quiet(i),'am')
        assert w['hold']['整理'] is True and w['entry']['整理'] is False
        assert w['state']=='整理' and w['event'] is None


@pytest.mark.parametrize('changes',[{'V30':.8},{'V30':None,'A30':None,'Q10':None}])
def test_upward_or_unknown_quiet_exit_cannot_reactivate_unchanged_losses(changes):
    m=seeded('整理');m.previous_group_negative={'core_failure'}
    for i in range(8):w=m.tick(quiet(i,**changes),'am')
    assert w['state']=='整理'


def test_new_downward_trend_can_exit_quiet_with_old_negative_groups():
    m=seeded('整理');m.previous_group_negative={'core_failure'}
    for i in range(2):w=m.tick(quiet(i,V30=-.8),'am')
    assert w['state']=='退潮'


def test_fresh_group_risk_can_override_quiet_hold():
    m=seeded('整理')
    m.tick(quiet(0,core_failure=False),'am')
    m.tick(quiet(1,core_failure=False),'am')
    w=m.tick(quiet(2),'am')
    # The new group deterioration blocks quiet protection; retreat needs 2 windows.
    assert w['state']=='整理'
    w=m.tick(quiet(3),'am')
    assert w['state']=='退潮'


def test_repeated_individual_failure_does_not_reset_group_calm():
    m=seeded('启动');m.start_cohort={f'{i:06}' for i in range(10,16)}
    last=None
    for i in range(5):
        for j,p in enumerate(window(i,'startup')):
            p['_values']['000010']=-3. if j%2 else 1.
            m._enrich(p);last=p
    assert last['startup_failure'] is False
    assert last['no_new_negative_15'] is False
    assert last['no_new_group_negative_15'] is True


def test_unknown_fixed_cohort_cannot_prove_group_calm():
    m=seeded('启动');m.start_cohort={'missing'}
    for i in range(5):
        for p in window(i,'startup'):m._enrich(p)
    assert p['no_new_group_negative_15'] is None


def single(n, **overrides):
    ps=window(n,'startup')
    for p in ps:
        p.update(B=80.,M=1.2,U=25.,D5=1.)
        p['ladder']['10'].append(['000003',2]);p['limit_up']=4
        p.update(overrides)
    return ps


def test_single_middle_requires_stronger_breadth_and_real_followers():
    m=seeded('分歧')
    for i in range(3):w=m.tick(single(i),'am')
    assert w['state']=='主升' and w['support'] is True
    for overrides in ({'B':70.},{'M':.8},{'U':15.},{'D5':6.}):
        m=seeded('分歧')
        for i in range(5):w=m.tick(single(i,**overrides),'am')
        assert w['state']!='主升'


def test_unrelated_board_failure_does_not_veto_matching_tier_recovery():
    m=seeded('分歧')
    for i in range(3):
        ps=single(i)
        for p in ps:
            p['middle_pools']['20']=['000020']
            p['competition_evidence']['20']['blocked']=['000020']
        w=m.tick(ps,'am')
    assert w['facts']['promotion_recovered'] is False
    assert w['state']=='主升' and w['event']['support_buckets']==['10']


def test_real_competition_in_supporting_bucket_still_blocks_recovery():
    m=seeded('分歧')
    for i in range(6):
        ps=single(i)
        for p in ps:
            p['middle_pools']['10']=['000003','000010']
            p['competition_evidence']['10']={'success':['000003'],'blocked':['000010']}
        w=m.tick(ps,'am')
    assert w['state']=='分歧' and w['support'] is True


def test_missing_prices_in_supporting_bucket_do_not_count_as_recovery():
    m=seeded('分歧')
    for i in range(6):
        ps=single(i)
        for p in ps:
            p['middle_pools']['10']=['000003','missing']
            p['competition_evidence']['10']={'success':['000003'],'blocked':[]}
        w=m.tick(ps,'am')
    assert w['state']!='主升'


@pytest.mark.parametrize('prior',['分歧','主升'])
def test_new_first_board_cohort_can_restart_after_old_phase_loses_tiers(prior):
    m=seeded(prior);m.start_cohort={'000010','000011','000012'}
    for i in range(5):assert m.tick(window(i,'startup'),'am')['state']==prior
    w=m.tick(window(5,'startup'),'am')
    assert w['state']=='启动' and w['cycle_id']==2
    assert w['event']['path']=='持续失效退出' and w['support'] is True
    assert set(w['startup_cohort'])=={'000000','000001','000002'}


def test_old_startup_members_alone_cannot_manufacture_new_cycle():
    m=seeded('分歧');m.start_cohort={'000000','000001','000002'}
    for i in range(10):w=m.tick(window(i,'startup'),'am')
    assert w['state']=='分歧'


def test_lunch_resets_group_calm_observation():
    m=seeded('分歧')
    for i in range(5):m.tick(single(i,B=60.,M=.6,U=10.),'am')
    ps=single(5,B=60.,M=.6,U=10.)
    for j,p in enumerate(ps):p['at']=f'2026-06-22T13:{j+1:02}'
    w=m.tick(ps,'pm')
    assert w['facts']['no_new_group_negative_15'] is None


def test_recovery_and_divergence_use_the_same_supporting_tier_group():
    m=seeded('分歧')
    for i in range(10):
        ps=single(i)
        for p in ps:
            p['ladder']['20']=[['000020',2]]
            p['middle_pools']['20']=['000020','000021']
            p['competition_evidence']['20']={'success':['000020'],'blocked':['000021']}
        w=m.tick(ps,'am')
        if i>=2:
            assert w['state']=='主升' and w['main_support_bucket']=='10'
            assert w['facts']['competition_v2']['20'] is True
    assert w['event'] is None


def test_old_limit_down_reclosing_does_not_expand_group_risk():
    m=Machine()
    for i in range(5):
        for j,p in enumerate(window(i,'startup')):
            p['limit_down_codes']=['000010'] if j%2==0 else []
            m._enrich(p)
    assert p['new_negative_events']==[['limit_down','000010']]
    assert p['repeated_limit_down_codes']==['000010']
    assert p['new_limit_down_codes']==[]
    assert p['no_new_group_negative_15'] is True
    assert p['no_new_negative_15'] is False
    p=window(5,'startup')[0];p['limit_down_codes']=['000010','000011']
    m._enrich(p)
    assert p['new_limit_down_codes']==['000011']
    assert p['no_new_group_negative_15'] is False
    assert p['new_group_negative_events']==["('limit_down', '000011')"]


def test_seen_limit_down_survives_lunch_but_resets_on_new_day():
    m=seeded('整理')
    m.tick(quiet(0,limit_down_codes=['000010']),'am')
    m.tick(quiet(1,limit_down_codes=[]),'am')
    ps=quiet(2,limit_down_codes=['000010'])
    for j,p in enumerate(ps):p['at']=f'2026-06-22T13:{j+1:02}'
    m.tick(ps,'pm')
    assert ps[0]['new_limit_down_codes']==[]
    assert ps[0]['repeated_limit_down_codes']==['000010']
    assert ps[-1]['no_new_group_negative_15'] is None
    ps=quiet(0,limit_down_codes=['000010'])
    for p in ps:p['at']=p['at'].replace('2026-06-22','2026-06-23')
    m.tick(ps,'am')
    assert ps[0]['new_limit_down_codes']==['000010']


def test_unchanged_old_limit_down_cannot_prove_calm_with_missing_prices():
    m=seeded('启动');m.start_cohort={'missing'}
    for i in range(5):
        for j,p in enumerate(window(i,'startup')):
            p['limit_down_codes']=['000010'] if j%2 else []
            m._enrich(p)
    assert p['no_new_group_negative_15'] is None
