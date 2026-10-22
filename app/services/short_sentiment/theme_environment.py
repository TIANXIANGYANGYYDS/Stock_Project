"""V2.4 research: recovery, group-risk evidence and consolidation hysteresis."""
from collections import Counter, deque
from datetime import datetime
import math

STATES=('整理','启动','主升','分歧','高潮','退潮')
ACTIVE={'启动','主升','分歧','高潮'}
ALLOWED={'整理':{'启动','主升','退潮'},'启动':{'主升','分歧','退潮','整理'},
         '主升':{'高潮','分歧','退潮','整理','启动'},'分歧':{'主升','高潮','退潮','整理','启动'},
         '高潮':{'主升','分歧','退潮','整理'},'退潮':{'整理','分歧','主升','启动'}}
VERSION='theme-v2.4-environment-recovery-20261020'
EXPIRY_WINDOWS=6


def both(*xs):return False if any(x is False for x in xs) else None if any(x is None for x in xs) else True
def either(*xs):return True if any(x is True for x in xs) else None if any(x is None for x in xs) else False
def vote(xs):return None if len(xs)!=5 or any(x is None for x in xs) else sum(xs)>=4 and xs[-1]
def cmp(p,k,op,v):
    x=p.get(k)
    if x is None:return None
    return {'ge':lambda:x>=v,'le':lambda:x<=v,'lt':lambda:x<v,'absle':lambda:abs(x)<=v}[op]()


def failure(bad,seen,total,minimum=1,fraction=0):
    if not total:return False
    if bad>=minimum and bad>=total*fraction:return True
    possible=bad+total-seen
    return False if possible<minimum or possible<total*fraction else None


def stable_codes(points,field='ladder',side=None,bucket=None):
    def codes(p):
        if field=='ladder':return {c for rows in p[field].values() for c,h in rows}
        return set(p[field][bucket][side])
    sets=[codes(p) for p in points]
    return {c for c in set.union(*sets) if vote([c in s for s in sets]) is True}


def structure(p,stable):
    out={}
    for bucket,rows in p['ladder'].items():
        hs=Counter(h for c,h in rows if c in stable and h is not None)
        H=max(hs,default=0)
        out[bucket]={'F':hs[1],'M':sum(hs[h] for h in (2,3,4)),
                     'H':sum(n for h,n in hs.items() if h>=5),'height':H,
                     'complete':H>=5 and all(hs[h] for h in range(1,H+1)) and not any(h is None for c,h in rows)}
    return out


