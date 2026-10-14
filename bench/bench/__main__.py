"""CLI entry point: ``python -m bench --api … --api-key … --model … --scenario single``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from bench import scenarios, trace
from bench.client import RequestResult, chat
from bench.report import CaseResult, render_json, render_markdown
from bench.scenarios import Case

DEFAULT_API = "http://localhost:8000"
REPEATS_REQUIRED_MESSAGE = "--repeats and --warmup are required (roadmap M1)"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bench",
        description="Measure TTFT/ITL/throughput of a deployed model (gateway or engine).",
    )
    parser.add_argument("--api", default=DEFAULT_API, help="gateway base URL")
    parser.add_argument("--api-key", default="", help="API key (gateway mode) or engine token")
    parser.add_argument(
        "--model",
        required=True,
        help="model name as the gateway knows it (catalog name), or the served model id in "
        "direct mode",
    )
    parser.add_argument(
        "--scenario",
        default="single",
        choices=["single", "agentic-loop", "long-context", "ttft-only"],
    )
    parser.add_argument("--conc", type=int, default=1, help="concurrency (0 keeps the sweep)")
    parser.add_argument("--repeats", type=int, default=0, help="iterations per case")
    parser.add_argument(
        "--warmup", type=int, default=0, help="warmup requests per case, not measured"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="run each case for N seconds instead of --repeats",
    )
    parser.add_argument("--mode", choices=["gateway", "direct"], default="gateway")
    parser.add_argument("--direct-base", default=None, help="engine origin for --mode direct")
    parser.add_argument("--replay", default=None, help="replay a recorded JSONL trace")
    parser.add_argument("--trace", default=None, help="record the requests of this run to JSONL")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", default=None, help="raw per-request JSONL output path")
    parser.add_argument(
        "--json", action="store_true", help="also print the machine-readable summary"
    )
    parser.add_argument(
        "--environment",
        default="unknown",
        help="label recorded in the report (e.g. mock, hardware)",
    )
    parser.add_argument(
        "--allow-errors", action="store_true", help="exit 0 even with failed requests"
    )
    return parser


def _base_url(args: argparse.Namespace) -> str:
    if args.mode == "direct":
        if not args.direct_base:
            raise SystemExit("--mode direct requires --direct-base (e.g. http://host:8010)")
        return args.direct_base
    return args.api


async def _run_case(
    client: httpx.AsyncClient,
    case: Case,
    *,
    base_url: str,
    api_key: str,
    model: str,
    repeats: int,
    warmup: int,
    duration: float,
    max_tokens: int,
    timeout: float,
    writer: trace.TraceWriter | None,
) -> CaseResult:
    result = CaseResult(label=case.label)

    async def one() -> RequestResult:
        if writer is not None:
            writer.write(
                "POST",
                "/v1/chat/completions",
                {"model": model, "messages": case.messages, "max_tokens": case.max_tokens},
            )
        return await chat(
            client,
            base_url,
            api_key,
            model,
            case.messages,
            max_tokens=case.max_tokens or max_tokens,
            timeout=timeout,
        )

    for _ in range(warmup):
        await one()

    semaphore = asyncio.Semaphore(max(1, case.concurrency))

    async def guarded() -> RequestResult:
        async with semaphore:
            return await one()

    if duration > 0:
        deadline = time.perf_counter() + duration
        while time.perf_counter() < deadline:
            result.results.append(await guarded())
    else:
        for _ in range(max(1, repeats)):
            batch = [guarded() for _ in range(max(1, case.concurrency))]
            result.results.extend(await asyncio.gather(*batch))
    return result


async def _run_replay(
    client: httpx.AsyncClient,
    entries: list[trace.TraceEntry],
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    timeout: float,
) -> CaseResult:
    """Replay a recorded trace, preserving inter-arrival times."""
    result = CaseResult(label=f"replay {len(entries)} requests")
    for entry, delay in zip(entries, trace.replay_schedule(entries), strict=False):
        if delay:
            await asyncio.sleep(delay)
        body = entry.body or {}
        result.results.append(
            await chat(
                client,
                base_url,
                api_key,
                str(body.get("model") or model),
                list(body.get("messages") or []),
                max_tokens=int(body.get("max_tokens") or max_tokens),
                timeout=timeout,
            )
        )
    return result


def _write_raw(path: str, cases: list[CaseResult]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for case in cases:
            for index, result in enumerate(case.results):
                handle.write(
                    json.dumps(
                        {
                            "case": case.label,
                            "index": index,
                            "status": result.status,
                            "ttft_ms": result.ttft_ms,
                            "e2e_ms": result.e2e_ms,
                            "itl_ms": result.itl_ms,
                            "prompt_tokens": result.prompt_tokens,
                            "completion_tokens": result.completion_tokens,
                            "error": result.error,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )


async def _main(args: argparse.Namespace) -> int:
    base_url = _base_url(args)
    if args.replay is None and args.repeats <= 0 and args.duration <= 0:
        print(REPEATS_REQUIRED_MESSAGE, file=sys.stderr)
        return 2

    writer = trace.TraceWriter(args.trace) if args.trace else None
    async with httpx.AsyncClient() as client:
        try:
            if args.replay:
                entries = trace.read_trace(args.replay)
                cases = [
                    await _run_replay(
                        client,
                        entries,
                        base_url=base_url,
                        api_key=args.api_key,
                        model=args.model,
                        max_tokens=args.max_tokens,
                        timeout=args.timeout,
                    )
                ]
                scenario = f"replay:{Path(args.replay).name}"
            else:
                built = scenarios.build_cases(
                    args.scenario, concurrency=args.conc, max_tokens=args.max_tokens
                )
                cases = [
                    await _run_case(
                        client,
                        case,
                        base_url=base_url,
                        api_key=args.api_key,
                        model=args.model,
                        repeats=args.repeats,
                        warmup=args.warmup,
                        duration=args.duration,
                        max_tokens=args.max_tokens,
                        timeout=args.timeout,
                        writer=writer,
                    )
                    for case in built
                ]
                scenario = args.scenario
        finally:
            if writer is not None:
                writer.close()

    print(
        render_markdown(
            cases,
            scenario=scenario,
            mode=args.mode,
            base_url=base_url,
            environment=args.environment,
        )
    )
    if args.json:
        print(json.dumps(render_json(cases), indent=2))
    if args.out:
        _write_raw(args.out, cases)
        print(f"\nraw results: {args.out}")

    errors = sum(len(case.results) - len(case.ok_results) for case in cases)
    if errors and not args.allow_errors:
        print(f"\n{errors} request(s) failed", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
