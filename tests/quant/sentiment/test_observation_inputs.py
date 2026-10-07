from app.quant.data.sentiment_inputs import observation_stock


def record(**changes):
    value = dict(trade_date='2026-08-03', symbol='sh.600000', listed_date_observed='1999-11-10',
        listing_session_observed=None, limit_up=None, limit_down=None, limit_provenance='legacy_formula_candidate',
        raw_open=10., raw_high=11., raw_low=9.9, raw_close=11., raw_preclose=10.,
        is_st=False, is_suspended=False, is_delisting=None, status_known_at=None,
        amount_yuan=1e9, volume_shares=1e8)
    value.update(changes)
    return value


def test_observation_does_not_fill_unknown_delisting_or_known_at():
    original = record()
    stock, feature, provenance = observation_stock(original)
    assert stock.is_delisting is None and stock.status_known_at is None
    assert original['limit_up'] is None
    assert stock.limit_up == 11 and feature.is_limit_up
    assert feature.evidence_mode == 'daily_observation'
    assert provenance == 'ordinary_rate_reconstruction_unverified_special_regime'


def test_extreme_drop_is_not_relabelled_as_delisting_or_unlimited():
    stock, feature, provenance = observation_stock(record(raw_open=.35, raw_high=.40, raw_low=.31, raw_close=.39))
    assert stock.is_delisting is None and stock.has_price_limit is True
    assert not feature.eligible and feature.error == 'price_outside_limits'


def test_new_listing_first_five_sessions_and_date_effective_st_rule():
    stock, feature, _ = observation_stock(record(listed_date_observed='2026-08-04',
        trade_date='2026-08-10', listing_session_observed=5))
    assert stock.has_price_limit is False and feature.exclusion_reason == 'no_price_limit'
    stock, _, _ = observation_stock(record(listed_date_observed='2026-08-04',
        trade_date='2026-08-11', listing_session_observed=6))
    assert stock.has_price_limit is True
    before, _, _ = observation_stock(record(is_st=True, trade_date='2026-07-03'))
    after, _, _ = observation_stock(record(is_st=True, trade_date='2026-07-06'))
    assert before.limit_up == 10.5 and after.limit_up == 11


def test_decimal_half_up_and_price_conflicts_remain_visible():
    stock, _, _ = observation_stock(record(raw_preclose=10.05))
    assert stock.limit_up == 11.06 and stock.limit_down == 9.05
    _, feature, _ = observation_stock(record(is_suspended=True))
    assert not feature.status_valid and feature.error == 'priced_suspension_conflict'
