from __future__ import annotations

import csv

from app.quant.cli.replay_factor_experiments import (
    _effective_replay_start,
    _write_csv,
)
from app.quant.core.models import Bar


def test_csv_output_uses_union_of_baseline_and_candidate_fields(tmp_path) -> None:
    path = tmp_path / "metrics.csv"

    _write_csv(path, [{"scenario": "baseline"}, {"scenario": "candidate", "criterion": True}])

    with path.open(encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    assert rows == [
        {"scenario": "baseline", "criterion": ""},
        {"scenario": "candidate", "criterion": "True"},
    ]


def test_effective_replay_start_waits_for_two_preceding_daily_bars() -> None:
    bars = [
        Bar(f"2026-07-0{day}", 10.0, 10.0, 10.0, 10.0)
        for day in range(1, 5)
    ]

    assert _effective_replay_start(
        bars, requested_start="2026-07-01", end_date="2026-07-04"
    ) == "2026-07-03"
    assert _effective_replay_start(
        bars, requested_start="2026-07-04", end_date="2026-07-04"
    ) == "2026-07-04"
    assert (
        _effective_replay_start(
            bars[:2], requested_start="2026-07-01", end_date="2026-07-02"
        )
        is None
    )
