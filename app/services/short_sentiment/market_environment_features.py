"""Causal market V4.2 features shared by live windows and historical validation.

Formulas are identical to the accepted replay; input prices are raw minute closes.
"""
import numpy as np


def cents(values):
    return np.array([round(v*100) if v is not None else np.nan for v in values])


def direction_groups(catalog):
    themes = [dict(id=t['theme_id'], name=t['name'], codes={c for c, ids in catalog.by_code.items() if t['theme_id'] in ids})
              for t in catalog.themes.values() if t['theme_id'] != 'unclassified']
    groups = []
    for theme in sorted(themes, key=lambda t: t['id']):
        for group in groups:
            if theme['codes'] and all(other['codes'] and len(theme['codes'] & other['codes']) / min(len(theme['codes']), len(other['codes'])) >= .6 for other in group):
                group.append(theme)
                break
        else:
            groups.append([theme])
    return [{'id': 'direction_' + g[0]['id'][6:], 'name': ' / '.join(t['name'] for t in g),
             'theme_ids': [t['id'] for t in g], 'codes': sorted(set().union(*(t['codes'] for t in g)))} for g in groups]


def metrics(values,expected_count,minimum):
    n=len(values)
    return {'N':n,'expected_N':expected_count,'coverage':n/expected_count if expected_count else 1.,
            'quality':bool(n>=minimum and expected_count and n/expected_count>=.98),
            'B':float((values>0).sum()/n*100) if n else None,
            'M':float(np.median(values)) if n else None,
            'U':float((values>=3).sum()/n*100) if n else None,
            'D5':float((values<=-5).sum()/n*100) if n else None,
            'K':int((values>=3).sum())}


