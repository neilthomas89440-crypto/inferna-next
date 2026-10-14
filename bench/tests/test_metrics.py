"""Percentile, throughput and histogram helpers."""

from __future__ import annotations

from bench.metrics import hist_p95, mean, pct, tps


def test_pct_interpolates_between_neighbours() -> None:
    values = [10.0, 20.0, 30.0, 40.0]

    assert pct(values, 50) == 25.0  # rank 1.5 -> halfway between 20 and 30
    assert pct(values, 0) == 10.0
    assert pct(values, 100) == 40.0
    assert pct([7.0], 95) == 7.0
    assert pct([], 95) is None


def test_mean_and_tps_handle_missing_data() -> None:
    assert mean([1.0, 2.0, 3.0]) == 2.0
    assert mean([]) is None
    assert tps(100, 2.0) == 50.0
    assert tps(None, 2.0) is None
    assert tps(100, 0.0) is None


def test_hist_p95_uses_bucket_boundaries() -> None:
    buckets = [0.1, 0.5, 1.0, 2.0]
    counts = [50, 80, 95, 100]

    assert hist_p95(buckets, counts) == 1.0  # exactly at the 95th observation
    # p95 falls inside the first bucket, so it is interpolated from zero (0.5 * 9.5/10).
    assert round(hist_p95([0.5, 1.0], [10, 10]) or 0, 3) == 0.475
    assert hist_p95([0.5, 1.0], [0, 0]) is None
    assert hist_p95([], []) is None
