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


def test_coalesced_stream_is_not_reported_as_infinite_throughput() -> None:
    """Regression: a stream delivered in one read made tokens/second absurd (19426 tok/s)."""
    from bench.client import RequestResult
    from bench.report import CaseResult

    coalesced = RequestResult(
        status=200,
        e2e_ms=3.0,
        ttft_ms=2.5,
        itl_ms=[0.02] * 7,  # all chunks arrived in the same read
        completion_tokens=8,
    )
    case = CaseResult(label="coalesced", results=[coalesced])

    assert case.summary()["mean_tps"] is None


def test_spaced_stream_reports_throughput() -> None:
    from bench.client import RequestResult
    from bench.report import CaseResult

    spaced = RequestResult(
        status=200,
        e2e_ms=200.0,
        ttft_ms=20.0,
        itl_ms=[20.0] * 7,  # 20 ms between tokens
        completion_tokens=8,
    )
    case = CaseResult(label="spaced", results=[spaced])

    throughput = case.summary()["mean_tps"]
    assert throughput is not None
    assert 50 < throughput < 60  # 8 tokens over 140 ms
