"""Five-minute accepted state machines over causal minute observations.

The restart checkpoint carries rolling observations and machine memory. Daily
OHLC can update the next day's cohorts, but cannot manufacture intraday windows.
"""
from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timedelta

import numpy as np

from app.services.short_sentiment.facts import CN, instant
from app.services.short_sentiment.market_environment import MarketEnvironment, RULE_VERSION as MARKET_VERSION
from app.services.short_sentiment.market_environment_features import direction_groups, market_points
from app.services.short_sentiment.theme_environment import ALLOWED, Machine, VERSION as THEME_VERSION
from app.services.short_sentiment.theme_environment_features import advance_levels, make_day

VERSION = 'short-sentiment-market-v4.2-theme-v2.4'


def encode(value):
    """JSON-only checkpoint; preserve counter tuple keys without executable data."""
    if isinstance(value, datetime):
        return {'$datetime': value.isoformat()}
    if isinstance(value, Counter):
        return {'$counter': [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, deque):
        return {'$deque': [encode(v) for v in value], 'maxlen': value.maxlen}
    if isinstance(value, (set, tuple)):
        return {'$set' if isinstance(value, set) else '$tuple': [encode(v) for v in value]}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if '$datetime' in value:
        return datetime.fromisoformat(value['$datetime'])
    if '$counter' in value:
        return Counter({decode(k): decode(v) for k, v in value['$counter']})
    if '$deque' in value:
        return deque((decode(v) for v in value['$deque']), maxlen=value['maxlen'])
    if '$set' in value:
        return {decode(v) for v in value['$set']}
    if '$tuple' in value:
        return tuple(decode(v) for v in value['$tuple'])
    return {k: decode(v) for k, v in value.items()}


def dump_machine(machine):
    return encode({k: v for k, v in vars(machine).items() if k not in {'parameters', 'events', 'leader_events'}})


def restore_machine(cls, state):
    machine = cls()
    if state:
        machine.__dict__.update(decode(state))
    return machine


def source_rows(stocks):
    return {c: {'code': c, 'ref': s.get('previous_close'), 'upper': s.get('limit_up_price'),
                'lower': s.get('limit_down_price'), 'close': s.get('price'), 'board': s['board_type'],
                'excluded': s.get('excluded_reason'), 'error': None if s.get('valid') else 'invalid_quote'}
            for c, s in stocks.items()}


def empty_emotion(at, *, theme=False, reason='awaiting_complete_five_minute_window'):
    return {'state': None, 'candidate_state': None, 'status': 'insufficient_data', 'state_since': None,
            'previous_state': None, 'intraday_change': 'unknown', 'extreme_tag': None,
            'evidence': [], 'counter_evidence': [], 'evaluated_at': at,
            'rule_version': THEME_VERSION if theme else MARKET_VERSION, 'method': 'five_minute_state_machine',
            'validated_for_trading': False, 'evaluation_interval_seconds': 300, 'support': None,
            'support_status': reason, 'missing_reason': reason, 'entry': {}, 'hold': {}, 'facts': {}}


def public_emotion(window, old=None, *, theme=False):
    old = old or {}
    result = empty_emotion(window['at'] + '+08:00', theme=theme)
    state, support = window['state'], window['support']
    candidates = window.get('candidates', [])
    result.update(state=state, state_since=window.get('since') or window.get('state_since'),
        previous_state=old.get('state') if state != old.get('state') else old.get('previous_state'),
        status='confirmed' if state else 'insufficient_data',
        candidate_state=candidates[0]['state'] if candidates else None, candidates=candidates,
        intraday_change='stable' if state == old.get('state') else 'unknown',
        support=support, support_status=window['status'], last_supported_at=window.get('last_support') or window.get('last_supported_at'),
        entry=window['entry'], hold=window['hold'], facts=window['facts'],
        evidence=[{'condition': k, 'passed': v} for k, v in window['entry'].items()],
        counter_evidence=[{'condition': k, 'passed': v} for k, v in window['hold'].items() if v is not True],
        age_windows=window['age_windows'], quality=window['quality'],
        missing_reason=None if support is not None else 'support_unknown',
        support_unknown_reasons=window.get('support_unknown_reasons', []))
    for key in ('entry_points', 'hold_points', 'structure', 'cycle_id', 'startup_cohort', 'high_cohort',
                'exit_evidence', 'fresh_first_codes', 'stable_codes', 'support_buckets', 'main_support_bucket'):
        if key in window:
            result[key] = window[key]
    for key in ('state_since', 'last_supported_at'):
        if result.get(key) and datetime.fromisoformat(result[key]).tzinfo is None:
            result[key] += '+08:00'
    return encode(result)


class EnvironmentRuntime:
    def __init__(self, catalog, previous_sources):
        self.catalog = catalog
        self.previous_sources = previous_sources
        self.directions = direction_groups(catalog)

    def process(self, stocks, before, at, *, final=False):
        day = at[:10]
        saved = before.get('environment_runtime', {})
        compatible = saved.get('version') == VERSION and saved.get('classification_version') == self.catalog.version
        # Minute rows and encoded machine states are immutable inputs; new rows
        # and decoded machines are built below. Avoid copying the whole rolling
        # market matrix on every five-second poll.
        runtime = {**saved, 'themes': dict(saved.get('themes', {}))} if compatible else {'version': VERSION, 'classification_version': self.catalog.version,
                                                       'market': None, 'themes': {}, 'minutes': [], 'levels': {}, 'day': day}
        old_market = before.get('market', {}).get('emotion') if compatible else None
        old_themes = {t['theme_id']: t['emotion'] for t in before.get('themes', [])} if compatible else {}
        market = deepcopy(old_market) if old_market else empty_emotion(at)
        themes = {k: deepcopy(old_themes[k]) if k in old_themes else empty_emotion(at, theme=True)
                  for k in self.catalog.themes}
        if runtime['day'] != day:
            runtime['levels'] = advance_levels(list(self.previous_sources), self.previous_sources, runtime['levels'], runtime['day'])
            runtime.update(day=day, minutes=[], last_minute=None, evaluated_at=None)
        # A final daily row preserves the last real window. No synthetic 15:00 decision.
        if final:
            for emotion in [market, *themes.values()]:
                emotion['is_carried_to_close'] = True
                if not runtime.get('evaluated_at') or runtime['evaluated_at'][:10] != day:
                    emotion.update(support=None, support_status='no_intraday_environment_window_for_day',
                                   missing_reason='no_intraday_environment_window_for_day', quality=False)
            return market, themes, runtime, False
        end = instant(at).astimezone(CN).replace(second=0, microsecond=0)
        tm = end.strftime('%H:%M')
        session_start = end.replace(hour=9 if end.hour < 12 else 13, minute=30 if end.hour < 12 else 0)
        now = instant(at)
        # A minute closes per security, not per network poll. A transient miss
        # in the last batch must not erase an earlier, still-fresh observation.
        cached = runtime.get('minute_quotes', {}) if runtime['day'] == day else {}
        usable = {c: q for c, q in cached.items()
                  if session_start <= instant(q['observed_at']) < end
                  and 0 < (end - instant(q['source_time'])).total_seconds() <= 30}
        prices = {c: q['price'] for c, q in usable.items()}
        refs_at_close = {c: q['previous_close'] for c, q in usable.items()}
        cached = {c: q for c, q in cached.items()
                  if instant(q['observed_at']) >= session_start
                  and 0 <= (now - instant(q['observed_at'])).total_seconds() <= 30
                  and -5 <= (now - instant(q['source_time'])).total_seconds() <= 30}
        for c, s in stocks.items():
            if s['eligible'] and s.get('source_time'):
                cached[c] = {k: s[k] for k in ('price', 'previous_close', 'limit_up_price', 'limit_down_price', 'source_time')}
                cached[c]['observed_at'] = at
            elif s.get('excluded_reason'):
                cached.pop(c, None)
        runtime['minute_quotes'] = cached
        if not ('09:31' <= tm <= '11:30' or '13:01' <= tm <= '14:55'):
            return market, themes, runtime, False
        end_label = end.replace(tzinfo=None).isoformat(timespec='minutes')
        if runtime.get('last_minute') and runtime['last_minute'] >= end_label:
            return market, themes, runtime, False
        rows = {r['at']: r for r in runtime['minutes'] if r['at'] > session_start.replace(tzinfo=None).isoformat(timespec='minutes')}
        # Close the preceding minute using observations received before its boundary.
        # No carry across missing minutes or a lunch break.
        rows[end_label] = {'at': end_label, 'prices': prices, 'refs': refs_at_close}
        begin = max(session_start + timedelta(minutes=1), end - timedelta(minutes=35))
        minutes = []
        while begin <= end:
            label = begin.replace(tzinfo=None).isoformat(timespec='minutes')
            minutes.append(rows.get(label, {'at': label, 'prices': {}}))
            begin += timedelta(minutes=1)
        runtime.update(minutes=minutes, last_minute=end_label)
        if end.minute % 5 or len(minutes) < 5:
            return market, themes, runtime, False
        codes = sorted(stocks)
        if not codes:
            return market, themes, runtime, False
        source = source_rows(stocks)
        for c, q in usable.items():
            if c in source:
                for field, key in (('ref', 'previous_close'), ('upper', 'limit_up_price'), ('lower', 'limit_down_price')):
                    if source[c][field] is None:
                        source[c][field] = q[key]
        # Current quote errors affect only that minute's prices. Reference/limit
        # metadata is usable for earlier stored observations of this same day.
        for s in source.values():
            s['error'] = None
        z = {'codes': codes, 'times': [r['at'][11:] for r in minutes],
             'prices': np.array([[r['prices'].get(c, np.nan) for c in codes] for r in minutes]),
             'volume': np.zeros((len(minutes), len(codes))), 'origin': np.ones((len(minutes), len(codes)))}
        bar_refs = np.array([[r.get('refs', {}).get(c, np.nan) for c in codes] for r in minutes])
        refs = np.array([source[c]['ref'] if source[c]['ref'] is not None else np.nan for c in codes])
        z['prices'][np.isfinite(bar_refs) & (np.rint(bar_refs*100) != np.rint(refs*100))] = np.nan
        model = restore_machine(MarketEnvironment, runtime['market'])
        points = market_points(day, source, self.previous_sources, z, self.directions)
        window = model.step(points[-5:])
        market = public_emotion(window, old_market)
        market['data_quality'] = {
            'policy': 'market_coverage_with_local_diagnostics_v1',
            'minimum_coverage': .98,
            'window_minimum_coverage': min(p['coverage'] for p in points[-5:]),
            'board_minimum_coverage': {b: min(p['board_coverage'].get(b, 0) for p in points[-5:])
                                       for b in points[-1]['board_coverage']},
            'incomplete_directions': sorted({d['name'] for p in points[-5:] for d in p['directions']
                                             if d['coverage'] < .98}),
        }
        runtime['market'] = dump_machine(model)
        for theme_id in self.catalog.themes:
            if theme_id == 'unclassified':
                themes[theme_id] = empty_emotion(at, theme=True, reason='unclassified_membership')
                themes[theme_id]['status'] = 'unclassified'
                continue
            members = [c for c in codes if theme_id in self.catalog.ids(c)]
            heights = {c: stocks[c]['confirmed_streak_before_today'] for c in members}
            model = restore_machine(Machine, runtime['themes'].get(theme_id))
            data = make_day(day, members, z, source, self.previous_sources, heights, {}, runtime['levels'])
            window = model.tick(data['points'][-5:], day + ('_AM' if end.hour < 12 else '_PM'))
            window['candidates'] = [{'state': '退潮' if k == '急剧退潮' else k, 'condition': k, 'windows': n,
                'required': 1 if k == '急剧退潮' else 3 if k not in ('退潮', '分歧') or k == '分歧' and model.state == '退潮' else 2}
                for k, n in model.counts.items() if n and k != model.state and
                (model.state is None or ('退潮' if k == '急剧退潮' else k) in ALLOWED[model.state])]
            themes[theme_id] = public_emotion(window, old_themes.get(theme_id), theme=True)
            runtime['themes'][theme_id] = dump_machine(model)
        runtime['evaluated_at'] = end_label
        return market, themes, runtime, True
