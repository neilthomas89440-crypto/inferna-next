"""Load shapes: deterministic filler, growing agentic prefix, long-context sizes."""

from __future__ import annotations

import pytest

from bench import scenarios


def test_filler_is_deterministic_and_sized() -> None:
    first = scenarios.filler_tokens(50, seed=1)
    second = scenarios.filler_tokens(50, seed=1)
    other = scenarios.filler_tokens(50, seed=2)

    assert first == second
    assert first != other
    assert len(first.split()) == 50


def test_single_sweep_covers_the_roadmap_concurrency_levels() -> None:
    cases = scenarios.single_cases()

    assert [case.concurrency for case in cases] == [1, 4, 8, 16]
    assert all(case.messages for case in cases)


def test_agentic_prefix_grows_monotonically() -> None:
    cases = scenarios.agentic_cases(iterations=5)

    lengths = [len(case.messages) for case in cases]
    assert lengths == [3, 4, 5, 6, 7]
    # The system prefix is sent unchanged every iteration: that is what a prefix cache can hit.
    assert len({case.messages[0]["content"] for case in cases}) == 1


def test_long_context_cases_are_large_and_labelled() -> None:
    cases = scenarios.long_context_cases()

    assert [case.label for case in cases] == [
        "long-context tokens=32000",
        "long-context tokens=128000",
    ]
    assert len(cases[0].messages[0]["content"]) > 32_000 * 3  # ~4 chars per token


def test_build_cases_applies_concurrency_and_rejects_unknown_scenarios() -> None:
    cases = scenarios.build_cases("single", concurrency=8)
    assert {case.concurrency for case in cases} == {8}

    ttft = scenarios.build_cases("ttft-only", concurrency=1)
    assert ttft[0].max_tokens == 1

    with pytest.raises(ValueError):
        scenarios.build_cases("nope", concurrency=1)
