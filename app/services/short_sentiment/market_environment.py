"""大盘六阶段 V4.2：有目标证据的恢复承接。

百分数使用 0—100，涨幅使用 r10。持久化、采集和题材情绪不由本模块负责。
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

RULE_VERSION = 'short-sentiment-market-v4.2-experiment'
STATES = ('沸点', '过热', '微热', '微冷', '过冷', '冰点')
STATE_RANK = {state: rank for rank, state in enumerate(reversed(STATES))}
# Negative evidence is nested by severity. Resolve the most severe state first.
DECISION_PRIORITY = ('冰点', '过冷', '沸点', '过热', '微热', '微冷')
WARM_STATES = ('微热', '过热', '沸点')
WARM_RANK = {state: rank for rank, state in enumerate(WARM_STATES)}
ROUTES = {
    '冰点': (('急剧冰点', 1), ('冰点', 2)),
    '过冷': (('急剧过冷', 1), ('过冷_深跌', 2), ('过冷_普跌', 2)),
    '沸点': (('沸点', 2),), '过热': (('过热', 2),), '微热': (('微热', 2),),
    '微冷': (('微冷_下行', 2), ('微冷_主线转弱', 2), ('微冷_收缩', 3)),
}
BRIDGES = (
    ('过冷', '过冷_冰点恢复承接'),
    ('微热', '微热_偏热降级承接'),
    ('微冷', '微冷_冷态恢复承接'),
    ('微冷', '微冷_偏热退出承接'),
)
METRICS = ('B', 'M', 'U', 'D5', 'D3', 'N', 'K', 'active', 'C1', 'C2', 'top_name',
           'coverage', 'delta15_B', 'delta15_M', 'delta15_D3', 'delta15_N')


@dataclass(frozen=True)
class Parameters:
    ice_b: float = 10.0
    ice_m: float = -2.0
    ice_d3: float = 25.0
    ice_d5: float = 10.0
    broad_cold_b: float = 15.0
    broad_cold_m: float = -1.5
    broad_cold_d3: float = 15.0
    cooling_b: float = 50.0
    leader_share: float = 10.0
    leader_enrichment: float = 2.0
    leader_relative_m: float = .8
    leader_dominance: float = 1.25


def conjunction(*values):
    return False if any(v is False for v in values) else None if any(v is None for v in values) else True


def vote(values):
    if len(values) != 5:
        raise ValueError('A decision requires five minute points')
    return None if any(v is None for v in values) else bool(values[-1] and sum(values) >= 4)


def compact(point):
    return {'at': point['at'], **{k: point.get(k) for k in METRICS}}


def structure_support(direction, *, holding=False):
    """封板数量或昨日固定强势组反馈，缺少旧组不阻断新首板扩散。"""
    if direction['limit_up'] >= (1 if holding else 3):
        return True
    if not direction['previous_strong_quality']:
        return None
    return (direction['previous_strong_M'] >= (-.5 if holding else 0)
            and direction['previous_strong_failure_upper'] <= (30 if holding else 20))


class MarketEnvironment:
    def __init__(self, parameters=None):
        self.parameters = parameters or Parameters()
        self.state = self.state_since = self.last_supported_at = None
        self.session = self.previous_at = None
        self.age = self.hold_failed = 0
        self.runs = Counter()
        self.events = []
        self.leader = None
        self.leader_candidate = None
        self.leader_run = self.leader_hold_failed = 0
        self.leader_events = []
        self.retired_leaders = []
        self.trade_days = deque(maxlen=5)

    def leader_candidate_at(self, point):
        p = self.parameters
        if not point['quality'] or not point['structure_quality']:
            return None
        candidates = [d for d in point['directions'] if d['quality'] and d['N'] >= 20
                      and d['B'] >= 70 and d['M'] >= 1 and d['U'] >= 20
                      and d['relative_M'] >= p.leader_relative_m
                      and d['enrichment'] is not None and d['enrichment'] >= p.leader_enrichment
                      and d['credit'] >= 10 and structure_support(d) is True]
        candidates.sort(key=lambda d: (-d['credit'], d['id']))
        if not candidates or point['K'] < 30:
            return False
        top = candidates[0]
        second = candidates[1]['credit'] if len(candidates) > 1 else 0
        return top['id'] if top['C'] >= p.leader_share and top['credit'] >= p.leader_dominance*second else False

    def leader_supported_at(self, point):
        if not self.leader:
            return False
        if not point['quality'] or not point['structure_quality']:
            return None
        d = next((d for d in point['directions'] if d['id'] == self.leader['id']), None)
        if d is None or not d['quality']:
            return None
        return conjunction(d['B'] >= 55, d['M'] >= 0, d['U'] >= 10, d['C'] is not None and d['C'] >= 6,
                           d['relative_M'] >= .3, d['enrichment'] is not None and d['enrichment'] >= 1.3,
                           structure_support(d, holding=True))

    def guards(self, point):
        route_names = ([name for routes in ROUTES.values() for name, _ in routes] +
                       ['恢复', '冰点恢复'] + [name for _, name in BRIDGES])
        if not point['quality']:
            return dict.fromkeys(route_names), dict.fromkeys(STATES)
        p = self.parameters
        b, m, u, d5, d3 = (point[k] for k in ('B', 'M', 'U', 'D5', 'D3'))
        ice = b <= p.ice_b and m <= p.ice_m and (d3 >= p.ice_d3 or d5 >= p.ice_d5)
        ice_hold = b < 15 and m < -1.5 and (d3 >= 20 or d5 >= 8)
        leader = self.leader_supported_at(point)
        former_weak = bool(self.former_leader_weakness(point))
        entry = {
            '冰点': ice,
            '急剧冰点': b <= 5 and m <= -3 and d5 >= 15,
            '微热': b >= 55 and m >= 0 and d5 < 5,
            '过热': conjunction(b >= 60 and m >= .5 and u >= 10 and d5 < 5,
                              point['active'] >= 3 if point['structure_quality'] else None),
            '沸点': conjunction(b >= 55 and m >= .5 and u >= 8 and d5 < 5, leader),
            '微冷_下行': b <= 45 and m <= -.5,
            '微冷_收缩': self.state in WARM_STATES and b < p.cooling_b and m < 0,
            '微冷_主线转弱': b < 55 and m < .5 and former_weak,
            '过冷_深跌': b <= 35 and m <= -1 and d5 >= 6,
            '过冷_普跌': b <= p.broad_cold_b and m <= p.broad_cold_m and d3 >= p.broad_cold_d3,
            '急剧过冷': b <= 25 and m <= -1.5 and d5 >= 10,
            '恢复': b >= 25 and m > -1.5 and d5 < 6,
            '冰点恢复': not ice_hold,
        }
        hold = {'冰点': ice_hold,
                '微热': b >= 45 and m >= -.3 and d5 < 6,
                '过热': conjunction(b >= 50 and m >= 0 and u >= 7 and d5 < 6,
                                  point['active'] >= 2 if point['structure_quality'] else None),
                '沸点': conjunction(b >= 45 and m >= 0 and u >= 5 and d5 < 6, leader),
                '微冷': b < 60 and m < .5,
                '过冷': b < 40 and (d5 >= 6 or m <= -1)}
        prior = hold.get(self.state)
        failed = None if prior is None else not prior
        entry.update({
            '过冷_冰点恢复承接': conjunction(self.state == '冰点', failed, entry['冰点恢复'], hold['过冷']),
            '微热_偏热降级承接': conjunction(self.state in ('沸点', '过热'), failed, hold['微热']),
            '微冷_冷态恢复承接': conjunction(self.state in ('冰点', '过冷'), failed, entry['恢复'], hold['微冷']),
            '微冷_偏热退出承接': conjunction(self.state in WARM_STATES, failed, hold['微冷']),
        })
        return entry, hold

    def former_leader_weakness(self, point):
        if not point['quality'] or not point['structure_quality']:
            return []
        retired = {old['id'] for old in self.retired_leaders}
        return [{k:d[k] for k in ('id','name','B','M','D5')}
                for d in point['directions'] if d['id'] in retired and d['quality']
                and d['B'] <= 45 and d['M'] <= -.5 and d['D5'] >= 10]

    def update_leader(self, points, at):
        ids = [self.leader_candidate_at(p) for p in points]
        candidate = ids[-1] if ids[-1] and None not in ids and ids.count(ids[-1]) >= 4 else None
        self.leader_run = (self.leader_run+1 if candidate == self.leader_candidate else 1) if candidate else 0
        self.leader_candidate = candidate
        supported = vote([self.leader_supported_at(p) for p in points]) if self.leader else None
        if self.leader:
            self.leader_hold_failed = self.leader_hold_failed+1 if supported is False else 0
            if supported is True:
                self.leader['last_supported_at'] = at
            if self.leader_hold_failed >= 2:
                self.retired_leaders.append({**self.leader, 'retired_day': at[:10], 'retired_at': at})
                self.leader_events.append({'at': at, 'type': '失效', **self.leader})
                self.leader = None
                self.leader_hold_failed = 0
        if candidate and self.leader_run >= 6 and (not self.leader or candidate != self.leader['id']):
            if self.leader:
                self.retired_leaders.append({**self.leader, 'retired_day': at[:10], 'retired_at': at})
            d = next(d for d in points[-1]['directions'] if d['id'] == candidate)
            self.leader = {'id': candidate, 'name': d['name'], 'since': at, 'last_supported_at': at}
            self.leader_hold_failed = 0
            self.leader_events.append({'at': at, 'type': '确认', **self.leader})
            supported = True
        return {'current': dict(self.leader) if self.leader else None,
                'support': supported if self.leader else None,
                'candidate': candidate, 'windows': self.leader_run, 'required': 6,
                'candidate_name': next((d['name'] for d in points[-1]['directions'] if d['id']==candidate), None),
                'current_facts': next((dict(d) for d in points[-1]['directions'] if self.leader and d['id']==self.leader['id']), None),
                'minute_candidates': ids, 'hold_failures': self.leader_hold_failed}

    def step(self, points):
        if len(points) != 5:
            raise ValueError('Expected five minute observations')
        dates = [datetime.fromisoformat(p['at']) for p in points]
        if any(b-a != timedelta(minutes=1) for a,b in zip(dates,dates[1:])):
            raise ValueError('Minute observations must be contiguous')
        end = dates[-1]
        clock = end.strftime('%H:%M')
        if end.minute % 5 or end.second or end.microsecond or not ('09:35' <= clock <= '11:30' or '13:05' <= clock <= '14:55'):
            raise ValueError('Not a complete continuous-trading five-minute window')
        if self.previous_at and end <= self.previous_at:
            raise ValueError('Do not replay late or duplicate observations into a live state')
        at, day = points[-1]['at'], points[-1]['at'][:10]
        session = day + ('_AM' if end.hour < 12 else '_PM')
        boundary = session != self.session
        if boundary or self.previous_at and end-self.previous_at != timedelta(minutes=5):
            self.runs.clear()
            self.hold_failed = self.leader_hold_failed = self.leader_run = 0
            self.leader_candidate = None
        if not self.trade_days or self.trade_days[-1] != day:
            self.trade_days.append(day)
            self.retired_leaders = [d for d in self.retired_leaders if d['retired_day'] in self.trade_days]
        self.session, self.previous_at = session, end
        leader_before = dict(self.leader) if self.leader else None
        # A leader first confirmed at this boundary may support market entries
        # only in later windows; never backdate its confirmation into these points.
        per_minute = [self.guards(p) for p in points]
        former_leader_evidence = [{'at':p['at'], 'directions':self.former_leader_weakness(p)} for p in points]
        entry = {name: vote([e[name] for e,h in per_minute]) for name in per_minute[0][0]}
        hold = {name: vote([h[name] for e,h in per_minute]) for name in STATES}
        structure = self.update_leader(points, at)
        quality = all(p['quality'] for p in points)
        before, changed = self.state, False
        facts = compact(points[-1])
        if not quality:
            self.runs.clear()
            self.hold_failed = 0
        else:
            self.age += int(self.state is not None)
            for name,value in entry.items():
                self.runs[name] = self.runs[name]+1 if value is True else 0
            self.hold_failed = self.hold_failed+1 if hold.get(self.state) is False else 0
            for target in DECISION_PRIORITY:
                route = next(((name,n) for name,n in ROUTES[target] if self.runs[name] >= n), None)
                if route is None:
                    continue
                if target == self.state:
                    break
                if self.recovery_blocked(target):
                    continue
                if self.warm_downgrade_blocked(target):
                    continue
                if self.state and self.age < 4 and not self.adverse_transition(target):
                    continue
                name, n = route
                self.events.append({'at': at, 'day': day, 'session': session, 'from': self.state, 'to': target,
                                    'direction': '初始化' if self.state is None else '改善' if STATE_RANK[target] > STATE_RANK[self.state] else '恶化',
                                    'condition': name, 'required_windows': n, 'prior_age_windows': self.age,
                                    'prior_state_since': self.state_since, 'decision_runs': dict(self.runs), 'facts': facts,
                                    'type': '切换' if self.state else '初始化'})
                self.state, self.state_since, self.last_supported_at = target, at, None
                self.age = self.hold_failed = 0
                self.runs.clear()
                changed = True
                break
            # First-entry paths retain priority, especially acute deterioration.
            # Bridges apply only after known failure and a supported destination.
            if not changed and self.state and self.hold_failed >= 2 and self.age >= 4:
                for target, name in BRIDGES:
                    if self.runs[name] < 2 or self.recovery_blocked(target):
                        continue
                    self.events.append({
                        'at': at, 'day': day, 'session': session, 'from': self.state, 'to': target,
                        'direction': '改善' if STATE_RANK[target] > STATE_RANK[self.state] else '恶化',
                        'condition': name, 'required_windows': 2, 'prior_age_windows': self.age,
                        'prior_state_since': self.state_since, 'decision_runs': dict(self.runs),
                        'facts': facts, 'type': '切换', 'prior_failed_windows': self.hold_failed})
                    self.state, self.state_since, self.last_supported_at = target, at, None
                    self.age = self.hold_failed = 0
                    self.runs.clear()
                    changed = True
                    break
        support = hold.get(self.state) if quality else None
        if support is True:
            self.last_supported_at = at
        status = ('初始化未完成' if not self.state else '数据不足，阶段沿用' if support is None else
                  '阶段延续、支持持续减弱' if self.hold_failed >= 3 else
                  '阶段延续、支持减弱' if support is False else '当前证据支持')
        candidates = []
        for target,routes in ROUTES.items():
            if target == self.state:
                continue
            for name,n in routes:
                if not self.runs[name]:
                    continue
                blocked = []
                if self.runs[name] < n:
                    blocked.append('连续窗口不足')
                if self.state and self.age < 4 and not self.adverse_transition(target):
                    blocked.append('最短保持不足20分钟')
                blocked.extend(self.recovery_blocked(target))
                if self.warm_downgrade_blocked(target):
                    blocked.append('当前偏热阶段保持条件尚未连续失效2窗口')
                candidates.append({'state': target, 'condition': name, 'windows': self.runs[name], 'required': n, 'blocked': blocked})
        for target, name in BRIDGES:
            if target == self.state or not self.runs[name]:
                continue
            blocked = self.recovery_blocked(target)
            if self.runs[name] < 2: blocked.append('目标保持证据不足2窗')
            if self.hold_failed < 2: blocked.append('当前保持失效不足2窗')
            if self.age < 4: blocked.append('最短保持不足20分钟')
            candidates.append({'state': target, 'condition': name, 'windows': self.runs[name],
                               'required': 2, 'blocked': blocked})
        return {'day': day, 'session': session, 'at': at,
                'start': (dates[0]-timedelta(minutes=1)).isoformat(timespec='minutes'),
                'facts': facts, 'quality': quality, 'entry': entry, 'hold': hold,
                'entry_points': {name: sum(e[name] is True for e,h in per_minute) for name in entry},
                'hold_points': {name: sum(h[name] is True for e,h in per_minute) for name in hold},
                'range': {k: [min(p[k] for p in points),max(p[k] for p in points)]
                          if all(p.get(k) is not None for p in points) else [None,None]
                          for k in ('B','M','U','D5','D3','delta15_B','delta15_M','delta15_D3')},
                'minutes': [compact(p) for p in points], 'state': self.state, 'state_since': self.state_since,
                'last_supported_at': self.last_supported_at, 'status': status, 'support': support,
                'age_windows': self.age, 'hold_failures': self.hold_failed,
                'candidate_windows': dict(self.runs), 'candidates': candidates,
                'leader_before': leader_before, 'structure': structure,
                'former_leader_evidence': former_leader_evidence,
                'carried_across_session': bool(boundary and before and not changed),
                'rule_version': RULE_VERSION, 'parameters': asdict(self.parameters)}

    def adverse_transition(self, target):
        return (self.state is not None and target in ('微冷','过冷','冰点')
                and STATE_RANK[target] < STATE_RANK[self.state])

    def recovery_blocked(self, target):
        blocked = []
        if self.state == '冰点' and self.runs['冰点恢复'] < 2:
            blocked.append('冰点恢复条件不足2窗口')
        if ((self.state == '过冷' and target != '冰点')
                or (self.state == '冰点' and target != '过冷')) and self.runs['恢复'] < 2:
            blocked.append('过冷恢复条件不足2窗口')
        return blocked

    def warm_downgrade_blocked(self, target):
        # A warm stage's broader hold band must have an effect: briefly losing
        # its entry threshold must not relabel a still-supported environment.
        return (self.state in WARM_RANK and target in WARM_RANK
                and WARM_RANK[target] < WARM_RANK[self.state] and self.hold_failed < 2)
