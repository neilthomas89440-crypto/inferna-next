"""Aggregation and markdown rendering for benchmark runs."""

from __future__ import annotations

from dataclasses import dataclass, field

from bench.client import RequestResult
from bench.metrics import mean, pct, tps


@dataclass(slots=True)
class CaseResult:
    label: str
    results: list[RequestResult] = field(default_factory=list)

    @property
    def ok_results(self) -> list[RequestResult]:
        return [result for result in self.results if result.ok]

    def summary(self) -> dict[str, float | int | None]:
        ok = self.ok_results
        ttfts = [result.ttft_ms for result in ok if result.ttft_ms is not None]
        e2es = [result.e2e_ms for result in ok]
        itls = [value for result in ok for value in result.itl_ms]
        throughputs = [
            value
            for result in ok
            if (value := tps(result.completion_tokens, (result.generation_ms or 0) / 1000))
            is not None
        ]
        return {
            "requests": len(self.results),
            "errors": len(self.results) - len(ok),
            "ttft_p50_ms": pct(ttfts, 50),
            "ttft_p95_ms": pct(ttfts, 95),
            "ttft_p99_ms": pct(ttfts, 99),
            "e2e_p50_ms": pct(e2es, 50),
            "e2e_p95_ms": pct(e2es, 95),
            "itl_p95_ms": pct(itls, 95),
            "mean_tps": mean(throughputs),
        }


def _fmt(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def render_markdown(
    cases: list[CaseResult],
    *,
    scenario: str,
    mode: str,
    base_url: str,
    environment: str = "unknown",
) -> str:
    """Markdown report: one row per case, with the thresholds from the roadmap in view."""
    lines = [
        f"# Benchmark — {scenario} ({mode})",
        "",
        f"- target: `{base_url}`",
        f"- environment: **{environment}**",
        "",
        "| case | requests | errors | TTFT p50 (ms) | TTFT p95 (ms) | TTFT p99 (ms) | "
        "e2e p50 (ms) | e2e p95 (ms) | ITL p95 (ms) | mean tok/s |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for case in cases:
        summary = case.summary()
        lines.append(
            "| {label} | {requests} | {errors} | {ttft_p50} | {ttft_p95} | {ttft_p99} | "
            "{e2e_p50} | {e2e_p95} | {itl_p95} | {mean_tps} |".format(
                label=case.label,
                requests=_fmt(summary["requests"]),
                errors=_fmt(summary["errors"]),
                ttft_p50=_fmt(summary["ttft_p50_ms"]),
                ttft_p95=_fmt(summary["ttft_p95_ms"]),
                ttft_p99=_fmt(summary["ttft_p99_ms"]),
                e2e_p50=_fmt(summary["e2e_p50_ms"]),
                e2e_p95=_fmt(summary["e2e_p95_ms"]),
                itl_p95=_fmt(summary["itl_p95_ms"], 2),
                mean_tps=_fmt(summary["mean_tps"], 1),
            )
        )
    return "\n".join(lines)


def render_json(cases: list[CaseResult]) -> dict[str, object]:
    """Machine-readable summary for artifacts and comparisons."""
    return {"cases": [{"label": case.label, "summary": case.summary()} for case in cases]}
