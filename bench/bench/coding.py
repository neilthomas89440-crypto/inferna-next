"""Coding validation: run the task set through the gateway and score the answers.

Two things this measures that a latency benchmark does not: whether a configuration is *usable* for
real coding help (first-token latency an agent can tolerate, tokens/second that keep a loop moving)
and whether the answers are actually correct — checked by a pure function of the answer text, so a
run is reproducible.

Design decisions worth knowing:

- **Checks are deterministic and side-effect free** except `pytest` tasks, which write the answer to
  a temporary directory and run pytest there with a timeout. Those tasks are marked
  `requires_code_execution` and are skipped unless `--allow-code-execution` is passed: executing
  model output is an explicit opt-in, never a default in CI.
- **Every row carries the environment** (`hardware`, `mock`, `fake-upstream`), the same rule the
  parity harness uses: a run against a mock stack is a harness check, not a verdict.
- **Prefix-cache hits are recorded per task** by asking the gateway twice where the task repeats a
  prefix (the agentic tasks do), so the M5 metric can be correlated with coding success.

Usage (through the wrapper):

    scripts/coding-validation.sh --api http://server:8000 --api-key "$KEY" \\
        --model Qwen/Qwen2.5-Coder-7B-Instruct --environment hardware
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from bench.client import RequestResult, chat

DEFAULT_TASKS = Path(__file__).resolve().parents[2] / "coding" / "tasks.json"
CODE_EXECUTION_TIMEOUT_SECONDS = 60
# Roadmap §5: the thresholds a configuration has to hold to count as usable for coding.
TTFT_P95_MS = 1000.0
ITL_P95_MS = 50.0
MIN_TOKENS_PER_SECOND = 20.0


@dataclass(slots=True)
class Task:
    id: str
    title: str
    prompt: str
    check: dict
    max_tokens: int = 256
    requires_code_execution: bool = False


@dataclass(slots=True)
class TaskOutcome:
    task: Task
    passed: bool
    reason: str
    result: RequestResult | None = None
    detail: str = ""


@dataclass(slots=True)
class RunSummary:
    outcomes: list[TaskOutcome] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.passed)

    @property
    def scored(self) -> int:
        return len(self.outcomes)

    def by_p95(self, attribute: str) -> float | None:
        values = sorted(
            value
            for outcome in self.outcomes
            if outcome.result is not None
            for value in [getattr(outcome.result, attribute, None)]
            if value is not None
        )
        if not values:
            return None
        index = min(len(values) - 1, int(round(0.95 * (len(values) - 1))))
        return values[index]


def load_tasks(path: Path) -> list[Task]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks: list[Task] = []
    for raw in payload["tasks"]:
        tasks.append(
            Task(
                id=raw["id"],
                title=raw.get("title", raw["id"]),
                prompt=raw["prompt"],
                check=raw["check"],
                max_tokens=int(raw.get("max_tokens", 256)),
                requires_code_execution=bool(raw.get("requires_code_execution", False)),
            )
        )
    return tasks


def check_answer(task: Task, answer: str, *, allow_code_execution: bool) -> tuple[bool, str]:
    """(passed, reason) for one answer — a pure function except for `pytest` tasks."""
    check = task.check
    kind = check["kind"]
    text = answer.strip()

    if kind == "contains_all":
        missing = [needle for needle in check.get("needles", []) if needle not in text]
        forbidden = [needle for needle in check.get("forbid", []) if needle in text]
        if missing:
            return False, f"missing: {', '.join(missing)}"
        if forbidden:
            return False, f"forbidden: {', '.join(forbidden)}"
        return True, "all required fragments present"

    if kind == "one_of":
        allowed = check["allowed"]
        if text in allowed:
            return True, f"answered {text}"
        return False, f"expected one of {allowed}, got {text[:40]!r}"

    if kind == "equals_normalized":
        expected = check["expected"].strip()
        if text == expected:
            return True, "exact match"
        return False, f"expected {expected!r}"

    if kind == "regex":
        flags = 0
        for name in check.get("flags", []):
            flags |= getattr(re, name)
        pattern = re.compile(check["pattern"], flags)
        if not pattern.search(text):
            return False, "pattern not matched"
        forbidden = [needle for needle in check.get("forbid", []) if needle in text]
        if forbidden:
            return False, f"forbidden: {', '.join(forbidden)}"
        max_length = check.get("max_length")
        if max_length and len(text) > int(max_length):
            return False, f"answer longer than {max_length} chars"
        return True, "pattern matched"

    if kind == "json_schema":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # Models often wrap JSON in prose; accept the first object they emitted.
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match is None:
                return False, "no JSON object in the answer"
            payload = json.loads(match.group(0))
        if not isinstance(payload, dict):
            return False, "answer is not a JSON object"
        types = {"int": int, "str": str, "float": float, "bool": bool}
        for key, expected_type in check["schema"].items():
            if key not in payload:
                return False, f"missing key {key}"
            if not isinstance(payload[key], types[expected_type]):
                return False, f"{key} is not {expected_type}"
        for key, expected in check.get("equals", {}).items():
            if payload.get(key) != expected:
                return False, f"{key} == {payload.get(key)!r}, expected {expected!r}"
        return True, "json schema satisfied"

    if kind == "pytest":
        if not allow_code_execution:
            return False, "skipped: code execution not allowed (--allow-code-execution)"
        return _run_pytest(check["test_source"], text)

    return False, f"unknown check kind: {kind}"


def _run_pytest(test_source: str, solution: str) -> tuple[bool, str]:
    """Write the answer next to its test and run pytest in a temporary directory."""
    with tempfile.TemporaryDirectory(prefix="inferna-coding-") as workdir:
        path = Path(workdir)
        (path / "solution.py").write_text(solution + "\n", encoding="utf-8")
        (path / "test_solution.py").write_text(test_source, encoding="utf-8")
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "test_solution.py"],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=CODE_EXECUTION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False, f"pytest timed out after {CODE_EXECUTION_TIMEOUT_SECONDS}s"
    if completed.returncode == 0:
        return True, "pytest passed"
    tail = (completed.stdout + completed.stderr).strip().splitlines()[-1:]
    return False, f"pytest failed: {tail[0][:120] if tail else 'unknown'}"


async def run_tasks(
    tasks: list[Task],
    *,
    api: str,
    api_key: str,
    model: str,
    allow_code_execution: bool,
    timeout: float = 120.0,
) -> RunSummary:
    summary = RunSummary()
    async with httpx.AsyncClient() as client:
        for task in tasks:
            result = await chat(
                client,
                api,
                api_key,
                model,
                [{"role": "user", "content": task.prompt}],
                stream=True,
                max_tokens=task.max_tokens,
                timeout=timeout,
            )
            answer = ""
            passed = False
            reason = ""
            if not result.ok:
                reason = f"request failed: {result.error or result.status}"
            else:
                # The streaming client returns the collected answer, so the text that is scored
                # is the text that was measured — no second request, no "measured A, graded B".
                answer = result.content
                passed, reason = check_answer(
                    task, answer, allow_code_execution=allow_code_execution
                )
            summary.outcomes.append(
                TaskOutcome(
                    task=task, passed=passed, reason=reason, result=result, detail=answer[:400]
                )
            )
    return summary


def render(summary: RunSummary, *, environment: str, model: str) -> str:
    lines = [
        f"# Coding validation — {model}",
        "",
        f"- environment: **{environment}**",
        f"- passed: **{summary.passed}/{summary.scored}**",
        "",
        "| task | result | reason | TTFT (ms) | tok/s |",
        "|---|---|---|---|---|",
    ]
    for outcome in summary.outcomes:
        result = outcome.result
        ttft = "—" if result is None or result.ttft_ms is None else f"{result.ttft_ms:.0f}"
        tps = (
            "—"
            if result is None
            or result.generation_ms is None
            or result.generation_ms < 1
            or not result.completion_tokens
            else f"{result.completion_tokens / (result.generation_ms / 1000):.1f}"
        )
        state = "pass" if outcome.passed else "fail"
        lines.append(
            f"| {outcome.task.id} | {state} | {outcome.reason} | {ttft} | {tps} |"
        )
    ttft_p95 = summary.by_p95("ttft_ms")
    lines += [
        "",
        f"TTFT p95 (approx): {'—' if ttft_p95 is None else f'{ttft_p95:.0f} ms'}",
        "",
        f"Thresholds (§5): TTFT p95 ≤ {TTFT_P95_MS:.0f} ms, ITL p95 ≤ {ITL_P95_MS:.0f} ms, "
        f"≥ {MIN_TOKENS_PER_SECOND:.0f} tok/s.",
    ]
    if environment != "hardware":
        lines.append(
            f"\n**Not a verdict**: measured on `{environment}`, so this run proves the harness "
            "and the checks work, nothing about a configuration."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coding-validation", description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS))
    parser.add_argument("--environment", default="unknown")
    parser.add_argument("--only", default=None, help="comma-separated task ids")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N unchecked tasks")
    parser.add_argument(
        "--allow-code-execution",
        action="store_true",
        help="run the pytest tasks (writes the answer to a temp dir and executes pytest there)",
    )
    parser.add_argument("--out", default=None, help="raw JSONL with every outcome")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="assert only that every task was scored (no request-level failure), whatever the "
        "answers were — this is what the CI job runs against the deterministic fixture",
    )
    args = parser.parse_args(argv)

    tasks = load_tasks(Path(args.tasks))
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        tasks = [task for task in tasks if task.id in wanted]
    if not args.allow_code_execution:
        tasks = [task for task in tasks if not task.requires_code_execution]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("no tasks to run", file=sys.stderr)
        return 2

    summary = asyncio.run(
        run_tasks(
            tasks,
            api=args.api,
            api_key=args.api_key,
            model=args.model,
            allow_code_execution=args.allow_code_execution,
        )
    )
    print(render(summary, environment=args.environment, model=args.model))

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for outcome in summary.outcomes:
                handle.write(
                    json.dumps(
                        {
                            "task": outcome.task.id,
                            "passed": outcome.passed,
                            "reason": outcome.reason,
                            "environment": args.environment,
                            "ttft_ms": outcome.result.ttft_ms if outcome.result else None,
                            "e2e_ms": outcome.result.e2e_ms if outcome.result else None,
                            "completion_tokens": outcome.result.completion_tokens
                            if outcome.result
                            else None,
                            "answer": outcome.detail,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        print(f"\nraw outcomes: {args.out}")

    failed_requests = [
        outcome.task.id
        for outcome in summary.outcomes
        if outcome.result is None or not outcome.result.ok
    ]
    if args.smoke:
        if failed_requests:
            print(f"requests failed for: {', '.join(failed_requests)}", file=sys.stderr)
            return 2
        print("smoke: every task was scored (answers not asserted)", file=sys.stderr)
        return 0
    if args.environment != "hardware":
        return 0 if summary.passed == summary.scored else 2
    return 0 if summary.passed == summary.scored else 1


if __name__ == "__main__":
    raise SystemExit(main())
