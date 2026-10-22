"""One-day causal price features. Closing facts update tomorrow's cohorts only."""
from collections import Counter
import numpy as np
from app.services.short_sentiment.theme_environment import failure


def bucket(s):return '30' if s.get('board')=='bse' else '20' if s.get('board') in ('chinext','star') else '10'
def number(v):return float(v) if np.isfinite(v) else None
def cent(v):return round(v*100) if v is not None else np.nan


def close_value(s):
    if s.get('excluded') or s.get('error'):return None
    p,r,u,d=[s.get(k) for k in ('close','ref','upper','lower')]
    if None in (p,r,u,d) or not 0<d<r<u or not d<=p<=u:return None
    return 10*(p-r)/(u-r if p>=r else r-d)


def advance_heights(codes,src,prev,heights):
    out={}
    for c in codes:
        s=src.get(c,{});old=heights.get(c)
        if s.get('excluded')=='no_price_limit':out[c]=0
        elif close_value(s) is None:out[c]=None
        elif cent(s['close'])!=cent(s['upper']):out[c]=0
        elif old is not None and (old==0 or bucket(s)==bucket(prev.get(c,{}))):out[c]=old+1
        else:out[c]=None
    return out


def advance_levels(codes,src,levels,day):
    """Call only after processing the day. Daily references neutralize splits.

    A missing daily link starts a new epoch; it must never be bridged by an old
    price anchor. Daily closes cannot affect any same-day intraday decision.
    """
    out={}
    for c in codes:
        s=src.get(c,{});old=levels.get(c,{'value':1.,'epoch':'seed'})
        close,ref=cent(s.get('close')),cent(s.get('ref'))
        if not s.get('error') and np.isfinite(close) and np.isfinite(ref) and close>0 and ref>0:
            out[c]={'value':old['value']*close/ref,'epoch':old['epoch']}
        else:out[c]={'value':1.,'epoch':day}
    return out


