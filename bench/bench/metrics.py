"""Percentile and throughput helpers (no numpy dependency)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def pct(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated percentile (``p`` in 0..100). None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * fraction)


def mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def tps(tokens: int | None, seconds: float | None) -> float | None:
    """Output tokens per second; None when the inputs are unusable."""
    if tokens is None or seconds is None or seconds <= 0:
        return None
    return tokens / seconds


def hist_p95(buckets: Iterable[float], counts: Iterable[int]) -> float | None:
    """p95 from Prometheus-style histogram buckets (upper bounds + cumulative counts).

    Used to turn the gateway's ``inferna_ttft_seconds`` histogram into the policy input for
    autoscaling. Returns None when there are no observations.
    """
    pairs = [(float(bound), int(count)) for bound, count in zip(buckets, counts, strict=False)]
    if not pairs:
        return None
    total = pairs[-1][1]
    if total <= 0:
        return None
    target = 0.95 * total
    previous_bound, previous_count = 0.0, 0
    for bound, count in pairs:
        if count >= target:
            if count == previous_count:
                return bound
            # Linear interpolation inside the bucket, mirroring Prometheus' histogram_quantile.
            span = count - previous_count
            return previous_bound + (bound - previous_bound) * ((target - previous_count) / span)
        previous_bound, previous_count = bound, count
    return pairs[-1][0]
