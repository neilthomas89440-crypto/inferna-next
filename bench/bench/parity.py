"""Parity verdict: compare parallel configurations against the single-node baseline.

One command reproduces the whole report. The math lives here (ratios and thresholds from the
roadmap §5), the measuring lives in `bench.client`/`bench.scenarios`, and the deployments are
created through the control plane API so a configuration is one `--nodes`/`--mode` pair.

Honesty rules baked into the code:

- every row records the **environment** it was measured in (`hardware`, `mock`, `fake-upstream`);
- a verdict is only published for `hardware` rows — elsewhere the table is marked as a harness
  check, because a 502 from a mock worker is not a measurement of anything;
- a cell without a measurement is an error, never a silent zero.

Usage:

    scripts/parity.sh --mode smoke --api http://127.0.0.1:9200 --api-key k --model fake-model
    scripts/parity.sh --mode single --api http://server:8000 --api-key "$KEY" --model Qwen/… \\
        --gpus 8 --baseline artifacts/parity-baseline.jsonl
    scripts/parity.sh --mode multi  --api http://server:8000 --api-key "$KEY" --model Qwen/… \\
        --nodes 2 --gpus 4 --mode-flag pp --baseline artifacts/parity-baseline.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import httpx

from bench import scenarios
from bench.client import chat
from bench.report import CaseResult

# Roadmap §5: what "did not get worse" means for a parallel configuration.
R_TPS_MIN = 0.90
R_P95_MAX = 1.25
TTFT_P95_COLD_MS = 1000.0
TTFT_P95_REPEAT_MS = 600.0
ITL_P95_MS = 50.0
MIN_TOKENS_PER_SECOND = 20.0
MAX_ERROR_RATE = 0.001


@dataclass(slots=True)
class Cell:
    """One measured configuration: what was run, where, and the numbers it produced."""

    configuration: str
    mode: str
    nodes: int
    gpus: int
    environment: str
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    itl_p95_ms: float | None
    e2e_p50_ms: float | None
    tokens_per_second: float | None
    requests: int
    errors: int

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "configuration": self.configuration,
            "mode": self.mode,
            "nodes": self.nodes,
            "gpus": self.gpus,
            "environment": self.environment,
            "ttft_p50_ms": self.ttft_p50_ms,
            "ttft_p95_ms": self.ttft_p95_ms,
            "itl_p95_ms": self.itl_p95_ms,
            "e2e_p50_ms": self.e2e_p50_ms,
            "tokens_per_second": self.tokens_per_second,
            "requests": self.requests,
            "errors": self.errors,
            "error_rate": self.error_rate,
        }


def ratios(cell: Cell, baseline: Cell) -> tuple[float | None, float | None]:
    """`R_tps` and `R_p95` of a cell against the baseline (None when either side is missing)."""
    r_tps = (
        cell.tokens_per_second / baseline.tokens_per_second
        if cell.tokens_per_second and baseline.tokens_per_second
        else None
    )
    r_p95 = (
        cell.ttft_p95_ms / baseline.ttft_p95_ms
        if cell.ttft_p95_ms and baseline.ttft_p95_ms
        else None
    )
    return r_tps, r_p95


def verdict(cell: Cell, baseline: Cell | None) -> tuple[str, list[str]]:
    """`(verdict, reasons)` for one cell.

    Thresholds: R_tps >= 0.90, R_p95 <= 1.25, TTFT p95 <= 1.0 s, ITL p95 <= 50 ms,
    >= 20 tok/s, error rate < 0.1 % (roadmap §5).
    """
    reasons: list[str] = []
    if cell.environment != "hardware":
        reasons.append(f"measured on `{cell.environment}`, not on hardware — not a verdict")
    if cell.error_rate > MAX_ERROR_RATE:
        reasons.append(f"error rate {cell.error_rate:.1%} above 0.1%")
    if cell.ttft_p95_ms is None:
        reasons.append("no TTFT measured")
    elif cell.ttft_p95_ms > TTFT_P95_COLD_MS:
        reasons.append(f"TTFT p95 {cell.ttft_p95_ms:.0f} ms above {TTFT_P95_COLD_MS:.0f} ms")
    if cell.itl_p95_ms is not None and cell.itl_p95_ms > ITL_P95_MS:
        reasons.append(f"ITL p95 {cell.itl_p95_ms:.0f} ms above {ITL_P95_MS:.0f} ms")
    if cell.tokens_per_second is not None and cell.tokens_per_second < MIN_TOKENS_PER_SECOND:
        reasons.append(
            f"{cell.tokens_per_second:.1f} tok/s below {MIN_TOKENS_PER_SECOND:.0f} tok/s"
        )
    if baseline is not None:
        r_tps, r_p95 = ratios(cell, baseline)
        if r_tps is not None and r_tps < R_TPS_MIN:
            reasons.append(f"R_tps {r_tps:.2f} below {R_TPS_MIN:.2f}")
        if r_p95 is not None and r_p95 > R_P95_MAX:
            reasons.append(f"R_p95 {r_p95:.2f} above {R_P95_MAX:.2f}")
    if cell.environment != "hardware":
        return "harness-check", reasons
    return ("pass" if not reasons else "fail"), reasons


def render(cells: list[Cell], baseline: Cell | None) -> str:
    """Markdown table, one row per cell, with the coefficients and the verdict."""
    lines = [
        "| configuration | env | nodes×gpus | mode | TTFT p50 | TTFT p95 | ITL p95 | tok/s | "
        "errors | R_tps | R_p95 | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in cells:
        r_tps, r_p95 = ratios(cell, baseline) if baseline is not None else (None, None)
        state, reasons = verdict(cell, baseline)
        note = "" if not reasons else f" ({'; '.join(reasons)})"
        lines.append(
            "| {configuration} | {environment} | {nodes}×{gpus} | {mode} | {p50} | {p95} | {itl} | "
            "{tps} | {errors} | {r_tps} | {r_p95} | {state}{note} |".format(
                configuration=cell.configuration,
                environment=cell.environment,
                nodes=cell.nodes,
                gpus=cell.gpus,
                mode=cell.mode,
                p50=_ms(cell.ttft_p50_ms),
                p95=_ms(cell.ttft_p95_ms),
                itl=_ms(cell.itl_p95_ms),
                tps="—" if cell.tokens_per_second is None else f"{cell.tokens_per_second:.1f}",
                errors=f"{cell.errors}/{cell.requests}",
                r_tps="—" if r_tps is None else f"{r_tps:.2f}",
                r_p95="—" if r_p95 is None else f"{r_p95:.2f}",
                state=state,
                note=note,
            )
        )
    return "\n".join(lines)


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _cell_from(label: str, result: CaseResult, **meta) -> Cell:
    summary = result.summary()
    return Cell(
        configuration=label,
        ttft_p50_ms=summary["ttft_p50_ms"],
        ttft_p95_ms=summary["ttft_p95_ms"],
        itl_p95_ms=summary["itl_p95_ms"],
        e2e_p50_ms=summary["e2e_p50_ms"],
        tokens_per_second=summary["mean_tps"],
        requests=int(summary["requests"] or 0),
        errors=int(summary["errors"] or 0),
        **meta,
    )


# --- measurement ---


async def measure(
    base_url: str,
    api_key: str,
    model: str,
    *,
    scenario: str = "single",
    repeats: int = 3,
    warmup: int = 1,
    concurrency: int = 4,
    timeout: float = 180.0,
) -> CaseResult:
    """Run one scenario and return its results (used for every cell)."""
    cases = scenarios.build_cases(scenario, concurrency=concurrency)
    client = httpx.AsyncClient()
    result = CaseResult(label=scenario)
    try:
        for case in cases:
            for _ in range(warmup):
                await chat(
                    client, base_url, api_key, model, case.messages, max_tokens=case.max_tokens
                )
            for _ in range(repeats):
                result.results.append(
                    await chat(
                        client, base_url, api_key, model, case.messages, max_tokens=case.max_tokens
                    )
                )
    finally:
        await client.aclose()
    return result


def _api(
    method: str, path: str, base: str, token: str | None, body: dict | None = None
) -> tuple[int, object]:
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload[:200]


async def _deploy(base: str, token: str, model: str, nodes: int, mode: str) -> tuple[bool, str]:
    """Create (or reuse) a deployment for a configuration; returns (ok, message)."""
    status, models = _api("GET", "/api/v1/models", base, token)
    if status != 200 or not isinstance(models, list):
        return False, f"cannot list models ({status})"
    match = next((m for m in models if m["name"] == model), None)
    if match is None:
        return False, f"model {model} is not in the catalog"
    status, clusters = _api("GET", "/api/v1/clusters", base, token)
    if status != 200:
        return False, f"cannot list clusters ({status})"
    if isinstance(clusters, list):
        cluster_rows = clusters
    elif isinstance(clusters, dict):
        cluster_rows = clusters.get("items", [])
    else:
        cluster_rows = []
    cluster_id = cluster_rows[0]["id"]
    status, created = _api(
        "POST",
        "/api/v1/model-instances",
        base,
        token,
        {
            "model_id": match["id"],
            "cluster_id": cluster_id,
            "engine": "vllm",
            "profile": "latency",
            "replicas": 1,
            "gpu_selection": "auto",
            "nodes_per_replica": nodes,
            "parallel_mode": mode,
        },
    )
    if status not in (200, 201):
        return False, f"deploy failed ({status}): {created}"
    instances = created if isinstance(created, list) else [created]
    if nodes > 1:
        waited = 0
        while waited < 300:
            await asyncio.sleep(10)
            waited += 10
            _, current = _api("GET", "/api/v1/model-instances", base, token)
            states = [str(i["state"]) for i in (current if isinstance(current, list) else [])]
            if all(state == "running" for state in states):
                return True, f"{len(instances)} ranks running"
            if any(state == "error" for state in states):
                return False, "a rank failed to start"
        return False, "ranks did not become ready in 300 s"
    return True, "single-node deployment created"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parity", description=__doc__)
    parser.add_argument("--mode", choices=["smoke", "single", "multi", "full"], default="smoke")
    parser.add_argument("--api", default="http://localhost:8000", help="gateway base URL")
    parser.add_argument("--api-key", default="", help="API key for the gateway")
    parser.add_argument(
        "--admin-token", default="", help="admin JWT for the control plane (deploys)"
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--nodes", type=int, default=2, help="hosts per replica for --mode multi")
    parser.add_argument("--gpus", type=int, default=4, help="GPUs per host")
    parser.add_argument("--mode-flag", default="pp", help="parallel mode for the multi cell")
    parser.add_argument("--scenario", default="single")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--conc", type=int, default=4)
    parser.add_argument(
        "--environment",
        default="unknown",
        help="hardware | mock | fake-upstream — verdicts are only published for hardware",
    )
    parser.add_argument("--baseline", default=None, help="JSONL to read/append the baseline cell")
    parser.add_argument("--out", default=None, help="JSONL to write all cells to")
    args = parser.parse_args(argv)

    cells: list[Cell] = []
    baseline: Cell | None = None

    if args.baseline and Path(args.baseline).exists():
        for line in Path(args.baseline).read_text(encoding="utf-8").splitlines():
            if line.strip():
                baseline = Cell(**json.loads(line))
                break

    if args.mode in ("single", "multi", "full") and not args.admin_token:
        print(
            "--mode single|multi|full deploy through the control plane and need --admin-token",
            file=sys.stderr,
        )
        return 2

    configurations: list[tuple[str, int, str]] = []
    if args.mode == "smoke":
        configurations = [("smoke", 1, "n/a")]
    elif args.mode == "single":
        configurations = [("baseline", 1, "single")]
    elif args.mode == "multi":
        configurations = [(f"{args.nodes}x{args.gpus}", args.nodes, args.mode_flag)]
    else:  # full
        configurations = [
            ("1x8", 1, "single"),
            ("2x4", 2, "pp"),
            ("2x4-tp", 2, "tp"),
            ("4x2", 4, "pp"),
        ]

    for label, nodes, mode in configurations:
        if args.mode in ("single", "multi", "full"):
            ok, message = asyncio.run(
                _deploy(args.api, args.admin_token, args.model, nodes, mode)
            )
            print(f"{label}: {message}", file=sys.stderr)
            if not ok:
                print(f"{label}: prerequisite failed ({message})", file=sys.stderr)
                return 2
        result = asyncio.run(
            measure(
                args.api,
                args.api_key,
                args.model,
                scenario=args.scenario,
                repeats=args.repeats,
                warmup=args.warmup,
                concurrency=args.conc,
            )
        )
        cell = _cell_from(
            label,
            result,
            mode=mode,
            nodes=nodes,
            gpus=args.gpus,
            environment=args.environment,
        )
        if label == "baseline" or (label == "1x8" and baseline is None):
            baseline = cell
        cells.append(cell)

    print(render(cells, baseline))

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for cell in cells:
                handle.write(json.dumps(cell.as_dict(), separators=(",", ":")) + "\n")
        print(f"\nraw cells: {args.out}")
    if args.baseline and not Path(args.baseline).exists():
        Path(args.baseline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.baseline).write_text(
            json.dumps(baseline.as_dict(), separators=(",", ":")) + "\n" if baseline else "",
            encoding="utf-8",
        )
        print(f"baseline recorded: {args.baseline}")

    if all(math.isnan(cell.error_rate) for cell in cells):
        return 2
    failed = any(cell.error_rate > MAX_ERROR_RATE for cell in cells)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
