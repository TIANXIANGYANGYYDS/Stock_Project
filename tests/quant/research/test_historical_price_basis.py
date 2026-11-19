import pytest

from app.quant.research.historical_price_basis import restore_historical_day
from app.quant.runtime.live import three_minute_bar_ends


def fixture():
    day='2026-07-02'
    rows=[dict(timestamp=t,open=9.,high=10.,low=8.,close=9.) for t in three_minute_bar_ends(day)]
    previous=dict(open=9.,high=10.,low=8.,close=9.)
    return day,rows,previous


@pytest.mark.parametrize('scale,offset',[(1.,.179),(2.,0.),(1.,0.)])
def test_previous_day_affine_mapping_restores_same_current_ohlc(scale,offset):
    day,rows,previous=fixture()
    target={k:scale*v+offset for k,v in previous.items()}
    bars,audit=restore_historical_day(rows=rows,previous_native=previous,previous_target=target,
        current_target=dict(target,pct_chg=0.),trade_date=day)
    assert len(bars)==80
    assert audit['scale']==pytest.approx(scale)
    assert audit['offset']==pytest.approx(offset)
    assert bars[0].previous_close==target['close']
    assert bars[-1].close==pytest.approx(target['close'])


def test_day_end_information_only_validates_never_recalibrates_prices():
    day,rows,previous=fixture()
    today=dict(previous,close=9.5,pct_chg=5.5556)
    bars,audit=restore_historical_day(rows=rows,previous_native=previous,previous_target=previous,
        current_target=today,trade_date=day)
    assert not bars
    assert audit['method']=='current_day_basis_disagrees'
    assert audit['scale']==1 and audit['offset']==0


def test_incomplete_native_grid_is_not_filled():
    day,rows,previous=fixture()
    bars,audit=restore_historical_day(rows=rows[1:],previous_native=previous,previous_target=previous,
        current_target=dict(previous,pct_chg=0.),trade_date=day)
    assert not bars and audit['method']=='historical_grid_incomplete'
