from __future__ import annotations

import pytest

from app.quant.cli.replay_sample import (
    maximum_drawdown,
    percentile,
    sample_stocks,
)


def test_sample_stocks_is_reproducible_and_without_replacement() -> None:
    universe = [(f"{index:06d}", str(index)) for index in range(20)]

    first = sample_stocks(universe, sample_size=5, seed=42)
    second = sample_stocks(list(reversed(universe)), sample_size=5, seed=42)

    assert first == second
    assert len({item.code for item in first}) == 5
    assert [item.sample_rank for item in first] == [1, 2, 3, 4, 5]


def test_sample_stocks_rejects_oversized_sample() -> None:
    with pytest.raises(ValueError, match="超过"):
        sample_stocks([("000001", "平安银行")], sample_size=2, seed=1)


def test_percentile_and_maximum_drawdown() -> None:
    assert percentile([0.0, 1.0, 2.0], 0.25) == pytest.approx(0.5)
    assert maximum_drawdown([100.0, 120.0, 90.0, 110.0]) == pytest.approx(0.25)