class Machine:
    def __init__(self):
        self.state=None;self.since=None;self.session=None;self.day=None;self.age=0
        self.counts=Counter();self.bucket_counts=Counter();self.hold_fail=Counter()
        self.exit_counts=Counter();self.exit_bucket_counts=Counter()
        self.cycle=0;self.cycle_open=False;self.closed_at=None
        self.first_seen={};self.start_cohort=set();self.high_cohort=set();self.peak={}
        self.baseline=None;self.climax_bucket=None;self.main_bucket=None;self.last_at=None
        self.negative_history=deque(maxlen=40);self.previous_negative=set();self.extreme=False
        self.group_negative_history=deque(maxlen=40);self.previous_group_negative=set()
        self.limit_down_day=None;self.seen_limit_down=set()
        self.last_support=None

    def _enrich(self,p):
        vals=p['_values'];up={c for rows in p['ladder'].values() for c,h in rows}
        for c,v in vals.items():
            if v is not None:self.peak[c]=max(self.peak.get(c,v),v)
        def bad_group(group):
            seen={c for c in group if vals.get(c) is not None}
            bad={c for c in seen if c not in up and (vals[c]<=-3 or self.peak.get(c,vals[c])-vals[c]>=5)}
            return bad,seen
        high=self.high_cohort|set(p['groups']['high'])
        high_bad,high_seen=bad_group(high)
        start_bad,start_seen=bad_group(self.start_cohort)
        p['high_negative']=failure(len(high_bad),len(high_seen),len(high))
        p['high_negative_codes']=sorted(high_bad)
        p['startup_failure']=failure(len(start_bad),len(start_seen),len(self.start_cohort),2,.5)
        p['startup_failure_codes']=sorted(start_bad)
        negatives={('high',c) for c in high_bad}|{('core',c) for c in p['core_failure_codes']}|{('startup',c) for c in start_bad}|{('limit_down',c) for c in p['limit_down_codes']}
        # Ordinary members use the same >=98% coverage gate as the breadth
        # features. Fixed risk cohorts still require their own valid prices.
        tracked=high|set(p['groups']['core'])|self.start_cohort
        known=p['quality'] and all(vals.get(c) is not None for c in tracked)
        new=negatives-self.previous_negative
        self.previous_negative=negatives
        p['new_negative_events']=[list(x) for x in sorted(new)]
        self.negative_history.append((p['at'],known,bool(new)))
        grouped={k for k in ('high_negative','core_failure','startup_failure') if p[k] is True}
        new_grouped=grouped-self.previous_group_negative
        self.previous_group_negative=grouped
        # Reclosing the same old limit-down stock does not expand today's
        # affected cohort. Preserve its raw individual event above; renewed
        # group failure, acceleration and downward drift remain independent.
        if self.limit_down_day!=p['at'][:10]:
            self.limit_down_day=p['at'][:10];self.seen_limit_down.clear()
        fresh_down=set(p['limit_down_codes'])-self.seen_limit_down
        self.seen_limit_down.update(p['limit_down_codes'])
        p['new_limit_down_codes']=sorted(fresh_down)
        p['repeated_limit_down_codes']=sorted(c for kind,c in new if kind=='limit_down' and c not in fresh_down)
        new_grouped|={('limit_down',c) for c in fresh_down}
        p['new_group_negative_events']=sorted(str(k) for k in new_grouped)
        self.group_negative_history.append((p['at'],known,bool(new_grouped)))
        now=datetime.fromisoformat(p['at'])
        for lag in (5,15):
            history=[r for r in self.negative_history if 0<=(now-datetime.fromisoformat(r[0])).total_seconds()<=lag*60]
            full=len(history)==lag+1 and all(r[1] for r in history)
            p[f'no_new_negative_{lag}']=not any(r[2] for r in history[1:]) if full else None
            history=[r for r in self.group_negative_history if 0<=(now-datetime.fromisoformat(r[0])).total_seconds()<=lag*60]
            full=len(history)==lag+1 and all(r[1] for r in history)
            p[f'no_new_group_negative_{lag}']=not any(r[2] for r in history[1:]) if full else None
            p[f'recent_group_negative_{lag}']=any(r[2] for r in history)
        p['contraction']=False;p['baseline_at']=self.baseline['at'] if self.baseline else None
        p['contraction_status']='无既有强势基准，不适用'
        if self.baseline:
            p['contraction']=None;p['contraction_status']='可比价格覆盖不足'
            old=self.baseline['values'];current={}
            for c,anchor in self.baseline.get('anchors',{}).items():
                level=p.get('_levels',{}).get(c);now_anchor=p.get('_anchors',{}).get(c,{})
                if level is None or now_anchor.get('epoch')!=anchor['epoch']:continue
                change=level-anchor['origin']
                # Price ticks are much larger than 1e-10 r10; rounding only
                # removes floating-point noise at the 0/3 breadth boundaries.
                current[c]=round(float(10*change/anchor['up' if change>=0 else 'down']),10)
            # Same-day unit fixtures and legacy callers have no chained levels.
            # Never compare differently based daily returns across dates.
            if not self.baseline.get('anchors') and self.baseline['at'][:10]==p['at'][:10]:current=vals
            common={c for c in p['_expected'] if old.get(c) is not None and current.get(c) is not None}
            if p['quality'] and len(common)>=.98*p['expected_N']:
                p['baseline_drop_B']=100*sum((old[c]>0)-(current[c]>0) for c in common)/len(common)
                p['baseline_drop_U']=100*sum((old[c]>=3)-(current[c]>=3) for c in common)/len(common)
                p['contraction']=p['baseline_drop_B']>=15 and p['baseline_drop_U']>=5
                p['contraction_status']='同基准可比价格'
            p['baseline_comparable_N']=len(common)

    def tick(self,points,session):
        at=points[-1]['at'];day=at[:10]
        assert len(points)==5 and len({p['at'] for p in points})==5
        gap=self.last_at is not None and (datetime.fromisoformat(at)-datetime.fromisoformat(self.last_at)).total_seconds()!=300
        if session!=self.session or gap:
            self.counts.clear();self.bucket_counts.clear();self.hold_fail.clear()
            self.exit_counts.clear();self.exit_bucket_counts.clear()
            self.negative_history.clear()
            self.group_negative_history.clear()
        if day!=self.day:
            self.peak.clear();self.first_seen={};self.extreme=False
        self.session,self.day,self.last_at=session,day,at
        stable=stable_codes(points)
        last_structure=structure(points[-1],stable)
        firsts={c for rows in points[-1]['ladder'].values() for c,h in rows if c in stable and h==1}
        for c in firsts:self.first_seen.setdefault(c,at)
        fresh={c for c in firsts if (datetime.fromisoformat(at)-datetime.fromisoformat(self.first_seen[c])).total_seconds()<=1800
               and (self.closed_at is None or self.first_seen[c]>self.closed_at)}
        witnesses={}
        for b in ('10','20','30'):
            witnesses[b]={s:stable_codes(points,'competition_evidence',s,b) for s in ('success','blocked')}
        all_entries=[];all_holds=[];all_buckets=[];all_exits=[];cooling_buckets=[]
        for p in points:
            self._enrich(p)
            q=lambda k,op,v:cmp(p,k,op,v)
            s=structure(p,stable);F=sum(b['F'] for b in s.values())
            middle=[b for b,x in s.items() if x['M']>=2 and x['F']>=1]
            broad_single=(all(p[k] is not None for k in ('B','M','U','D5')) and p['B']>=75 and p['M']>=1 and p['U']>=20 and p['D5']<5
                          and F>=max(3,math.ceil(.01*p['N'])))
            single=[b for b,x in s.items() if broad_single and x['M']>=1 and x['F']>=2]
            middle=sorted(set(middle)|set(single))
            p['single_middle_breadth_buckets']=single
            climax=[b for b,x in s.items() if x['H']>=1 and x['M']>=2 and x['F']>=2]
            competing=[];recovery=[]
            for b in ('10','20','30'):
                pool=p['middle_pools'][b];ev=p['competition_evidence'][b]
                fail=set(ev['blocked'])&witnesses[b]['blocked'];win=set(ev['success'])&witnesses[b]['success']
                missing=sum(p['_values'].get(c) is None for c in pool)
                competing.append(both(bool(win),failure(len(fail),len(pool)-missing,len(pool),1,.3)))
                recovery.append(True if not pool else (len(ev['blocked'])+missing)/len(pool)<.2)
            p['competition_v2']=dict(zip(('10','20','30'),competing))
            p['promotion_recovered']=all(recovery)
            # Only the tier group actually supporting the new phase may veto it.
            recovery_buckets=[b for b in ('10','20','30') if p['competition_v2'][b] is False]
            p['promotion_recovery_buckets']=recovery_buckets
            if self.state=='分歧':
                middle=[b for b in middle if b in recovery_buckets]
                climax=[b for b in climax if b in recovery_buckets]
            anchor=self.main_bucket if self.state=='主升' else self.climax_bucket if self.state=='高潮' else None
            phase_competition={b:v if anchor is None or b==anchor else False for b,v in p['competition_v2'].items()}
            p['phase_support_bucket']=anchor
            div_buckets=[b for b,x in s.items() if x['M']+x['H']>=1 and either(phase_competition[b],p['contraction'],p['high_negative']) is True]
            div_unknown=any(x['M']+x['H']>=1 and either(phase_competition[b],p['contraction'],p['high_negative']) is None for b,x in s.items())
            divergence=True if div_buckets else None if div_unknown else False
            active=self.cycle_open or (self.state is None and p['previous_mid_high'])
            depleted=both(active,q('B','le',40),q('M','le',-.5),sum(x['M'] for x in s.values())==0)
            not_depleted=None if depleted is None else not depleted
            cooling=both(active,either(q('B','lt',50),q('M','lt',0),q('U','lt',10)),q('D5','lt',15))
            cooling_bs=[b for b,x in s.items() if x['M']+x['H']>=1 and cooling is True]
            cooled_divergence=both(cooling,any(x['M']+x['H']>=1 for x in s.values()),not_depleted)
            adverse=either(p['high_negative'],p['core_failure'],p['startup_failure'],both(active,q('D5','ge',15)))
            clear=both(p['high_negative'] is False if p['high_negative'] is not None else None,
                       p['core_failure'] is False if p['core_failure'] is not None else None,
                       p['startup_failure'] is False if p['startup_failure'] is not None else None)
            quiet_limit=max(2,math.ceil(.01*p['N']))
            p['quiet_limit']=quiet_limit;p['cooling_divergence']=cooled_divergence;p['middle_depleted_pressure']=depleted
            startup_shape=both(F>=max(3,math.ceil(.01*p['N'])),q('B','ge',55),q('M','ge',0),q('U','ge',5),q('D5','lt',5))
            not_starting=None if startup_shape is None else not startup_shape
            p['startup_shape']=startup_shape
            quiet=both(q('limit_up','le',quiet_limit),sum(x['M']+x['H'] for x in s.values())==0,
                       q('A30','le',1),q('V30','absle',.3),q('Q10','le',5),p['no_new_group_negative_15'],not_starting)
            new_cycle_firsts={c for c in firsts-self.start_cohort
                              if self.last_support is not None and self.first_seen[c]>self.last_support}
            low_cycle_restart=(self.state in ('分歧','主升') and sum(x['M']+x['H'] for x in s.values())==0
                               and len(new_cycle_firsts)>=2)
            p['new_cycle_first_codes']=sorted(new_cycle_firsts)
            exits={
                '退潮':both(self.state in ACTIVE,depleted),
                '分歧':both(self.state in ('启动','主升','高潮'),cooled_divergence),
                '启动':both(self.state in ('整理','退潮') or low_cycle_restart,
                            startup_shape,clear,p['no_new_group_negative_15']),
            }
            entry={
                '启动':both(F>=max(3,math.ceil(.01*p['N'])),q('B','ge',55),q('M','ge',0),q('U','ge',5),q('D5','lt',5),
                            self.state is None or (self.state in ('整理','退潮') and len(fresh)>=2)),
                '主升':both(q('B','ge',60),q('M','ge',.5),q('U','ge',15),q('D5','lt',10),bool(middle)),
                '高潮':both(q('B','ge',75),q('M','ge',1.5),q('U','ge',30),q('D5','lt',5),bool(climax)),
                '分歧':both(active,q('D5','lt',15),divergence,not_depleted),
                '退潮':both(q('B','le',40),q('M','le',-.5),adverse),
                '急剧退潮':both(q('B','le',20),q('M','le',-1.5),q('D5','ge',25)),
                '整理':quiet,
            }
            if self.state=='分歧':
                for k in ('主升','高潮'):
                    entry[k]=both(entry[k],clear,
                                  None if p['contraction'] is None else not p['contraction'])
            elif self.state=='退潮':
                entry['主升']=both(entry['主升'],clear,p['no_new_group_negative_15'])
                entry['分歧']=both(entry['分歧'],q('B','ge',45),q('M','ge',-.2),q('D5','lt',10),p['no_new_group_negative_15'])
            elif self.state=='整理':entry['主升']=both(entry['主升'],clear)
            original=s.get(self.climax_bucket,{})
            holds={
                '启动':both(q('B','ge',45),q('M','ge',-.5),q('D5','lt',10),F>=1),
                '主升':both(q('B','ge',50),q('M','ge',0),q('U','ge',10),q('D5','lt',15),
                            any(x['F']+x['M']+x['H']>=2 and x['M']+x['H']>=1
                                for b,x in s.items() if self.state!='主升' or self.main_bucket is None or b==self.main_bucket)),
                '高潮':both(q('B','ge',60),q('M','ge',.5),q('U','ge',20),q('D5','lt',10),all(original.get(k,0)>=1 for k in ('F','M','H'))),
                '分歧':both(q('D5','lt',20),either(divergence,cooled_divergence),not_depleted),
                '退潮':either(both(q('B','le',45),q('M','le',-.3)),p['high_negative'],p['core_failure'],p['startup_failure']),
                '整理':both(q('limit_up','le',quiet_limit+1),q('A30','le',1.5),q('V30','absle',.5),q('Q10','le',10),not_starting),
            }
            if not p['quality']:
                entry={k:None for k in entry};holds={k:None for k in holds}
                exits={k:None for k in exits}
            all_entries.append(entry);all_holds.append(holds);all_buckets.append({'主升':middle,'高潮':climax,'分歧':div_buckets})
            all_exits.append(exits);cooling_buckets.append(cooling_bs)
        entries={k:vote([r[k] for r in all_entries]) for k in all_entries[0]}
        holds={k:vote([r[k] for r in all_holds]) for k in STATES}
        buckets={k:[b for b in ('10','20','30') if vote([both(e[k],b in bs[k]) for e,bs in zip(all_entries,all_buckets)]) is True]
                 for k in ('主升','高潮','分歧')}
        for k,bs in buckets.items():
            if entries[k] is True and not bs:entries[k]=False
        quality=all(p['quality'] for p in points)
        if quality:self.age+=1
        required={k:1 if k=='急剧退潮' else 2 if k in ('退潮','分歧') else 3 for k in entries}
        if self.state=='退潮':required['分歧']=3
        for k,v in entries.items():self.counts[k]=self.counts[k]+1 if v is True else 0
        for k,v in holds.items():self.hold_fail[k]=self.hold_fail[k]+1 if v is False else 0
        exit_entries={k:vote([r[k] for r in all_exits]) for k in all_exits[0]}
        exit_required={'退潮':2,'分歧':2,'启动':3}
        for k,v in exit_entries.items():self.exit_counts[k]=self.exit_counts[k]+1 if v is True else 0
        for b in ('10','20','30'):
            supported=vote([both(e['分歧'],b in bs) for e,bs in zip(all_exits,cooling_buckets)]) is True
            self.exit_bucket_counts[b]=self.exit_bucket_counts[b]+1 if supported else 0
        lost_windows=self.hold_fail.get(self.state,0)
        exit_ready={k for k in exit_entries if lost_windows>=EXPIRY_WINDOWS and self.exit_counts[k]>=exit_required[k]}
        exit_div_buckets=[b for b,n in self.exit_bucket_counts.items() if n>=exit_required['分歧']]
        if not exit_div_buckets:exit_ready.discard('分歧')
        for k,bs in buckets.items():
            for b in ('10','20','30'):self.bucket_counts[(k,b)]=self.bucket_counts[(k,b)]+1 if b in bs else 0
        mature={k for k,n in self.counts.items() if n>=required[k]}
        confirmed={k:[b for b in bs if self.bucket_counts[(k,b)]>=required[k]] for k,bs in buckets.items()}
        for k,bs in confirmed.items():
            if not bs:mature.discard(k)
        new5=any(p['new_group_negative_events'] for p in points)
        recent_risk=any(p['recent_group_negative_15'] for p in points)
        accelerating=vote([cmp(p,'Q10','ge',10) for p in points]) is True
        extreme=entries['急剧退潮'] is True
        new_extreme=extreme and not self.extreme;self.extreme=extreme
        quiet_exit='整理' in mature and self.state=='退潮' and not(new5 or accelerating or new_extreme)
        quiet_preserve=self.state=='整理' and holds['整理'] is True and not(recent_risk or accelerating or new_extreme)
        # A confirmed quiet interval is stronger evidence than the expiry-only
        # broad-decline branch. Existing acute/risk logic keeps its priority.
        if '整理' in mature and not(new5 or accelerating or new_extreme):exit_ready.discard('退潮')
        risk=('急剧退潮' in mature or '退潮' in mature) and not quiet_exit and not quiet_preserve
        if self.state=='整理':
            # Leaving a quiet band upward (or a missing rolling window after
            # lunch) is not fresh deterioration. Require independent new loss.
            downward=vote([cmp(p,'V30','lt',-.5) for p in points]) is True
            risk=risk and (recent_risk or accelerating or new_extreme or downward)
        order=[]
        if risk:order.append('退潮')
        if self.state in ('启动','主升','高潮') and '分歧' in mature:order.append('分歧')
        order += ['高潮','主升']
        if self.state=='退潮':order+=['分歧']
        order+=['启动','整理']
        if self.state is None:order=['退潮','高潮','主升','启动','整理','分歧']
        chosen=None;expiry_path=False
        for target in order:
            if target==self.state:continue
            if self.state is not None and target not in ALLOWED[self.state]:continue
            if target=='退潮':eligible=risk
            else:eligible=target in mature
            if target=='主升' and self.state=='高潮':eligible=eligible and self.hold_fail['高潮']>=3
            danger=target=='退潮' or target=='分歧' and self.state in ('启动','主升','高潮')
            if eligible and (self.state is None or self.age>=6 or danger):chosen=target;break
        if chosen is None:
            for target in ('退潮','分歧','启动'):
                if target in exit_ready and target in ALLOWED.get(self.state,set()):
                    chosen=target;expiry_path=True;break
        exit_evidence={'failed_windows':lost_windows,'entries':exit_entries,'counts':dict(self.exit_counts),
                       'required':exit_required,'ready':sorted(exit_ready),'divergence_buckets':exit_div_buckets}
        event=None
        if chosen:
            old=self.state
            if chosen=='整理':
                self.cycle_open=False;self.closed_at=at;self.start_cohort.clear();self.high_cohort.clear()
                self.baseline=None;self.climax_bucket=None;self.main_bucket=None
            elif old in (None,'整理'):
                self.cycle+=1;self.cycle_open=True
            elif old in ('退潮','分歧','主升') and chosen=='启动':
                # A restart is a new cycle, with its own startup/high-tier cohort
                # and breadth baseline; past price/risk observations stay causal.
                self.cycle+=1;self.cycle_open=True
                self.high_cohort.clear();self.baseline=None;self.climax_bucket=None;self.main_bucket=None
            if chosen=='启动':self.start_cohort=set(firsts)
            if chosen=='主升':self.main_bucket=confirmed['主升'][0]
            if chosen=='高潮':
                self.climax_bucket=confirmed['高潮'][0]
                holds['高潮']=True  # The confirmed entry also satisfies this bucket's weaker hold gate.
            event={'at':at,'from':old,'to':chosen,'cycle_id':self.cycle,'path':'持续失效退出' if expiry_path else 'seed' if old is None else '静稳退出' if quiet_exit else '急剧退潮' if chosen=='退潮' and '急剧退潮' in mature else '直接激活' if old=='整理' and chosen=='主升' else chosen,
                   'confirmed_windows':dict(self.counts),'support_buckets':exit_div_buckets if expiry_path and chosen=='分歧' else confirmed.get(chosen,[]),
                   'exit_evidence':exit_evidence if expiry_path else None}
            self.state,self.since,self.age=chosen,at,0
            self.counts.clear();self.bucket_counts.clear();self.hold_fail.clear()
            self.exit_counts.clear();self.exit_bucket_counts.clear()
        # High-tier membership starts only after the current complete window confirms it.
        self.high_cohort|={c for rows in points[-1]['ladder'].values() for c,h in rows if c in stable and h is not None and h>=5}
        if self.state in ('主升','高潮') and (event or self.baseline is None and holds[self.state] is True):
            vals=points[-1]['_values'].copy();self.baseline={'at':at,'values':vals,'anchors':points[-1].get('_anchors',{}).copy()}
        support=holds.get(self.state)
        if support is True:self.last_support=at
        reasons=[]
        if support is None:
            if not quality:reasons.append('行情覆盖不足')
            if self.state=='整理' and any(p[k] is None for p in points for k in ('A30','V30','Q10')):reasons.append('滚动观察窗口或共同样本不足')
            if self.state=='分歧' and any(p['contraction'] is None for p in points):reasons.append('收缩基准的可比价格覆盖不足')
            if self.state=='退潮' and any(p[k] is None for p in points for k in ('high_negative','core_failure','startup_failure')):reasons.append('固定跟踪组反馈未知')
            if not reasons:reasons.append('必要判据尚不可计算')
        status='；'.join(reasons)+'，沿用已确认阶段' if support is None else '支持成立' if support else '支持持续失效，转出证据待确认' if self.hold_fail.get(self.state,0)>=EXPIRY_WINDOWS else '支持减弱，延续'
        facts={k:v for k,v in points[-1].items() if not k.startswith('_')}
        return {'at':at,'day':day,'session':session,'state':self.state,'since':self.since,'cycle_id':self.cycle,
                'quality':quality,'support':support,'status':status,'support_unknown_reasons':reasons,'exit_evidence':exit_evidence,
                'last_support':self.last_support,'entry':entries,'hold':holds,'event':event,'age_windows':self.age,
                'fresh_first_codes':sorted(fresh),'stable_codes':sorted(stable),'support_buckets':confirmed,
                'main_support_bucket':self.main_bucket,
                'startup_cohort':sorted(self.start_cohort),'high_cohort':sorted(self.high_cohort),'facts':facts}
