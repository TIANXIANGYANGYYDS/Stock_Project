"""Observable price/amount alerts. No inference about trade initiators or funds."""
from datetime import datetime

from app.services.short_sentiment.facts import CN, MAX_GAP_SECONDS, instant

VERSION = 'price-behavior-v1'
LABELS = {'first_touch': '首次触板', 'first_seal': '首次封板', 'open': '打开涨停板',
          'reseal': '回封涨停', 'limit_down': '触及跌停', 'open_limit_down': '打开跌停板',
          'rapid_rise': '暴力拉升', 'rapid_fall': '急速下跌', 'quick_rebound': '快速反弹',
          'high_dive': '高台跳水', 'volume_surge': '放量异动', 'emotion_changed': '环境切换'}
WINDOW_SECONDS = 300
COOLDOWN_SECONDS = 300


def session(at):
    tm = at.astimezone(CN).strftime('%H:%M:%S')
    return at.astimezone(CN).date().isoformat() + ('/am' if '09:30:00' <= tm <= '11:30:00' else
                                                   '/pm' if '13:00:00' <= tm <= '14:57:00' else '/off')


def price_events(stocks, prior, runtime, observed_at, *, final=False):
    """Bounded samples; crossing triggers + cooldown survive worker restarts.

    Histories require monotonic source times with <=15s gaps, the same provider,
    and continuous trading. Duplicate quotes do not advance any detector.
    """
    if final:
        return [], {}
    state, events = {}, []
    for code, stock in stocks.items():
        at = instant(stock.get('source_time'))
        old = runtime.get(code, {})
        if not stock['eligible'] or at is None or session(at).endswith('/off'):
            if old.get('session') == session(instant(observed_at)):
                state[code] = {**old, 'samples': [], 'active': {}}
            continue
        timestamp = at.timestamp()
        samples = old.get('samples', [])
        if samples and timestamp <= samples[-1][0]:
            state[code] = old
            continue
        connected = bool(samples and timestamp - samples[-1][0] <= MAX_GAP_SECONDS
                         and old.get('session') == session(at) and old.get('provider') == stock.get('provider'))
        if not connected:
            samples = []
        # A cumulative counter reset invalidates the amount window as well.
        amount = stock.get('amount_yuan')
        if samples and amount is not None and samples[-1][2] is not None and amount < samples[-1][2]:
            samples = []
        samples = [s for s in samples if timestamp - s[0] <= WINDOW_SECONDS + MAX_GAP_SECONDS]
        samples.append([timestamp, stock['price'], amount])
        previous_flags = old.get('active', {}) if connected else {}
        last_events = dict(old.get('last_events', {})) if old.get('session') == session(at) else {}
        active = {}
        def emit(kind, hit, evidence):
            active[kind] = hit
            if not hit or previous_flags.get(kind) or timestamp - last_events.get(kind, 0) < COOLDOWN_SECONDS:
                return
            last_events[kind] = timestamp
            events.append({'type': kind, 'label': LABELS[kind], 'code': code, 'theme_ids': stock['theme_ids'],
                'source_time': at.isoformat(), 'observed_at': observed_at, 'timing_basis': 'continuous_quote_window',
                'rule_version': VERSION, 'evidence': evidence, 'cooldown_seconds': COOLDOWN_SECONDS})
        def anchor(seconds):
            eligible = [s for s in samples if s[0] <= timestamp - seconds]
            return eligible[-1] if eligible and timestamp - seconds - eligible[-1][0] <= MAX_GAP_SECONDS else None
        base = anchor(180)
        if base:
            change = stock['price'] / base[1] - 1
            evidence = {'window_seconds': 180, 'start_price': base[1], 'end_price': stock['price'],
                        'change_ratio': change, 'threshold_ratio': .03, 'observed_window_seconds': timestamp - base[0],
                        'window_start': datetime.fromtimestamp(base[0], CN).isoformat()}
            emit('rapid_rise', change >= .03 - 1e-10, evidence)
            emit('rapid_fall', change <= -.03 + 1e-10, {**evidence, 'threshold_ratio': -.03})
        base = anchor(300)
        if base:
            window = [s for s in samples if s[0] >= base[0]]
            low = min(window, key=lambda s: s[1]); high = max(window, key=lambda s: s[1])
            rebound = stock['price'] / low[1] - 1
            dive = stock['price'] / high[1] - 1
            # The preceding opposite move distinguishes these from one-way rise/fall.
            before_low = max(s[1] for s in window if s[0] <= low[0])
            before_high = min(s[1] for s in window if s[0] <= high[0])
            emit('quick_rebound', low[1] / before_low - 1 <= -.01 + 1e-10 and rebound >= .02 - 1e-10,
                 {'window_seconds': 300, 'low_price': low[1], 'end_price': stock['price'],
                  'rebound_ratio': rebound, 'threshold_ratio': .02, 'preceding_decline_ratio': low[1] / before_low - 1})
            emit('high_dive', high[1] / before_high - 1 >= .01 - 1e-10 and dive <= -.02 + 1e-10,
                 {'window_seconds': 300, 'high_price': high[1], 'end_price': stock['price'],
                  'drawdown_ratio': dive, 'threshold_ratio': -.02, 'preceding_rise_ratio': high[1] / before_high - 1})
            minute = anchor(60)
            if minute and all(s[2] is not None for s in window):
                recent = amount - minute[2]
                baseline = (minute[2] - base[2]) / ((minute[0] - base[0]) / 60)
                observed_seconds = timestamp - minute[0]
                multiple = (recent * 60 / observed_seconds) / baseline if baseline > 0 else None
                emit('volume_surge', multiple is not None and multiple >= 3 and recent >= 10_000_000,
                     {'window_seconds': 60, 'observed_window_seconds': observed_seconds, 'baseline_seconds': minute[0] - base[0], 'amount_yuan': recent,
                      'baseline_per_minute_yuan': baseline, 'multiple': multiple,
                      'threshold_multiple': 3, 'minimum_amount_yuan': 10_000_000})
        previous = prior.get(code, {})
        if connected and previous.get('is_limit_down') is True and stock.get('is_limit_down') is False:
            emit('open_limit_down', True, {'previous_price': previous.get('price'), 'price': stock['price'],
                                         'limit_down_price': stock['limit_down_price']})
        state[code] = {'samples': samples, 'session': session(at), 'provider': stock.get('provider'),
                       'active': active, 'last_events': last_events}
    return events, state