def make_day(day,codes,z,src,prev,heights,lifecycle,levels=None):
    lookup={str(c):i for i,c in enumerate(z['codes'])};indices=[lookup[c] for c in codes]
    prices=z['prices'][:,indices].copy();volumes=z['volume'][:,indices].copy()
    refs=np.array([cent(src.get(c,{}).get('ref')) for c in codes]);ups=np.array([cent(src.get(c,{}).get('upper')) for c in codes]);downs=np.array([cent(src.get(c,{}).get('lower')) for c in codes])
    expected=np.array([not src.get(c,{}).get('excluded') and not lifecycle.get(c,{}).get('ipoDate','')>day for c in codes],dtype=bool)
    ok=expected & np.array([not src.get(c,{}).get('error') for c in codes],dtype=bool)
    pc=np.rint(prices*100)
    valid=np.isfinite(pc)&ok&(downs>0)&(refs>downs)&(ups>refs)&(pc>=downs)&(pc<=ups)
    with np.errstate(invalid='ignore',divide='ignore'):
        vals=10*(pc-refs)/np.where(pc>=refs,ups-refs,refs-downs)
        raw=100*(pc-refs)/refs
    vals[~valid]=np.nan;raw[~valid]=np.nan;prices[~valid]=np.nan
    up=valid&(pc==ups);down=valid&(pc==downs)
    middle=np.array([heights.get(c) is not None and 1<=heights[c]<=3 for c in codes],dtype=bool)
    high=np.array([heights.get(c) is not None and heights[c]>=5 for c in codes],dtype=bool)
    core=np.array([(heights.get(c) is not None and heights[c]>=2) or (close_value(prev.get(c,{})) is not None and close_value(prev[c])>=5) for c in codes],dtype=bool)
    groups={label:[c for c,m in zip(codes,mask) if m] for label,mask in [('middle',middle),('high',high),('core',core)]}
    pools={b:[c for c,m in zip(codes,middle) if m and bucket(src.get(c,{}))==b] for b in ('10','20','30')}
    total=int(expected.sum());points=[]
    anchors={}
    for j,c in enumerate(codes):
        old=(levels or {}).get(c,{'value':1.,'epoch':'seed'});origin=old['value']
        if ok[j] and np.isfinite(refs[j]) and 0<downs[j]<refs[j]<ups[j]:
            anchors[c]={'origin':origin,'up':origin*(ups[j]-refs[j])/refs[j],
                        'down':origin*(refs[j]-downs[j])/refs[j],'epoch':old['epoch']}
    for i,tm in enumerate(z['times']):
        observed=vals[i,valid[i]];n=len(observed)
        ladder={b:[] for b in ('10','20','30')}
        for j in np.flatnonzero(up[i]):
            c=codes[j];h=heights.get(c);ladder[bucket(src[c])].append([c,h+1 if h is not None else None])
        evidence={b:{'success':[],'blocked':[]} for b in pools}
        for j,c in enumerate(codes):
            if not middle[j] or not valid[i,j]:continue
            b=bucket(src[c])
            if up[i,j]:evidence[b]['success'].append(c)
            elif vals[i,j]<=-1:evidence[b]['blocked'].append(c)
        cb=core&valid[i]&(vals[i]<=-3)
        p={'at':day+'T'+str(tm),'N':n,'expected_N':total,'coverage':n/total if total else 0,'quality':n>=10 and n>=.98*total,
           'B':float((observed>0).mean()*100) if n else None,'M':float(np.median(observed)) if n else None,
           'U':float((observed>=3).mean()*100) if n else None,'D5':float((observed<=-5).mean()*100) if n else None,
           'limit_up':int(up[i].sum()),'limit_down':int(down[i].sum()),'limit_down_codes':[codes[j] for j in np.flatnonzero(down[i])],
           'ladder':ladder,'groups':groups,'middle_pools':pools,'competition_evidence':evidence,
           'core_failure':failure(int(cb.sum()),int((core&valid[i]).sum()),int(core.sum()),2,.5),
           'core_failure_codes':[codes[j] for j in np.flatnonzero(cb)],'previous_mid_high':any(h is not None and h>=2 for h in heights.values()),
           'A30':None,'V30':None,'Q10':None,'_values':{c:number(v) for c,v in zip(codes,vals[i])},
           '_expected':[c for c,yes in zip(codes,expected) if yes],
           '_anchors':anchors,
           '_levels':{c:anchors[c]['origin']*pc[i,j]/refs[j] if c in anchors and valid[i,j] else None for j,c in enumerate(codes)}}
        start=0 if i<120 else 120
        for lag in (10,30):
            if i-start<lag:continue
            common=expected&valid[i-lag:i+1].all(axis=0)
            if not total or common.sum()<.98*total:continue
            change=vals[i,common]-vals[i-lag,common]
            if lag==10:p['Q10']=float((change<=-1.5).mean()*100)
            else:p['A30']=float(np.median(np.ptp(vals[i-lag:i+1,common],axis=0)));p['V30']=float(np.median(change))
        p['n1']=sum(h==1 for rows in ladder.values() for c,h in rows)
        p['nM']=sum(h is not None and 2<=h<=4 for rows in ladder.values() for c,h in rows)
        p['nH']=sum(h is not None and h>=5 for rows in ladder.values() for c,h in rows)
        points.append(p)
    return {'codes':codes,'prices':prices,'volume':volumes,'raw_returns':raw,'up':up,'down':down,'valid':valid,'points':points,
            'refs':refs/100,'uppers':ups/100,'lowers':downs/100,'expected':expected,
            'audit':{'day':day,'expected_N':total,'minimum_coverage':min(p['coverage'] for p in points),
                     'quality_minutes':sum(p['quality'] for p in points),'real_time_valid_cells':int(((z['origin'][:,indices]==1)&valid).sum()),
                     'historical_valid_cells':int(((z['origin'][:,indices]==2)&valid).sum())}}