def market_points(day, source, previous, z, directions):
    codes=list(z['codes']); times=[day+'T'+str(t) for t in z['times']]
    expected=np.array([not source[c].get('excluded') for c in codes])
    boards=np.array([source[c]['board'] for c in codes])
    pc=np.rint(z['prices']*100)
    refs=cents([source[c]['ref'] for c in codes]);ups=cents([source[c]['upper'] for c in codes]);downs=cents([source[c]['lower'] for c in codes])
    valid=np.isfinite(pc)&expected&np.array([not source[c].get('error') for c in codes])&(downs>0)&(refs>downs)&(ups>refs)&(pc>=downs)&(pc<=ups)
    if 'previous' in z:
        bar_refs=np.rint(z['previous']*100)
        valid &= ~(np.isfinite(bar_refs)&(bar_refs!=refs))
    with np.errstate(invalid='ignore',divide='ignore'):
        r10=10*(pc-refs)/np.where(pc>=refs,ups-refs,refs-downs)
    r10[~valid]=np.nan
    sets=[set(d['codes']) for d in directions]
    membership=np.array([[c in s for c in codes] for s in sets], dtype=bool).reshape(len(sets), len(codes))
    counts=membership.sum(axis=0);weights=membership/np.maximum(counts,1)
    pools=membership&expected;board_names=sorted(set(boards[expected]))
    observed=expected&valid;strongs=observed&(r10>=3)
    contributions=strongs.astype(float)@weights.T
    index={c:i for i,c in enumerate(codes)}
    previous_eligible=[r for r in previous.values() if r['excluded'] is None]
    good=lambda r:r['error'] is None and all(r[k] is not None for k in ('ref','upper','lower','close')) and r['upper']>r['ref']>r['lower']>0
    previous_quality=bool(previous_eligible) and all(
        sum(good(r) for r in previous_eligible if r['board']==b)/sum(r['board']==b for r in previous_eligible)>=.98
        for b in {r['board'] for r in previous_eligible})
    strong_codes,limit_codes=set(),set()
    for r in previous_eligible:
        if not good(r):continue
        c,ref,upper=cents([r['close'],r['ref'],r['upper']])
        if 2*(c-ref)>=upper-ref:strong_codes.add(r['code'])
        if c==upper:limit_codes.add(r['code'])
    groups=[strong_codes&s for s in sets]
    prices=np.rint(z['prices']*100)
    upper=cents([source[c]['upper'] for c in codes])
    rows=[]
    for i in range(len(times)):
        seen=observed[i];strong=strongs[i]
        m=metrics(r10[i,seen],int(expected.sum()),100)
        board_coverage={b:float((seen&(boards==b)).sum()/(expected&(boards==b)).sum()) for b in board_names}
        # Breadth is a whole-market statistic. Keep its >=98% gate and report
        # local coverage separately; one thinly traded board must not veto an
        # otherwise complete all-market window. Direction gates remain local.
        ds=[]
        for j,g in enumerate(directions):
            mask=pools[j]&valid[i]
            d=metrics(r10[i,mask],int(pools[j].sum()),10)
            d.update(id=g['id'],name=g['name'],credit=float(contributions[i,j]),
                     C=float(contributions[i,j]/m['K']*100) if m['K'] else None)
            ds.append(d)
        structure=all(d['coverage']>=.98 for d in ds)
        active=[d['id'] for d in ds if d['quality'] and d['B']>=60 and d['M']>=.5 and d['U']>=15 and d['credit']>=10]
        ranking=sorted(ds,key=lambda d:(-d['credit'],d['id']));top=ranking[0] if ranking else {'C':None,'id':None,'name':None}
        m.update(day=day,at=times[i],kind='minute',board_coverage=board_coverage,
                 directions=ds,structure_quality=structure,active=len(active) if structure else None,active_ids=active,
                 C1=top['C'] if structure else None,C2=ranking[1]['C'] if structure and len(ranking)>1 else None,
                 top_id=top['id'] if structure and m['K'] else None,top_name=top['name'] if structure and m['K'] else None,
                 unclassified_strong=int((strong&(counts==0)).sum()),
                 unclassified_share=float((strong&(counts==0)).sum())/m['K']*100 if m['K'] else None)
        m['D3']=float(np.mean(r10[i,seen]<=-3)*100) if seen.any() else None
        m.update(delta15_B=None,delta15_M=None,delta15_D3=None,delta15_N=None)
        if i >= 15 and (times[i][11:13] < '12') == (times[i-15][11:13] < '12'):
            common=expected&valid[i-15:i+1].all(axis=0)
            if common.sum()>=100 and all((common&(boards==b)).sum()/(expected&(boards==b)).sum()>=.98 for b in board_names):
                old_values,new_values=r10[i-15,common],r10[i,common]
                m.update(delta15_B=float((np.mean(new_values>0)-np.mean(old_values>0))*100),
                         delta15_M=float(np.median(new_values)-np.median(old_values)),
                         delta15_D3=float((np.mean(new_values<=-3)-np.mean(old_values<=-3))*100),delta15_N=int(common.sum()))
        for j,d in enumerate(ds):
            base_share=float(weights[j,seen].sum()/seen.sum()*100) if seen.any() else 0.
            group=groups[j];observed_group=[index[c] for c in group if c in index and valid[i,index[c]]]
            gv=r10[i,observed_group];missing=len(group)-len(gv)
            limit_now=membership[j]&seen&(prices[i]==upper)
            d.update(base_share=base_share,enrichment=d['C']/base_share if base_share and d['C'] is not None else None,
                relative_M=d['M']-m['M'] if d['M'] is not None else None,limit_up=int(limit_now.sum()),
                continuation=int(sum(limit_now[index[c]] for c in limit_codes if c in index)),
                previous_strong_quality=bool(previous_quality and len(group)>=3 and len(gv)/len(group)>=.98),
                previous_strong_N=len(group) if previous_quality else None,previous_strong_seen=len(gv) if previous_quality else None,
                previous_strong_M=float(np.median(gv)) if previous_quality and len(gv) else None,
                previous_strong_failure_upper=float(((gv<=-5).sum()+missing)/len(group)*100) if previous_quality and group else None)
        rows.append(m)
    return rows
