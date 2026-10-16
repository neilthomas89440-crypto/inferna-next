"""Parity math: ratios against the baseline and the roadmap §5 verdicts."""

from __future__ import annotations

from bench.parity import (
    ITL_P95_MS,
    R_P95_MAX,
    R_TPS_MIN,
    Cell,
    ratios,
    render,
    verdict,
)


def _cell(**overrides) -> Cell:
    payload = {
        "configuration": "2x4",
        "mode": "pp",
        "nodes": 2,
        "gpus": 4,
        "environment": "hardware",
        "ttft_p50_ms": 100.0,
        "ttft_p95_ms": 150.0,
        "itl_p95_ms": 20.0,
        "e2e_p50_ms": 900.0,
        "tokens_per_second": 40.0,
        "requests": 12,
        "errors": 0,
    }
    payload.update(overrides)
    return Cell(**payload)


def test_ratios_use_the_baseline_for_both_coefficients() -> None:
    baseline = _cell(configuration="1x8", tokens_per_second=50.0, ttft_p95_ms=120.0)
    cell = _cell(tokens_per_second=45.0, ttft_p95_ms=150.0)

    r_tps, r_p95 = ratios(cell, baseline)

    assert r_tps == 0.9
    assert r_p95 == 1.25


def test_ratios_are_none_without_measurements() -> None:
    baseline = _cell(tokens_per_second=None, ttft_p95_ms=None)

    assert ratios(_cell(), baseline) == (None, None)
    # A missing measurement is None; the coefficient that *can* be computed still is.
    r_tps, r_p95 = ratios(_cell(tokens_per_second=None), _cell())
    assert r_tps is None
    assert r_p95 == 1.0


def test_hardware_cell_inside_every_threshold_passes() -> None:
    baseline = _cell(configuration="1x8", tokens_per_second=50.0, ttft_p95_ms=140.0)
    cell = _cell(tokens_per_second=46.0, ttft_p95_ms=150.0)

    state, reasons = verdict(cell, baseline)

    assert state == "pass"
    assert reasons == []


def test_threshold_violations_are_named_individually() -> None:
    baseline = _cell(configuration="1x8", tokens_per_second=50.0, ttft_p95_ms=100.0)

    slow, slow_reasons = verdict(_cell(tokens_per_second=20.0), baseline)
    hot, hot_reasons = verdict(_cell(itl_p95_ms=ITL_P95_MS + 1), baseline)
    lossy, lossy_reasons = verdict(_cell(errors=2, requests=100), baseline)

    assert slow == "fail" and any("R_tps" in reason for reason in slow_reasons)
    assert hot == "fail" and any("ITL p95" in reason for reason in hot_reasons)
    assert lossy == "fail" and any("error rate" in reason for reason in lossy_reasons)


def test_thresholds_match_the_roadmap() -> None:
    assert (R_TPS_MIN, R_P95_MAX) == (0.90, 1.25)


def test_non_hardware_rows_are_harness_checks_not_verdicts() -> None:
    """A mock or fake-upstream number must never read as a published verdict."""
    baseline = _cell(configuration="1x8", environment="fake-upstream")
    cell = _cell(environment="fake-upstream")

    state, reasons = verdict(cell, baseline)

    assert state == "harness-check"
    assert any("not on hardware" in reason for reason in reasons)


def test_missing_ttft_is_a_reason_not_a_zero() -> None:
    state, reasons = verdict(_cell(ttft_p95_ms=None), None)

    assert state == "fail"
    assert any("no TTFT measured" in reason for reason in reasons)


def test_render_marks_unmeasured_cells_and_prints_coefficients() -> None:
    baseline = _cell(configuration="1x8", tokens_per_second=50.0)
    cell = _cell(tokens_per_second=45.0, environment="mock")

    table = render([baseline, cell], baseline)

    assert "R_tps" in table
    assert "0.90" in table  # the coefficient of the mock cell
    assert "harness-check" in table
    assert "—" in table  # the baseline has no ratio against itself


def test_error_rate_without_requests_is_treated_as_total_failure() -> None:
    assert _cell(requests=0, errors=0).error_rate == 1.0
