from __future__ import annotations

import pytest

from app.manually_execute_script.materialize_quant_3m_history import (
    DESTINATION_COLLECTION,
    build_materialization_pipeline,
    three_minute_bucket_end,
)


@pytest.mark.parametrize(
    ("source_time", "expected"),
    (
        ((9, 30), (9, 33)),
        ((9, 31), (9, 33)),
        ((9, 33), (9, 33)),
        ((9, 34), (9, 36)),
        ((11, 30), (11, 30)),
        ((13, 1), (13, 3)),
        ((13, 3), (13, 3)),
        ((13, 4), (13, 6)),
        ((15, 0), (15, 0)),
    ),
)
def test_three_minute_bucket_end(
    source_time: tuple[int, int], expected: tuple[int, int]
) -> None:
    assert three_minute_bucket_end(*source_time) == expected


@pytest.mark.parametrize("source_time", ((9, 29), (11, 31), (13, 0), (15, 1)))
def test_three_minute_bucket_rejects_non_trading_minutes(
    source_time: tuple[int, int],
) -> None:
    with pytest.raises(ValueError, match="连续竞价时段"):
        three_minute_bucket_end(*source_time)


def test_materialization_pipeline_merges_into_independent_collection() -> None:
    pipeline = build_materialization_pipeline(
        {"trade_date": "2026-08-31", "adjust": "qfq", "interval": "1m"}
    )

    assert pipeline[-1]["$merge"]["into"] == DESTINATION_COLLECTION
    assert pipeline[-1]["$merge"]["whenMatched"] == "replace"
